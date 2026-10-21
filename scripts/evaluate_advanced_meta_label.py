"""Run a strict, research-only walk-forward SMA meta-label challenger experiment."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from evoryn.domain.models import MarketCandle
from evoryn.learning_lab.advanced_meta_label import (
    ADVANCED_FEATURE_SCHEMA_VERSION,
    MetaLabelEvent,
    build_advanced_feature_matrix,
    build_sma_meta_labels,
    make_meta_label_classifier,
    select_features_train_only,
    select_meta_label_threshold,
    tune_meta_labeler,
)
from evoryn.learning_lab.backtest import (
    BacktestConfig,
    make_walk_forward_folds,
    run_backtest,
)
from evoryn.learning_lab.historical_data import HistoricalDataset, HistoricalDatasetStore
from evoryn.strategies.baseline import SmaCrossoverBaseline


class _BuyAndHold:
    """Long-only reference which exposes only after at least one candle has closed."""

    strategy_id = "buy-and-hold-reference"
    strategy_version = "1.0.0"

    def parameters(self) -> dict[str, str]:
        return {"position_policy": "long_only", "signal_price": "completed_close"}

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        return Decimal("1") if history else Decimal("0")


class _SmaCrossEventBaseline:
    """Unfiltered SMA entry-event comparator with the same flat-at-test-start policy."""

    strategy_id = "sma-crossover-entry-events-long-flat"
    strategy_version = "1.0.0"

    def __init__(
        self, *, eligible_start_index: int, fast_window: int = 12, slow_window: int = 48
    ) -> None:
        self._eligible_start_index = eligible_start_index
        self._fast_window = fast_window
        self._slow_window = slow_window
        self._holding = False

    def parameters(self) -> dict[str, str]:
        return {
            "primary_signal": "SMA golden cross 12/48",
            "entry_start_index": str(self._eligible_start_index),
            "position_policy": "flat_until_new_cross_up_inside_test",
        }

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        if len(history) < self._slow_window + 1:
            self._holding = False
            return Decimal("0")
        closes = [float(candle.close) for candle in history]
        fast_now = sum(closes[-self._fast_window :]) / self._fast_window
        slow_now = sum(closes[-self._slow_window :]) / self._slow_window
        fast_before = sum(closes[-self._fast_window - 1 : -1]) / self._fast_window
        slow_before = sum(closes[-self._slow_window - 1 : -1]) / self._slow_window
        if fast_now <= slow_now:
            self._holding = False
        elif fast_before <= slow_before and len(history) - 1 >= self._eligible_start_index:
            self._holding = True
        return Decimal("1") if self._holding else Decimal("0")


class _SmaMetaLabelStrategy:
    """Fold-fitted meta-filter for completed-bar SMA golden-cross events."""

    strategy_id = "sma-xgb-lightgbm-meta-label-long-flat"
    strategy_version = "1.0.0"

    def __init__(
        self,
        *,
        model: Any,
        feature_rows: dict[int, tuple[float, ...]],
        selected_column_indices: tuple[int, ...],
        fast_window: int,
        slow_window: int,
        probability_threshold: float,
        eligible_start_index: int,
        model_name: str,
        fold_index: int,
    ) -> None:
        self._model = model
        self._feature_rows = feature_rows
        self._selected_column_indices = selected_column_indices
        self._fast_window = fast_window
        self._slow_window = slow_window
        self._probability_threshold = probability_threshold
        self._eligible_start_index = eligible_start_index
        self._model_name = model_name
        self._fold_index = fold_index
        self._holding = False
        self._last_probability: float | None = None

    def parameters(self) -> dict[str, str]:
        return {
            "primary_signal": "SMA golden cross 12/48",
            "meta_model": self._model_name,
            "fold_index": str(self._fold_index),
            "probability_threshold": str(self._probability_threshold),
            "eligible_start_index": str(self._eligible_start_index),
            "cost_label_bps": "50",
        }

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        if len(history) < self._slow_window + 1:
            self._holding = False
            return Decimal("0")

        closes = [float(candle.close) for candle in history]
        fast_now = sum(closes[-self._fast_window :]) / self._fast_window
        slow_now = sum(closes[-self._slow_window :]) / self._slow_window
        fast_before = (
            sum(closes[-self._fast_window - 1 : -1]) / self._fast_window
        )
        slow_before = (
            sum(closes[-self._slow_window - 1 : -1]) / self._slow_window
        )
        if fast_now <= slow_now:
            self._holding = False
            self._last_probability = None
            return Decimal("0")

        signal_index = len(history) - 1
        crossed_up = fast_before <= slow_before and fast_now > slow_now
        if crossed_up and signal_index >= self._eligible_start_index:
            row = self._feature_rows.get(signal_index)
            if row is None:
                self._holding = False
                self._last_probability = None
                return Decimal("0")
            selected = [[row[index] for index in self._selected_column_indices]]
            probability = float(self._model.predict_proba(selected)[0][1])
            self._last_probability = probability
            self._holding = probability >= self._probability_threshold

        return Decimal("1") if self._holding else Decimal("0")


def _events_for_range(
    events: Sequence[MetaLabelEvent], start: int, end: int
) -> tuple[MetaLabelEvent, ...]:
    return tuple(
        event
        for event in events
        if event.signal_index >= start and event.exit_index < end
    )


def _purge_overlapping_events(events: Sequence[MetaLabelEvent]) -> tuple[MetaLabelEvent, ...]:
    """Keep chronological events whose label outcomes do not overlap one another."""
    kept: list[MetaLabelEvent] = []
    last_exit = -1
    for event in sorted(events, key=lambda item: (item.signal_index, item.exit_index)):
        if event.signal_index <= last_exit:
            continue
        kept.append(event)
        last_exit = event.exit_index
    return tuple(kept)


def _feature_arrays(
    events: Sequence[MetaLabelEvent],
    feature_rows: dict[int, tuple[float, ...]],
    selected_columns: tuple[int, ...] | None = None,
) -> tuple[list[list[float]], list[int]]:
    x: list[list[float]] = []
    y: list[int] = []
    for event in events:
        row = feature_rows.get(event.signal_index)
        if row is None:
            continue
        if selected_columns is None:
            x.append(list(row))
        else:
            x.append([row[index] for index in selected_columns])
        y.append(event.label)
    return x, y


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _bootstrap_interval(values: Sequence[float], *, seed: int) -> dict[str, float | int]:
    import numpy

    array = numpy.asarray(values, dtype="float64")
    if array.ndim != 1 or len(array) == 0 or not numpy.isfinite(array).all():
        raise ValueError("Paired-fold bootstrap needs a non-empty finite vector.")
    rng = numpy.random.default_rng(seed)
    sample_indices = rng.integers(0, len(array), size=(10000, len(array)))
    bootstrap_means = array[sample_indices].mean(axis=1)
    lower, upper = numpy.quantile(bootstrap_means, [0.025, 0.975])
    return {
        "mean_difference": float(array.mean()),
        "ci95_lower": float(lower),
        "ci95_upper": float(upper),
        "fold_count": int(len(array)),
        "resamples": 10000,
    }


def _metrics(result: Any) -> dict[str, str | int]:
    stats = result.metrics
    return {
        "net_return_fraction": str(stats.net_return_fraction),
        "max_drawdown_fraction": str(stats.max_drawdown_fraction),
        "closed_trade_count": stats.closed_trade_count,
        "fill_count": stats.fill_count,
        "commission_paid": str(stats.commission_paid),
        "estimated_spread_cost": str(stats.estimated_spread_cost),
        "estimated_slippage_cost": str(stats.estimated_slippage_cost),
        "time_in_market_fraction": str(stats.time_in_market_fraction),
        "gross_turnover": str(stats.gross_turnover),
    }


def _load_dataset(output: Path) -> HistoricalDataset:
    report_path = output / "training-report.json"
    if not report_path.is_file():
        raise SystemExit("Training report is missing; run the public-history trainer first.")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    dataset_metadata = report.get("dataset_store")
    if not isinstance(dataset_metadata, dict):
        raise SystemExit("Training report has no verified dataset-store identity.")
    dataset_id = dataset_metadata.get("dataset_id")
    dataset_version = dataset_metadata.get("dataset_version")
    if not isinstance(dataset_id, str) or not isinstance(dataset_version, str):
        raise SystemExit("Training report dataset identity is invalid.")
    dataset = HistoricalDatasetStore(output / "datasets").load(dataset_id, dataset_version)
    if len(dataset.candles) < 2500:
        raise SystemExit("At least 2,500 verified hourly candles are required for this protocol.")
    return dataset


def run_experiment(
    dataset: HistoricalDataset,
    *,
    output_dir: Path,
    n_trials: int,
    random_seed: int,
    top_k: int,
) -> dict[str, Any]:
    """Fit only within fold history and keep runtime promotion unconditionally disabled."""
    joblib = cast(Any, importlib.import_module("joblib"))
    numpy = cast(Any, importlib.import_module("numpy"))
    metrics = cast(Any, importlib.import_module("sklearn.metrics"))

    candles = dataset.candles
    feature_names, indices, matrix = build_advanced_feature_matrix(candles)
    feature_rows = {index: row for index, row in zip(indices, matrix, strict=True)}
    all_events = build_sma_meta_labels(
        candles,
        short_window=12,
        long_window=48,
        horizon_bars=6,
        round_trip_cost_bps=50.0,
    )
    confirmation_bars = max(1, (len(candles) + 9) // 10)
    research_end = len(candles) - confirmation_bars
    train_bars = int(len(candles) * 0.35)
    validation_bars = int(len(candles) * 0.15)
    test_bars = max(1, int(len(candles) * 0.03))
    folds = make_walk_forward_folds(
        dataset,
        train_bars=train_bars,
        validation_bars=validation_bars,
        test_bars=test_bars,
        step_bars=test_bars,
        evaluation_end_index=research_end,
    )
    backtest_config = BacktestConfig(
        initial_cash=Decimal("10000"),
        commission_bps=Decimal("10"),
        spread_bps=Decimal("10"),
        slippage_bps=Decimal("10"),
        max_bar_participation=Decimal("1"),
        allocation_fraction=Decimal("0.25"),
        quantity_step=Decimal("0.00000001"),
        allow_gaps=False,
        enforce_volume_participation=False,
    )

    artifacts_dir = output_dir / "advanced-meta-label"
    model_dir = artifacts_dir / "models"
    model_dir.mkdir(parents=True, exist_ok=True)
    per_fold: list[dict[str, Any]] = []
    fold_results: dict[str, dict[str, list[float]]] = {
        name: {
            "net": [],
            "vs_buy_hold": [],
            "vs_sma": [],
            "vs_sma_event": [],
            "ap": [],
            "prevalence": [],
            "trades": [],
        }
        for name in ("xgboost", "lightgbm")
    }
    pooled_test_labels: dict[str, list[int]] = {
        name: [] for name in ("xgboost", "lightgbm")
    }
    pooled_test_probabilities: dict[str, list[float]] = {
        name: [] for name in ("xgboost", "lightgbm")
    }

    model_names: tuple[Literal["xgboost", "lightgbm"], ...] = ("xgboost", "lightgbm")
    for fold in folds:
        train_events = _purge_overlapping_events(
            _events_for_range(all_events, fold.train.start, fold.train.end)
        )
        validation_events = _purge_overlapping_events(
            _events_for_range(all_events, fold.validation.start, fold.validation.end)
        )
        test_events = _purge_overlapping_events(
            _events_for_range(all_events, fold.test.start, fold.test.end)
        )
        fold_record: dict[str, Any] = {
            "fold_index": fold.fold_index,
            "ranges": {
                "train": [fold.train.start, fold.train.end],
                "validation": [fold.validation.start, fold.validation.end],
                "test": [fold.test.start, fold.test.end],
            },
            "event_counts": {
                "train": len(train_events),
                "validation": len(validation_events),
                "test": len(test_events),
            },
            "models": {},
        }
        sma_baseline = run_backtest(
            dataset,
            SmaCrossoverBaseline(fast_window=12, slow_window=48),
            backtest_config,
            start_index=fold.test.start,
            end_index=fold.test.end,
            warmup_start_index=0,
            segment_name=f"advanced-meta-test-sma-state-fold-{fold.fold_index}",
        )
        sma_event_baseline = run_backtest(
            dataset,
            _SmaCrossEventBaseline(eligible_start_index=fold.test.start),
            backtest_config,
            start_index=fold.test.start,
            end_index=fold.test.end,
            warmup_start_index=0,
            segment_name=f"advanced-meta-test-sma-event-fold-{fold.fold_index}",
        )
        buy_hold = run_backtest(
            dataset,
            _BuyAndHold(),
            backtest_config,
            start_index=fold.test.start,
            end_index=fold.test.end,
            warmup_start_index=0,
            segment_name=f"advanced-meta-test-buyhold-fold-{fold.fold_index}",
        )
        baseline_sma_return = float(sma_baseline.metrics.net_return_fraction)
        baseline_sma_event_return = float(sma_event_baseline.metrics.net_return_fraction)
        baseline_hold_return = float(buy_hold.metrics.net_return_fraction)

        for model_offset, model_name in enumerate(model_names):
            model_record: dict[str, Any] = {
                "status": "NOT_EVALUATED",
                "runtime_promotion_allowed": False,
            }
            fold_record["models"][model_name] = model_record
            if (
                len(train_events) < 40
                or len(validation_events) < 15
                or len(test_events) < 1
                or set(event.label for event in train_events) != {0, 1}
                or set(event.label for event in validation_events) != {0, 1}
            ):
                model_record["reason"] = "insufficient_events_or_label_classes_after_purging"
                continue

            x_train_all, y_train = _feature_arrays(train_events, feature_rows)
            x_valid_all, y_valid = _feature_arrays(validation_events, feature_rows)
            if (
                len(x_train_all) != len(y_train)
                or len(x_valid_all) != len(y_valid)
                or set(y_train) != {0, 1}
                or set(y_valid) != {0, 1}
            ):
                model_record["reason"] = "feature_rows_missing_or_validation_labels_incomplete"
                continue

            selected_names = select_features_train_only(
                x_train_all, y_train, feature_names, top_k=min(top_k, len(feature_names)),
                random_seed=random_seed + fold.fold_index * 100 + model_offset,
            )
            name_to_index = {name: index for index, name in enumerate(feature_names)}
            selected_columns = tuple(name_to_index[name] for name in selected_names)
            x_train, _ = _feature_arrays(train_events, feature_rows, selected_columns)
            x_valid, _ = _feature_arrays(validation_events, feature_rows, selected_columns)
            if len(x_valid) != len(validation_events):
                model_record["reason"] = "validation_feature_rows_not_aligned_with_label_outcomes"
                continue
            tuning_seed = random_seed + fold.fold_index * 100 + model_offset
            tune_result = tune_meta_labeler(
                model_name, x_train, y_train, x_valid, y_valid,
                n_trials=n_trials,
                random_seed=tuning_seed,
            )
            # Fit only on the training partition so the validation-selected probability
            # threshold remains calibrated to the same model used on the locked test block.
            final_model = make_meta_label_classifier(
                model_name, tune_result.best_params, random_seed=tuning_seed
            )
            final_model.fit(
                numpy.asarray(x_train, dtype="float64"),
                numpy.asarray(y_train, dtype="int64"),
            )
            validation_probabilities = final_model.predict_proba(
                numpy.asarray(x_valid, dtype="float64")
            )[:, 1]
            threshold_selection = select_meta_label_threshold(
                validation_events, validation_probabilities
            )

            model_path = model_dir / f"fold-{fold.fold_index:02d}-{model_name}.joblib"
            joblib.dump(
                {
                    "model": final_model,
                    "model_name": model_name,
                    "feature_schema_version": ADVANCED_FEATURE_SCHEMA_VERSION,
                    "selected_feature_names": list(selected_names),
                    "class_order": [0, 1],
                    "target_definition": "future_sma_cross_net_return_after_50bps_cost_gt_zero",
                    "fold_index": fold.fold_index,
                    "train_range": [fold.train.start, fold.train.end],
                    "validation_range": [fold.validation.start, fold.validation.end],
                    "test_range": [fold.test.start, fold.test.end],
                    "validation_threshold": threshold_selection.threshold,
                    "threshold_selected_event_count": (
                        threshold_selection.selected_event_count
                    ),
                    "validation_threshold_net_sum": (
                        threshold_selection.summed_net_return_fraction
                    ),
                    "final_fit_policy": (
                        "train_only_validation_reserved_for_tuning_and_threshold"
                    ),
                    "tuning_validation_average_precision": (
                        tune_result.best_validation_average_precision
                    ),
                    "best_params": tune_result.best_params,
                },
                model_path,
            )
            model_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()

            test_x, test_y = _feature_arrays(test_events, feature_rows, selected_columns)
            test_probability = final_model.predict_proba(
                numpy.asarray(test_x, dtype="float64")
            )[:, 1] if test_x else numpy.asarray([], dtype="float64")
            if test_y:
                pooled_test_labels[model_name].extend(int(value) for value in test_y)
                pooled_test_probabilities[model_name].extend(
                    float(value) for value in test_probability
                )
            test_ap: float | None = None
            prevalence: float | None = None
            if len(test_y) >= 2 and len(set(test_y)) == 2:
                test_ap = float(metrics.average_precision_score(test_y, test_probability))
                prevalence = float(sum(test_y) / len(test_y))

            strategy = _SmaMetaLabelStrategy(
                model=final_model,
                feature_rows=feature_rows,
                selected_column_indices=selected_columns,
                fast_window=12,
                slow_window=48,
                probability_threshold=threshold_selection.threshold,
                eligible_start_index=fold.test.start,
                model_name=model_name,
                fold_index=fold.fold_index,
            )
            backtest = run_backtest(
                dataset,
                strategy,
                backtest_config,
                start_index=fold.test.start,
                end_index=fold.test.end,
                warmup_start_index=0,
                segment_name=f"advanced-meta-test-{model_name}-fold-{fold.fold_index}",
            )
            model_net = float(backtest.metrics.net_return_fraction)
            closed_trades = int(backtest.metrics.closed_trade_count)
            delta_hold = model_net - baseline_hold_return
            delta_sma = model_net - baseline_sma_return
            delta_sma_event = model_net - baseline_sma_event_return
            record = {
                "status": "EVALUATED",
                "tuning": {
                    "objective": "validation_average_precision",
                    "decision_threshold_policy": "fixed_grid_validation_net_return_with_abstain",
                    "threshold_selection": {
                        "threshold": threshold_selection.threshold,
                        "validation_event_count": (
                            threshold_selection.validation_event_count
                        ),
                        "selected_event_count": threshold_selection.selected_event_count,
                        "summed_net_return_fraction": (
                            threshold_selection.summed_net_return_fraction
                        ),
                        "mean_net_contribution_per_event": (
                            threshold_selection.mean_net_contribution_per_event
                        ),
                        "abstain": threshold_selection.abstain,
                    },
                    "best_validation_average_precision": (
                        tune_result.best_validation_average_precision
                    ),
                    "completed_trials": tune_result.completed_trials,
                    "random_seed": tune_result.random_seed,
                    "best_params": tune_result.best_params,
                },
                "feature_selection": {
                    "method": "mutual_info_classif",
                    "fit_rows": len(x_train_all),
                    "selected_count": len(selected_names),
                    "selected_features": list(selected_names),
                    "fit_scope": "train_partition_only",
                },
                "model_artifact": str(model_path.relative_to(output_dir)),
                "model_artifact_sha256": model_hash,
                "test_event_average_precision": test_ap,
                "test_event_positive_prevalence": prevalence,
                "test_backtest_metrics": _metrics(backtest),
                "same_fold_baselines": {
                    "buy_and_hold": _metrics(buy_hold),
                    "sma_12_48_state": _metrics(sma_baseline),
                    "sma_12_48_entry_events": _metrics(sma_event_baseline),
                },
                "net_return_difference_vs_buy_and_hold": delta_hold,
                "net_return_difference_vs_sma": delta_sma,
                "net_return_difference_vs_sma_entry_event_baseline": delta_sma_event,
                "runtime_promotion_allowed": False,
            }
            fold_record["models"][model_name] = record
            fold_results[model_name]["net"].append(model_net)
            fold_results[model_name]["vs_buy_hold"].append(delta_hold)
            fold_results[model_name]["vs_sma"].append(delta_sma)
            fold_results[model_name]["vs_sma_event"].append(delta_sma_event)
            fold_results[model_name]["trades"].append(float(closed_trades))
            if test_ap is not None and prevalence is not None:
                fold_results[model_name]["ap"].append(test_ap)
                fold_results[model_name]["prevalence"].append(prevalence)

        fold_record["baselines"] = {
            "buy_and_hold": _metrics(buy_hold),
            "sma_12_48_state": _metrics(sma_baseline),
            "sma_12_48_entry_events": _metrics(sma_event_baseline),
        }
        per_fold.append(fold_record)

    gates: dict[str, dict[str, Any]] = {}
    for model_index, gate_model_name in enumerate(("xgboost", "lightgbm")):
        values = fold_results[gate_model_name]
        folds_evaluated = len(values["net"])
        complete_coverage = folds_evaluated == len(folds) and len(folds) >= 10
        intervals: dict[str, Any] = {}
        interval_offsets = {"vs_buy_hold": 0, "vs_sma": 1, "vs_sma_event": 2}
        for metric, offset in interval_offsets.items():
            if values[metric]:
                intervals[metric] = _bootstrap_interval(
                    values[metric], seed=random_seed + 5000 + model_index * 10 + offset
                )
        pooled_labels = pooled_test_labels[gate_model_name]
        pooled_probabilities = pooled_test_probabilities[gate_model_name]
        pooled_ap: float | None = None
        pooled_prevalence: float | None = None
        if len(pooled_labels) >= 2 and set(pooled_labels) == {0, 1}:
            pooled_ap = float(
                metrics.average_precision_score(pooled_labels, pooled_probabilities)
            )
            pooled_prevalence = float(sum(pooled_labels) / len(pooled_labels))
        classification_gate = (
            complete_coverage
            and len(pooled_labels) >= 30
            and pooled_ap is not None
            and pooled_prevalence is not None
            and pooled_ap > pooled_prevalence
        )
        economic_gate = (
            complete_coverage
            and bool(values["net"])
            and _mean(values["net"]) > 0
            and "vs_buy_hold" in intervals
            and "vs_sma" in intervals
            and "vs_sma_event" in intervals
            and float(intervals["vs_buy_hold"]["ci95_lower"]) > 0
            and float(intervals["vs_sma"]["ci95_lower"]) > 0
            and float(intervals["vs_sma_event"]["ci95_lower"]) > 0
            and sum(values["trades"]) >= 20
        )
        gates[gate_model_name] = {
            "status": "EVALUATED" if complete_coverage else "INCOMPLETE_FOLD_COVERAGE",
            "evaluated_folds": folds_evaluated,
            "expected_folds": len(folds),
            "complete_fold_coverage": complete_coverage,
            "mean_net_return_fraction": _mean(values["net"]) if values["net"] else None,
            "mean_average_precision": _mean(values["ap"]) if values["ap"] else None,
            "mean_positive_prevalence": (
                _mean(values["prevalence"]) if values["prevalence"] else None
            ),
            "pooled_test_event_count": len(pooled_labels),
            "pooled_test_event_average_precision": pooled_ap,
            "pooled_test_event_positive_prevalence": pooled_prevalence,
            "closed_trades_total": int(sum(values["trades"])),
            "paired_fold_bootstrap_intervals": intervals,
            "classification_gate_passed": classification_gate,
            "economic_gate_passed": economic_gate,
            "empirical_candidate_gate_passed": classification_gate and economic_gate,
            "runtime_promotion_allowed": False,
        }

    report: dict[str, Any] = {
        "schema_version": 1,
        "experiment_id": hashlib.sha256(
            (
            f"{dataset.version}:{ADVANCED_FEATURE_SCHEMA_VERSION}:{n_trials}:"
            f"{random_seed}:validation-threshold-v2:walk-forward-35-15-3"
        ).encode()
        ).hexdigest(),
        "created_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "dataset_id": dataset.dataset_id,
            "dataset_version": dataset.version,
            "market": {
                "symbol": dataset.market.symbol,
                "venue": dataset.market.venue,
                "market_type": dataset.market.market_type,
            },
            "interval_seconds": int(dataset.interval.total_seconds()),
            "candle_count": len(candles),
            "first_candle_open": candles[0].timestamp.isoformat(),
            "last_candle_open": candles[-1].timestamp.isoformat(),
            "volume_unit": dataset.volume_unit,
            "volume_semantics": "UNKNOWN_EXCLUDED_FROM_FEATURES_AND_CAPACITY",
        },
        "protocol": {
            "feature_schema_version": ADVANCED_FEATURE_SCHEMA_VERSION,
            "feature_count": len(feature_names),
            "feature_names": list(feature_names),
            "primary_strategy": "SMA golden-cross 12/48 long/flat",
            "label_policy": "next_open_to_close_after_6_bars_minus_50bps",
            "label_event_count": len(all_events),
            "overlap_policy": "chronologically purge events whose label horizons overlap",
            "models": ["xgboost", "lightgbm"],
            "feature_selection": "mutual information fit on train partition only",
            "hyperparameter_tuning": "seeded Optuna TPE maximizing validation average precision",
            "trials_per_fold_model": n_trials,
            "threshold_policy": {
                "method": "fixed_grid_validation_net_return_contribution",
                "candidate_thresholds": [
                    0.00, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45,
                    0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95,
                ],
                "minimum_selected_events": "max(3, ceil(20% of validation events))",
                "abstain_threshold_when_no_positive_validation_net_sum": 1.01,
                "test_threshold_selection": False,
            },
            "train_bars": train_bars,
            "validation_bars": validation_bars,
            "test_bars": test_bars,
            "walk_forward_fold_count": len(folds),
            "terminal_confirmation_reserve_bars": confirmation_bars,
            "research_end_index_exclusive": research_end,
            "test_blocks_non_overlapping": True,
            "fold_scaler": "none (tree models; features are dimensionless/price-normalized)",
            "final_fit": (
                "train_only; validation used for Optuna and threshold selection, not fitting"
            ),
            "costs": {
                "initial_cash": "10000",
                "allocation_fraction": "0.25",
                "commission_bps": "10",
                "spread_bps": "10",
                "slippage_bps": "10",
                "round_trip_label_cost_bps": "50",
                "volume_capacity_constraints": "disabled_because_volume_units_unknown",
            },
            "order_book": "NOT_USED_NO_VERIFIED_HISTORICAL_DEPTH_SOURCE",
            "elliott_wave": (
                "NOT_USED_SUBJECTIVE_LABELS_AVOIDED; causal confirmed-swing proxies are used"
            ),
        },
        "folds": per_fold,
        "candidate_gates": gates,
        "selected_model_for_runtime": None,
        "runtime_promotion_allowed": False,
        "release_disposition": "RESEARCH_ONLY_NO_CHAMPION_NO_LIVE_TRADING",
        "limitations": [
            "OHLCV-derived multi-timeframe features are not independent native 4h/daily feeds.",
            "Confirmed swing structure is not a full Elliott-wave interpretation.",
            "Fibonacci levels are rolling-range references, not hand-validated swing anchors.",
            (
                "Nobitex volume units remain unknown; liquidity constraints are disabled "
                "and results are optimistic."
            ),
            "Spread and slippage are assumptions, not historical quotes.",
            "Historical provider usage and retention rights remain unreviewed.",
            "The terminal 10 percent is reserved and not evaluated by these folds.",
            "Empirical gates never automatically promote a model to Runtime.",
        ],
    }
    report_bytes = json.dumps(
        report, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    report_path = artifacts_dir / "advanced-meta-label-report.json"
    report_path.write_bytes(report_bytes + bytes([10]))
    report_digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    (artifacts_dir / "advanced-meta-label-report.json.sha256").write_text(
        report_digest + chr(10), encoding="ascii"
    )
    print(f"ADVANCED_EXPERIMENT_ID={report['experiment_id']}")
    print(f"ADVANCED_DATASET_VERSION={dataset.version}")
    print(f"ADVANCED_FOLDS={len(folds)}")
    print(f"ADVANCED_REPORT_SHA256={report_digest}")
    print("SELECTED_MODEL_FOR_RUNTIME=null")
    for name, gate in gates.items():
        print(
            f"GATE {name}: status={gate['status']} "
            f"folds={gate['evaluated_folds']}/{gate['expected_folds']} "
            f"mean_net_return={gate['mean_net_return_fraction']} "
            f"classification={gate['classification_gate_passed']} "
            f"economic={gate['economic_gate_passed']} "
            f"promotion_allowed=false"
        )
    return report


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/nobitex-regime"))
    parser.add_argument("--n-trials", type=int, default=10)
    parser.add_argument("--seed", type=int, default=41)
    parser.add_argument("--top-k", type=int, default=15)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if type(args.n_trials) is not int or not 5 <= args.n_trials <= 100:
        raise SystemExit("--n-trials must be in [5, 100].")
    if type(args.seed) is not int or args.seed < 0:
        raise SystemExit("--seed must be a non-negative integer.")
    if type(args.top_k) is not int or args.top_k < 1:
        raise SystemExit("--top-k must be a positive integer.")
    output = args.output.expanduser().resolve()
    dataset = _load_dataset(output)
    run_experiment(
        dataset,
        output_dir=output,
        n_trials=args.n_trials,
        random_seed=args.seed,
        top_k=args.top_k,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
