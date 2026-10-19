"""Domain records for licensed external market/news data and NLP provenance."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from urllib.parse import urlsplit


class DataUse(StrEnum):
    """Explicit purposes that must be authorized independently by each source license."""

    MARKET_REFERENCE = "market_reference"
    NEWS_METADATA = "news_metadata"
    NEWS_TEXT_PROCESSING = "news_text_processing"
    INTERNAL_RESEARCH = "internal_research"
    LOCAL_CONTENT_STORAGE = "local_content_storage"
    PUBLIC_DISPLAY = "public_display"
    REDISTRIBUTION = "redistribution"


class NewsIngestionStatus(StrEnum):
    """Terminal status for a candidate evaluated by the news quality gate."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    SUSPICIOUS = "suspicious"


class SentimentLabel(StrEnum):
    """Normalized three-way sentiment plus explicit unclassified state."""

    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    UNKNOWN = "unknown"


def require_aware_utc(value: datetime, name: str) -> None:
    """Require an aware UTC value so ingestion and as-of comparisons are unambiguous."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{name} must use canonical UTC.")


def valid_https_url(value: str, name: str) -> None:
    """Reject non-HTTPS URLs, embedded credentials, empty hosts and fragments."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be an HTTPS URL without credentials or fragment.")


@dataclass(frozen=True, slots=True)
class SourceLicenseAuthorization:
    """Explicit terms review for one source and a precise set of permitted data uses."""

    source_id: str
    provider_name: str
    base_url: str
    license_id: str
    license_uri: str
    terms_uri: str
    terms_revision: str
    accepted_by: str
    accepted_at: datetime
    review_due_at: datetime
    permitted_uses: frozenset[DataUse]
    attribution_text: str
    trust_score: Decimal
    allowed_languages: frozenset[str]
    active: bool = True
    raw_content_retention_days: int = 0

    def __post_init__(self) -> None:
        for name in (
            "source_id",
            "provider_name",
            "license_id",
            "terms_revision",
            "accepted_by",
            "attribution_text",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be empty.")
        for name in ("base_url", "license_uri", "terms_uri"):
            valid_https_url(getattr(self, name), name)
        require_aware_utc(self.accepted_at, "accepted_at")
        require_aware_utc(self.review_due_at, "review_due_at")
        if self.review_due_at <= self.accepted_at:
            raise ValueError("review_due_at must be after accepted_at.")
        if not isinstance(self.permitted_uses, frozenset) or not self.permitted_uses:
            raise ValueError("permitted_uses must be a non-empty frozenset.")
        if any(not isinstance(item, DataUse) for item in self.permitted_uses):
            raise ValueError("permitted_uses contains an unknown use.")
        if not isinstance(self.trust_score, Decimal) or (
            not self.trust_score.is_finite()
            or self.trust_score < 0
            or self.trust_score > 1
        ):
            raise ValueError("trust_score must be a finite Decimal in [0, 1].")
        if any(not lang.strip() for lang in self.allowed_languages):
            raise ValueError("allowed_languages cannot contain empty values.")
        if type(self.active) is not bool:
            raise ValueError("active must be an explicit boolean.")
        if (
            type(self.raw_content_retention_days) is not int
            or self.raw_content_retention_days < 0
        ):
            raise ValueError("raw_content_retention_days must be a non-negative integer.")
        if (
            DataUse.LOCAL_CONTENT_STORAGE in self.permitted_uses
            and self.raw_content_retention_days <= 0
        ):
            raise ValueError(
                "Local content storage permission requires an explicit positive retention period."
            )

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""

    def authorize(self, use: DataUse, *, now: datetime) -> None:
        """Raise before network or processing when the source review is absent or expired."""
        require_aware_utc(now, "authorization time")
        if not self.active:
            raise PermissionError(f"Source {self.source_id!r} is disabled.")
        if now < self.accepted_at:
            raise PermissionError("The source authorization is not yet effective.")
        if now >= self.review_due_at:
            raise PermissionError("The source license/terms review is expired.")
        if use not in self.permitted_uses:
            raise PermissionError(
                f"Source {self.source_id!r} is not authorized for {use.value!r}."
            )


@dataclass(frozen=True, slots=True)
class ExternalMarketQuote:
    """Normalized reference quote with observation/receipt and license provenance."""

    source_id: str
    provider_asset_id: str
    asset_name: str
    symbol: str
    quote_currency: str
    price: Decimal
    market_cap: Decimal | None
    volume_24h: Decimal | None
    observed_at: datetime
    retrieved_at: datetime
    source_license_id: str
    source_terms_revision: str
    attribution_text: str
    payload_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "source_id",
            "provider_asset_id",
            "asset_name",
            "symbol",
            "quote_currency",
            "source_license_id",
            "source_terms_revision",
            "attribution_text",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty.")
        require_aware_utc(self.observed_at, "market observation time")
        require_aware_utc(self.retrieved_at, "market receipt time")
        if self.observed_at > self.retrieved_at:
            raise ValueError("Provider market timestamp cannot be later than retrieval.")
        _positive(self.price, "price")
        for name in ("market_cap", "volume_24h"):
            value = getattr(self, name)
            if value is not None:
                _nonnegative(value, name)
        _require_sha256(self.payload_sha256, "payload_sha256")


@dataclass(frozen=True, slots=True)
class NewsArticleCandidate:
    """Untrusted normalized fields returned by a licensed provider's JSON API."""

    source_id: str
    provider_article_id: str
    source_name: str
    article_url: str
    published_at: datetime | None
    retrieved_at: datetime
    language: str
    title: str
    summary: str
    provider_revision: str | None
    source_license_id: str
    source_terms_revision: str
    attribution_text: str
    payload_sha256: str
    declared_content_sha256: str | None = None
    related_asset_ids: tuple[str, ...] = ()
    language_evidence: str = "provider_field"

    def __post_init__(self) -> None:
        """Validate structural provenance, while leaving content quality to the quality gate."""
        for name in (
            "source_id",
            "provider_article_id",
            "source_name",
            "language",
            "source_license_id",
            "source_terms_revision",
            "attribution_text",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty.")
        valid_https_url(self.article_url, "article_url")
        require_aware_utc(self.retrieved_at, "retrieved_at")
        if self.published_at is not None:
            require_aware_utc(self.published_at, "published_at")
        _require_sha256(self.payload_sha256, "payload_sha256")
        if self.declared_content_sha256 is not None:
            _require_sha256(self.declared_content_sha256, "declared_content_sha256")
        if self.provider_revision is not None and not self.provider_revision.strip():
            raise ValueError("provider_revision must be None or a non-empty string.")
        if self.language_evidence not in {
            "provider_field",
            "request_filter",
            "operator_asserted",
        }:
            raise ValueError("language_evidence must identify the actual provenance of language.")
        if len(set(self.related_asset_ids)) != len(self.related_asset_ids):
            raise ValueError("related_asset_ids must be unique.")
        if any(not asset.strip() for asset in self.related_asset_ids):
            raise ValueError("related_asset_ids cannot contain empty values.")

    @property
    def normalized_content(self) -> str:
        """Use only title/summary fields returned by the API, never fetch the publisher URL."""
        return f"{self.title.strip()}\n{self.summary.strip()}".strip()

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.normalized_content.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class NewsArticleRecord:
    """Accepted immutable news record with source, license, timing and integrity evidence."""

    source_id: str
    provider_article_id: str
    source_name: str
    article_url: str
    published_at: datetime
    retrieved_at: datetime
    ingested_at: datetime
    language: str
    title: str
    summary: str
    provider_revision: str | None
    source_license_id: str
    source_terms_revision: str
    attribution_text: str
    content_sha256: str
    payload_sha256: str
    integrity_evidence: str
    related_asset_ids: tuple[str, ...] = ()
    source_trust_score: Decimal = Decimal("0")
    language_evidence: str = "provider_field"

    def __post_init__(self) -> None:
        for name in (
            "source_id",
            "provider_article_id",
            "source_name",
            "language",
            "source_license_id",
            "source_terms_revision",
            "attribution_text",
            "integrity_evidence",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty.")
        valid_https_url(self.article_url, "article_url")
        for name in ("published_at", "retrieved_at", "ingested_at"):
            require_aware_utc(getattr(self, name), name)
        if self.retrieved_at > self.ingested_at:
            raise ValueError("Article ingestion cannot precede retrieval.")
        if not self.title.strip():
            raise ValueError("An accepted article must have a title.")
        _require_sha256(self.content_sha256, "content_sha256")
        _require_sha256(self.payload_sha256, "payload_sha256")
        expected_content_hash = hashlib.sha256(
            f"{self.title.strip()}\\n{self.summary.strip()}".strip().encode("utf-8")
        ).hexdigest()
        if self.content_sha256 != expected_content_hash:
            raise ValueError("Accepted article content hash does not match title/summary bytes.")
        if self.language_evidence not in {
            "provider_field",
            "request_filter",
            "operator_asserted",
        }:
            raise ValueError("language_evidence must identify the actual provenance of language.")
        if len(set(self.related_asset_ids)) != len(self.related_asset_ids):
            raise ValueError("related_asset_ids must be unique.")
        if any(not asset.strip() for asset in self.related_asset_ids):
            raise ValueError("related_asset_ids cannot contain empty values.")
        if (
            not isinstance(self.source_trust_score, Decimal)
            or not self.source_trust_score.is_finite()
            or not Decimal("0") <= self.source_trust_score <= Decimal("1")
        ):
            raise ValueError("source_trust_score must be a finite Decimal in [0, 1].")


@dataclass(frozen=True, slots=True)
class NewsIngestionResult:
    """Quality-gate result with a stable reason code and no silent repairs."""

    status: NewsIngestionStatus
    reason_code: str
    explanation: str
    article: NewsArticleRecord | None = None

    def __post_init__(self) -> None:
        if not self.reason_code.strip() or not self.explanation.strip():
            raise ValueError("An ingestion result requires a reason and explanation.")
        if (self.status is NewsIngestionStatus.ACCEPTED) != (self.article is not None):
            raise ValueError("Only ACCEPTED results may contain a trusted article record.")


@dataclass(frozen=True, slots=True)
class NewsSentimentRecord:
    """Advisory sentiment with model revision and source provenance."""

    article: NewsArticleRecord
    analyzed_at: datetime
    label: SentimentLabel
    positive_probability: Decimal
    neutral_probability: Decimal
    negative_probability: Decimal
    model_id: str
    model_revision: str
    model_license_id: str
    model_card_uri: str
    model_training_dataset_id: str
    model_training_dataset_revision: str
    model_training_dataset_license_id: str
    model_training_data_rights_verified: bool
    commercial_use_approved: bool
    feature_schema_version: str = "news-sentiment/v1"

    def __post_init__(self) -> None:
        require_aware_utc(self.analyzed_at, "analyzed_at")
        for name in (
            "model_id",
            "model_revision",
            "model_license_id",
            "model_training_dataset_id",
            "model_training_dataset_revision",
            "model_training_dataset_license_id",
            "feature_schema_version",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty.")
        valid_https_url(self.model_card_uri, "model_card_uri")
        if type(self.model_training_data_rights_verified) is not bool:
            raise ValueError("model_training_data_rights_verified must be explicit.")
        if type(self.commercial_use_approved) is not bool:
            raise ValueError("commercial_use_approved must be explicit.")
        values = (
            self.positive_probability,
            self.neutral_probability,
            self.negative_probability,
        )
        for value in values:
            if not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 1:
                raise ValueError("Sentiment probabilities must be finite Decimals in [0, 1].")
        if abs(sum(values, Decimal("0")) - Decimal("1")) > Decimal("0.000001"):
            raise ValueError("Sentiment probabilities must sum to one.")
        if self.analyzed_at < self.article.retrieved_at:
            raise ValueError("Sentiment analysis cannot precede data retrieval.")
        if self.analyzed_at < self.article.ingested_at:
            raise ValueError("Sentiment analysis cannot precede article ingestion.")
        if not isinstance(self.label, SentimentLabel) or self.label is SentimentLabel.UNKNOWN:
            raise ValueError(
                "UNKNOWN or invalid sentiment must be represented by abstention, not probabilities."
            )
        for name in ("model_training_data_rights_verified", "commercial_use_approved"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be an explicit boolean.")

    @property
    def sentiment_score(self) -> Decimal:
        """Positive-minus-negative score in [-1, 1]; neutral probability is kept separately."""
        return self.positive_probability - self.negative_probability

    @property
    def confidence(self) -> Decimal:
        return max(
            self.positive_probability,
            self.neutral_probability,
            self.negative_probability,
        )

    @property
    def available_at(self) -> datetime:
        """Earliest point at which publication, retrieval and inference have all completed."""
        return max(self.article.published_at, self.article.retrieved_at, self.analyzed_at)


def _positive(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a finite positive Decimal.")


def _nonnegative(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a finite non-negative Decimal.")


def _require_sha256(value: str, name: str) -> None:
    if (
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest.")
