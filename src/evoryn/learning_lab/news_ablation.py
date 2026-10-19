"""Price-only versus licensed-news ablation, with causal availability and immutable reports.

The news overlay is a conservative veto: it can suppress a baseline long exposure, but it never
creates a new long, increases position size, shorts, or changes RiskEngine limits.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from evoryn.adapters.data.license_registry import SourceLicenseRegistry
from evoryn.domain.external_data import DataUse, NewsSentimentRecord, SentimentLabel, require_aware_utc
from evoryn.learning_lab.backtest import (
    BacktestConfig,
    BacktestResult,
    TemporalPartitions,
    run_backtest,
)
from evoryn.learning_lab.historical_data import HistoricalDataset
from evoryn.strategies.contracts import CausalTargetStrategy


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ZERO = Decimal("0")
_ONE = Decimal("1")


class AblationConclusion(StrEnum):
    """Evaluation conclusion; inconclusive is distinct from a negative result."""

    VALUE_ADDED_SUPPORTED = "value_added_supported"
    VALUE_ADDED_NOT_SUPPORTED = "value_added_not_supported"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class NewsAblationPolicy:
    """Conservative veto threshold and a predeclared minimum evidence count."""

    negative_sentiment_threshold: Decimal = Decimal("0.45")
    minimum_sentiment_confidence: Decimal = Decimal("0.70")
    minimum_source_trust: Decimal = Decimal("0.60")
    maximum_news_age: timedelta = timedelta(hours=24)
    minimum_effective_news_events: int = 3

    def __post_init__(self) -> None:
        for name in (
            "negative_sentiment_threshold",
            "minimum_sentiment_confidence",
            "minimum_source_trust",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value < 0
                or value > 1
            ):
                raise ValueError(f"{name} must be a finite Decimal in [0, 1].")
        if self.negative_sentiment_threshold == 0:
            raise ValueError("negative_sentiment_threshold must be positive.")
        if self.minimum_sentiment_confidence <= Decimal("0.5"):
            raise ValueError("minimum_sentiment_confidence must exceed chance-level 0.5.")
        if not isinstance(self.maximum_news_age, timedelta) or self.maximum_news_age <= timedelta(0):
            raise ValueError("maximum_news_age must be positive.")
        if (
            type(self.minimum_effective_news_events) is not int
            or self.minimum_effective_news_events <= 0
        ):
            raise ValueError("minimum_effective_news_events must be a positive integer.")

    def as_dict(self) -> dict[str, object]:
        return {
            "negative_sentiment_threshold": str(self.negative_sentiment_threshold),
            "minimum_sentiment_confidence": str(self.minimum_sentiment_confidence),
            "minimum_source_trust": str(self.minimum_source_trust),
            "maximum_news_age_seconds": int(self.maximum_news_age.total_seconds()),
            "minimum_effective_news_events": self.minimum_effective_news_events,
            "action": "veto_existing_long_only",
        }


class NewsAwareConservativeOverlay:
    """Causal news overlay over a price-only strategy, with no execution/risk port."""

    strategy_id = "price-baseline-with-negative-news-veto"
    strategy_version = "1.0.0"

    def __init__(
        self,
        *,
        market_symbol: str,
        price_only_strategy: CausalTargetStrategy,
        sentiment_records: tuple[NewsSentimentRecord, ...],
        policy: NewsAblationPolicy,
    ) -> None:
        if not market_symbol.strip():
            raise ValueError("market_symbol must not be empty.")
        if not getattr(price_only_strategy, "strategy_id", "").strip():
            raise ValueError("Price-only strategy must have an explicit ID.")
        self._market_symbol = market_symbol.upper()
        self._baseline = price_only_strategy
        self._policy = policy
        self._sentiments = tuple(
            sorted(
                sentiment_records,
                key=lambda record: (
                    record.available_at,
                    record.article.source_id,
                    record.article.provider_article_id,
                    record.model_id,
                    record.model_revision,
                ),
            )
        )
        self._used_article_ids: set[str] = set()
        self._veto_decision_times: set[datetime] = set()
        self._corpus_fingerprint = _sentiment_corpus_fingerprint(self._sentiments)

    @property
    def parameters(self) -> dict[str, str]:  # pragma: no cover - property is intentionally absent
        """Not used by CausalTargetStrategy; use parameters() below."""
        return {}

    def parameters(self) -> dict[str, str]:
        """Include baseline, article corpus, model revisions and veto policy in run identity."""
        return {
            "market_symbol": self._market_symbol,
            "price_only_strategy_id": self._baseline.strategy_id,
            "price_only_strategy_version": self._baseline.strategy_version,
            "price_only_parameters_sha256": hashlib.sha256(
                _canonical_json_bytes(self._baseline.parameters())
            ).hexdigest(),
            "news_corpus_sha256": self._corpus_fingerprint,
            "news_record_count": str(len(self._sentiments)),
            "policy_sha256": hashlib.sha256(
                _canonical_json_bytes(self._policy.as_dict())
            ).hexdigest(),
            "action": "negative_news_veto_only",
        }

    def target_exposure(self, history) -> Decimal:
        """Return the price-only target unless usable, timely negative news vetoes a long."""
        baseline_target = self._baseline.target_exposure(history)
        if baseline_target not in (_ZERO, _ONE):
            raise ValueError("Price-only baseline must return Decimal('0') or Decimal('1').")
        if baseline_target == _ZERO or not history:
            return baseline_target

        last = history[-1]
        decision_at = last.timestamp + last.interval
        if decision_at.tzinfo is None or decision_at.utcoffset() is None:
            return _ZERO
        eligible: list[NewsSentimentRecord] = []
        for sentiment in self._sentiments:
            article = sentiment.article
            if self._market_symbol not in {
                asset.upper() for asset in article.related_asset_ids
            }:
                continue
            if sentiment.available_at > decision_at:
                continue
            if decision_at - sentiment.available_at > self._policy.maximum_news_age:
                continue
            if article.source_trust_score < self._policy.minimum_source_trust:
                continue
            if sentiment.confidence < self._policy.minimum_sentiment_confidence:
                continue
            if (
                sentiment.label is SentimentLabel.NEGATIVE
                and sentiment.sentiment_score <= -self._policy.negative_sentiment_threshold
            ):
                eligible.append(sentiment)

        if eligible:
            self._used_article_ids.update(
                f"{item.article.source_id}:{item.article.provider_article_id}"
                for item in eligible
            )
            self._veto_decision_times.add(decision_at)
            return _ZERO
        return baseline_target

    @property
    def used_article_count(self) -> int:
        return len(self._used_article_ids)

    @property
    def veto_event_count(self) -> int:
        return len(self._veto_decision_times)

    @property
    def corpus_fingerprint(self) -> str:
        return self._corpus_fingerprint


@dataclass(frozen=True, slots=True)
class NewsAblationReport:
    """Immutable paired OOS reports and a predeclared test of news-added-value criteria."""

    report_id: str
    dataset_id: str
    dataset_version: str
    test_start_index: int
    test_end_index_exclusive: int
    baseline_result: BacktestResult
    news_result: BacktestResult
    news_articles_available_in_test: int
    news_articles_actually_vetoed: int
    veto_decision_count: int
    source_provenance: tuple[tuple[str, str, str, str], ...]
    model_provenance: tuple[tuple[str, str, str, str, str, bool], ...]
    policy_json: str
    news_corpus_fingerprint: str
    net_profit_delta: Decimal
    net_return_delta: Decimal
    max_drawdown_delta: Decimal
    expectancy_delta: Decimal | None
    conclusion: AblationConclusion
    conclusion_reason: str

    def to_report_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "report_id": self.report_id,
            "dataset": {
                "dataset_id": self.dataset_id,
                "dataset_version": self.dataset_version,
            },
            "test_range": {
                "start": self.test_start_index,
                "end_exclusive": self.test_end_index_exclusive,
                "note": "Held-out test only; do not retune with this period.",
            },
            "price_only_baseline": self.baseline_result.to_report_dict(),
            "news_aware_result": self.news_result.to_report_dict(),
            "news_evidence": {
                "available_articles_in_test": self.news_articles_available_in_test,
                "articles_that_vetoed": self.news_articles_actually_vetoed,
                "veto_decision_count": self.veto_decision_count,
                "corpus_fingerprint": self.news_corpus_fingerprint,
                "sources": [
                    {
                        "source_id": source_id,
                        "license_id": license_id,
                        "terms_revision": terms_revision,
                        "attribution_text": attribution,
                    }
                    for source_id, license_id, terms_revision, attribution in self.source_provenance
                ],
                "models": [
                    {
                        "model_id": model_id,
                        "model_revision": revision,
                        "model_license_id": model_license,
                        "training_dataset_id": dataset_id,
                        "training_dataset_license_id": dataset_license,
                        "training_data_rights_verified": rights_verified,
                    }
                    for (
                        model_id,
                        revision,
                        model_license,
                        dataset_id,
                        dataset_license,
                        rights_verified,
                    ) in self.model_provenance
                ],
            },
            "policy": json.loads(self.policy_json),
            "deltas_news_minus_price_only": {
                "net_profit": str(self.net_profit_delta),
                "net_return_fraction": str(self.net_return_delta),
                "max_drawdown_fraction": str(self.max_drawdown_delta),
                "expectancy_per_closed_trade": (
                    str(self.expectancy_delta) if self.expectancy_delta is not None else None
                ),
            },
            "conclusion": self.conclusion.value,
            "conclusion_reason": self.conclusion_reason,
        }

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self.to_report_dict())


def evaluate_news_ablation(
    dataset: HistoricalDataset,
    *,
    partitions: TemporalPartitions,
    price_only_strategy: CausalTargetStrategy,
    sentiment_records: tuple[NewsSentimentRecord, ...],
    license_registry: SourceLicenseRegistry,
    config: BacktestConfig,
    evaluated_at: datetime,
    policy: NewsAblationPolicy | None = None,
    commercial_use: bool = False,
) -> NewsAblationReport:
    """Compare price-only and licensed news-veto variants on identical untouched test data."""
    require_aware_utc(evaluated_at, "ablation evaluation timestamp")
    chosen_policy = policy or NewsAblationPolicy()
    if partitions.dataset_version != dataset.version or partitions.total_bars != len(dataset.candles):
        raise ValueError("Temporal partitions do not match the exact historical dataset.")
    if dataset.market.market_type.casefold() != "spot":
        raise ValueError("Phase 16 news ablation currently supports Spot data only.")
    if not getattr(price_only_strategy, "strategy_id", "").strip():
        raise ValueError("Price-only baseline requires strategy_id.")
    if not getattr(price_only_strategy, "strategy_version", "").strip():
        raise ValueError("Price-only baseline requires strategy_version.")
    if type(commercial_use) is not bool:
        raise ValueError("commercial_use must be an explicit boolean.")

    # Every candidate article must retain an active license allowing storage, NLP and research use.
    for sentiment in sentiment_records:
        article = sentiment.article
        authorization = license_registry.require(
            article.source_id, DataUse.NEWS_TEXT_PROCESSING, now=evaluated_at
        )
        authorization.authorize(DataUse.INTERNAL_RESEARCH, now=evaluated_at)
        if (
            article.source_license_id != authorization.license_id
            or article.source_terms_revision != authorization.terms_revision
            or article.attribution_text != authorization.attribution_text
        ):
            raise PermissionError("News evidence license provenance differs from current authorization.")
        if not sentiment.model_training_data_rights_verified:
            raise PermissionError("Sentiment model training-data rights are not verified.")
        if commercial_use and not sentiment.commercial_use_approved:
            raise PermissionError("Sentiment model is not approved for commercial use.")
        if sentiment.article.source_trust_score < chosen_policy.minimum_source_trust:
            raise PermissionError("An input sentiment record is below the required source trust.")

    overlay = NewsAwareConservativeOverlay(
        market_symbol=dataset.market.symbol,
        price_only_strategy=price_only_strategy,
        sentiment_records=sentiment_records,
        policy=chosen_policy,
    )
    baseline = run_backtest(
        dataset,
        price_only_strategy,
        config,
        start_index=partitions.test.start,
        end_index=partitions.test.end,
        warmup_start_index=0,
        segment_name="phase16-price-only-ablation-control",
    )
    news_result = run_backtest(
        dataset,
        overlay,
        config,
        start_index=partitions.test.start,
        end_index=partitions.test.end,
        warmup_start_index=0,
        segment_name="phase16-price-plus-news-ablation",
    )
    if not sentiment_records:
        conclusion = AblationConclusion.INCONCLUSIVE
        reason = "No licensed sentiment records were available for comparison."
    elif overlay.veto_event_count < chosen_policy.minimum_effective_news_events:
        conclusion = AblationConclusion.INCONCLUSIVE
        reason = (
            f"Only {overlay.veto_event_count} effective veto events; "
            f"need {chosen_policy.minimum_effective_news_events}."
        )
    elif (
        news_result.metrics.net_return_fraction >= baseline.metrics.net_return_fraction
        and news_result.metrics.max_drawdown_fraction < baseline.metrics.max_drawdown_fraction
    ):
        conclusion = AblationConclusion.VALUE_ADDED_SUPPORTED
        reason = (
            "On this held-out test only, the news variant met the predeclared rule: "
            "net return did not fall and maximum drawdown improved. This is not a future guarantee."
        )
    else:
        conclusion = AblationConclusion.VALUE_ADDED_NOT_SUPPORTED
        reason = (
            "The news variant did not satisfy the predeclared joint net-return/drawdown criterion "
            "on this held-out test."
        )

    baseline_expectancy = baseline.metrics.expectancy_per_closed_trade
    news_expectancy = news_result.metrics.expectancy_per_closed_trade
    expectancy_delta = (
        news_expectancy - baseline_expectancy
        if news_expectancy is not None and baseline_expectancy is not None
        else None
    )
    source_provenance = tuple(
        sorted(
            {
                (
                    item.article.source_id,
                    item.article.source_license_id,
                    item.article.source_terms_revision,
                    item.article.attribution_text,
                )
                for item in sentiment_records
            }
        )
    )
    model_provenance = tuple(
        sorted(
            {
                (
                    item.model_id,
                    item.model_revision,
                    item.model_license_id,
                    item.model_training_dataset_id,
                    item.model_training_dataset_license_id,
                    item.model_training_data_rights_verified,
                )
                for item in sentiment_records
            }
        )
    )
    policy_json = _canonical_json_bytes(chosen_policy.as_dict()).decode("utf-8")
    run_material = {
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.version,
        "test_range": {
            "start": partitions.test.start,
            "end_exclusive": partitions.test.end,
        },
        "baseline_run_id": baseline.run_id,
        "news_run_id": news_result.run_id,
        "news_corpus_fingerprint": overlay.corpus_fingerprint,
        "policy": json.loads(policy_json),
        "sources": [list(item) for item in source_provenance],
        "models": [list(item) for item in model_provenance],
    }
    report_id = hashlib.sha256(_canonical_json_bytes(run_material)).hexdigest()
    news_evidence_in_test = sum(
        1
        for sentiment in sentiment_records
        if partitions.test.start < _available_time_as_index(
            sentiment.available_at,
            dataset,
        ) < partitions.test.end
    )
    return NewsAblationReport(
        report_id=report_id,
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        test_start_index=partitions.test.start,
        test_end_index_exclusive=partitions.test.end,
        baseline_result=baseline,
        news_result=news_result,
        news_articles_available_in_test=news_evidence_in_test,
        news_articles_actually_vetoed=overlay.used_article_count,
        veto_decision_count=overlay.veto_event_count,
        source_provenance=source_provenance,
        model_provenance=model_provenance,
        policy_json=policy_json,
        news_corpus_fingerprint=overlay.corpus_fingerprint,
        net_profit_delta=news_result.metrics.net_profit - baseline.metrics.net_profit,
        net_return_delta=(
            news_result.metrics.net_return_fraction - baseline.metrics.net_return_fraction
        ),
        max_drawdown_delta=(
            news_result.metrics.max_drawdown_fraction - baseline.metrics.max_drawdown_fraction
        ),
        expectancy_delta=expectancy_delta,
        conclusion=conclusion,
        conclusion_reason=reason,
    )


def _available_time_as_index(available_at: datetime, dataset: HistoricalDataset) -> int:
    """Return first candle-open index at or after an availability time; no timestamp round-down."""
    for index, candle in enumerate(dataset.candles):
        if candle.timestamp >= available_at:
            return index
    return len(dataset.candles)


def _sentiment_corpus_fingerprint(records: tuple[NewsSentimentRecord, ...]) -> str:
    material = []
    for record in records:
        material.append(
            {
                "source_id": record.article.source_id,
                "article_id": record.article.provider_article_id,
                "source_license_id": record.article.source_license_id,
                "source_terms_revision": record.article.source_terms_revision,
                "content_sha256": record.article.content_sha256,
                "payload_sha256": record.article.payload_sha256,
                "related_asset_ids": list(record.article.related_asset_ids),
                "source_trust_score": str(record.article.source_trust_score),
                "published_at": _iso_utc(record.article.published_at),
                "retrieved_at": _iso_utc(record.article.retrieved_at),
                "analyzed_at": _iso_utc(record.analyzed_at),
                "sentiment": record.label.value,
                "probabilities": {
                    "positive": str(record.positive_probability),
                    "neutral": str(record.neutral_probability),
                    "negative": str(record.negative_probability),
                },
                "model_id": record.model_id,
                "model_revision": record.model_revision,
                "model_license_id": record.model_license_id,
                "training_dataset_id": record.model_training_dataset_id,
                "training_dataset_revision": record.model_training_dataset_revision,
                "training_dataset_license_id": record.model_training_dataset_license_id,
                "training_data_rights_verified": record.model_training_data_rights_verified,
                "commercial_use_approved": record.commercial_use_approved,
            }
        )
    return hashlib.sha256(_canonical_json_bytes(material)).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _iso_utc(value: datetime) -> str:
    require_aware_utc(value, "timestamp")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
