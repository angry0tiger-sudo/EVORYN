"""Optional local Hugging Face news sentiment inference behind explicit license/quality gates.

No model is downloaded or loaded on import. The reviewed Financial PhraseBank candidate is not
enabled by default because the model card reports Apache-2.0 weights while the referenced
Financial PhraseBank dataset is CC BY-NC-SA 3.0; training-data rights must be explicitly approved
for the intended use before inference is permitted.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from evoryn.adapters.data.license_registry import SourceLicenseRegistry
from evoryn.domain.external_data import (
    DataUse,
    NewsArticleRecord,
    NewsSentimentRecord,
    SentimentLabel,
    require_aware_utc,
)

FINANCIAL_NEWS_MODEL_ID = (
    "mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis"
)
FINANCIAL_NEWS_MODEL_REVISION = "b986b74bfd4963613dcbc38d701747eda656ded4"
FINANCIAL_NEWS_MODEL_CARD = (
    "https://huggingface.co/mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis"
)
FINANCIAL_PHRASEBANK_DATASET_ID = "takala/financial_phrasebank"
FINANCIAL_PHRASEBANK_DATASET_REVISION = "8d3fe0c36d5feec6b3cc5e455b0fcb4820fb9964"
FINANCIAL_PHRASEBANK_DATASET_LICENSE = "CC-BY-NC-SA-3.0"


class NLPDependencyUnavailable(RuntimeError):
    """Raised when the optional local NLP runtime is not installed or its artifact is absent."""


@dataclass(frozen=True, slots=True)
class SentimentModelReview:
    """Human-review record for weights, model revision, training data rights and intended use."""

    model_id: str
    model_revision: str
    model_license_id: str
    model_license_uri: str
    model_card_uri: str
    training_dataset_id: str
    training_dataset_revision: str
    training_dataset_license_id: str
    training_dataset_license_uri: str
    training_data_rights_verified: bool
    commercial_use_approved: bool
    reviewed_by: str
    reviewed_at: datetime
    review_due_at: datetime
    model_labels: tuple[tuple[str, SentimentLabel], ...]
    supported_languages: frozenset[str]
    max_input_chars: int = 8_000
    max_token_count: int = 512

    def __post_init__(self) -> None:
        for name in (
            "model_id",
            "model_revision",
            "model_license_id",
            "training_dataset_id",
            "training_dataset_revision",
            "training_dataset_license_id",
            "reviewed_by",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty.")
        for name in ("model_license_uri", "model_card_uri", "training_dataset_license_uri"):
            parsed = urlsplit(getattr(self, name))
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError(f"{name} must be an HTTPS URL without embedded credentials.")
        for name in ("model_revision", "training_dataset_revision"):
            value = getattr(self, name)
            if len(value) != 40 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ValueError(f"{name} must be an immutable 40-character Git commit SHA.")
        require_aware_utc(self.reviewed_at, "model review timestamp")
        require_aware_utc(self.review_due_at, "model review expiry")
        if self.review_due_at <= self.reviewed_at:
            raise ValueError("review_due_at must be after reviewed_at.")
        if type(self.training_data_rights_verified) is not bool:
            raise ValueError("training_data_rights_verified must be explicit.")
        if type(self.commercial_use_approved) is not bool:
            raise ValueError("commercial_use_approved must be explicit.")
        if len(self.model_labels) != 3:
            raise ValueError("Model review must map exactly three output labels.")
        if any(not raw.strip() for raw, _ in self.model_labels):
            raise ValueError("Raw model output labels must not be empty.")
        if len({raw.casefold() for raw, _ in self.model_labels}) != 3:
            raise ValueError("Raw model output labels must be unique.")
        if {normalized for _, normalized in self.model_labels} != {
            SentimentLabel.POSITIVE,
            SentimentLabel.NEUTRAL,
            SentimentLabel.NEGATIVE,
        }:
            raise ValueError("The explicit model map must cover positive, neutral and negative.")
        if not self.supported_languages or any(
            not lang.strip() for lang in self.supported_languages
        ):
            raise ValueError("supported_languages must be a non-empty explicit set.")
        for name in ("max_input_chars", "max_token_count"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")

    def authorize(self, *, now: datetime, commercial_use: bool) -> None:
        """Fail closed until a reviewer verifies both model and training-data rights."""
        require_aware_utc(now, "model authorization time")
        if now < self.reviewed_at or now >= self.review_due_at:
            raise PermissionError("Sentiment model review is not currently valid.")
        if not self.training_data_rights_verified:
            raise PermissionError(
                "Training-data rights have not been verified for this model and intended use."
            )
        if commercial_use and not self.commercial_use_approved:
            raise PermissionError(
                "Commercial use is not approved by the model/training-data license review."
            )


class SentimentRuntime(Protocol):
    """Injectable inference port. Tests use a fake and do not import a transformer framework."""

    def predict_probabilities(
        self,
        text: str,
        *,
        model_id: str,
        revision: str,
        max_token_count: int,
    ) -> Mapping[str, Decimal | float]:
        """Return raw model-label probabilities for one text."""
        ...


class LocalHuggingFaceRuntime:
    """Lazily load pinned, safetensors-only model files; offline by default."""

    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        allow_model_download: bool = False,
    ) -> None:
        self._cache_dir = None if cache_dir is None else str(Path(cache_dir).expanduser())
        if type(allow_model_download) is not bool:
            raise ValueError("allow_model_download must be an explicit boolean.")
        self._allow_model_download = allow_model_download
        self._loaded: dict[tuple[str, str], tuple[Any, Any, Any]] = {}

    def predict_probabilities(
        self,
        text: str,
        *,
        model_id: str,
        revision: str,
        max_token_count: int,
    ) -> Mapping[str, Decimal]:
        key = (model_id, revision)
        if key not in self._loaded:
            self._load(key)
        tokenizer, model, torch = self._loaded[key]
        encoded = tokenizer(
            text,
            truncation=True,
            max_length=max_token_count,
            return_tensors="pt",
        )
        model.eval()
        with torch.no_grad():
            logits = model(**encoded).logits
            scores = torch.softmax(logits[0], dim=-1).tolist()
        raw_labels = model.config.id2label
        return {
            str(
                raw_labels[str(index)] if str(index) in raw_labels else raw_labels[index]
            ).casefold(): Decimal(str(float(score)))
            for index, score in enumerate(scores)
        }

    def _load(self, key: tuple[str, str]) -> None:
        model_id, revision = key
        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:
            raise NLPDependencyUnavailable(
                "Install the optional news-nlp dependencies before loading Hugging Face weights."
            ) from exc
        kwargs = {
            "local_files_only": not self._allow_model_download,
            "trust_remote_code": False,
            "cache_dir": self._cache_dir,
        }
        try:
            tokenizer = AutoTokenizer.from_pretrained(
                model_id, revision=revision, use_fast=True, **kwargs
            )
            model = AutoModelForSequenceClassification.from_pretrained(
                model_id,
                revision=revision,
                use_safetensors=True,
                **kwargs,
            )
        except (OSError, ValueError) as exc:
            raise NLPDependencyUnavailable(
                "Pinned model files are unavailable locally or failed safe loading."
            ) from exc
        self._loaded[key] = (tokenizer, model, torch)


@dataclass(frozen=True, slots=True)
class NewsSentimentOutcome:
    """Result including fail-closed abstention when license, language or model confidence fails."""

    record: NewsSentimentRecord | None
    abstained: bool
    reason_code: str
    explanation: str

    def __post_init__(self) -> None:
        if type(self.abstained) is not bool:
            raise ValueError("abstained must be an explicit boolean.")
        if self.abstained == (self.record is not None):
            raise ValueError("Exactly one of a sentiment record or abstention must be present.")
        if not self.reason_code.strip() or not self.explanation.strip():
            raise ValueError("Sentiment outcome must include reason and explanation.")


class HuggingFaceNewsSentimentAdapter:
    """News sentiment gate with source permission, model-rights and confidence abstention."""

    def __init__(
        self,
        *,
        review: SentimentModelReview,
        source_license_registry: SourceLicenseRegistry,
        runtime: SentimentRuntime | None = None,
        minimum_confidence: Decimal = Decimal("0.55"),
        commercial_use: bool = False,
    ) -> None:
        if (
            not isinstance(minimum_confidence, Decimal)
            or not minimum_confidence.is_finite()
            or not Decimal("0") < minimum_confidence <= Decimal("1")
        ):
            raise ValueError("minimum_confidence must be a finite Decimal in (0, 1].")
        if type(commercial_use) is not bool:
            raise ValueError("commercial_use must be an explicit boolean.")
        self._review = review
        self._licenses = source_license_registry
        self._runtime = runtime or LocalHuggingFaceRuntime()
        self._minimum_confidence = minimum_confidence
        self._commercial_use = commercial_use

    def analyze(
        self,
        article: NewsArticleRecord,
        *,
        now: datetime,
    ) -> NewsSentimentOutcome:
        """Analyze only accepted provider title/summary text, never fetch the publisher URL."""
        require_aware_utc(now, "sentiment analysis time")
        self._review.authorize(now=now, commercial_use=self._commercial_use)
        authorization = self._licenses.require(
            article.source_id, DataUse.NEWS_TEXT_PROCESSING, now=now
        )
        authorization.authorize(DataUse.INTERNAL_RESEARCH, now=now)
        authorization.authorize(DataUse.LOCAL_CONTENT_STORAGE, now=now)
        if (
            article.source_license_id != authorization.license_id
            or article.source_terms_revision != authorization.terms_revision
        ):
            raise PermissionError(
                "Article source-license provenance differs from current authorization."
            )
        if article.language.lower() not in {
            lang.lower() for lang in self._review.supported_languages
        }:
            return NewsSentimentOutcome(
                record=None,
                abstained=True,
                reason_code="LANGUAGE_UNSUPPORTED",
                explanation="This model review does not include the article language.",
            )
        text = f"{article.title}\n{article.summary}".strip()
        expected_content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if (
            article.integrity_evidence != "RECEIVED_JSON_SHA256_AND_NORMALIZED_TEXT_SHA256"
            or article.content_sha256 != expected_content_hash
        ):
            return NewsSentimentOutcome(
                record=None,
                abstained=True,
                reason_code="ARTICLE_INTEGRITY_NOT_VERIFIED",
                explanation=(
                    "The article does not carry expected quality-gate integrity evidence."
                ),
            )
        if article.source_trust_score < Decimal("0.60"):
            return NewsSentimentOutcome(
                record=None,
                abstained=True,
                reason_code="SOURCE_TRUST_TOO_LOW",
                explanation="The source trust score is below the NLP processing minimum.",
            )
        if not text or len(text) > self._review.max_input_chars:
            return NewsSentimentOutcome(
                record=None,
                abstained=True,
                reason_code="INPUT_EMPTY_OR_TOO_LARGE",
                explanation="Article text is empty or above the reviewed inference-size limit.",
            )
        raw = self._runtime.predict_probabilities(
            text,
            model_id=self._review.model_id,
            revision=self._review.model_revision,
            max_token_count=self._review.max_token_count,
        )
        mapping = {
            raw_label.casefold(): normalized
            for raw_label, normalized in self._review.model_labels
        }
        if set(raw) != set(mapping):
            return NewsSentimentOutcome(
                record=None,
                abstained=True,
                reason_code="MODEL_LABEL_MAPPING_MISMATCH",
                explanation="Runtime outputs differ from the reviewed model's explicit label map.",
            )
        normalized: dict[SentimentLabel, Decimal] = {}
        for raw_label, value in raw.items():
            try:
                probability = Decimal(str(value))
            except (ArithmeticError, ValueError):
                return _abstain("INVALID_MODEL_PROBABILITY")
            if not probability.is_finite() or probability < 0 or probability > 1:
                return _abstain("INVALID_MODEL_PROBABILITY")
            normalized[mapping[raw_label.casefold()]] = probability
        total = sum(normalized.values(), Decimal("0"))
        if total <= 0 or abs(total - Decimal("1")) > Decimal("0.02"):
            return _abstain("INVALID_PROBABILITY_DISTRIBUTION")
        normalized = {key: value / total for key, value in normalized.items()}
        label, confidence = max(
            normalized.items(),
            key=lambda item: item[1],
        )
        if confidence < self._minimum_confidence:
            return _abstain("LOW_SENTIMENT_CONFIDENCE")
        record = NewsSentimentRecord(
            article=article,
            analyzed_at=now,
            label=label,
            positive_probability=normalized[SentimentLabel.POSITIVE],
            neutral_probability=normalized[SentimentLabel.NEUTRAL],
            negative_probability=normalized[SentimentLabel.NEGATIVE],
            model_id=self._review.model_id,
            model_revision=self._review.model_revision,
            model_license_id=self._review.model_license_id,
            model_card_uri=self._review.model_card_uri,
            model_training_dataset_id=self._review.training_dataset_id,
            model_training_dataset_revision=self._review.training_dataset_revision,
            model_training_dataset_license_id=self._review.training_dataset_license_id,
            model_training_data_rights_verified=self._review.training_data_rights_verified,
            commercial_use_approved=self._review.commercial_use_approved,
        )
        return NewsSentimentOutcome(
            record=record,
            abstained=False,
            reason_code="SENTIMENT_CLASSIFIED",
            explanation=(
                "Sentiment was classified by a reviewed local model; "
                "this is advisory metadata."
            ),
        )

    @staticmethod
    def financial_phrasebank_candidate(
        *,
        reviewed_by: str,
        reviewed_at: datetime,
        review_due_at: datetime,
        training_data_rights_verified: bool = False,
        commercial_use_approved: bool = False,
    ) -> SentimentModelReview:
        """Return the reviewed English finance-news candidate with conservative rights defaults."""
        return SentimentModelReview(
            model_id=FINANCIAL_NEWS_MODEL_ID,
            model_revision=FINANCIAL_NEWS_MODEL_REVISION,
            model_license_id="Apache-2.0",
            model_license_uri="https://www.apache.org/licenses/LICENSE-2.0",
            model_card_uri=FINANCIAL_NEWS_MODEL_CARD,
            training_dataset_id=FINANCIAL_PHRASEBANK_DATASET_ID,
            training_dataset_revision=FINANCIAL_PHRASEBANK_DATASET_REVISION,
            training_dataset_license_id=FINANCIAL_PHRASEBANK_DATASET_LICENSE,
            training_dataset_license_uri="https://creativecommons.org/licenses/by-nc-sa/3.0/",
            training_data_rights_verified=training_data_rights_verified,
            commercial_use_approved=commercial_use_approved,
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
            review_due_at=review_due_at,
            model_labels=(
                ("negative", SentimentLabel.NEGATIVE),
                ("neutral", SentimentLabel.NEUTRAL),
                ("positive", SentimentLabel.POSITIVE),
            ),
            supported_languages=frozenset({"en"}),
        )


def _abstain(reason_code: str) -> NewsSentimentOutcome:
    return NewsSentimentOutcome(
        record=None,
        abstained=True,
        reason_code=reason_code,
        explanation="The sentiment output failed validation; no sentiment label is issued.",
    )
