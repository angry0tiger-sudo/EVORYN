"""Leakage-controlled regime-model training, calibration, and holdout backtesting.

Labels are weak targets derived from future returns/volatility, not human-verified ground truth.
The chronological test partition is never used to fit model parameters, calibration temperature,
or target thresholds. This research module has no network and no exchange execution capability.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import platform
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal
from pathlib import Path
from typing import Any

from evoryn.domain.models import MarketCandle
from evoryn.intelligence.regime import RegimeFeatureExtractor, RegimeLabel, RegimeProbabilityModel
from evoryn.intelligence.trained_regime import TrainedRegimeProbabilityModel
from evoryn.learning_lab.backtest import BacktestConfig, BacktestResultStore, run_backtest
from evoryn.learning_lab.historical_data import HistoricalDataset
from evoryn.strategies.baseline import SmaCrossoverBaseline
from evoryn.strategies.contracts import CausalTargetStrategy

_LABELS = (
    RegimeLabel.BULL_TREND,
    RegimeLabel.BEAR_TREND,
    RegimeLabel.RANGE_BOUND,
    RegimeLabel.HIGH_VOLATILITY,
)
_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class RegimeTrainingConfig:
    """Explicit fixed research configuration and chronological split policy."""

    feature_lookback: int = 20
    horizon_bars: int = 6
    train_fraction: float = 0.60
    validation_fraction: float = 0.20
    trend_quantile: float = 0.65
    volatility_quantile: float = 0.75
    min_class_samples: int = 8
    random_seed: int = 41
    confidence_threshold: Decimal = Decimal("0.65")
    use_volume_feature: bool = False

    def __post_init__(self) -> None:
        if type(self.feature_lookback) is not int or self.feature_lookback < 2:
            raise ValueError("feature_lookback must be an integer >= 2.")
        if type(self.horizon_bars) is not int or self.horizon_bars < 1:
            raise ValueError("horizon_bars must be a positive integer.")
        if (
            not 0.4 <= self.train_fraction <= 0.8
            or not 0.1 <= self.validation_fraction <= 0.3
            or self.train_fraction + self.validation_fraction >= 0.9
        ):
            raise ValueError("Chronological train/validation fractions are outside safe bounds.")
        if not 0.5 <= self.trend_quantile <= 0.9:
            raise ValueError("trend_quantile must be in [0.5, 0.9].")
        if not 0.5 <= self.volatility_quantile <= 0.95:
            raise ValueError("volatility_quantile must be in [0.5, 0.95].")
        if type(self.min_class_samples) is not int or self.min_class_samples < 3:
            raise ValueError("min_class_samples must be an integer >= 3.")
        if type(self.random_seed) is not int or self.random_seed < 0:
            raise ValueError("random_seed must be a non-negative integer.")
        if (
            not isinstance(self.confidence_threshold, Decimal)
            or not self.confidence_threshold.is_finite()
            or not Decimal("0.5") <= self.confidence_threshold < Decimal("1")
        ):
            raise ValueError("confidence_threshold must be a finite Decimal in [0.5, 1).")
        if type(self.use_volume_feature) is not bool:
            raise ValueError("use_volume_feature must be an explicit boolean.")


@dataclass(frozen=True, slots=True)
class ForwardOutcome:
    """Outcome computed only for offline training/evaluation labels."""

    forward_return: float
    forward_volatility: float


def forward_outcome(
    candles: Sequence[MarketCandle],
    index: int,
    horizon_bars: int,
) -> ForwardOutcome:
    """Measure the return and realized volatility after candle index, strictly for labels."""
    if type(index) is not int or index < 0 or index + horizon_bars >= len(candles):
        raise ValueError("Forward outcome index does not have a complete future horizon.")
    if type(horizon_bars) is not int or horizon_bars <= 0:
        raise ValueError("horizon_bars must be a positive integer.")
    start_price = float(candles[index].close)
    end_price = float(candles[index + horizon_bars].close)
    if not math.isfinite(start_price) or not math.isfinite(end_price) or start_price <= 0:
        raise ValueError("Forward outcome prices must be finite and positive.")
    returns: list[float] = []
    for offset in range(index + 1, index + horizon_bars + 1):
        previous = float(candles[offset - 1].close)
        current = float(candles[offset].close)
        if not math.isfinite(previous) or not math.isfinite(current) or previous <= 0:
            raise ValueError("Forward outcome contains invalid prices.")
        returns.append(current / previous - 1.0)
    average = sum(returns) / len(returns)
    volatility = math.sqrt(sum((value - average) ** 2 for value in returns) / len(returns))
    return ForwardOutcome(
        forward_return=end_price / start_price - 1.0,
        forward_volatility=volatility,
    )


def linear_quantile(values: Sequence[float], quantile: float) -> float:
    """Small deterministic linearly interpolated quantile helper, testable without NumPy."""
    if not values or not 0 <= quantile <= 1:
        raise ValueError("Quantile requires values and a fraction in [0, 1].")
    if any(not math.isfinite(value) for value in values):
        raise ValueError("Quantile input must be finite.")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def assign_forward_regime(
    outcome: ForwardOutcome,
    *,
    trend_threshold: float,
    volatility_threshold: float,
) -> RegimeLabel:
    """Generate a transparent weak label; strong directional moves outrank the volatility tag."""
    if (
        not math.isfinite(trend_threshold)
        or not math.isfinite(volatility_threshold)
        or trend_threshold <= 0
        or volatility_threshold < 0
    ):
        raise ValueError("Regime label thresholds must be finite and non-negative.")
    if outcome.forward_return >= trend_threshold:
        return RegimeLabel.BULL_TREND
    if outcome.forward_return <= -trend_threshold:
        return RegimeLabel.BEAR_TREND
    if outcome.forward_volatility >= volatility_threshold:
        return RegimeLabel.HIGH_VOLATILITY
    return RegimeLabel.RANGE_BOUND


class RegimeLongFlatStrategy:
    """Use the frozen trained regime model for a long/flat holdout backtest only."""

    def __init__(
        self,
        model: RegimeProbabilityModel,
        confidence_threshold: Decimal,
        training_dataset_version: str,
    ) -> None:
        self._model = model
        model_features = getattr(model, "feature_extractor", None)
        if model_features is None:
            model_features = RegimeFeatureExtractor()
        if not isinstance(model_features, RegimeFeatureExtractor):
            raise ValueError("Model feature extractor is not a supported causal extractor.")
        self._features = model_features
        self._confidence_threshold = confidence_threshold
        self._dataset_version = training_dataset_version
        self.strategy_id = "trained-regime-long-flat"
        self.strategy_version = model.model_version

    def parameters(self) -> dict[str, str]:
        return {
            "model_fingerprint": self._model.model_fingerprint,
            "training_dataset_version": self._dataset_version,
            "confidence_threshold": str(self._confidence_threshold),
            "target_policy": "long_only_when_bull_regime_confident_else_flat",
        }

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        if len(history) < self._features.minimum_candles:
            return _ZERO
        probabilities = self._model.predict_probabilities(history)
        best_label, confidence = max(probabilities.items(), key=lambda item: item[1])
        if (
            best_label is RegimeLabel.BULL_TREND
            and confidence >= self._confidence_threshold
        ):
            return Decimal("1")
        return _ZERO


class BuyAndHoldStrategy:
    """Simple out-of-sample market-exposure reference for the same execution assumptions."""

    strategy_id = "buy-and-hold-reference"
    strategy_version = "1.0.0"

    def parameters(self) -> dict[str, str]:
        return {"target_policy": "long_after_first_completed_candle"}

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        return Decimal("1") if history else _ZERO


def train_regime_model(
    dataset: HistoricalDataset,
    *,
    artifact_dir: str | Path,
    config: RegimeTrainingConfig | None = None,
) -> dict[str, Any]:
    """Fit RF on early history, calibrate on validation, and evaluate only the final time block."""
    options = config or RegimeTrainingConfig()
    if dataset.market.market_type.casefold() != "spot":
        raise ValueError("Regime training supports Spot data only.")
    if not dataset.market.symbol.upper().endswith("-USDT"):
        raise ValueError("This training candidate is restricted to USDT-quoted markets.")
    if options.use_volume_feature and dataset.volume_unit == "unknown":
        raise ValueError(
            "Volume features are disabled when the source does not document volume units."
        )
    if dataset.allow_gaps or dataset.gap_count:
        raise ValueError("Training refuses gapped OHLCV instead of bridging missing intervals.")
    candles = dataset.candles
    count = len(candles)
    train_end = int(count * options.train_fraction)
    validation_end = int(count * (options.train_fraction + options.validation_fraction))
    lookback, horizon = options.feature_lookback, options.horizon_bars
    train_indices = list(range(lookback, train_end - horizon))
    validation_indices = list(range(train_end, validation_end - horizon))
    test_indices = list(range(validation_end, count - horizon))
    if (
        len(train_indices) < 100
        or len(validation_indices) < 30
        or len(test_indices) < 30
    ):
        raise ValueError(
            "Historical dataset is too short for purged chronological model partitions."
        )

    use_volume_feature = options.use_volume_feature
    feature_schema = (
        "2.0.0-price-plus-volume" if use_volume_feature else "2.0.0-price-only"
    )
    extractor = RegimeFeatureExtractor(
        lookback=lookback,
        use_volume_feature=use_volume_feature,
        feature_schema_version=feature_schema,
    )
    series = extractor.extract_series(candles)
    feature_by_index = {
        lookback + offset: list(feature.as_float_tuple())
        for offset, feature in enumerate(series)
    }
    outcomes = {
        index: forward_outcome(candles, index, horizon)
        for index in (*train_indices, *validation_indices, *test_indices)
    }
    train_returns = [abs(outcomes[index].forward_return) for index in train_indices]
    train_volatility = [outcomes[index].forward_volatility for index in train_indices]
    trend_threshold = max(
        linear_quantile(train_returns, options.trend_quantile),
        0.001,
    )
    volatility_threshold = linear_quantile(
        train_volatility,
        options.volatility_quantile,
    )
    if not math.isfinite(volatility_threshold) or volatility_threshold < 0:
        raise ValueError("Training-only volatility threshold is invalid.")

    labels = {
        index: assign_forward_regime(
            outcome,
            trend_threshold=trend_threshold,
            volatility_threshold=volatility_threshold,
        )
        for index, outcome in outcomes.items()
    }
    train_counts = Counter(labels[index].value for index in train_indices)
    required_classes = {label.value for label in _LABELS}
    if set(train_counts) != required_classes or any(
        train_counts[label.value] < options.min_class_samples for label in _LABELS
    ):
        raise ValueError(
            "Training partition lacks adequate samples for all four target regimes; "
            "no model will be promoted or silently relabeled."
        )

    numpy = importlib.import_module("numpy")
    sklearn_ensemble = importlib.import_module("sklearn.ensemble")
    estimator = sklearn_ensemble.RandomForestClassifier(
        n_estimators=160,
        max_depth=7,
        min_samples_leaf=4,
        class_weight="balanced_subsample",
        random_state=options.random_seed,
        n_jobs=1,
    )
    x_train = numpy.asarray([feature_by_index[index] for index in train_indices], dtype="float64")
    y_train = numpy.asarray([labels[index].value for index in train_indices], dtype=str)
    estimator.fit(x_train, y_train)
    class_order = tuple(RegimeLabel(str(value)) for value in estimator.classes_)
    if set(class_order) != set(_LABELS) or len(class_order) != 4:
        raise ValueError("Fitted estimator did not retain all required regime classes.")

    x_validation = numpy.asarray(
        [feature_by_index[index] for index in validation_indices],
        dtype="float64",
    )
    y_validation = [labels[index].value for index in validation_indices]
    raw_validation = estimator.predict_proba(x_validation)
    temperature = _fit_temperature(raw_validation.tolist(), y_validation, class_order)
    validation_metrics = _classification_metrics(
        y_validation,
        raw_validation.tolist(),
        class_order,
        temperature=temperature,
    )

    x_test = numpy.asarray([feature_by_index[index] for index in test_indices], dtype="float64")
    y_test = [labels[index].value for index in test_indices]
    raw_test = estimator.predict_proba(x_test)
    test_metrics = _classification_metrics(
        y_test,
        raw_test.tolist(),
        class_order,
        temperature=temperature,
    )
    baseline_label = Counter(y_train).most_common(1)[0][0]
    baseline_metrics = _constant_classifier_metrics(y_test, baseline_label, class_order)

    artifact_root = Path(artifact_dir).expanduser().resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    model_path = artifact_root / "regime-model.joblib"
    joblib = importlib.import_module("joblib")
    joblib.dump(
        {
            "schema_version": 1,
            "estimator": estimator,
            "class_order": [label.value for label in class_order],
            "temperature": temperature,
            "feature_lookback": lookback,
        },
        model_path,
        compress=3,
    )
    model_sha = _sha256_file(model_path)
    training_info: dict[str, Any] = {
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.version,
        "dataset_sha256": hashlib.sha256(dataset.candles_csv_bytes()).hexdigest(),
        "market_symbol": dataset.market.symbol,
        "market_venue": dataset.market.venue,
        "market_type": dataset.market.market_type,
        "source_name": dataset.source_name,
        "source_uri": dataset.source_uri,
        "source_revision": dataset.source_revision,
        "license_id": dataset.license_id,
        "license_review_status": "UNREVIEWED",
        "candle_count": count,
        "first_candle_open": candles[0].timestamp.astimezone(UTC).isoformat(),
        "last_candle_open": candles[-1].timestamp.astimezone(UTC).isoformat(),
        "collected_at": dataset.collected_at.astimezone(UTC).isoformat(),
        "interval_seconds": int(dataset.interval.total_seconds()),
        "feature_schema_version": extractor.feature_schema_version,
        "feature_lookback": lookback,
        "volume_feature_enabled": use_volume_feature,
        "volume_semantics_status": (
            "DECLARED_BASE_ASSET" if dataset.volume_unit == "base_asset"
            else "DECLARED_QUOTE_ASSET" if dataset.volume_unit == "quote_asset"
            else "UNVERIFIED_NOT_USED_IN_MODEL"
        ),
        "label_horizon_bars": horizon,
        "weak_label_policy": {
            "name": "forward-return-volatility-thresholds-v1",
            "trend_threshold": trend_threshold,
            "volatility_threshold": volatility_threshold,
            "trend_quantile_fit_on_train_only": options.trend_quantile,
            "volatility_quantile_fit_on_train_only": options.volatility_quantile,
            "priority": [
                "forward_return_above_positive_threshold",
                "forward_return_below_negative_threshold",
                "high_forward_volatility_with_no_strong_direction",
                "range_bound_otherwise",
            ],
            "ground_truth": "weak_future-outcome_labels_not_human_verified",
        },
        "partitions": {
            "train": {"start": 0, "end": train_end, "sample_count": len(train_indices)},
            "validation": {
                "start": train_end,
                "end": validation_end,
                "sample_count": len(validation_indices),
            },
            "test": {"start": validation_end, "end": count, "sample_count": len(test_indices)},
            "purging": {
                "horizon_bars_removed_from_each_partition_end": horizon,
                "feature_inputs_use_only_trailing_completed_bars": True,
            },
        },
        "train_class_counts": dict(sorted(train_counts.items())),
        "classifier": {
            "algorithm": "scikit-learn RandomForestClassifier",
            "n_estimators": 160,
            "max_depth": 7,
            "min_samples_leaf": 4,
            "class_weight": "balanced_subsample",
            "random_seed": options.random_seed,
            "calibration": "single_temperature_fitted_on_validation_nll",
            "calibration_temperature": temperature,
            "calibration_validation_metrics": validation_metrics,
        },
        "test_classification_metrics": test_metrics,
        "majority_train_label_baseline_metrics": baseline_metrics,
        "artifact_sha256": model_sha,
        "python_version": platform.python_version(),
    }
    manifest = {
        "schema_version": 1,
        "model_id": TrainedRegimeProbabilityModel.model_id,
        "model_version": model_sha[:16],
        "artifact_name": model_path.name,
        "artifact_sha256": model_sha,
        "training": training_info,
    }
    _write_json(artifact_root / "model-manifest.json", manifest)
    model = TrainedRegimeProbabilityModel(
        estimator=estimator,
        class_order=class_order,
        temperature=temperature,
        model_fingerprint=model_sha,
        model_version=model_sha[:16],
        feature_extractor=extractor,
        training_manifest=training_info,
    )

    partition_warmup = max(0, validation_end - max(lookback + 1, 48))
    backtest_config = BacktestConfig(
        initial_cash=Decimal("10000"),
        commission_bps=Decimal("10"),
        spread_bps=Decimal("10"),
        slippage_bps=Decimal("10"),
        max_bar_participation=Decimal("0.05"),
        allocation_fraction=Decimal("0.25"),
        quantity_step=Decimal("0.00001"),
        enforce_volume_participation=(dataset.volume_unit == "base_asset"),
    )
    strategies: list[CausalTargetStrategy] = [
        RegimeLongFlatStrategy(
            model,
            options.confidence_threshold,
            dataset.version,
        ),
        SmaCrossoverBaseline(fast_window=12, slow_window=48),
        BuyAndHoldStrategy(),
    ]
    backtests: dict[str, Any] = {}
    result_store = BacktestResultStore(artifact_root / "backtests")
    for strategy in strategies:
        result = run_backtest(
            dataset,
            strategy,
            backtest_config,
            start_index=validation_end,
            end_index=count,
            warmup_start_index=partition_warmup,
            segment_name="untouched-final-holdout",
        )
        path = result_store.save(result)
        backtests[result.strategy_id] = {
            "run_id": result.run_id,
            "report_path": str(path.relative_to(artifact_root)),
            "dataset_version": result.dataset_version,
            "strategy_id": result.strategy_id,
            "strategy_version": result.strategy_version,
            "evaluation_range": {
                "start": result.evaluation_range.start,
                "end": result.evaluation_range.end,
            },
            "metrics": _backtest_metrics(result.metrics),
        }
    report = {
        "schema_version": 1,
        "model_manifest_path": "model-manifest.json",
        "model_artifact_path": "regime-model.joblib",
        "model_artifact_sha256": model_sha,
        "dataset": {
            **dataset.metadata(),
            "dataset_version": dataset.version,
            "candles_sha256": hashlib.sha256(dataset.candles_csv_bytes()).hexdigest(),
            "dataset_manifest_status": "versioned_in_memory_not_persisted_by_trainer",
            "volume_semantics_status": (
                "DECLARED_BASE_ASSET" if dataset.volume_unit == "base_asset"
                else "DECLARED_QUOTE_ASSET" if dataset.volume_unit == "quote_asset"
                else "UNVERIFIED"
            ),
        },
        "training": training_info,
        "backtests": backtests,
        "economics": {
            "initial_cash_usdt": str(backtest_config.initial_cash),
            "commission_bps": str(backtest_config.commission_bps),
            "assumption_not_provider_quote": True,
            "spread_bps": str(backtest_config.spread_bps),
            "slippage_bps": str(backtest_config.slippage_bps),
            "max_bar_participation": str(backtest_config.max_bar_participation),
            "enforce_volume_participation": backtest_config.enforce_volume_participation,
            "volume_capacity_status": (
                "ENFORCED_BASE_ASSET" if backtest_config.enforce_volume_participation
                else "UNCONSTRAINED_UNKNOWN_VOLUME_UNIT"
            ),
            "allocation_fraction": str(backtest_config.allocation_fraction),
            "quantity_step": str(backtest_config.quantity_step),
        },
        "release_disposition": "RESEARCH_ONLY_NOT_APPROVED_FOR_LIVE_TRADING",
        "limitations": [
            "Regime labels are weak targets derived from future returns and realized volatility.",
            (
                "Only the validation partition fits the probability temperature; "
                "test is never used for tuning."
            ),
            (
                "OHLCV cannot reconstruct historical spreads/depth; spread and slippage "
                "are explicit assumptions."
            ),
            (
                "UDF volume units are undocumented; price-only features are used and volume-based "
                "fill participation is disabled for this dataset."
            ),
            "Historical market-data rights/commercial-use permission remain unreviewed.",
            "A positive simulated return is not evidence of future profitability.",
        ],
    }
    _write_json(artifact_root / "training-report.json", report)
    return report


def _fit_temperature(
    probabilities: Sequence[Sequence[float]],
    targets: Sequence[str],
    class_order: Sequence[RegimeLabel],
) -> float:
    """Fit one scalar on the validation block only, minimizing multiclass negative log loss."""
    if not probabilities or len(probabilities) != len(targets):
        raise ValueError("Temperature calibration requires aligned validation predictions/labels.")
    target_indices = [class_order.index(RegimeLabel(target)) for target in targets]
    candidates = [round(0.50 + step * 0.05, 2) for step in range(51)]
    best_temperature = 1.0
    best_loss = float("inf")
    for temperature in candidates:
        loss = 0.0
        for row, target in zip(probabilities, target_indices, strict=True):
            calibrated = _temperature_scale(row, temperature)
            loss -= math.log(max(calibrated[target], 1e-15))
        loss /= len(targets)
        if loss < best_loss:
            best_loss = loss
            best_temperature = temperature
    return best_temperature


def _temperature_scale(probabilities: Sequence[float], temperature: float) -> list[float]:
    if not probabilities or not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("Temperature scaling inputs are invalid.")
    logs = [math.log(max(float(value), 1e-15)) / temperature for value in probabilities]
    peak = max(logs)
    values = [math.exp(value - peak) for value in logs]
    total = sum(values)
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Temperature scaling normalization failed.")
    return [value / total for value in values]


def _classification_metrics(
    targets: Sequence[str],
    probabilities: Sequence[Sequence[float]],
    class_order: Sequence[RegimeLabel],
    *,
    temperature: float,
) -> dict[str, Any]:
    if not targets or len(targets) != len(probabilities):
        raise ValueError("Classification evaluation requires aligned non-empty arrays.")
    predictions: list[str] = []
    calibrated_rows: list[list[float]] = []
    for row in probabilities:
        scaled = _temperature_scale(row, temperature)
        calibrated_rows.append(scaled)
        predictions.append(class_order[max(range(len(scaled)), key=scaled.__getitem__)].value)
    correct = sum(pred == target for pred, target in zip(predictions, targets, strict=True))
    supports: dict[str, int] = {}
    recalls: list[float] = []
    f1s: list[float] = []
    for label in class_order:
        true_positive = sum(
            target == label.value and prediction == label.value
            for target, prediction in zip(targets, predictions, strict=True)
        )
        false_positive = sum(
            target != label.value and prediction == label.value
            for target, prediction in zip(targets, predictions, strict=True)
        )
        false_negative = sum(
            target == label.value and prediction != label.value
            for target, prediction in zip(targets, predictions, strict=True)
        )
        support = sum(target == label.value for target in targets)
        supports[label.value] = support
        if support:
            recall = true_positive / support
            recalls.append(recall)
            denominator = (2 * true_positive) + false_positive + false_negative
            f1s.append((2 * true_positive / denominator) if denominator else 0.0)
    brier = 0.0
    negative_log_likelihood = 0.0
    for target, row in zip(targets, calibrated_rows, strict=True):
        target_index = class_order.index(RegimeLabel(target))
        brier += sum(
            (probability - (1.0 if index == target_index else 0.0)) ** 2
            for index, probability in enumerate(row)
        )
        negative_log_likelihood -= math.log(max(row[target_index], 1e-15))
    count = len(targets)
    return {
        "sample_count": count,
        "accuracy": correct / count,
        "balanced_accuracy": sum(recalls) / len(recalls) if recalls else 0.0,
        "macro_f1": sum(f1s) / len(f1s) if f1s else 0.0,
        "multiclass_brier_score": brier / count,
        "log_loss": negative_log_likelihood / count,
        "class_support": supports,
        "target_provenance": "weak_future-outcome_labels",
    }


def _constant_classifier_metrics(
    targets: Sequence[str],
    label: str,
    class_order: Sequence[RegimeLabel],
) -> dict[str, Any]:
    if not targets:
        raise ValueError("Baseline evaluation requires test labels.")
    predicted = [label] * len(targets)
    accuracy = sum(target == label for target in targets) / len(targets)
    present = [item for item in class_order if any(target == item.value for target in targets)]
    recalls = [
        sum(
            target == item.value and prediction == item.value
            for target, prediction in zip(targets, predicted, strict=True)
        )
        / sum(target == item.value for target in targets)
        for item in present
    ]
    return {
        "sample_count": len(targets),
        "constant_label": label,
        "accuracy": accuracy,
        "balanced_accuracy": sum(recalls) / len(recalls) if recalls else 0.0,
        "baseline_type": "majority-class-from-training-only",
    }


def _backtest_metrics(metrics: Any) -> dict[str, str | int]:
    return {
        "initial_equity": str(metrics.initial_equity),
        "final_equity": str(metrics.final_equity),
        "net_profit": str(metrics.net_profit),
        "net_return_fraction": str(metrics.net_return_fraction),
        "max_drawdown_fraction": str(metrics.max_drawdown_fraction),
        "closed_trade_count": metrics.closed_trade_count,
        "win_rate_fraction": (
            str(metrics.win_rate_fraction) if metrics.win_rate_fraction is not None else "n/a"
        ),
        "commission_paid": str(metrics.commission_paid),
        "estimated_spread_cost": str(metrics.estimated_spread_cost),
        "estimated_slippage_cost": str(metrics.estimated_slippage_cost),
        "fill_count": metrics.fill_count,
        "time_in_market_fraction": str(metrics.time_in_market_fraction),
        "open_position_quantity": str(metrics.open_position_quantity),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload + b"\n")
    temporary.replace(path)
