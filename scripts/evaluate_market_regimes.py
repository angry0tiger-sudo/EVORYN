"""Evaluate causal three-state market-regime models and regime-gated SMA strategies.

This runner uses only public historical candles and immutable dataset snapshots. No credentials,
exchange account routes, runtime loader, or order submission are available to this module.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import itertools
import json
import math
import random
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from evoryn.domain.models import MarketCandle
from evoryn.intelligence.regime import RegimeLabel
from evoryn.learning_lab.backtest import BacktestConfig, make_walk_forward_folds, run_backtest
from evoryn.learning_lab.historical_data import HistoricalDataset, HistoricalDatasetStore
from evoryn.learning_lab.market_regimes import (
    FEATURE_NAMES,
    REGIME_NAMES,
    REGIME_SCHEMA_VERSION,
    RegimeGatedSmaStrategy,
    build_causal_feature_matrix,
    fit_trend_threshold,
    forward_filter_probabilities,
    label_regimes,
)
from evoryn.strategies.baseline import SmaCrossoverBaseline

_SEED = 41


class _BuyAndHold:
    """Long-only baseline starting on the first evaluated candle."""

    strategy_id = "buy-and-hold-reference"
    strategy_version = "1.0.0"

    def parameters(self) -> dict[str, str]:
        return {"position_policy": "long_only", "signal_price": "completed_close"}

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        return Decimal("1") if history else Decimal("0")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=Path("artifacts/nobitex-regime"),
        help="Directory containing training-report.json and the datasets content store.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/market-regime-detection"),
    )
    return parser.parse_args(argv)


def _load_dataset(dataset_root: Path) -> tuple[HistoricalDataset, dict[str, Any]]:
    report_path = dataset_root / "training-report.json"
    try:
        training_report = json.loads(report_path.read_text(encoding="utf-8"))
        reference = training_report["dataset_store"]
        dataset_id = reference["dataset_id"]
        dataset_version = reference["dataset_version"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("Verified dataset reference is missing from training-report.json.") from exc
    if not isinstance(dataset_id, str) or not isinstance(dataset_version, str):
        raise ValueError("Dataset reference fields must be strings.")
    dataset = HistoricalDatasetStore(dataset_root / "datasets").load(dataset_id, dataset_version)
    return dataset, training_report


def _array(values: Sequence[Sequence[float]]) -> Any:
    numpy = importlib.import_module("numpy")
    return numpy.asarray(values, dtype="float64")


def _metrics(actual: Sequence[str], predicted: Sequence[str]) -> dict[str, Any]:
    if not actual or len(actual) != len(predicted):
        raise ValueError("Classification metrics require equal non-empty sequences.")
    supports = Counter(actual)
    confusion = {
        actual_name: {
            predicted_name: sum(
                1
                for truth, guess in zip(actual, predicted, strict=True)
                if truth == actual_name and guess == predicted_name
            )
            for predicted_name in REGIME_NAMES
        }
        for actual_name in REGIME_NAMES
    }
    recalls: list[float] = []
    f1_scores: list[float] = []
    per_class: dict[str, Any] = {}
    for name in REGIME_NAMES:
        true_positive = confusion[name][name]
        false_positive = sum(confusion[other][name] for other in REGIME_NAMES if other != name)
        false_negative = sum(confusion[name][other] for other in REGIME_NAMES if other != name)
        support = supports.get(name, 0)
        recall = true_positive / support if support else None
        precision_denominator = true_positive + false_positive
        precision = true_positive / precision_denominator if precision_denominator else 0.0
        f1_denominator = 2 * true_positive + false_positive + false_negative
        f1 = 2 * true_positive / f1_denominator if f1_denominator else 0.0
        if recall is not None:
            recalls.append(recall)
        f1_scores.append(f1)
        per_class[name] = {
            "support": support,
            "recall": recall,
            "precision": precision,
            "f1": f1,
        }
    accuracy = sum(
        1 for truth, guess in zip(actual, predicted, strict=True) if truth == guess
    ) / len(actual)
    return {
        "sample_count": len(actual),
        "accuracy": accuracy,
        "balanced_accuracy": sum(recalls) / len(recalls) if recalls else 0.0,
        "macro_f1": sum(f1_scores) / len(f1_scores),
        "true_label_counts": dict(sorted(supports.items())),
        "confusion_matrix": confusion,
        "per_class": per_class,
    }


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _bootstrap_lower_95(values: Sequence[float], *, seed: int) -> float | None:
    """Deterministic paired-fold bootstrap lower percentile; descriptive, not IID proof."""
    if len(values) < 2:
        return None
    generator = random.Random(seed)
    draws = [
        sum(generator.choice(values) for _ in values) / len(values)
        for _ in range(4000)
    ]
    draws.sort()
    return draws[int(0.05 * (len(draws) - 1))]


def _backtest_summary(result: Any) -> dict[str, Any]:
    metrics = result.metrics
    return {
        "run_id": result.run_id,
        "net_return_fraction": float(metrics.net_return_fraction),
        "max_drawdown_fraction": float(metrics.max_drawdown_fraction),
        "closed_trade_count": metrics.closed_trade_count,
        "fill_count": metrics.fill_count,
        "time_in_market_fraction": float(metrics.time_in_market_fraction),
        "commission_paid": str(metrics.commission_paid),
        "estimated_spread_cost": str(metrics.estimated_spread_cost),
        "estimated_slippage_cost": str(metrics.estimated_slippage_cost),
    }


def _state_mapping(states: Sequence[int], labels: Sequence[str]) -> dict[int, str]:
    if len(states) != len(labels) or not states:
        raise ValueError("HMM state mapping requires paired training observations.")
    best_assignment: tuple[str, ...] | None = None
    best_score = -1
    for assignment in itertools.permutations(REGIME_NAMES):
        score = sum(
            1
            for state, label in zip(states, labels, strict=True)
            if 0 <= state < len(assignment) and assignment[state] == label
        )
        if score > best_score:
            best_score = score
            best_assignment = assignment
    if best_assignment is None:
        raise ValueError("Could not map the HMM hidden states to descriptive regimes.")
    return {state: best_assignment[state] for state in range(len(best_assignment))}


def _train_hmm(
    train_features: Sequence[Sequence[float]],
    train_labels: Sequence[str],
    local_features: Sequence[Sequence[float]],
    *,
    seed: int,
) -> tuple[dict[int, str], list[str], Any, Any]:
    numpy = importlib.import_module("numpy")
    preprocessing = importlib.import_module("sklearn.preprocessing")
    hmmlearn = importlib.import_module("hmmlearn.hmm")
    scaler = preprocessing.StandardScaler()
    x_train = scaler.fit_transform(_array(train_features))
    x_local = scaler.transform(_array(local_features))
    model = hmmlearn.GaussianHMM(
        n_components=3,
        covariance_type="diag",
        n_iter=120,
        tol=1e-3,
        min_covar=1e-3,
        random_state=seed,
        implementation="log",
    )
    model.fit(x_train)
    training_probabilities = forward_filter_probabilities(model, x_train.tolist())
    training_states = [
        max(range(len(probabilities)), key=probabilities.__getitem__)
        for probabilities in training_probabilities
    ]
    mapping = _state_mapping(training_states, train_labels)
    local_probabilities = forward_filter_probabilities(model, x_local.tolist())
    local_states = [
        max(range(len(probabilities)), key=probabilities.__getitem__)
        for probabilities in local_probabilities
    ]
    predictions = [mapping[state] for state in local_states]
    return mapping, predictions, model, scaler


def _train_xgboost(
    train_features: Sequence[Sequence[float]],
    train_labels: Sequence[str],
    local_features: Sequence[Sequence[float]],
    *,
    seed: int,
) -> tuple[list[str], Any]:
    xgboost = importlib.import_module("xgboost")
    counts = Counter(train_labels)
    if any(counts.get(name, 0) < 10 for name in REGIME_NAMES):
        raise ValueError("Training partition lacks at least 10 examples of each regime.")
    label_to_int = {name: index for index, name in enumerate(REGIME_NAMES)}
    y_train = [label_to_int[label] for label in train_labels]
    model = xgboost.XGBClassifier(
        objective="multi:softprob",
        num_class=3,
        eval_metric="mlogloss",
        n_estimators=160,
        max_depth=3,
        learning_rate=0.04,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_lambda=1.0,
        min_child_weight=5,
        random_state=seed,
        n_jobs=1,
        verbosity=0,
    )
    model.fit(_array(train_features), _array([[float(label)] for label in y_train]).reshape(-1))
    local_probabilities = model.predict_proba(_array(local_features))
    predictions: list[str] = []
    for probabilities in local_probabilities.tolist():
        selected = max(range(len(probabilities)), key=probabilities.__getitem__)
        predictions.append(REGIME_NAMES[selected])
    return predictions, model


def _aggregate_classification(
    fold_results: Sequence[dict[str, Any]], key: str
) -> dict[str, Any]:
    metric_names = ("accuracy", "balanced_accuracy", "macro_f1")
    result: dict[str, Any] = {
        "evaluated_folds": len(fold_results),
        "mean_metrics": {},
    }
    for name in metric_names:
        values = [
            float(fold[key][name])
            for fold in fold_results
            if isinstance(fold.get(key), dict) and fold[key].get(name) is not None
        ]
        result["mean_metrics"][name] = _mean(values)
    result["total_test_samples"] = sum(
        int(fold[key]["sample_count"])
        for fold in fold_results
        if isinstance(fold.get(key), dict)
    )
    return result


def _aggregate_strategy(
    fold_results: Sequence[dict[str, Any]],
    baseline_folds: dict[int, dict[str, Any]],
    *,
    seed: int,
) -> dict[str, Any]:
    returns = [float(fold["strategy"]["net_return_fraction"]) for fold in fold_results]
    draws = [float(fold["strategy"]["max_drawdown_fraction"]) for fold in fold_results]
    trades = sum(int(fold["strategy"]["closed_trade_count"]) for fold in fold_results)
    differences_sma = [
        float(fold["strategy"]["net_return_fraction"])
        - float(baseline_folds[int(fold["fold_index"])]["sma"]["net_return_fraction"])
        for fold in fold_results
        if int(fold["fold_index"]) in baseline_folds
    ]
    differences_buy_hold = [
        float(fold["strategy"]["net_return_fraction"])
        - float(baseline_folds[int(fold["fold_index"])]["buy_and_hold"]["net_return_fraction"])
        for fold in fold_results
        if int(fold["fold_index"]) in baseline_folds
    ]
    return {
        "evaluated_folds": len(fold_results),
        "mean_net_return_fraction": _mean(returns),
        "mean_max_drawdown_fraction": _mean(draws),
        "total_closed_trades": trades,
        "lower_95_paired_fold_return_difference_vs_sma": _bootstrap_lower_95(
            differences_sma, seed=seed
        ),
        "lower_95_paired_fold_return_difference_vs_buy_and_hold": _bootstrap_lower_95(
            differences_buy_hold, seed=seed + 1
        ),
        "net_return_differences_vs_sma_by_fold": differences_sma,
        "net_return_differences_vs_buy_and_hold_by_fold": differences_buy_hold,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    dataset_root = args.dataset_root.expanduser().resolve()
    output_root = args.output.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "models").mkdir(parents=True, exist_ok=True)
    dataset, training_report = _load_dataset(dataset_root)
    if dataset.market.market_type.casefold() != "spot":
        raise ValueError("Market-regime research supports Spot candles only.")
    if dataset.volume_unit == "base_asset":
        volume_note = "Volume is still excluded; this experiment intentionally stays price-only."
    else:
        volume_note = (
            "Volume units are unknown or unverified and are excluded; capacity limits are disabled."
        )
    candle_count = len(dataset.candles)
    if candle_count < 2500:
        raise ValueError("At least 2500 contiguous hourly candles are required for this walk-forward study.")

    indices, all_features = build_causal_feature_matrix(dataset.candles, lookback=72)
    row_by_index = dict(zip(indices, all_features, strict=True))
    feature_by_index = dict(zip(indices, all_features, strict=True))
    train_bars = max(500, int(candle_count * 0.50))
    validation_bars = max(168, int(candle_count * 0.10))
    test_bars = max(168, int(candle_count * 0.08))
    folds = make_walk_forward_folds(
        dataset,
        train_bars=train_bars,
        validation_bars=validation_bars,
        test_bars=test_bars,
        step_bars=test_bars,
    )
    backtest_config = BacktestConfig(
        initial_cash=Decimal("10000"),
        commission_bps=Decimal("10"),
        spread_bps=Decimal("10"),
        slippage_bps=Decimal("10"),
        max_bar_participation=Decimal("0.05"),
        allocation_fraction=Decimal("0.25"),
        quantity_step=Decimal("0.00001"),
        enforce_volume_participation=False,
    )
    baseline_folds: dict[int, dict[str, Any]] = {}
    for fold in folds:
        sma_result = run_backtest(
            dataset,
            SmaCrossoverBaseline(fast_window=12, slow_window=48),
            backtest_config,
            start_index=fold.test.start,
            end_index=fold.test.end,
            warmup_start_index=0,
            segment_name=f"regime-study-fold-{fold.fold_index}-sma-baseline",
        )
        hold_result = run_backtest(
            dataset,
            _BuyAndHold(),
            backtest_config,
            start_index=fold.test.start,
            end_index=fold.test.end,
            warmup_start_index=0,
            segment_name=f"regime-study-fold-{fold.fold_index}-buy-and-hold",
        )
        baseline_folds[fold.fold_index] = {
            "test_start_index": fold.test.start,
            "test_end_index": fold.test.end,
            "sma": _backtest_summary(sma_result),
            "buy_and_hold": _backtest_summary(hold_result),
        }

    model_reports: dict[str, dict[str, Any]] = {}
    candidate_gates: dict[str, dict[str, bool]] = {}
    joblib = importlib.import_module("joblib")
    for family in ("hmm", "xgboost"):
        family_folds: list[dict[str, Any]] = []
        for fold in folds:
            train_indices = [
                index for index in indices
                if fold.train.start <= index < fold.train.end
            ]
            test_indices = [
                index for index in indices
                if fold.test.start <= index < fold.test.end
            ]
            local_indices = [index for index in indices if index >= fold.train.start]
            train_rows = [feature_by_index[index] for index in train_indices]
            test_rows = [feature_by_index[index] for index in test_indices]
            local_rows = [feature_by_index[index] for index in local_indices]
            try:
                threshold = fit_trend_threshold(train_rows)
                train_labels = label_regimes(train_rows, threshold)
                test_labels = label_regimes(test_rows, threshold)
                local_labels = label_regimes(local_rows, threshold)
                majority_label = Counter(train_labels).most_common(1)[0][0]
                numpy = importlib.import_module("numpy")
                train_features = _array(train_rows)
                local_features = _array(local_rows)
                if family == "hmm":
                    mapping, local_predictions, model, scaler = _train_hmm(
                        train_rows, train_labels, local_rows, seed=_SEED + fold.fold_index
                    )
                    local_prediction_by_index = dict(
                        zip(local_indices, local_predictions, strict=True)
                    )
                    model_payload = {
                        "schema_version": REGIME_SCHEMA_VERSION,
                        "family": family,
                        "model": model,
                        "scaler": scaler,
                        "hidden_state_to_regime": mapping,
                        "trend_threshold": threshold,
                        "feature_names": FEATURE_NAMES,
                        "target_policy": "trailing_24_bar_return_train_quantile",
                        "future_label_inputs": False,
                    }
                    state_mapping = {str(key): value for key, value in mapping.items()}
                else:
                    local_predictions, model = _train_xgboost(
                        train_rows, train_labels, local_rows, seed=_SEED + fold.fold_index
                    )
                    local_prediction_by_index = dict(
                        zip(local_indices, local_predictions, strict=True)
                    )
                    model_payload = {
                        "schema_version": REGIME_SCHEMA_VERSION,
                        "family": family,
                        "model": model,
                        "trend_threshold": threshold,
                        "feature_names": FEATURE_NAMES,
                        "target_policy": "trailing_24_bar_return_train_quantile",
                        "future_label_inputs": False,
                    }
                    state_mapping = {}
                test_predictions = [local_prediction_by_index[index] for index in test_indices]
                classification = _metrics(test_labels, test_predictions)
                majority_metrics = _metrics(test_labels, [majority_label] * len(test_labels))
                if len(test_indices) == 0 or len(train_indices) < 300:
                    raise ValueError("Fold does not contain enough train/test feature rows.")

                model_path = output_root / "models" / f"{family}-fold-{fold.fold_index:02d}.joblib"
                joblib.dump(model_payload, model_path, compress=3)
                model_hash = _sha256_file(model_path)
                predictions_by_timestamp = {
                    dataset.candles[index].timestamp: local_prediction_by_index[index]
                    for index in local_indices
                }
                strategy = RegimeGatedSmaStrategy(
                    predictions_by_timestamp,
                    fast_window=12,
                    slow_window=48,
                    model_id=family,
                )
                gated_result = run_backtest(
                    dataset,
                    strategy,
                    backtest_config,
                    start_index=fold.test.start,
                    end_index=fold.test.end,
                    warmup_start_index=0,
                    segment_name=f"regime-study-fold-{fold.fold_index}-{family}-gated-sma",
                )
                distribution = Counter(test_labels)
                family_folds.append(
                    {
                        "fold_index": fold.fold_index,
                        "train_range": {"start": fold.train.start, "end": fold.train.end},
                        "validation_range": {
                            "start": fold.validation.start,
                            "end": fold.validation.end,
                        },
                        "test_range": {"start": fold.test.start, "end": fold.test.end},
                        "training_threshold": threshold,
                        "training_regime_counts": dict(sorted(Counter(train_labels).items())),
                        "test_regime_counts": dict(sorted(distribution.items())),
                        "classification": classification,
                        "majority_baseline": majority_metrics,
                        "hidden_state_mapping": state_mapping,
                        "strategy": _backtest_summary(gated_result),
                        "model_artifact": {
                            "path": str(model_path.relative_to(output_root)),
                            "sha256": model_hash,
                        },
                        "status": "EVALUATED",
                    }
                )
            except Exception as exc:
                family_folds.append(
                    {
                        "fold_index": fold.fold_index,
                        "train_range": {"start": fold.train.start, "end": fold.train.end},
                        "test_range": {"start": fold.test.start, "end": fold.test.end},
                        "status": "FAILED",
                        "error_type": type(exc).__name__,
                        "error": " ".join(str(exc).split())[:240] or type(exc).__name__,
                    }
                )

        successful = [fold for fold in family_folds if fold.get("status") == "EVALUATED"]
        classification_summary = _aggregate_classification(successful, "classification")
        majority_summary = _aggregate_classification(successful, "majority_baseline")
        strategy_summary = _aggregate_strategy(
            successful, baseline_folds, seed=_SEED + (0 if family == "hmm" else 100)
        )
        complete_coverage = len(successful) == len(folds)
        class_gate = bool(
            complete_coverage
            and classification_summary["mean_metrics"]["balanced_accuracy"] is not None
            and majority_summary["mean_metrics"]["balanced_accuracy"] is not None
            and classification_summary["mean_metrics"]["macro_f1"] is not None
            and majority_summary["mean_metrics"]["macro_f1"] is not None
            and classification_summary["mean_metrics"]["balanced_accuracy"]
            > majority_summary["mean_metrics"]["balanced_accuracy"]
            and classification_summary["mean_metrics"]["macro_f1"]
            > majority_summary["mean_metrics"]["macro_f1"]
        )
        lower_sma = strategy_summary["lower_95_paired_fold_return_difference_vs_sma"]
        lower_hold = strategy_summary[
            "lower_95_paired_fold_return_difference_vs_buy_and_hold"
        ]
        econ_gate = bool(
            complete_coverage
            and strategy_summary["mean_net_return_fraction"] is not None
            and strategy_summary["mean_net_return_fraction"] > 0
            and strategy_summary["total_closed_trades"] >= 20
            and lower_sma is not None
            and lower_sma > 0
            and lower_hold is not None
            and lower_hold > 0
        )
        candidate_gates[family] = {
            "complete_fold_coverage": complete_coverage,
            "classification_gate_passed": class_gate,
            "economic_gate_passed": econ_gate,
            "empirical_candidate_gate_passed": class_gate and econ_gate,
        }
        model_reports[family] = {
            "evaluated_folds": len(successful),
            "expected_folds": len(folds),
            "failed_folds": [
                fold for fold in family_folds if fold.get("status") != "EVALUATED"
            ],
            "classification_summary": classification_summary,
            "training_majority_baseline_summary": majority_summary,
            "regime_gated_sma_strategy_summary": strategy_summary,
            "folds": family_folds,
            **candidate_gates[family],
        }

    baseline_returns_sma = [
        float(result["sma"]["net_return_fraction"]) for result in baseline_folds.values()
    ]
    baseline_returns_hold = [
        float(result["buy_and_hold"]["net_return_fraction"])
        for result in baseline_folds.values()
    ]
    report = {
        "schema_version": 1,
        "research_schema": REGIME_SCHEMA_VERSION,
        "experiment_id": hashlib.sha256(
            f"{dataset.version}:{REGIME_SCHEMA_VERSION}:{_SEED}".encode("utf-8")
        ).hexdigest(),
        "created_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "dataset_id": dataset.dataset_id,
            "dataset_version": dataset.version,
            "candles_sha256": hashlib.sha256(dataset.candles_csv_bytes()).hexdigest(),
            "candle_count": candle_count,
            "first_candle_open": dataset.candles[0].timestamp.astimezone(UTC).isoformat(),
            "last_candle_open": dataset.candles[-1].timestamp.astimezone(UTC).isoformat(),
            "source_name": dataset.source_name,
            "source_revision": dataset.source_revision,
            "license_id": dataset.license_id,
            "license_review_status": "UNREVIEWED_PROVIDER_TERMS",
            "volume_unit": dataset.volume_unit,
            "order_book_data_available": False,
            "volume_and_order_book_policy": volume_note,
        },
        "problem_definition": {
            "target": "current descriptive state: BULL_TREND, BEAR_TREND, or RANGE",
            "target_policy": "sign of trailing 24-bar return outside a train-only magnitude threshold",
            "threshold_quantile": 0.55,
            "threshold_fit_scope": "training partition only, separately per fold",
            "future_price_label_inputs": False,
            "model_goal": "classify current coarse regime, not predict future price or return",
            "weak_label_caveat": (
                "Targets are mechanical trailing-return labels, not human-verified market regimes; "
                "classification primarily measures reproduction of this transparent definition."
            ),
            "features": list(FEATURE_NAMES),
            "feature_schema_version": REGIME_SCHEMA_VERSION,
            "feature_prefix_causality_tested": True,
        },
        "walk_forward": {
            "train_bars": train_bars,
            "validation_bars": validation_bars,
            "test_bars": test_bars,
            "test_windows_non_overlapping": True,
            "folds": [
                {
                    "fold_index": fold.fold_index,
                    "train": {"start": fold.train.start, "end": fold.train.end},
                    "validation": {
                        "start": fold.validation.start,
                        "end": fold.validation.end,
                    },
                    "test": {"start": fold.test.start, "end": fold.test.end},
                }
                for fold in folds
            ],
            "fitted_only_on_train": True,
            "scaler_fit_only_on_train": True,
            "hmm_state_mapping_fit_only_on_train": True,
            "hmm_test_inference": "forward_filter_only_no_future_smoothing",
            "validation_used_for_model_fitting": False,
        },
        "backtest_config": backtest_config.as_dict(),
        "baseline_strategy_results": {
            "sma_12_48": {
                "folds": baseline_folds,
                "mean_net_return_fraction": _mean(baseline_returns_sma),
                "total_closed_trades": sum(
                    int(result["sma"]["closed_trade_count"])
                    for result in baseline_folds.values()
                ),
            },
            "buy_and_hold": {
                "folds": baseline_folds,
                "mean_net_return_fraction": _mean(baseline_returns_hold),
                "total_closed_trades": sum(
                    int(result["buy_and_hold"]["closed_trade_count"])
                    for result in baseline_folds.values()
                ),
            },
        },
        "models": model_reports,
        "candidate_gates": candidate_gates,
        "selected_model_for_runtime": None,
        "runtime_promotion_allowed": False,
        "release_disposition": "RESEARCH_ONLY_NO_CHAMPION_NO_LIVE_TRADING",
        "source_training_report_disposition": training_report.get(
            "release_disposition", "UNKNOWN"
        ),
    }
    report_path = output_root / "market-regime-report.json"
    report_bytes = json.dumps(
        report, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    report_path.write_bytes(report_bytes + bytes([10]))
    report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
    (output_root / "market-regime-report.json.sha256").write_text(
        report_hash + chr(10), encoding="ascii"
    )
    manifest = {
        "schema_version": 1,
        "experiment_id": report["experiment_id"],
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.version,
        "dataset_sha256": hashlib.sha256(dataset.candles_csv_bytes()).hexdigest(),
        "report_path": report_path.name,
        "report_sha256": report_hash,
        "candidate_gates": candidate_gates,
        "selected_model_for_runtime": None,
        "runtime_promotion_allowed": False,
    }
    manifest_bytes = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    (output_root / "market-regime-manifest.json").write_bytes(manifest_bytes + bytes([10]))
    (output_root / "market-regime-manifest.json.sha256").write_text(
        hashlib.sha256((output_root / "market-regime-manifest.json").read_bytes()).hexdigest()
        + chr(10),
        encoding="ascii",
    )
    print(f"EXPERIMENT_ID={report['experiment_id']}")
    print(f"DATASET_VERSION={dataset.version}")
    print(f"FOLDS_EXPECTED={len(folds)}")
    for family, gate in candidate_gates.items():
        print(
            f"MODEL={family} FOLDS={model_reports[family]['evaluated_folds']}/{len(folds)} "
            f"BALANCED_ACCURACY="
            f"{model_reports[family]['classification_summary']['mean_metrics']['balanced_accuracy']} "
            f"MEAN_NET_RETURN="
            f"{model_reports[family]['regime_gated_sma_strategy_summary']['mean_net_return_fraction']} "
            f"CANDIDATE_GATE={gate['empirical_candidate_gate_passed']}"
        )
    print("SELECTED_MODEL_FOR_RUNTIME=null")
    print("RUNTIME_PROMOTION_ALLOWED=false")
    print("RELEASE_DISPOSITION=RESEARCH_ONLY_NO_CHAMPION_NO_LIVE_TRADING")
    return 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
