"""Cross-fitted Triple-Barrier Meta-Labeling research, isolated from Runtime.

The primary signal is a fixed, deterministic SMA-12/48 golden cross; it has no fitted
parameters, so its proposals are causal and out-of-fit by construction. Learned meta-models
are evaluated only on later, disjoint test folds. Labels and features use the Phase 31 schema.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from evoryn.domain.models import MarketCandle
from evoryn.learning_lab.backtest import BacktestConfig, run_backtest
from evoryn.learning_lab.historical_data import HistoricalDataset
from evoryn.learning_lab.triple_barrier import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    BarrierOutcome,
    PurgedEventFold,
    TripleBarrierConfig,
    TripleBarrierEvent,
    build_triple_barrier_events,
    make_purged_event_folds,
    purge_overlapping_events,
)

MetaModelName = Literal["xgboost", "lightgbm"]
_META_MODELS: tuple[MetaModelName, ...] = ("xgboost", "lightgbm")
META_LABEL_SCHEMA_VERSION = "1.0.0-purged-triple-barrier-crossfit"
_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class CrossFittedMetaConfig:
    """Fixed protocol, model parameters and acceptance gates declared before evaluation."""

    short_window: int = 12
    long_window: int = 48
    vertical_horizon_bars: int = 24
    profit_taking_multiple: float = 1.5
    stop_loss_multiple: float = 1.0
    label_round_trip_cost_bps: float = 50.0
    train_event_count: int = 30
    validation_event_count: int = 30
    test_event_count: int = 10
    step_event_count: int = 10
    expected_fold_count: int = 8
    embargo_bars: int = 24
    min_train_events: int = 25
    min_class_count: int = 5
    min_calibration_events: int = 10
    min_threshold_events: int = 8
    min_closed_trades: int = 20
    max_drawdown_fraction: float = 0.15
    max_tail_loss_fraction: float = 0.10
    commission_bps: Decimal = Decimal("10")
    spread_bps: Decimal = Decimal("10")
    slippage_bps: Decimal = Decimal("10")
    allocation_fraction: Decimal = Decimal("0.25")
    initial_cash: Decimal = Decimal("10000")
    quantity_step: Decimal = Decimal("0.00000001")
    random_seed: int = 41

    def __post_init__(self) -> None:
        for name in (
            "short_window",
            "long_window",
            "vertical_horizon_bars",
            "train_event_count",
            "validation_event_count",
            "test_event_count",
            "step_event_count",
            "expected_fold_count",
            "min_train_events",
            "min_class_count",
            "min_calibration_events",
            "min_threshold_events",
            "min_closed_trades",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        if self.short_window >= self.long_window:
            raise ValueError("short_window must be smaller than long_window.")
        if self.step_event_count < self.test_event_count:
            raise ValueError("Test event folds must not overlap.")
        if type(self.embargo_bars) is not int or self.embargo_bars < 0:
            raise ValueError("embargo_bars must be a non-negative integer.")
        if type(self.random_seed) is not int or self.random_seed < 0:
            raise ValueError("random_seed must be a non-negative integer.")
        for name in ("profit_taking_multiple", "stop_loss_multiple"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive.")
        for name in ("max_drawdown_fraction", "max_tail_loss_fraction"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value < 1:
                raise ValueError(f"{name} must be finite and in (0, 1).")
        if (
            not math.isfinite(self.label_round_trip_cost_bps)
            or not 0 <= self.label_round_trip_cost_bps < 10_000
        ):
            raise ValueError("label_round_trip_cost_bps must be in [0, 10000).")
        if not self.train_event_count >= self.min_train_events:
            raise ValueError("train_event_count cannot be below min_train_events.")


@dataclass(frozen=True, slots=True)
class ThresholdSelection:
    """Validation-only probability threshold; 1.01 explicitly means abstain."""

    threshold: float
    validation_event_count: int
    selected_event_count: int
    compounded_net_label_return: float
    abstain: bool


class BuyAndHoldReference:
    """Long-only reference held over the same exact test candle window."""

    strategy_id = "buy-and-hold-reference"
    strategy_version = "1.0.0"

    def parameters(self) -> dict[str, str]:
        return {"position_policy": "long_only", "signal_price": "completed_close"}

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        return _ONE if history else _ZERO


class TripleBarrierEventPolicy:
    """Offline strategy that trades only the allowed SMA events and obeys frozen barriers.

    It reads barrier levels and the predeclared vertical horizon, never the realized label,
    realized exit index, outcome, or future return stored on the research event.
    """

    strategy_id = "sma12-48-triple-barrier-primary"
    strategy_version = "1.0.0"

    def __init__(
        self,
        *,
        events_by_index: dict[int, TripleBarrierEvent],
        allowed_event_indices: set[int],
        horizon_bars: int,
        selected_event_indices: set[int] | None = None,
        model_name: str = "unfiltered",
        fold_index: int = -1,
    ) -> None:
        self._events_by_index = events_by_index
        self._allowed = set(allowed_event_indices)
        self._selected = (
            set(selected_event_indices)
            if selected_event_indices is not None
            else set(allowed_event_indices)
        )
        self._horizon_bars = horizon_bars
        self._model_name = model_name
        self._fold_index = fold_index
        self._active_index: int | None = None
        self._consumed: set[int] = set()

    def parameters(self) -> dict[str, str]:
        return {
            "primary_signal": "SMA golden cross 12/48",
            "exit_policy": "first completed-bar frozen horizontal barrier or vertical expiry",
            "meta_model": self._model_name,
            "fold_index": str(self._fold_index),
            "eligible_event_count": str(len(self._allowed)),
            "accepted_event_count": str(len(self._selected.intersection(self._allowed))),
            "vertical_horizon_bars": str(self._horizon_bars),
        }

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        if not history:
            return _ZERO
        current_index = len(history) - 1
        candle = history[-1]
        if self._active_index is not None:
            active = self._events_by_index[self._active_index]
            # The current candle is closed and was not available for any earlier fill.
            stop_hit = candle.low <= active.stop_loss_price
            profit_hit = candle.high >= active.profit_taking_price
            expired = current_index - active.event_index >= self._horizon_bars
            if stop_hit or profit_hit or expired:
                self._active_index = None
                # Do not immediately re-enter on a simultaneous cross/exit bar.
                if current_index in self._allowed:
                    self._consumed.add(current_index)
                return _ZERO
            if current_index in self._allowed:
                self._consumed.add(current_index)
            return _ONE

        if (
            current_index not in self._allowed
            or current_index in self._consumed
            or current_index not in self._selected
        ):
            self._consumed.add(current_index) if current_index in self._allowed else None
            return _ZERO
        # Entry is sent only after event candle close and fills no earlier than next open.
        self._active_index = current_index
        self._consumed.add(current_index)
        return _ONE


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _target(event: TripleBarrierEvent) -> int:
    """Fixed target: event outcome after the label's assumed costs is strictly positive."""
    return int(event.net_return_fraction > 0.0)


def _event_matrix(events: Sequence[TripleBarrierEvent]) -> tuple[Any, Any]:
    numpy = importlib.import_module("numpy")
    features = numpy.asarray([event.feature_values for event in events], dtype="float64")
    labels = numpy.asarray([_target(event) for event in events], dtype="int64")
    if (
        features.ndim != 2
        or features.shape[1] != len(FEATURE_NAMES)
        or features.shape[0] != labels.shape[0]
        or not numpy.isfinite(features).all()
    ):
        raise ValueError("Meta-label event matrix is malformed or non-finite.")
    return features, labels


def _fit_meta_model(model_name: MetaModelName, seed: int) -> Any:
    """Use predeclared regularized tree parameters; no test-time or Optuna tuning."""
    from evoryn.learning_lab.advanced_meta_label import make_meta_label_classifier

    if model_name == "xgboost":
        params: dict[str, int | float] = {
            "n_estimators": 80,
            "max_depth": 2,
            "learning_rate": 0.03,
            "min_child_weight": 5.0,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "reg_alpha": 0.1,
            "reg_lambda": 5.0,
        }
    else:
        params = {
            "n_estimators": 80,
            "max_depth": 3,
            "num_leaves": 7,
            "learning_rate": 0.03,
            "min_child_samples": 10,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "reg_alpha": 0.1,
            "reg_lambda": 5.0,
        }
    return make_meta_label_classifier(model_name, params, random_seed=seed)


def _fit_probability_calibrator(raw_probabilities: Sequence[float], labels: Sequence[int], seed: int) -> Any:
    """Fit Platt scaling on a separate, later calibration partition only."""
    numpy = importlib.import_module("numpy")
    linear_model = importlib.import_module("sklearn.linear_model")
    probabilities = numpy.asarray(raw_probabilities, dtype="float64")
    target = numpy.asarray(labels, dtype="int64")
    if (
        probabilities.ndim != 1
        or target.ndim != 1
        or len(probabilities) != len(target)
        or len(target) < 10
        or not numpy.isfinite(probabilities).all()
        or not numpy.isfinite(target).all()
        or (probabilities < 0).any()
        or (probabilities > 1).any()
        or set(int(value) for value in target) != {0, 1}
        or int((target == 0).sum()) < 2
        or int((target == 1).sum()) < 2
    ):
        raise ValueError("Calibration partition needs >=10 finite samples and both classes (>=2 each).")
    clipped = numpy.clip(probabilities, 1e-6, 1.0 - 1e-6)
    logits = numpy.log(clipped / (1.0 - clipped)).reshape(-1, 1)
    calibrator = linear_model.LogisticRegression(
        C=1.0,
        max_iter=1000,
        solver="lbfgs",
        random_state=seed,
    )
    calibrator.fit(logits, target)
    return calibrator


def _calibrate_probabilities(calibrator: Any, raw_probabilities: Sequence[float]) -> list[float]:
    numpy = importlib.import_module("numpy")
    probabilities = numpy.asarray(raw_probabilities, dtype="float64")
    clipped = numpy.clip(probabilities, 1e-6, 1.0 - 1e-6)
    logits = numpy.log(clipped / (1.0 - clipped)).reshape(-1, 1)
    calibrated = calibrator.predict_proba(logits)[:, 1]
    if not numpy.isfinite(calibrated).all():
        raise ValueError("Probability calibration emitted non-finite values.")
    return [float(value) for value in calibrated]


def _selected_non_overlapping(
    events: Sequence[TripleBarrierEvent],
    probabilities: Sequence[float],
    threshold: float,
    *,
    embargo_bars: int,
) -> tuple[TripleBarrierEvent, ...]:
    """Apply an event-level concurrency constraint to validation threshold selection."""
    selected: list[TripleBarrierEvent] = []
    next_eligible_index = -1
    for event, probability in zip(events, probabilities, strict=True):
        if probability < threshold or event.event_index <= next_eligible_index:
            continue
        selected.append(event)
        next_eligible_index = event.label_end_index + embargo_bars
    return tuple(selected)


def select_validation_threshold(
    events: Sequence[TripleBarrierEvent],
    probabilities: Sequence[float],
    *,
    embargo_bars: int,
) -> ThresholdSelection:
    """Choose a fixed-grid threshold on a validation-only block; otherwise abstain."""
    if not events or len(events) != len(probabilities):
        raise ValueError("Threshold selection requires aligned non-empty validation events.")
    if any(not math.isfinite(value) or value < 0 or value > 1 for value in probabilities):
        raise ValueError("Threshold probabilities must be finite values in [0, 1].")
    minimum_selected = max(3, math.ceil(0.30 * len(events)))
    best_threshold: float | None = None
    best_return = 0.0
    best_count = 0
    for threshold in (
        0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50,
        0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95,
    ):
        selected = _selected_non_overlapping(
            events, probabilities, threshold, embargo_bars=embargo_bars
        )
        compounded = math.prod(1.0 + event.net_return_fraction for event in selected) - 1.0
        if len(selected) < minimum_selected or compounded <= 0:
            continue
        if compounded > best_return or (
            math.isclose(compounded, best_return)
            and (best_threshold is None or threshold > best_threshold)
        ):
            best_threshold = threshold
            best_return = compounded
            best_count = len(selected)
    if best_threshold is None:
        return ThresholdSelection(
            threshold=1.01,
            validation_event_count=len(events),
            selected_event_count=0,
            compounded_net_label_return=0.0,
            abstain=True,
        )
    return ThresholdSelection(
        threshold=best_threshold,
        validation_event_count=len(events),
        selected_event_count=best_count,
        compounded_net_label_return=best_return,
        abstain=False,
    )


def _backtest_config(config: CrossFittedMetaConfig) -> BacktestConfig:
    return BacktestConfig(
        initial_cash=config.initial_cash,
        commission_bps=config.commission_bps,
        spread_bps=config.spread_bps,
        slippage_bps=config.slippage_bps,
        max_bar_participation=Decimal("1"),
        allocation_fraction=config.allocation_fraction,
        quantity_step=config.quantity_step,
        allow_gaps=False,
        # UDF historical volume semantics are not verified; do not invent capacity.
        enforce_volume_participation=False,
    )


def _metrics(result: Any) -> dict[str, Any]:
    stats = result.metrics
    return {
        "net_return_fraction": float(stats.net_return_fraction),
        "max_drawdown_fraction": float(stats.max_drawdown_fraction),
        "closed_trade_count": int(stats.closed_trade_count),
        "commission_paid": str(stats.commission_paid),
        "estimated_spread_cost": str(stats.estimated_spread_cost),
        "estimated_slippage_cost": str(stats.estimated_slippage_cost),
        "gross_turnover": str(stats.gross_turnover),
        "time_in_market_fraction": float(stats.time_in_market_fraction),
    }


def _bootstrap_mean_interval(values: Sequence[float], *, seed: int) -> dict[str, float | int] | None:
    numpy = importlib.import_module("numpy")
    array = numpy.asarray(values, dtype="float64")
    if array.ndim != 1 or len(array) < 2 or not numpy.isfinite(array).all():
        return None
    rng = numpy.random.default_rng(seed)
    sample_indices = rng.integers(0, len(array), size=(10_000, len(array)))
    bootstrap_means = array[sample_indices].mean(axis=1)
    lower, upper = numpy.quantile(bootstrap_means, [0.025, 0.975])
    return {
        "mean_difference": float(array.mean()),
        "ci95_lower": float(lower),
        "ci95_upper": float(upper),
        "fold_count": int(len(array)),
        "resamples": 10_000,
    }


def _purge_validation_blocks(
    validation_events: Sequence[TripleBarrierEvent],
    *,
    embargo_bars: int,
) -> tuple[tuple[TripleBarrierEvent, ...], tuple[TripleBarrierEvent, ...]]:
    if len(validation_events) < 2:
        return (), ()
    midpoint = len(validation_events) // 2
    calibration_candidates = tuple(validation_events[:midpoint])
    threshold_candidates = tuple(validation_events[midpoint:])
    calibration = purge_overlapping_events(
        calibration_candidates,
        threshold_candidates,
        embargo_bars=embargo_bars,
    )
    return calibration, threshold_candidates


def run_cross_fitted_meta_label_research(
    dataset: HistoricalDataset,
    *,
    output_dir: str | Path,
    config: CrossFittedMetaConfig | None = None,
) -> dict[str, Any]:
    """Run two predeclared challengers on purged, chronological, out-of-fold test windows."""
    options = config or CrossFittedMetaConfig()
    if dataset.market.market_type.casefold() != "spot" or not dataset.market.symbol.upper().endswith("-USDT"):
        raise ValueError("Meta-label research is restricted to Spot/USDT.")
    if dataset.allow_gaps or dataset.gap_count:
        raise ValueError("Meta-label research refuses datasets with gaps.")
    candles = dataset.candles
    label_config = TripleBarrierConfig(
        short_window=options.short_window,
        long_window=options.long_window,
        vertical_horizon_bars=options.vertical_horizon_bars,
        profit_taking_multiple=options.profit_taking_multiple,
        stop_loss_multiple=options.stop_loss_multiple,
        round_trip_cost_bps=options.label_round_trip_cost_bps,
    )
    built = build_triple_barrier_events(candles, label_config)
    # A strategy that exits after a completed horizon needs a subsequent open for the fill.
    events = tuple(
        event for event in built.events
        if event.event_index + options.vertical_horizon_bars + 1 < len(candles)
    )
    fold_set = make_purged_event_folds(
        events,
        train_event_count=options.train_event_count,
        validation_event_count=options.validation_event_count,
        test_event_count=options.test_event_count,
        step_event_count=options.step_event_count,
        expected_fold_count=options.expected_fold_count,
        embargo_bars=options.embargo_bars,
    )
    if not fold_set.folds:
        raise ValueError("No chronological event folds can be built from this dataset.")

    output = Path(output_dir).expanduser().resolve()
    model_dir = output / "models"
    model_dir.mkdir(parents=True, exist_ok=True)
    joblib = importlib.import_module("joblib")
    scoring = importlib.import_module("sklearn.metrics")
    model_reports: dict[str, dict[str, Any]] = {}
    for model_name in _META_MODELS:
        model_reports[model_name] = {
            "expected_fold_count": options.expected_fold_count,
            "evaluated_fold_count": 0,
            "folds": [],
            "oof_predictions": [],
            "test_labels": [],
            "test_probabilities": [],
            "fold_net_returns": [],
            "fold_base_net_returns": [],
            "fold_buy_hold_net_returns": [],
            "fold_drawdowns": [],
            "closed_trades_total": 0,
            "closed_trade_returns": [],
            "model_artifacts": [],
        }

    backtest_config = _backtest_config(options)
    events_by_index = {event.event_index: event for event in events}
    for fold in fold_set.folds:
        if not fold.test_events:
            # Preserve the planned fold record; an empty test fold is a coverage failure.
            for model_name in _META_MODELS:
                model_reports[model_name]["folds"].append({
                    "fold_index": fold.fold_index,
                    "status": "NOT_EVALUABLE",
                    "reason": "empty_test_fold",
                })
            continue
        all_test_indices = {event.event_index for event in fold.test_events}
        first_test_event = fold.test_events[0]
        last_test_event = fold.test_events[-1]
        test_start_index = first_test_event.event_index + 1
        test_end_index = min(
            len(candles),
            last_test_event.event_index + options.vertical_horizon_bars + 2,
        )
        baseline_result = run_backtest(
            dataset,
            TripleBarrierEventPolicy(
                events_by_index=events_by_index,
                allowed_event_indices=all_test_indices,
                horizon_bars=options.vertical_horizon_bars,
                model_name="unfiltered-primary",
                fold_index=fold.fold_index,
            ),
            backtest_config,
            start_index=test_start_index,
            end_index=test_end_index,
            warmup_start_index=0,
            segment_name=f"phase32-base-test-fold-{fold.fold_index:02d}",
        )
        buy_hold_result = run_backtest(
            dataset,
            BuyAndHoldReference(),
            backtest_config,
            start_index=test_start_index,
            end_index=test_end_index,
            warmup_start_index=0,
            segment_name=f"phase32-buyhold-test-fold-{fold.fold_index:02d}",
        )

        train_events = fold.train_events
        calibration_events, threshold_events = _purge_validation_blocks(
            fold.validation_events,
            embargo_bars=options.embargo_bars,
        )
        fold_event_record = {
            "fold_index": fold.fold_index,
            "train_events_before_purge": fold.purged_train_count + len(train_events),
            "train_events_after_purge": len(train_events),
            "validation_events_after_test_purge": len(fold.validation_events),
            "calibration_events_after_cross_purge": len(calibration_events),
            "threshold_validation_events": len(threshold_events),
            "test_events": len(fold.test_events),
            "purged_train_count": fold.purged_train_count,
            "purged_validation_count": fold.purged_validation_count,
            "embargo_bars": fold.embargo_bars,
        }
        train_x, train_y = _event_matrix(train_events)
        test_x, test_y = _event_matrix(fold.test_events)
        calibration_x, calibration_y = _event_matrix(calibration_events)
        threshold_x, threshold_y = _event_matrix(threshold_events)

        for model_offset, model_name in enumerate(_META_MODELS):
            record: dict[str, Any] = {
                **fold_event_record,
                "status": "NOT_EVALUABLE",
                "reason": None,
                "threshold": 1.01,
                "threshold_abstained": True,
                "calibration_method": "platt-sigmoid-on-separate-validation-block",
                "classification": {},
                "test_net_returns": {},
                "model_sha256": None,
                "calibrator_sha256": None,
            }
            model_reports[model_name]["folds"].append(record)
            train_support = Counter(int(value) for value in train_y)
            if (
                len(train_events) < options.min_train_events
                or train_support.get(0, 0) < options.min_class_count
                or train_support.get(1, 0) < options.min_class_count
            ):
                record["reason"] = "insufficient_purged_train_event_or_class_support"
                continue
            if len(calibration_events) < options.min_calibration_events:
                record["reason"] = "insufficient_separate_calibration_block"
                continue
            calibration_support = Counter(int(value) for value in calibration_y)
            if (
                calibration_support.get(0, 0) < 2
                or calibration_support.get(1, 0) < 2
            ):
                record["reason"] = "calibration_block_missing_class_support"
                continue
            if len(threshold_events) < options.min_threshold_events:
                record["reason"] = "insufficient_threshold_validation_block"
                continue
            threshold_support = Counter(int(value) for value in threshold_y)
            if len(threshold_support) < 2:
                record["reason"] = "threshold_validation_block_missing_class_support"
                continue
            if len(fold.test_events) != options.test_event_count:
                record["reason"] = "test_fold_event_coverage_incomplete"
                continue

            seed = options.random_seed + fold.fold_index * 100 + model_offset
            model = _fit_meta_model(model_name, seed)
            model.fit(train_x, train_y)
            raw_calibration_probabilities = model.predict_proba(calibration_x)[:, 1]
            try:
                calibrator = _fit_probability_calibrator(
                    raw_calibration_probabilities,
                    calibration_y.tolist(),
                    seed,
                )
            except ValueError as exc:
                record["reason"] = str(exc)
                continue
            calibrated_validation_probabilities = _calibrate_probabilities(
                calibrator,
                model.predict_proba(threshold_x)[:, 1],
            )
            threshold_selection = select_validation_threshold(
                threshold_events,
                calibrated_validation_probabilities,
                embargo_bars=options.embargo_bars,
            )
            record["threshold"] = threshold_selection.threshold
            record["threshold_abstained"] = threshold_selection.abstain
            record["threshold_validation_selected_event_count"] = (
                threshold_selection.selected_event_count
            )
            record["threshold_validation_compounded_net_label_return"] = (
                threshold_selection.compounded_net_label_return
            )

            test_raw_probabilities = model.predict_proba(test_x)[:, 1]
            test_probabilities = _calibrate_probabilities(calibrator, test_raw_probabilities)
            probability_array = importlib.import_module("numpy").asarray(
                test_probabilities, dtype="float64"
            )
            label_array = importlib.import_module("numpy").asarray(test_y, dtype="int64")
            brier = float(importlib.import_module("numpy").mean((probability_array - label_array) ** 2))
            log_loss_value = float(scoring.log_loss(label_array, probability_array, labels=[0, 1]))
            ap: float | None = None
            positive_count = int(label_array.sum())
            if 0 < positive_count < len(label_array):
                ap = float(scoring.average_precision_score(label_array, probability_array))
            precision_threshold = threshold_selection.threshold
            selected_test_events = tuple(
                event for event, probability in zip(fold.test_events, test_probabilities, strict=True)
                if probability >= precision_threshold
            )
            record["classification"] = {
                "average_precision": ap,
                "positive_prevalence": positive_count / len(label_array),
                "brier_score": brier,
                "log_loss": log_loss_value,
                "selected_test_event_count_before_concurrency": len(selected_test_events),
                "selected_test_event_coverage": len(selected_test_events) / len(fold.test_events),
            }

            selected_indices = {event.event_index for event in selected_test_events}
            model_backtest = run_backtest(
                dataset,
                TripleBarrierEventPolicy(
                    events_by_index=events_by_index,
                    allowed_event_indices=all_test_indices,
                    selected_event_indices=selected_indices,
                    horizon_bars=options.vertical_horizon_bars,
                    model_name=model_name,
                    fold_index=fold.fold_index,
                ),
                backtest_config,
                start_index=test_start_index,
                end_index=test_end_index,
                warmup_start_index=0,
                segment_name=f"phase32-{model_name}-test-fold-{fold.fold_index:02d}",
            )
            meta_metrics = _metrics(model_backtest)
            base_metrics = _metrics(baseline_result)
            hold_metrics = _metrics(buy_hold_result)
            record["test_net_returns"] = {
                "meta_filtered": meta_metrics,
                "unfiltered_triple_barrier_sma": base_metrics,
                "buy_and_hold": hold_metrics,
                "difference_vs_unfiltered_sma": (
                    meta_metrics["net_return_fraction"] - base_metrics["net_return_fraction"]
                ),
                "difference_vs_buy_and_hold": (
                    meta_metrics["net_return_fraction"] - hold_metrics["net_return_fraction"]
                ),
            }
            record["status"] = "EVALUATED"
            model_reports[model_name]["evaluated_fold_count"] += 1
            model_reports[model_name]["oof_predictions"].extend(
                {
                    "fold_index": fold.fold_index,
                    "event_id": event.event_id,
                    "event_index": event.event_index,
                    "label": _target(event),
                    "calibrated_probability": probability,
                    "accepted": event.event_index in selected_indices,
                    "threshold": threshold_selection.threshold,
                }
                for event, probability in zip(fold.test_events, test_probabilities, strict=True)
            )
            model_reports[model_name]["test_labels"].extend(int(value) for value in test_y.tolist())
            model_reports[model_name]["test_probabilities"].extend(test_probabilities)
            model_reports[model_name]["fold_net_returns"].append(meta_metrics["net_return_fraction"])
            model_reports[model_name]["fold_base_net_returns"].append(
                base_metrics["net_return_fraction"]
            )
            model_reports[model_name]["fold_buy_hold_net_returns"].append(
                hold_metrics["net_return_fraction"]
            )
            model_reports[model_name]["fold_drawdowns"].append(meta_metrics["max_drawdown_fraction"])
            model_reports[model_name]["closed_trades_total"] += meta_metrics["closed_trade_count"]
            model_reports[model_name]["closed_trade_returns"].extend(
                float(trade.net_return_fraction) for trade in model_backtest.trades
            )

            model_artifact_path = model_dir / (
                f"fold-{fold.fold_index:02d}-{model_name}-model.joblib"
            )
            calibrator_path = model_dir / (
                f"fold-{fold.fold_index:02d}-{model_name}-calibrator.joblib"
            )
            joblib.dump(
                {
                    "model": model,
                    "model_name": model_name,
                    "model_schema_version": META_LABEL_SCHEMA_VERSION,
                    "feature_schema_version": FEATURE_SCHEMA_VERSION,
                    "feature_names": list(FEATURE_NAMES),
                    "class_order": [0, 1],
                    "target": "triple_barrier_net_return_fraction_after_50bps_assumed_cost_gt_zero",
                    "primary_signal": "causal_completed_bar_sma12_48_golden_cross",
                    "fold_index": fold.fold_index,
                    "train_event_ids": [event.event_id for event in train_events],
                    "threshold": threshold_selection.threshold,
                    "model_parameters_fixed_before_test": True,
                },
                model_artifact_path,
            )
            joblib.dump(calibrator, calibrator_path)
            model_sha = _sha256_file(model_artifact_path)
            calibrator_sha = _sha256_file(calibrator_path)
            record["model_sha256"] = model_sha
            record["calibrator_sha256"] = calibrator_sha
            model_reports[model_name]["model_artifacts"].append(
                {
                    "fold_index": fold.fold_index,
                    "model_path": model_artifact_path.relative_to(output).as_posix(),
                    "model_sha256": model_sha,
                    "calibrator_path": calibrator_path.relative_to(output).as_posix(),
                    "calibrator_sha256": calibrator_sha,
                }
            )

    aggregate: dict[str, Any] = {}
    for model_name in _META_MODELS:
        report = model_reports[model_name]
        folds = report["folds"]
        evaluated = report["evaluated_fold_count"]
        net_values = report["fold_net_returns"]
        base_values = report["fold_base_net_returns"]
        hold_values = report["fold_buy_hold_net_returns"]
        net_diff_base = [
            filtered - base for filtered, base in zip(net_values, base_values, strict=True)
        ]
        net_diff_hold = [
            filtered - hold for filtered, hold in zip(net_values, hold_values, strict=True)
        ]
        ci_base = _bootstrap_mean_interval(
            net_diff_base, seed=options.random_seed + (11 if model_name == "xgboost" else 21)
        )
        ci_hold = _bootstrap_mean_interval(
            net_diff_hold, seed=options.random_seed + (12 if model_name == "xgboost" else 22)
        )
        trade_returns = report["closed_trade_returns"]
        tail_quantile: float | None = None
        if trade_returns:
            tail_quantile = float(
                importlib.import_module("numpy").quantile(
                    importlib.import_module("numpy").asarray(trade_returns, dtype="float64"),
                    0.05,
                )
            )
        coverage_complete = (
            fold_set.actual_fold_count == options.expected_fold_count
            and len(folds) == options.expected_fold_count
            and evaluated == options.expected_fold_count
            and all(fold_record.get("status") == "EVALUATED" for fold_record in folds)
        )
        mean_net = sum(net_values) / len(net_values) if net_values else None
        max_drawdown = max(report["fold_drawdowns"], default=None)
        trades_gate = report["closed_trades_total"] >= options.min_closed_trades
        positive_net_gate = mean_net is not None and mean_net > 0
        improvement_base_gate = ci_base is not None and ci_base["ci95_lower"] > 0
        improvement_hold_gate = ci_hold is not None and ci_hold["ci95_lower"] > 0
        drawdown_gate = max_drawdown is not None and max_drawdown <= options.max_drawdown_fraction
        tail_gate = tail_quantile is not None and tail_quantile >= -options.max_tail_loss_fraction
        economic_gate = (
            coverage_complete
            and trades_gate
            and positive_net_gate
            and improvement_base_gate
            and improvement_hold_gate
            and drawdown_gate
            and tail_gate
        )
        aggregate[model_name] = {
            "expected_fold_count": options.expected_fold_count,
            "actual_fold_count": fold_set.actual_fold_count,
            "evaluated_fold_count": evaluated,
            "complete_fold_coverage": coverage_complete,
            "pooled_oof_event_count": len(report["oof_predictions"]),
            "pooled_oof_average_precision": (
                float(scoring.average_precision_score(
                    report["test_labels"], report["test_probabilities"]
                ))
                if report["test_labels"] and len(set(report["test_labels"])) == 2
                else None
            ),
            "pooled_oof_brier_score": (
                float(importlib.import_module("sklearn.metrics").brier_score_loss(
                    report["test_labels"], report["test_probabilities"]
                ))
                if report["test_labels"] else None
            ),
            "mean_fold_net_return_fraction": mean_net,
            "mean_fold_net_return_difference_vs_unfiltered_sma": ci_base,
            "mean_fold_net_return_difference_vs_buy_and_hold": ci_hold,
            "closed_trades_total": report["closed_trades_total"],
            "max_fold_drawdown_fraction": max_drawdown,
            "worst_5pct_closed_trade_net_return_fraction": tail_quantile,
            "gates": {
                "complete_oof_fold_coverage": coverage_complete,
                "minimum_closed_trade_count": trades_gate,
                "positive_mean_net_return": bool(positive_net_gate),
                "positive_lower_ci_vs_unfiltered_sma": bool(improvement_base_gate),
                "positive_lower_ci_vs_buy_and_hold": bool(improvement_hold_gate),
                "drawdown_within_limit": bool(drawdown_gate),
                "tail_loss_within_limit": bool(tail_gate),
            },
            "economic_gate_passed": bool(economic_gate),
            "disposition": "ACCEPT_FOR_INDEPENDENT_REVIEW" if economic_gate else "REJECT",
            "runtime_promotion_allowed": False,
            "champion": None,
        }

    candle_bytes = dataset.candles_csv_bytes()
    return {
        "schema_version": 1,
        "experiment": {
            "phase": 32,
            "name": "cross-fitted-triple-barrier-meta-labeling",
            "schema_version": META_LABEL_SCHEMA_VERSION,
            "research_only": True,
            "random_seed": options.random_seed,
            "configuration": asdict(options),
            "configuration_sha256": hashlib.sha256(_canonical_json_bytes(asdict(options))).hexdigest(),
        },
        "dataset": {
            "dataset_id": dataset.dataset_id,
            "dataset_version": dataset.version,
            "market": dataset.metadata()["market"],
            "interval_microseconds": dataset.interval_microseconds,
            "candle_count": len(candles),
            "candle_csv_sha256": hashlib.sha256(candle_bytes).hexdigest(),
            "gap_count": dataset.gap_count,
            "allow_gaps": dataset.allow_gaps,
            "volume_unit": dataset.volume_unit,
            "volume_used_as_feature": False,
            "source_name": dataset.source_name,
            "source_revision": dataset.source_revision,
            "license_id": dataset.license_id,
            "data_use_review": "UNREVIEWED",
        },
        "feature_contract": {
            "feature_names": list(FEATURE_NAMES),
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "information_set": "completed bars at or before event close only",
            "future_ohlc_used_for_features": False,
            "future_ohlc_used_for_labels_only": True,
        },
        "labeling": {
            "detected_primary_events": built.detected_bullish_crossovers,
            "labelled_events_before_execution_window_exclusion": len(built.events),
            "events_excluded_without_next_open_exit_opportunity": (
                len(built.events) - len(events)
            ),
            "event_count_used": len(events),
            "target": "triple-barrier event net_return_fraction > 0 after fixed 50 bps label cost",
            "outcomes": {
                outcome.value: sum(event.outcome is outcome for event in events)
                for outcome in BarrierOutcome
            },
            "ambiguous_horizontal_touch_count": sum(
                event.ambiguous_horizontal_touch for event in events
            ),
            "event_ledger_sha256": hashlib.sha256(
                _canonical_json_bytes([
                    {
                        "event_id": event.event_id,
                        "event_index": event.event_index,
                        "label_start_index": event.label_start_index,
                        "label_end_index": event.label_end_index,
                        "outcome": event.outcome.value,
                        "net_return_fraction": event.net_return_fraction,
                        "feature_values": event.feature_values,
                    }
                    for event in events
                ])
            ).hexdigest(),
        },
        "cross_fitting": {
            "primary_signal": "fixed deterministic SMA-12/48 golden cross; no fit-able primary-model parameters",
            "primary_proposals_are_causal": True,
            "meta_model_fit_rows": "earlier purged event rows only",
            "calibration_fit_rows": "separate, later validation slice with its own labels",
            "threshold_selection_rows": "later validation slice purged from calibration intervals and outer test",
            "test_rows_used_for_fitting_or_calibration": False,
            "test_predictions_are_out_of_fold": True,
            "purge_rule": "inclusive label intervals overlapping validation/test are removed from train",
            "embargo_bars": options.embargo_bars,
            "expected_fold_count": options.expected_fold_count,
            "actual_fold_count": fold_set.actual_fold_count,
            "fold_coverage_structural": fold_set.coverage_complete,
        },
        "models": {
            name: {
                "folds": report["folds"],
                "aggregate": aggregate[name],
                "oof_predictions": report["oof_predictions"],
                "model_artifacts": report["model_artifacts"],
            }
            for name, report in model_reports.items()
        },
        "acceptance": {
            "economic_gate_predeclared": {
                "min_closed_trades": options.min_closed_trades,
                "positive_mean_net_return": True,
                "positive_lower_95pct_ci_vs_unfiltered_sma": True,
                "positive_lower_95pct_ci_vs_buy_and_hold": True,
                "max_drawdown_fraction": options.max_drawdown_fraction,
                "minimum_5pct_trade_return_fraction": -options.max_tail_loss_fraction,
                "all_expected_folds_evaluable": True,
            },
            "model_candidates": {
                name: aggregate[name]["disposition"] for name in _META_MODELS
            },
            "champion": None,
            "selected_model_for_runtime": None,
            "runtime_promotion_allowed": False,
        },
        "limitations": [
            "Historical UDF volume units remain unknown; no volume feature or volume-based capacity was used.",
            "Historical order-book depth is unavailable; spread/slippage are fixed assumptions, not observed fills.",
            "Public provider data-use terms are unreviewed.",
            "Triple-Barrier targets are event labels; the backtest separately simulates entries/exits from completed-bar observations.",
            "Model parameter grids were fixed before the outer OOF test folds; only the separate validation block selected the probability threshold.",
            "No exchange client, credentials, private account endpoint, order-write path or Runtime model loader is used.",
        ],
    }
