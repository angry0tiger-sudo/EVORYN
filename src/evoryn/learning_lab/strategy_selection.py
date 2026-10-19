"""Out-of-sample strategy-selector adapter for the Phase 13 backtest engine.

This module composes Phase 13's causal simulator, the Phase 14 regime detector, the Phase 10
append-only execution/decision ledger, and the Phase 15 Strategy Plugin registry. It has no
order submission API and cannot bypass the standalone RiskEngine.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from evoryn.domain.models import MarketCandle
from evoryn.domain.strategy_decisions import StrategySelectionLedgerEntry
from evoryn.execution.spot import ExecutionLedger
from evoryn.intelligence.regime import RegimePrediction
from evoryn.learning_lab.backtest import (
    BacktestConfig,
    BacktestResult,
    TemporalPartitions,
    run_backtest,
)
from evoryn.learning_lab.historical_data import HistoricalDataset
from evoryn.strategies.contracts import CausalTargetStrategy
from evoryn.strategies.plugins import StrategyPluginRegistry
from evoryn.strategies.selector import (
    STRATEGY_SELECTOR_VERSION,
    RegimeStrategySelector,
    StrategySelectionPolicy,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class TrainingManifestPort(Protocol):
    """Minimum immutable evidence required before OOS selection may run."""

    @property
    def model_id(self) -> str:
        """Stable model identifier."""
        ...

    @property
    def model_version(self) -> str:
        """Immutable model version."""
        ...

    @property
    def feature_schema_version(self) -> str:
        """Feature contract version."""
        ...

    @property
    def training_dataset_version(self) -> str:
        """Exact immutable training dataset identity."""
        ...

    @property
    def training_split(self) -> str:
        """Training partition identity."""
        ...

    @property
    def train_start_index(self) -> int:
        """Inclusive train start index."""
        ...

    @property
    def train_end_index_exclusive(self) -> int:
        """Exclusive train end index."""
        ...

    @property
    def train_start_at(self) -> datetime:
        """UTC start time for the training range."""
        ...

    @property
    def train_end_at(self) -> datetime:
        """UTC end time for the training range."""
        ...

    @property
    def model_fingerprint(self) -> str:
        """Fingerprint for model and training configuration."""
        ...

    def to_dict(self) -> dict[str, Any]:
        """Return the model's reproducibility manifest."""
        ...


class RegimeDetectorPort(Protocol):
    """The selector needs predictions plus a verifiable train-only model manifest."""

    @property
    def training_manifest(self) -> TrainingManifestPort | None:
        ...

    @property
    def model_metadata(self) -> Mapping[str, str]:
        ...

    def classify(
        self, completed_history: Sequence[MarketCandle], *, now: datetime
    ) -> RegimePrediction:
        ...


@dataclass(frozen=True, slots=True)
class AdaptiveRegimeStrategy:
    """A causal backtest strategy wrapper over the regime detector and hysteretic selector."""

    dataset_id: str
    dataset_version: str
    detector: RegimeDetectorPort
    registry: StrategyPluginRegistry
    selector: RegimeStrategySelector
    strategy_id: str = "evoryn-adaptive-regime-selector"
    strategy_version: str = "1.0.0"

    def parameters(self) -> dict[str, str]:
        """Fingerprint model, registry and selector policy in the Phase 13 result."""
        metadata = self.detector.model_metadata
        policy = self.selector.policy.as_dict()
        values = {
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "selector_engine_version": STRATEGY_SELECTOR_VERSION,
            "registry_fingerprint": self.registry.fingerprint,
            "routing_fingerprint": self.selector.routing_fingerprint,
            "selection_policy": json.dumps(
                policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ),
            "model_id": metadata.get("model_id", "unknown"),
            "model_version": metadata.get("model_version", "unknown"),
            "model_fingerprint": metadata.get("model_fingerprint", "unknown"),
            "confidence_semantics": metadata.get("confidence_semantics", "unknown"),
            "feature_schema_version": metadata.get("feature_schema_version", "unknown"),
            "execution_scope": "research_only_no_order_authority",
        }
        return values

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        """Produce long/flat target exposure from the active plugin or abstain flat."""
        if not history:
            return Decimal("0")
        last = history[-1]
        now = last.timestamp + last.interval
        prediction = self.detector.classify(history, now=now)
        market_symbol = last.market.symbol
        timestamp_key = now.astimezone(UTC).isoformat(timespec="microseconds")
        correlation_id = (
            f"backtest:{market_symbol}:{self.dataset_version[:16]}:{timestamp_key}"
        )
        decision = self.selector.select(
            prediction,
            now=now,
            correlation_id=correlation_id,
            market_symbol=market_symbol,
        )
        if not decision.allow_new_signals or decision.selected_strategy_id is None:
            return Decimal("0")
        registration = self.registry.get(decision.selected_strategy_id)
        target = registration.strategy.target_exposure(history)
        if (
            not isinstance(target, Decimal)
            or not target.is_finite()
            or target not in (Decimal("0"), Decimal("1"))
        ):
            raise ValueError(
                f"Plugin {registration.plugin_id!r} violated the target-exposure contract."
            )
        return target


@dataclass(frozen=True, slots=True)
class StrategySelectionOOSReport:
    """Immutable, explicitly test-period comparison against a fixed strategy baseline."""

    report_id: str
    dataset_id: str
    dataset_version: str
    test_range_start: int
    test_range_end_exclusive: int
    train_range_start: int
    train_range_end_exclusive: int
    adaptive_result: BacktestResult
    fixed_baseline_result: BacktestResult
    fixed_baseline_strategy_id: str
    fixed_baseline_strategy_version: str
    selector_engine_version: str
    selection_policy_json: str
    registry_fingerprint: str
    routing_fingerprint: str
    model_metadata: tuple[tuple[str, str], ...]
    training_manifest_json: str
    decision_ledger_entries: tuple[StrategySelectionLedgerEntry, ...]
    net_profit_delta: Decimal
    net_return_delta: Decimal
    max_drawdown_delta: Decimal
    expectancy_delta: Decimal | None

    def to_report_dict(self) -> dict[str, Any]:
        """Serialize OOS comparison plus selector decisions, including failed switches/abstains."""
        return {
            "schema_version": 1,
            "report_id": self.report_id,
            "dataset": {
                "dataset_id": self.dataset_id,
                "dataset_version": self.dataset_version,
            },
            "training_range": {
                "start": self.train_range_start,
                "end_exclusive": self.train_range_end_exclusive,
            },
            "test_range": {
                "start": self.test_range_start,
                "end_exclusive": self.test_range_end_exclusive,
                "note": "Out-of-sample prices; do not use this report to retune the selector.",
            },
            "adaptive_result": self.adaptive_result.to_report_dict(),
            "fixed_baseline_result": self.fixed_baseline_result.to_report_dict(),
            "fixed_baseline": {
                "strategy_id": self.fixed_baseline_strategy_id,
                "strategy_version": self.fixed_baseline_strategy_version,
            },
            "selector_engine_version": self.selector_engine_version,
            "selection_policy": json.loads(self.selection_policy_json),
            "registry_fingerprint": self.registry_fingerprint,
            "routing_fingerprint": self.routing_fingerprint,
            "model_metadata": dict(self.model_metadata),
            "training_manifest": json.loads(self.training_manifest_json),
            "comparison_deltas": {
                "net_profit": str(self.net_profit_delta),
                "net_return_fraction": str(self.net_return_delta),
                "max_drawdown_fraction": str(self.max_drawdown_delta),
                "expectancy_per_closed_trade": (
                    str(self.expectancy_delta) if self.expectancy_delta is not None else None
                ),
            },
            "selection_decisions": [
                {
                    "event_id": entry.event_id,
                    "correlation_id": entry.correlation_id,
                    "selector_version": entry.selector_version,
                    "recorded_at": _iso_utc(entry.recorded_at),
                    "regime_as_of": _iso_utc(entry.regime_as_of),
                    "market_symbol": entry.market_symbol,
                    "regime_label": entry.regime_label,
                    "regime_confidence": str(entry.regime_confidence),
                    "confidence_semantics": entry.confidence_semantics,
                    "model_id": entry.model_id,
                    "model_version": entry.model_version,
                    "model_fingerprint": entry.model_fingerprint,
                    "feature_schema_version": entry.feature_schema_version,
                    "previous_strategy_id": entry.previous_strategy_id,
                    "candidate_strategy_id": entry.candidate_strategy_id,
                    "active_strategy_id": entry.active_strategy_id,
                    "alternatives": list(entry.alternative_strategy_ids),
                    "status": entry.status,
                    "reason_code": entry.reason_code,
                    "reason": entry.reason,
                    "evidence_count": entry.evidence_count,
                    "required_evidence_count": entry.required_evidence_count,
                    "candidate_since": (
                        _iso_utc(entry.candidate_since)
                        if entry.candidate_since is not None
                        else None
                    ),
                    "last_switch_at": (
                        _iso_utc(entry.last_switch_at)
                        if entry.last_switch_at is not None
                        else None
                    ),
                    "required_stability_seconds": entry.required_stability_seconds,
                    "minimum_hold_seconds": entry.minimum_hold_seconds,
                    "maximum_prediction_age_seconds": entry.maximum_prediction_age_seconds,
                    "confidence_threshold": str(entry.confidence_threshold),
                    "allow_new_signals": entry.allow_new_signals,
                    "invalidation_conditions": list(entry.invalidation_conditions),
                    "registry_fingerprint": entry.registry_fingerprint,
                    "routing_fingerprint": entry.routing_fingerprint,
                }
                for entry in self.decision_ledger_entries
            ],
        }

    def to_json_bytes(self) -> bytes:
        """Return canonical JSON bytes suitable for immutable storage."""
        return _canonical_json_bytes(self.to_report_dict())


def evaluate_strategy_selector_out_of_sample(
    dataset: HistoricalDataset,
    *,
    partitions: TemporalPartitions,
    detector: RegimeDetectorPort,
    registry: StrategyPluginRegistry,
    selection_policy: StrategySelectionPolicy,
    fixed_baseline: CausalTargetStrategy,
    backtest_config: BacktestConfig,
    decision_ledger: ExecutionLedger | None = None,
) -> StrategySelectionOOSReport:
    """Compare adaptive routing with one fixed strategy on the held-out test partition.

    The detector's training manifest must bind to this exact dataset digest and remain wholly
    inside partitions.train. The selector and its fixed routing policy are created before the
    test period is simulated. Do not tune on the returned test metrics and continue calling this
    result out-of-sample.
    """
    if not isinstance(registry, StrategyPluginRegistry):
        raise TypeError("registry must be a validated StrategyPluginRegistry.")
    if partitions.dataset_version != dataset.version:
        raise ValueError("Train/validation/test partitions belong to a different dataset version.")
    if partitions.total_bars != len(dataset.candles):
        raise ValueError("Temporal partitions do not match the historical dataset length.")
    manifest = detector.training_manifest
    if manifest is None:
        raise ValueError("OOS selector evaluation requires a fitted model training manifest.")
    initial_registry_fingerprint = registry.fingerprint
    initial_model_metadata = dict(detector.model_metadata)
    if manifest.training_dataset_version != dataset.version:
        raise ValueError("Regime model was trained on a different dataset version.")
    if manifest.training_split != "train":
        raise ValueError("OOS selector evaluation requires a model fitted only on train.")
    metadata_at_start = dict(detector.model_metadata)
    if (
        metadata_at_start.get("model_id") != manifest.model_id
        or metadata_at_start.get("model_version") != manifest.model_version
        or metadata_at_start.get("model_fingerprint") != manifest.model_fingerprint
        or metadata_at_start.get("feature_schema_version") != manifest.feature_schema_version
    ):
        raise ValueError("Regime-model identity metadata does not match its training manifest.")
    if (
        manifest.train_start_index < partitions.train.start
        or manifest.train_end_index_exclusive > partitions.train.end
        or manifest.train_end_index_exclusive <= manifest.train_start_index
    ):
        raise ValueError("Regime model training range is not contained in the train partition.")
    if (
        manifest.train_start_at != dataset.candles[manifest.train_start_index].timestamp
        or manifest.train_end_at
        != dataset.candles[manifest.train_end_index_exclusive - 1].timestamp
        + dataset.interval
    ):
        raise ValueError(
            "Regime-model training timestamps do not match the declared train indices."
        )
    if partitions.test.size <= 0:
        raise ValueError("Out-of-sample test partition must be non-empty.")
    if not getattr(fixed_baseline, "strategy_id", "").strip():
        raise ValueError("Fixed comparison strategy requires strategy_id.")
    if not getattr(fixed_baseline, "strategy_version", "").strip():
        raise ValueError("Fixed comparison strategy requires strategy_version.")

    ledger = decision_ledger or ExecutionLedger()
    initial_ledger_length = len(ledger.strategy_selections)
    selector = RegimeStrategySelector(
        registry,
        policy=selection_policy,
        decision_ledger=ledger,
    )
    adaptive_strategy = AdaptiveRegimeStrategy(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        detector=detector,
        registry=registry,
        selector=selector,
    )
    adaptive_result = run_backtest(
        dataset,
        adaptive_strategy,
        backtest_config,
        start_index=partitions.test.start,
        end_index=partitions.test.end,
        warmup_start_index=0,
        segment_name="strategy-selector-out-of-sample-test",
    )
    fixed_result = run_backtest(
        dataset,
        fixed_baseline,
        backtest_config,
        start_index=partitions.test.start,
        end_index=partitions.test.end,
        warmup_start_index=0,
        segment_name="fixed-strategy-out-of-sample-test",
    )
    if registry.fingerprint != initial_registry_fingerprint:
        raise ValueError("Strategy registry changed during OOS evaluation; report is invalid.")
    if dict(detector.model_metadata) != initial_model_metadata:
        raise ValueError("Regime-model identity changed during OOS evaluation; report is invalid.")

    adaptive_expectancy = adaptive_result.metrics.expectancy_per_closed_trade
    fixed_expectancy = fixed_result.metrics.expectancy_per_closed_trade
    expectancy_delta = (
        adaptive_expectancy - fixed_expectancy
        if adaptive_expectancy is not None and fixed_expectancy is not None
        else None
    )
    metadata = tuple(
        sorted((str(key), str(value)) for key, value in detector.model_metadata.items())
    )
    manifest_json = _canonical_json_bytes(manifest.to_dict()).decode("utf-8")
    decision_entries = ledger.strategy_selections[initial_ledger_length:]
    run_material = {
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.version,
        "train_range": {
            "start": partitions.train.start,
            "end": partitions.train.end,
        },
        "test_range": {
            "start": partitions.test.start,
            "end": partitions.test.end,
        },
        "adaptive_run_id": adaptive_result.run_id,
        "fixed_baseline_run_id": fixed_result.run_id,
        "selector_engine_version": STRATEGY_SELECTOR_VERSION,
        "registry_fingerprint": registry.fingerprint,
        "routing_fingerprint": selector.routing_fingerprint,
        "model_metadata": dict(metadata),
        "selection_policy": selection_policy.as_dict(),
        "model_training_manifest": json.loads(manifest_json),
    }
    report_id = hashlib.sha256(_canonical_json_bytes(run_material)).hexdigest()
    return StrategySelectionOOSReport(
        report_id=report_id,
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        test_range_start=partitions.test.start,
        test_range_end_exclusive=partitions.test.end,
        train_range_start=partitions.train.start,
        train_range_end_exclusive=partitions.train.end,
        adaptive_result=adaptive_result,
        fixed_baseline_result=fixed_result,
        fixed_baseline_strategy_id=fixed_baseline.strategy_id,
        fixed_baseline_strategy_version=fixed_baseline.strategy_version,
        selector_engine_version=STRATEGY_SELECTOR_VERSION,
        selection_policy_json=_canonical_json_bytes(selection_policy.as_dict()).decode("utf-8"),
        registry_fingerprint=registry.fingerprint,
        routing_fingerprint=selector.routing_fingerprint,
        model_metadata=metadata,
        training_manifest_json=manifest_json,
        decision_ledger_entries=decision_entries,
        net_profit_delta=(
            adaptive_result.metrics.net_profit - fixed_result.metrics.net_profit
        ),
        net_return_delta=(
            adaptive_result.metrics.net_return_fraction
            - fixed_result.metrics.net_return_fraction
        ),
        max_drawdown_delta=(
            adaptive_result.metrics.max_drawdown_fraction
            - fixed_result.metrics.max_drawdown_fraction
        ),
        expectancy_delta=expectancy_delta,
    )


class StrategySelectionOOSReportStore:
    """Immutable report store with a checksum sidecar and reproducible identity validation."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()

    def save(self, report: StrategySelectionOOSReport) -> Path:
        if not _SHA256_PATTERN.fullmatch(report.report_id):
            raise ValueError("report_id must be a lowercase SHA-256 digest.")
        report_dict = report.to_report_dict()
        if _oos_report_identity_hash(report_dict) != report.report_id:
            raise ValueError("Report ID does not match its versioned experiment inputs.")
        report_bytes = _canonical_json_bytes(report_dict)
        report_hash = hashlib.sha256(report_bytes).hexdigest()
        destination = self._root / f"{report.report_id}.json"
        checksum_path = self._root / f"{report.report_id}.json.sha256"
        self._root.mkdir(parents=True, exist_ok=True)

        if destination.exists() or checksum_path.exists():
            return self._verify_existing(destination, checksum_path, report_bytes, report_hash)

        report_fd, report_tmp_name = tempfile.mkstemp(
            prefix=".oos-", suffix=".json.tmp", dir=self._root
        )
        checksum_fd, checksum_tmp_name = tempfile.mkstemp(
            prefix=".oos-", suffix=".sha256.tmp", dir=self._root
        )
        report_tmp = Path(report_tmp_name)
        checksum_tmp = Path(checksum_tmp_name)
        try:
            with os.fdopen(report_fd, "wb") as report_file:
                report_file.write(report_bytes)
            with os.fdopen(checksum_fd, "w", encoding="ascii", newline="") as checksum_file:
                checksum_file.write(f"{report_hash}\n")
            try:
                os.link(report_tmp, destination)
            except FileExistsError:
                return self._verify_existing(
                    destination, checksum_path, report_bytes, report_hash
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
        """Verify file bytes, canonical JSON, and the report's input-derived ID."""
        if not _SHA256_PATTERN.fullmatch(report_id):
            raise ValueError("report_id must be a lowercase SHA-256 digest.")
        report_path = self._root / f"{report_id}.json"
        checksum_path = self._root / f"{report_id}.json.sha256"
        try:
            report_bytes = report_path.read_bytes()
            checksum_text = checksum_path.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError("OOS report or checksum sidecar is missing or invalid.") from exc
        actual_hash = hashlib.sha256(report_bytes).hexdigest()
        if checksum_text != f"{actual_hash}\n":
            raise ValueError("OOS report checksum does not match stored bytes.")
        try:
            report_dict = json.loads(report_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("OOS report is not valid JSON.") from exc
        if not isinstance(report_dict, dict) or report_dict.get("report_id") != report_id:
            raise ValueError("OOS report identity does not match the requested ID.")
        if _canonical_json_bytes(report_dict) != report_bytes:
            raise ValueError("OOS report is not in canonical JSON form.")
        if _oos_report_identity_hash(report_dict) != report_id:
            raise ValueError("OOS report ID does not match its experiment inputs.")
        return report_dict

    @staticmethod
    def _verify_existing(
        destination: Path,
        checksum_path: Path,
        report_bytes: bytes,
        report_hash: str,
    ) -> Path:
        try:
            existing_report = destination.read_bytes()
            existing_checksum = checksum_path.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(
                "Existing OOS report package is incomplete; refusing overwrite."
            ) from exc
        if existing_report != report_bytes or existing_checksum != f"{report_hash}\n":
            raise ValueError("Existing OOS report ID has conflicting report/checksum bytes.")
        return destination


def _oos_report_identity_hash(report: dict[str, Any]) -> str:
    """Derive the report ID from exact dataset, train/test ranges, model, routes, and run IDs."""
    try:
        dataset = report["dataset"]
        training = report["training_range"]
        test = report["test_range"]
        adaptive = report["adaptive_result"]
        fixed = report["fixed_baseline_result"]
        run_material = {
            "dataset_id": dataset["dataset_id"],
            "dataset_version": dataset["dataset_version"],
            "train_range": {
                "start": training["start"],
                "end": training["end_exclusive"],
            },
            "test_range": {
                "start": test["start"],
                "end": test["end_exclusive"],
            },
            "adaptive_run_id": adaptive["run_id"],
            "fixed_baseline_run_id": fixed["run_id"],
            "registry_fingerprint": report["registry_fingerprint"],
            "routing_fingerprint": report["routing_fingerprint"],
            "model_metadata": report["model_metadata"],
            "selection_policy": report["selection_policy"],
            "selector_engine_version": report["selector_engine_version"],
            "model_training_manifest": report["training_manifest"],
        }
    except (KeyError, TypeError) as exc:
        raise ValueError("OOS report omits fields required for its identity.") from exc
    return hashlib.sha256(_canonical_json_bytes(run_material)).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _iso_utc(value: datetime) -> str:
    _require_aware(value, "timestamp")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")
