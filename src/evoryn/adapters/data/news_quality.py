"""Fail-closed quality and duplicate gate for licensed provider news records."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from threading import RLock
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from evoryn.adapters.data.license_registry import SourceLicenseRegistry
from evoryn.domain.external_data import (
    DataUse,
    NewsArticleCandidate,
    NewsArticleRecord,
    NewsIngestionResult,
    NewsIngestionStatus,
    require_aware_utc,
)


_TRACKING_QUERY_KEYS = frozenset(
    {"fbclid", "gclid", "ref", "ref_src", "source", "mc_cid", "mc_eid"}
)
_DANGEROUS_HTML = re.compile(r"<\s*/?\s*(script|iframe|object|embed|form)\b", re.IGNORECASE)
_DANGEROUS_SCHEME = re.compile(r"\bjavascript\s*:", re.IGNORECASE)
_HTML_TAG = re.compile(r"<\s*[a-z][^>]*>", re.IGNORECASE)
_BIDI_CONTROLS = frozenset(
    chr(codepoint)
    for codepoint in (0x202A, 0x202B, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069)
)


@dataclass(frozen=True, slots=True)
class NewsQualityPolicy:
    """Explicit content size, age, trust and look-alike duplicate limits."""

    max_article_age: timedelta = timedelta(hours=24)
    max_future_skew: timedelta = timedelta(0)
    minimum_source_trust: float = 0.6
    minimum_title_chars: int = 12
    maximum_title_chars: int = 500
    maximum_summary_chars: int = 10_000
    maximum_content_chars: int = 12_000
    near_duplicate_similarity: float = 0.94

    def __post_init__(self) -> None:
        if not isinstance(self.max_article_age, timedelta) or self.max_article_age <= timedelta(0):
            raise ValueError("max_article_age must be positive.")
        if not isinstance(self.max_future_skew, timedelta) or self.max_future_skew < timedelta(0):
            raise ValueError("max_future_skew must be non-negative.")
        if not 0 <= self.minimum_source_trust <= 1:
            raise ValueError("minimum_source_trust must be in [0, 1].")
        for name in (
            "minimum_title_chars",
            "maximum_title_chars",
            "maximum_summary_chars",
            "maximum_content_chars",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        if self.minimum_title_chars > self.maximum_title_chars:
            raise ValueError("minimum title length must not exceed its maximum.")
        if (
            not 0 < self.near_duplicate_similarity <= 1
        ):
            raise ValueError("near_duplicate_similarity must be in (0, 1].")


@dataclass(frozen=True, slots=True)
class _AcceptedSignature:
    source_id: str
    provider_article_id: str
    provider_revision: str | None
    content_sha256: str
    canonical_url: str
    normalized_title: str


class NewsDeduplicationIndex:
    """Thread-safe in-memory duplicate index; only accepted records enter its signatures."""

    def __init__(self, *, near_duplicate_similarity: float = 0.94) -> None:
        if not 0 < near_duplicate_similarity <= 1:
            raise ValueError("near_duplicate_similarity must be in (0, 1].")
        self.near_duplicate_similarity = near_duplicate_similarity
        self._lock = RLock()
        self._by_provider_identity: dict[tuple[str, str], _AcceptedSignature] = {}
        self._content_hashes: set[str] = set()
        self._canonical_urls: set[str] = set()
        self._titles: dict[str, str] = {}

    def classify(
        self,
        candidate: NewsArticleCandidate,
        *,
        canonical_url: str,
    ) -> tuple[NewsIngestionStatus, str] | None:
        """Return duplicate/manipulation decision without mutating the index."""
        key = (candidate.source_id, candidate.provider_article_id)
        title = _normalize_title(candidate.title)
        with self._lock:
            previous = self._by_provider_identity.get(key)
            if previous is not None:
                if previous.content_sha256 == candidate.content_sha256:
                    return NewsIngestionStatus.DUPLICATE, "SAME_PROVIDER_ID_AND_CONTENT"
                if candidate.provider_revision is None or (
                    candidate.provider_revision == previous.provider_revision
                ):
                    return (
                        NewsIngestionStatus.SUSPICIOUS,
                        "CONTENT_MUTATED_WITHOUT_NEW_PROVIDER_REVISION",
                    )
                # An explicit provider revision is the only exception to same-item URL/title dedup.
                return None

            if candidate.content_sha256 in self._content_hashes:
                return NewsIngestionStatus.DUPLICATE, "DUPLICATE_CONTENT_HASH"
            if canonical_url in self._canonical_urls:
                return NewsIngestionStatus.DUPLICATE, "DUPLICATE_CANONICAL_URL"
            if title in self._titles:
                return NewsIngestionStatus.DUPLICATE, "DUPLICATE_NORMALIZED_TITLE"
            for known_title in self._titles:
                if min(len(title), len(known_title)) >= 20 and (
                    SequenceMatcher(None, title, known_title).ratio()
                    >= self._near_duplicate_similarity
                ):
                    return NewsIngestionStatus.DUPLICATE, "NEAR_DUPLICATE_NORMALIZED_TITLE"
        return None

    def commit(self, candidate: NewsArticleCandidate, *, canonical_url: str) -> None:
        """Index accepted content only; duplicate/suspicious candidates never poison the index."""
        signature = _AcceptedSignature(
            source_id=candidate.source_id,
            provider_article_id=candidate.provider_article_id,
            provider_revision=candidate.provider_revision,
            content_sha256=candidate.content_sha256,
            canonical_url=canonical_url,
            normalized_title=_normalize_title(candidate.title),
        )
        key = (candidate.source_id, candidate.provider_article_id)
        with self._lock:
            previous = self._by_provider_identity.get(key)
            if previous is not None:
                self._content_hashes.discard(previous.content_sha256)
                self._canonical_urls.discard(previous.canonical_url)
                self._titles.pop(previous.normalized_title, None)
            self._by_provider_identity[key] = signature
            self._content_hashes.add(signature.content_sha256)
            self._canonical_urls.add(signature.canonical_url)
            self._titles[signature.normalized_title] = candidate.source_id


class NewsQualityGate:
    """Reject stale, weak, suspicious, repeated, or unlicensed articles before NLP."""

    def __init__(
        self,
        *,
        license_registry: SourceLicenseRegistry,
        deduplication_index: NewsDeduplicationIndex | None = None,
        policy: NewsQualityPolicy | None = None,
    ) -> None:
        self._licenses = license_registry
        self._policy = policy or NewsQualityPolicy()
        self._dedup = deduplication_index or NewsDeduplicationIndex(
            near_duplicate_similarity=self._policy.near_duplicate_similarity
        )

    @property
    def deduplication_index(self) -> NewsDeduplicationIndex:
        return self._dedup

    def evaluate(
        self,
        candidate: NewsArticleCandidate,
        *,
        now: datetime,
    ) -> NewsIngestionResult:
        """Return a stable gate result; no article text or token is written to logs."""
        require_aware_utc(now, "quality gate time")
        try:
            authorization = self._licenses.require(
                candidate.source_id, DataUse.NEWS_METADATA, now=now
            )
            authorization.authorize(DataUse.LOCAL_CONTENT_STORAGE, now=now)
        except (PermissionError, KeyError) as exc:
            return _result(
                NewsIngestionStatus.REJECTED,
                "SOURCE_USE_NOT_AUTHORIZED",
                str(exc),
            )

        if (
            candidate.source_license_id != authorization.license_id
            or candidate.source_terms_revision != authorization.terms_revision
            or candidate.attribution_text != authorization.attribution_text
        ):
            return _result(
                NewsIngestionStatus.REJECTED,
                "LICENSE_PROVENANCE_MISMATCH",
                "Candidate provenance does not match the current registered source authorization.",
            )

        if authorization.trust_score < self._policy.minimum_source_trust:
            return _result(
                NewsIngestionStatus.REJECTED,
                "SOURCE_TRUST_BELOW_MINIMUM",
                "The source trust score is below the configured minimum.",
            )

        if not _language_allowed(candidate.language, authorization.allowed_languages):
            return _result(
                NewsIngestionStatus.REJECTED,
                "LANGUAGE_NOT_AUTHORIZED",
                "The source authorization does not include this article language.",
            )

        if candidate.published_at is None:
            return _result(
                NewsIngestionStatus.REJECTED,
                "PUBLICATION_TIME_MISSING",
                "Publication time is missing; the record cannot be placed causally.",
            )
        if candidate.retrieved_at > now:
            return _result(
                NewsIngestionStatus.SUSPICIOUS,
                "RETRIEVAL_TIME_IN_FUTURE",
                "Provider receipt time is later than the quality-gate clock.",
            )
        if candidate.published_at > now + self._policy.max_future_skew:
            return _result(
                NewsIngestionStatus.SUSPICIOUS,
                "PUBLICATION_TIME_IN_FUTURE",
                "Provider publication time is later than the configured future-skew bound.",
            )
        if now - candidate.published_at > self._policy.max_article_age:
            return _result(
                NewsIngestionStatus.REJECTED,
                "ARTICLE_TOO_OLD",
                "Publication time exceeds the maximum news age.",
            )
        if now - candidate.retrieved_at > self._policy.max_article_age:
            return _result(
                NewsIngestionStatus.REJECTED,
                "RETRIEVAL_TOO_OLD",
                "The retrieved record is stale relative to the current ingestion time.",
            )
        title = candidate.title.strip()
        summary = candidate.summary.strip()
        content = candidate.normalized_content
        if not title or len(title) < self._policy.minimum_title_chars:
            return _result(
                NewsIngestionStatus.REJECTED,
                "TITLE_TOO_SHORT",
                "Article title is empty or below the configured minimum length.",
            )
        if len(title) > self._policy.maximum_title_chars:
            return _result(
                NewsIngestionStatus.REJECTED,
                "TITLE_TOO_LONG",
                "Article title exceeds the configured size limit.",
            )
        if len(summary) > self._policy.maximum_summary_chars or len(content) > (
            self._policy.maximum_content_chars
        ):
            return _result(
                NewsIngestionStatus.REJECTED,
                "CONTENT_TOO_LARGE",
                "Article title/summary exceeds configured content limits.",
            )
        if _has_control_or_bidi(content):
            return _result(
                NewsIngestionStatus.SUSPICIOUS,
                "HIDDEN_CONTROL_CHARACTERS",
                "Article text contains control or directional override characters.",
            )
        if _DANGEROUS_HTML.search(content) or _DANGEROUS_SCHEME.search(content):
            return _result(
                NewsIngestionStatus.SUSPICIOUS,
                "ACTIVE_CONTENT_MARKERS",
                "Article text contains active HTML/script markers and is rejected.",
            )
        if _HTML_TAG.search(content):
            return _result(
                NewsIngestionStatus.SUSPICIOUS,
                "HTML_MARKUP_IN_TEXT",
                "Unexpected markup in provider title/summary is rejected for manual review.",
            )
        if (
            candidate.declared_content_sha256 is not None
            and candidate.declared_content_sha256 != candidate.content_sha256
        ):
            return _result(
                NewsIngestionStatus.SUSPICIOUS,
                "DECLARED_CONTENT_HASH_MISMATCH",
                "Provider-declared normalized text digest does not match the "
                "received title/summary.",
            )
        canonical_url = _canonical_url(candidate.article_url)
        duplicate = self._dedup.classify(candidate, canonical_url=canonical_url)
        if duplicate is not None:
            status, code = duplicate
            explanation = (
                "Existing accepted news appears to have the same identity or content."
                if status is NewsIngestionStatus.DUPLICATE
                else "Existing provider identity changed content without an explicit new revision."
            )
            return _result(status, code, explanation)

        record = NewsArticleRecord(
            source_id=candidate.source_id,
            provider_article_id=candidate.provider_article_id,
            source_name=candidate.source_name,
            article_url=candidate.article_url,
            published_at=candidate.published_at,
            retrieved_at=candidate.retrieved_at,
            ingested_at=now,
            language=candidate.language,
            title=title,
            summary=summary,
            provider_revision=candidate.provider_revision,
            source_license_id=authorization.license_id,
            source_terms_revision=authorization.terms_revision,
            attribution_text=authorization.attribution_text,
            content_sha256=candidate.content_sha256,
            payload_sha256=candidate.payload_sha256,
            integrity_evidence="RECEIVED_JSON_SHA256_AND_NORMALIZED_TEXT_SHA256",
            related_asset_ids=candidate.related_asset_ids,
            source_trust_score=authorization.trust_score,
        )
        self._dedup.commit(candidate, canonical_url=canonical_url)
        return NewsIngestionResult(
            status=NewsIngestionStatus.ACCEPTED,
            reason_code="ACCEPTED",
            explanation=(
                "Licensed provider metadata passed freshness, provenance, integrity and "
                "dedup checks."
            ),
            article=record,
        )


def _result(
    status: NewsIngestionStatus,
    reason_code: str,
    explanation: str,
) -> NewsIngestionResult:
    return NewsIngestionResult(status=status, reason_code=reason_code, explanation=explanation)


def _canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    query = sorted(
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_QUERY_KEYS
    )
    host = parsed.hostname.casefold() if parsed.hostname else ""
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(query, doseq=True), ""))


def _normalize_title(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _language_allowed(language: str, allowed_languages: frozenset[str]) -> bool:
    normalized = language.strip().lower()
    primary = normalized.split("-", maxsplit=1)[0]
    allowed = {item.strip().lower() for item in allowed_languages}
    return normalized in allowed or primary in allowed or "*" in allowed


def _has_control_or_bidi(value: str) -> bool:
    if any(char in _BIDI_CONTROLS for char in value):
        return True
    total = max(1, len(value))
    controls = sum(
        1
        for char in value
        if unicodedata.category(char) in {"Cc", "Cf"} and char not in "\n\t"
    )
    return controls / total > 0.01
