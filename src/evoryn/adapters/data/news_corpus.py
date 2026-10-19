"""Retention-aware, fail-closed in-memory event corpus for accepted news and sentiment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock

from evoryn.adapters.data.license_registry import SourceLicenseRegistry
from evoryn.domain.external_data import (
    DataUse,
    NewsArticleRecord,
    NewsSentimentRecord,
    require_aware_utc,
)


@dataclass(frozen=True, slots=True)
class _StoredArticle:
    record: NewsArticleRecord
    retention_expires_at: datetime


@dataclass(frozen=True, slots=True)
class NewsCorpusSnapshot:
    """Immutable in-memory snapshot with a deterministic provenance-and-content fingerprint."""

    captured_at: datetime
    articles: tuple[NewsArticleRecord, ...]
    sentiments: tuple[NewsSentimentRecord, ...]
    fingerprint: str

    def __post_init__(self) -> None:
        require_aware_utc(self.captured_at, "corpus snapshot timestamp")
        if len(self.fingerprint) != 64 or any(
            char not in "0123456789abcdef" for char in self.fingerprint
        ):
            raise ValueError("Corpus fingerprint must be a SHA-256 digest.")


class NewsEventCorpus:
    """Append-only within process, except mandatory retention/authorization purges.

    The corpus does not silently persist copyrighted titles/summaries to disk. A future durable
    store must implement the same license, revision and retention checks before writing content.
    """

    def __init__(self, license_registry: SourceLicenseRegistry) -> None:
        self._licenses = license_registry
        self._lock = RLock()
        self._articles: dict[tuple[str, str, str, str | None], _StoredArticle] = {}
        self._sentiments: dict[tuple[tuple[str, str, str, str | None], str, str], NewsSentimentRecord] = {}

    def append_article(self, article: NewsArticleRecord, *, now: datetime) -> None:
        """Store an accepted article only when active source terms permit bounded retention."""
        require_aware_utc(now, "corpus write timestamp")
        authorization = self._licenses.require(
            article.source_id, DataUse.NEWS_METADATA, now=now
        )
        authorization.authorize(DataUse.LOCAL_CONTENT_STORAGE, now=now)
        if (
            article.source_license_id != authorization.license_id
            or article.source_terms_revision != authorization.terms_revision
            or article.attribution_text != authorization.attribution_text
        ):
            raise PermissionError("Article license provenance differs from the current authorization.")
        if article.ingested_at > now:
            raise ValueError("An article cannot be stored before its ingestion timestamp.")
        expiry = min(
            article.ingested_at + timedelta(days=authorization.raw_content_retention_days),
            authorization.review_due_at,
        )
        if now >= expiry:
            raise PermissionError("Article is already beyond its authorized retention period.")
        key = _article_key(article)
        stored = _StoredArticle(record=article, retention_expires_at=expiry)
        with self._lock:
            existing = self._articles.get(key)
            if existing is not None:
                if existing.record != article:
                    raise ValueError("Article identity conflicts with the accepted corpus record.")
                return
            self._articles[key] = stored

    def append_sentiment(self, sentiment: NewsSentimentRecord, *, now: datetime) -> None:
        """Append sentiment only when its source article and training-data rights are authorized."""
        require_aware_utc(now, "corpus sentiment write timestamp")
        if not sentiment.model_training_data_rights_verified:
            raise PermissionError("Sentiment model training-data rights are not verified.")
        article = sentiment.article
        authorization = self._licenses.require(
            article.source_id, DataUse.NEWS_TEXT_PROCESSING, now=now
        )
        authorization.authorize(DataUse.LOCAL_CONTENT_STORAGE, now=now)
        if (
            article.source_license_id != authorization.license_id
            or article.source_terms_revision != authorization.terms_revision
        ):
            raise PermissionError("Sentiment source terms no longer match the accepted article.")
        article_key = _article_key(article)
        event_key = (article_key, sentiment.model_id, sentiment.model_revision)
        with self._lock:
            stored = self._articles.get(article_key)
            if stored is None or stored.record != article:
                raise ValueError("Sentiment cannot be stored without its accepted source article.")
            if now >= stored.retention_expires_at:
                raise PermissionError("Source article retention has expired.")
            existing = self._sentiments.get(event_key)
            if existing is not None and existing != sentiment:
                raise ValueError("A model revision produced conflicting sentiment for one article.")
            self._sentiments[event_key] = sentiment

    def purge_expired(self, *, now: datetime) -> tuple[int, int]:
        """Purge records after retention/review expiry or when source terms no longer match."""
        require_aware_utc(now, "corpus purge timestamp")
        expired_articles: set[tuple[str, str, str, str | None]] = set()
        with self._lock:
            for key, stored in self._articles.items():
                article = stored.record
                try:
                    authorization = self._licenses.get(article.source_id)
                except PermissionError:
                    expired_articles.add(key)
                    continue
                if (
                    not authorization.active
                    or now >= authorization.review_due_at
                    or now >= stored.retention_expires_at
                    or article.source_license_id != authorization.license_id
                    or article.source_terms_revision != authorization.terms_revision
                    or DataUse.LOCAL_CONTENT_STORAGE not in authorization.permitted_uses
                ):
                    expired_articles.add(key)
            for key in expired_articles:
                self._articles.pop(key, None)
            before_sentiments = len(self._sentiments)
            self._sentiments = {
                key: sentiment
                for key, sentiment in self._sentiments.items()
                if key[0] not in expired_articles
            }
            removed_sentiments = before_sentiments - len(self._sentiments)
        return len(expired_articles), removed_sentiments

    def snapshot(self, *, now: datetime) -> NewsCorpusSnapshot:
        """Return only unexpired records after applying all required license/retention purges."""
        require_aware_utc(now, "corpus snapshot timestamp")
        self.purge_expired(now=now)
        with self._lock:
            articles = tuple(
                sorted(
                    (stored.record for stored in self._articles.values()),
                    key=lambda item: (
                        item.published_at,
                        item.source_id,
                        item.provider_article_id,
                        item.content_sha256,
                    ),
                )
            )
            sentiments = tuple(
                sorted(
                    self._sentiments.values(),
                    key=lambda item: (
                        item.available_at,
                        item.article.source_id,
                        item.article.provider_article_id,
                        item.model_id,
                        item.model_revision,
                    ),
                )
            )
        material = {
            "articles": [
                {
                    "source_id": item.source_id,
                    "provider_article_id": item.provider_article_id,
                    "published_at": item.published_at.isoformat(),
                    "retrieved_at": item.retrieved_at.isoformat(),
                    "ingested_at": item.ingested_at.isoformat(),
                    "language": item.language,
                    "language_evidence": item.language_evidence,
                    "source_license_id": item.source_license_id,
                    "source_terms_revision": item.source_terms_revision,
                    "content_sha256": item.content_sha256,
                    "payload_sha256": item.payload_sha256,
                    "related_asset_ids": list(item.related_asset_ids),
                }
                for item in articles
            ],
            "sentiments": [
                {
                    "source_id": item.article.source_id,
                    "article_id": item.article.provider_article_id,
                    "model_id": item.model_id,
                    "model_revision": item.model_revision,
                    "model_license_id": item.model_license_id,
                    "training_dataset_id": item.model_training_dataset_id,
                    "training_dataset_revision": item.model_training_dataset_revision,
                    "training_dataset_license_id": item.model_training_dataset_license_id,
                    "training_data_rights_verified": item.model_training_data_rights_verified,
                    "positive_probability": str(item.positive_probability),
                    "neutral_probability": str(item.neutral_probability),
                    "negative_probability": str(item.negative_probability),
                    "analyzed_at": item.analyzed_at.isoformat(),
                }
                for item in sentiments
            ],
        }
        canonical = json.dumps(
            material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return NewsCorpusSnapshot(
            captured_at=now,
            articles=articles,
            sentiments=sentiments,
            fingerprint=hashlib.sha256(canonical).hexdigest(),
        )


def _article_key(article: NewsArticleRecord) -> tuple[str, str, str, str | None]:
    return (
        article.source_id,
        article.provider_article_id,
        article.content_sha256,
        article.provider_revision,
    )
