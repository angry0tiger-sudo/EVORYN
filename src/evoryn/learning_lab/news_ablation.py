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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from evoryn.adapters.data.license_registry import SourceLicenseRegistry
from evoryn.domain.external_data import (
    DataUse,
    NewsSentimentRecord,
    SentimentLabel,
    require_aware_utc,
)
from evoryn.domain.models import MarketCandle
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
        if (
            not isinstance(self.maximum_news_age, timedelta)
            or self.maximum_news_age <= timedelta(0)
        ):
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

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        """Return the price-only target unless usable, timely negative news vetoes a long."""
        baseline_target = self._baseline.target_exposure(history)
        if (
            not isinstance(baseline_target, Decimal)
            or not baseline_target.is_finite()
            or baseline_target not in (_ZERO, _ONE)
        ):
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
            # Equal timestamps do not prove the article was processed before the bar open.
            if sentiment.available_at >= decision_at:
                continue
            if (
                decision_at - sentiment.available_at > self._policy.maximum_news_age
                or decision_at - article.published_at > self._policy.maximum_news_age
            ):
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
    model_provenance: tuple[tuple[str, str, str, str, str, str, bool, bool], ...]
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
                        "training_dataset_revision": dataset_revision,
                        "training_dataset_license_id": dataset_license,
                        "training_data_rights_verified": rights_verified,
                        "commercial_use_approved": commercial_approved,
                    }
                    for (
                        model_id,
                        revision,
                        model_license,
                        dataset_id,
                        dataset_revision,
                        dataset_license,
                        rights_verified,
                        commercial_approved,
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
    if (
        partitions.dataset_version != dataset.version
        or partitions.total_bars != len(dataset.candles)
    ):
        raise ValueError("Temporal partitions do not match the exact historical dataset.")
    if dataset.market.market_type.casefold() != "spot":
        raise ValueError("Phase 16 news ablation currently supports Spot data only.")
    if not getattr(price_only_strategy, "strategy_id", "").strip():
        raise ValueError("Price-only baseline requires strategy_id.")
    if not getattr(price_only_strategy, "strategy_version", "").strip():
        raise ValueError("Price-only baseline requires strategy_version.")
    if type(commercial_use) is not bool:
        raise ValueError("commercial_use must be an explicit boolean.")

    # Only records linked to this canonical market can enter this ablation.
    relevant_records = tuple(
        record
        for record in sentiment_records
        if dataset.market.symbol.upper()
        in {asset.upper() for asset in record.article.related_asset_ids}
    )
    # Every relevant article must retain active authorization for storage, NLP and research use.
    for sentiment in relevant_records:
        article = sentiment.article
        authorization = license_registry.require(
            article.source_id, DataUse.NEWS_TEXT_PROCESSING, now=evaluated_at
        )
        authorization.authorize(DataUse.INTERNAL_RESEARCH, now=evaluated_at)
        authorization.authorize(DataUse.LOCAL_CONTENT_STORAGE, now=evaluated_at)
        if (
            article.source_license_id != authorization.license_id
            or article.source_terms_revision != authorization.terms_revision
            or article.attribution_text != authorization.attribution_text
        ):
            raise PermissionError(
                "News evidence license provenance differs from current authorization."
            )
        if not sentiment.model_training_data_rights_verified:
            raise PermissionError("Sentiment model training-data rights are not verified.")
        if commercial_use and not sentiment.commercial_use_approved:
            raise PermissionError("Sentiment model is not approved for commercial use.")
        if sentiment.article.source_trust_score < chosen_policy.minimum_source_trust:
            raise PermissionError("An input sentiment record is below the required source trust.")

    overlay = NewsAwareConservativeOverlay(
        market_symbol=dataset.market.symbol,
        price_only_strategy=price_only_strategy,
        sentiment_records=relevant_records,
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
    if not relevant_records:
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
                for item in relevant_records
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
                    item.model_training_dataset_revision,
                    item.model_training_dataset_license_id,
                    item.model_training_data_rights_verified,
                    item.commercial_use_approved,
                )
                for item in relevant_records
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
    news_evidence_in_test = len(
        {
            f"{sentiment.article.source_id}:{sentiment.article.provider_article_id}"
            for sentiment in relevant_records
            if partitions.test.start
            <= _available_time_as_index(sentiment.available_at, dataset)
            < partitions.test.end
        }
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


class NewsAblationReportStore:
    """Immutable JSON/checksum storage for reproducible news-versus-price ablation reports."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()

    def save(self, report: NewsAblationReport) -> Path:
        if not _SHA256_PATTERN.fullmatch(report.report_id):
            raise ValueError("report_id must be a lowercase SHA-256 digest.")
        report_dict = report.to_report_dict()
        if _ablation_report_identity_hash(report_dict) != report.report_id:
            raise ValueError("Report ID does not match ablation inputs.")
        report_bytes = report.to_json_bytes()
        report_digest = hashlib.sha256(report_bytes).hexdigest()
        destination = self._root / f"{report.report_id}.json"
        checksum_path = self._root / f"{report.report_id}.json.sha256"
        self._root.mkdir(parents=True, exist_ok=True)
        if destination.exists() or checksum_path.exists():
            return self._verify_existing(destination, checksum_path, report_bytes, report_digest)

        report_fd, report_tmp_name = tempfile.mkstemp(
            prefix=".news-ablation-", suffix=".json.tmp", dir=self._root
        )
        checksum_fd, checksum_tmp_name = tempfile.mkstemp(
            prefix=".news-ablation-", suffix=".sha256.tmp", dir=self._root
        )
        report_tmp = Path(report_tmp_name)
        checksum_tmp = Path(checksum_tmp_name)
        try:
            with os.fdopen(report_fd, "wb") as report_file:
                report_file.write(report_bytes)
            with os.fdopen(checksum_fd, "w", encoding="ascii", newline="") as checksum_file:
                checksum_file.write(f"{report_digest}\\n")
            try:
                os.link(report_tmp, destination)
            except FileExistsError:
                return self._verify_existing(
                    destination, checksum_path, report_bytes, report_digest
                )
            try:
                os.link(checksum_tmp, checksum_path)
            except OSError:
                if destination.exists() and not checksum_path.exists():
                    destination.unlink()
                raise
        finally:
            if report_tmp.exists():
                report_tmp.unlink()
            if checksum_tmp.exists():
                checksum_tmp.unlink()
        self.load(report.report_id)
        return destination

    def load(self, report_id: str) -> dict[str, Any]:
        """Read reports with a valid checksum, canonical form and recomputed input identity."""
        if not _SHA256_PATTERN.fullmatch(report_id):
            raise ValueError("report_id must be a lowercase SHA-256 digest.")
        destination = self._root / f"{report_id}.json"
        checksum_path = self._root / f"{report_id}.json.sha256"
        try:
            report_bytes = destination.read_bytes()
            checksum = checksum_path.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError("Ablation report or checksum sidecar is missing/invalid.") from exc
        digest = hashlib.sha256(report_bytes).hexdigest()
        if checksum != f"{digest}\\n":
            raise ValueError("Ablation report checksum does not match stored bytes.")
        try:
            report_dict = json.loads(report_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Ablation report is invalid JSON.") from exc
        if not isinstance(report_dict, dict) or report_dict.get("report_id") != report_id:
            raise ValueError("Ablation report does not match the requested ID.")
        if _canonical_json_bytes(report_dict) != report_bytes:
            raise ValueError("Ablation report is not canonical JSON.")
        if _ablation_report_identity_hash(report_dict) != report_id:
            raise ValueError("Ablation report ID does not match its experiment inputs.")
        return report_dict

    @staticmethod
    def _verify_existing(
        destination: Path,
        checksum_path: Path,
        report_bytes: bytes,
        report_digest: str,
    ) -> Path:
        try:
            existing = destination.read_bytes()
            checksum = checksum_path.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError("Existing ablation report is incomplete; refusing overwrite.") from exc
        if existing != report_bytes or checksum != f"{report_digest}\\n":
            raise ValueError("Existing ablation report ID has conflicting content.")
        return destination


def _ablation_report_identity_hash(report: dict[str, Any]) -> str:
    """Recompute report identity from the dataset, held-out range, runs, news corpus and terms."""
    try:
        dataset = report["dataset"]
        test_range = report["test_range"]
        baseline = report["price_only_baseline"]
        news_result = report["news_aware_result"]
        news_evidence = report["news_evidence"]
        run_material = {
            "dataset_id": dataset["dataset_id"],
            "dataset_version": dataset["dataset_version"],
            "test_range": {
                "start": test_range["start"],
                "end_exclusive": test_range["end_exclusive"],
            },
            "baseline_run_id": baseline["run_id"],
            "news_run_id": news_result["run_id"],
            "news_corpus_fingerprint": news_evidence["corpus_fingerprint"],
            "policy": report["policy"],
            "sources": [
                [
                    source["source_id"],
                    source["license_id"],
                    source["terms_revision"],
                    source["attribution_text"],
                ]
                for source in news_evidence["sources"]
            ],
            "models": [
                [
                    model["model_id"],
                    model["model_revision"],
                    model["model_license_id"],
                    model["training_dataset_id"],
                    model["training_dataset_revision"],
                    model["training_dataset_license_id"],
                    model["training_data_rights_verified"],
                    model["commercial_use_approved"],
                ]
                for model in news_evidence["models"]
            ],
        }
    except (KeyError, TypeError) as exc:
        raise ValueError("Ablation report omits required identity inputs.") from exc
    return hashlib.sha256(_canonical_json_bytes(run_material)).hexdigest()


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
