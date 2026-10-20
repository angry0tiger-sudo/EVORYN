"""Purged rolling walk-forward research for price-only regime models.

Compares two weak-label policies and three model families: Random Forest,
HistGradientBoosting, and a filtered Gaussian Hidden Markov Model. Every fit, scaler,
label threshold, state-to-label map, and probability temperature is trained within a
fold's past; final fold-test observations are never used for tuning. Outputs are advisory.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from evoryn.domain.models import MarketCandle
from evoryn.intelligence.regime import (
    RegimeFeatureExtractor,
    RegimeLabel,
)
from evoryn.intelligence.regime_training import (
    BuyAndHoldStrategy,
    RegimeLongFlatStrategy,
    _classification_metrics,
    _constant_classifier_metrics,
    _fit_temperature,
    _temperature_scale,
    assign_forward_regime,
    forward_outcome,
    linear_quantile,
)
from evoryn.learning_lab.backtest import (
    BacktestConfig,
    BacktestResultStore,
    make_walk_forward_folds,
    run_backtest,
)
from evoryn.learning_lab.historical_data import HistoricalDataset
from evoryn.strategies.baseline import SmaCrossoverBaseline

_LABELS = (
    RegimeLabel.BULL_TREND,
    RegimeLabel.BEAR_TREND,
    RegimeLabel.RANGE_BOUND,
    RegimeLabel.HIGH_VOLATILITY,
)
_POLICIES = ("forward_thresholds", "volatility_scaled_triple_barrier")
_MODELS = ("random_forest", "hist_gradient_boosting", "gaussian_hmm")


@dataclass(frozen=True, slots=True)
class WalkForwardResearchConfig:
    """Fixed rolling windows and thresholds; no parameter search on fold test blocks."""

    feature_lookback: int = 20
    horizon_bars: int = 6
    train_bars: int = 1728
    validation_bars: int = 432
    test_bars: int = 432
    step_bars: int = 432
    trend_quantile: float = 0.65
    volatility_quantile: float = 0.75
    triple_barrier_multiplier: float = 1.5
    min_class_samples: int = 12
    random_seed: int = 41
    confidence_threshold: float = 0.65

    def __post_init__(self) -> None:
        for name in (
            "feature_lookback",
            "horizon_bars",
            "train_bars",
            "validation_bars",
            "test_bars",
            "step_bars",
            "min_class_samples",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        if self.feature_lookback < 2 or self.horizon_bars < 1:
            raise ValueError("Feature lookback/horizon are below their minimum.")
        if self.step_bars < self.test_bars:
            raise ValueError("Walk-forward test blocks must not overlap.")
        if not 0.5 <= self.trend_quantile <= 0.9:
            raise ValueError("trend_quantile must be in [0.5, 0.9].")
        if not 0.5 <= self.volatility_quantile <= 0.95:
            raise ValueError("volatility_quantile must be in [0.5, 0.95].")
        if not math.isfinite(self.triple_barrier_multiplier) or self.triple_barrier_multiplier <= 0:
            raise ValueError("triple_barrier_multiplier must be finite and positive.")
        if type(self.random_seed) is not int or self.random_seed < 0:
            raise ValueError("random_seed must be a non-negative integer.")
        if not math.isfinite(self.confidence_threshold) or not 0.5 <= self.confidence_threshold < 1:
            raise ValueError("confidence_threshold must be finite and in [0.5, 1).")


def volatility_scaled_triple_barrier_label(
    candles: Sequence[MarketCandle],
    index: int,
    *,
    trailing_volatility: float,
    horizon_bars: int,
    barrier_multiplier: float,
    high_volatility_threshold: float,
) -> RegimeLabel:
    """Use past volatility for symmetric barriers and future OHLC only for offline labels.

    The first future bar touching one horizontal barrier determines direction. If both
    barriers are touched in the same OHLC bar, ordering is unknowable from candles, so the
    sample is conservatively labeled high_volatility. If neither barrier is touched, the
    vertical horizon ends the label and forward realized volatility distinguishes high-vol
    from range. Features never use this future path.
    """
    values = (trailing_volatility, barrier_multiplier, high_volatility_threshold)
    if any(not math.isfinite(value) for value in values):
        raise ValueError("Triple-barrier inputs must be finite.")
    if (
        type(index) is not int
        or index < 0
        or type(horizon_bars) is not int
        or horizon_bars <= 0
        or trailing_volatility < 0
        or barrier_multiplier <= 0
        or high_volatility_threshold < 0
    ):
        raise ValueError("Triple-barrier inputs are outside their valid range.")
    if index + horizon_bars >= len(candles):
        raise ValueError("Triple-barrier requires a complete future horizon.")
    decision_candle = candles[index]
    barrier = max(
        trailing_volatility * math.sqrt(horizon_bars) * barrier_multiplier,
        0.001,
    )
    barrier_decimal = Decimal(str(barrier))
    upper_barrier = decision_candle.close * (Decimal("1") + barrier_decimal)
    lower_barrier = decision_candle.close * (Decimal("1") - barrier_decimal)
    for future_candle in candles[index + 1 : index + horizon_bars + 1]:
        upper_hit = future_candle.high >= upper_barrier
        lower_hit = future_candle.low <= lower_barrier
        if upper_hit and lower_hit:
            return RegimeLabel.HIGH_VOLATILITY
        if upper_hit:
            return RegimeLabel.BULL_TREND
        if lower_hit:
            return RegimeLabel.BEAR_TREND
    vertical_outcome = forward_outcome(candles, index, horizon_bars)
    if vertical_outcome.forward_volatility >= high_volatility_threshold:
        return RegimeLabel.HIGH_VOLATILITY
    return RegimeLabel.RANGE_BOUND

def filtered_gaussian_hmm_probabilities(model: Any, observations: Any) -> Any:
    """Compute forward-filtered state probabilities (never backward-smoothed with future rows)."""
    numpy = importlib.import_module("numpy")
    matrix = numpy.asarray(observations, dtype="float64")
    means = numpy.asarray(model.means_, dtype="float64")
    covariance = numpy.asarray(model.covars_, dtype="float64")
    if covariance.ndim == 3:
        variances = numpy.diagonal(covariance, axis1=1, axis2=2)
    elif covariance.ndim == 2:
        variances = covariance
    else:
        raise ValueError("GaussianHMM covariance schema is unsupported.")
    if (
        matrix.ndim != 2
        or matrix.shape[1] != means.shape[1]
        or variances.shape != means.shape
        or not numpy.isfinite(matrix).all()
        or not numpy.isfinite(means).all()
        or not numpy.isfinite(variances).all()
        or (variances <= 0).any()
    ):
        raise ValueError("GaussianHMM observations or fitted parameters are invalid.")
    log_emission = -0.5 * (
        numpy.log(2.0 * math.pi * variances)[None, :, :]
        + (matrix[:, None, :] - means[None, :, :]) ** 2 / variances[None, :, :]
    ).sum(axis=2)
    log_emission -= log_emission.max(axis=1, keepdims=True)
    emissions = numpy.exp(numpy.clip(log_emission, -700.0, 0.0))
    start = numpy.asarray(model.startprob_, dtype="float64")
    transition = numpy.asarray(model.transmat_, dtype="float64")
    if start.shape != (means.shape[0],) or transition.shape != (means.shape[0], means.shape[0]):
        raise ValueError("GaussianHMM transition schema is invalid.")
    filtered = numpy.zeros((matrix.shape[0], means.shape[0]), dtype="float64")
    current = start * emissions[0]
    total = float(current.sum())
    if not math.isfinite(total) or total <= 0:
        raise ValueError("GaussianHMM initial filtering normalization failed.")
    filtered[0] = current / total
    for index in range(1, matrix.shape[0]):
        current = (filtered[index - 1] @ transition) * emissions[index]
        total = float(current.sum())
        if not math.isfinite(total) or total <= 0:
            raise ValueError("GaussianHMM filtering normalization failed.")
        filtered[index] = current / total
    if not numpy.isfinite(filtered).all():
        raise ValueError("GaussianHMM filtering generated non-finite probabilities.")
    return filtered


class _EstimatorAdapter:
    """In-memory fold-scoped classifier adapter used only by offline backtests."""

    def __init__(
        self,
        estimator: Any,
        *,
        class_order: tuple[RegimeLabel, ...],
        temperature: float,
        extractor: RegimeFeatureExtractor,
        fingerprint: str,
    ) -> None:
        self.estimator = estimator
        self.class_order = class_order
        self.temperature = temperature
        self.feature_extractor = extractor
        self.model_fingerprint = fingerprint
        self.model_version = fingerprint[:16]
        self.model_id = "evoryn.walk-forward-classifier"
        self.confidence_semantics = "calibrated_probability"
        self.is_ready = True

    def predict_probabilities(
        self, history: Sequence[MarketCandle]
    ) -> Mapping[RegimeLabel, Decimal]:
        numpy = importlib.import_module("numpy")
        feature = self.feature_extractor.extract(history[-self.feature_extractor.minimum_candles :])
        raw = self.estimator.predict_proba(
            numpy.asarray([feature.as_float_tuple()], dtype="float64")
        )[0]
        scaled = _temperature_scale([float(item) for item in raw], self.temperature)
        return {
            label: Decimal(str(scaled[index]))
            for index, label in enumerate(self.class_order)
        }


class _HMMAdapter:
    """Filtered HMM posterior mapped to training-only weak labels for a fold simulation."""

    def __init__(
        self,
        model: Any,
        scaler: Any,
        state_to_label_probabilities: Any,
        *,
        extractor: RegimeFeatureExtractor,
        training_start_index: int,
        temperature: float,
        fingerprint: str,
    ) -> None:
        self.model = model
        self.scaler = scaler
        self.state_to_label_probabilities = state_to_label_probabilities
        self.feature_extractor = extractor
        self.training_start_index = training_start_index
        self.temperature = temperature
        self.model_fingerprint = fingerprint
        self.model_version = fingerprint[:16]
        self.model_id = "evoryn.walk-forward-gaussian-hmm"
        self.confidence_semantics = "calibrated_probability"
        self.is_ready = True

    def predict_probabilities(
        self, history: Sequence[MarketCandle]
    ) -> Mapping[RegimeLabel, Decimal]:
        numpy = importlib.import_module("numpy")
        local_history = history[self.training_start_index :]
        vectors = self.feature_extractor.extract_series(local_history)
        observations = numpy.asarray(
            [vector.as_float_tuple() for vector in vectors], dtype="float64"
        )
        scaled = self.scaler.transform(observations)
        states = filtered_gaussian_hmm_probabilities(self.model, scaled)
        label_probabilities = states[-1] @ self.state_to_label_probabilities
        raw = [float(value) for value in label_probabilities]
        calibrated = _temperature_scale(raw, self.temperature)
        return {
            label: Decimal(str(calibrated[index]))
            for index, label in enumerate(_LABELS)
        }


def run_walk_forward_research(
    dataset: HistoricalDataset,
    *,
    output_dir: str | Path,
    config: WalkForwardResearchConfig | None = None,
) -> dict[str, Any]:
    """Run all model/policy combinations over rolling folds and persist immutable evidence."""
    options = config or WalkForwardResearchConfig()
    if (
        dataset.market.market_type.casefold() != "spot"
        or not dataset.market.symbol.upper().endswith("-USDT")
    ):
        raise ValueError("Walk-forward research is restricted to Spot/USDT datasets.")
    if dataset.allow_gaps or dataset.gap_count:
        raise ValueError("Walk-forward research refuses gapped data.")
    candles = dataset.candles
    folds = make_walk_forward_folds(
        dataset,
        train_bars=options.train_bars,
        validation_bars=options.validation_bars,
        test_bars=options.test_bars,
        step_bars=options.step_bars,
    )
    extractor = RegimeFeatureExtractor(lookback=options.feature_lookback)
    feature_series = extractor.extract_series(candles)
    feature_by_index = {
        options.feature_lookback + offset: vector.as_float_tuple()
        for offset, vector in enumerate(feature_series)
    }
    output = Path(output_dir).expanduser().resolve()
    model_dir = output / "models"
    backtest_store = BacktestResultStore(output / "backtests")
    model_dir.mkdir(parents=True, exist_ok=True)
    numpy = importlib.import_module("numpy")
    sklearn_ensemble = importlib.import_module("sklearn.ensemble")
    sklearn_preprocessing = importlib.import_module("sklearn.preprocessing")
    sklearn_hmm = importlib.import_module("hmmlearn.hmm")
    joblib = importlib.import_module("joblib")
    fold_reports: list[dict[str, Any]] = []
    aggregate_source: dict[str, list[dict[str, Any]]] = {}
    backtest_config = BacktestConfig(
        initial_cash=Decimal("10000"),
        commission_bps=__import__("decimal").Decimal("10"),
        spread_bps=__import__("decimal").Decimal("10"),
        slippage_bps=__import__("decimal").Decimal("10"),
        max_bar_participation=__import__("decimal").Decimal("0.05"),
        allocation_fraction=__import__("decimal").Decimal("0.25"),
        quantity_step=__import__("decimal").Decimal("0.00001"),
        enforce_volume_participation=(dataset.volume_unit == "base_asset"),
    )
    baseline_strategy_types = (SmaCrossoverBaseline, BuyAndHoldStrategy)
    for fold in folds:
        horizon = options.horizon_bars
        train_indices = list(
            range(
                max(fold.train.start + options.feature_lookback, options.feature_lookback),
                fold.train.end - horizon,
            )
        )
        validation_indices = list(
            range(fold.validation.start, fold.validation.end - horizon)
        )
        test_indices = list(range(fold.test.start, fold.test.end - horizon))
        if min(len(train_indices), len(validation_indices), len(test_indices)) < 30:
            raise ValueError(f"Fold {fold.fold_index} is too small after purging.")
        outcomes = {
            index: forward_outcome(candles, index, horizon)
            for index in (*train_indices, *validation_indices, *test_indices)
        }
        train_returns = [abs(outcomes[index].forward_return) for index in train_indices]
        train_forward_volatility = [outcomes[index].forward_volatility for index in train_indices]
        trend_threshold = max(linear_quantile(train_returns, options.trend_quantile), 0.001)
        volatility_threshold = linear_quantile(
            train_forward_volatility, options.volatility_quantile
        )
        training_matrix = numpy.asarray(
            [feature_by_index[index] for index in train_indices], dtype="float64"
        )
        scaler = sklearn_preprocessing.StandardScaler()
        scaler.fit(training_matrix)
        scaled_train = scaler.transform(training_matrix)
        # HMM parameters and the scaler fit on this fold's training samples only.
        hmm_model = sklearn_hmm.GaussianHMM(
            n_components=4,
            covariance_type="diag",
            n_iter=120,
            tol=0.01,
            min_covar=1e-4,
            random_state=options.random_seed + fold.fold_index,
            implementation="log",
        )
        hmm_model.fit(scaled_train)
        hmm_converged = bool(hmm_model.monitor_.converged)
        hmm_iterations = int(hmm_model.monitor_.iter)
        history_indices = list(range(train_indices[0], test_indices[-1] + 1))
        scaled_history = scaler.transform(
            numpy.asarray([feature_by_index[index] for index in history_indices], dtype="float64")
        )
        filtered_history = filtered_gaussian_hmm_probabilities(hmm_model, scaled_history)
        train_posteriors = filtered_history[
            [index - history_indices[0] for index in train_indices]
        ]
        val_posteriors = filtered_history[
            [index - history_indices[0] for index in validation_indices]
        ]
        test_posteriors = filtered_history[
            [index - history_indices[0] for index in test_indices]
        ]
        for policy in _POLICIES:
            if policy == "forward_thresholds":
                labels = {
                    index: assign_forward_regime(
                        outcomes[index],
                        trend_threshold=trend_threshold,
                        volatility_threshold=volatility_threshold,
                    )
                    for index in outcomes
                }
                policy_parameters = {
                    "trend_threshold_abs_return": trend_threshold,
                    "high_forward_volatility_threshold": volatility_threshold,
                    "threshold_fit_scope": "training_partition_only",
                }
            else:
                labels = {
                    index: volatility_scaled_triple_barrier_label(
                        outcomes[index],
                        trailing_volatility=float(feature_by_index[index][2]),
                        horizon_bars=horizon,
                        barrier_multiplier=options.triple_barrier_multiplier,
                        high_volatility_threshold=volatility_threshold,
                    )
                    for index in outcomes
                }
                policy_parameters = {
                    "barrier": "symmetric_volatility_scaled",
                    "barrier_multiplier": options.triple_barrier_multiplier,
                    "volatility_lookback": options.feature_lookback,
                    "barrier_floor_abs_return": 0.001,
                    "high_forward_volatility_threshold": volatility_threshold,
                    "threshold_fit_scope": "training_partition_only",
                }
            y_train_labels = [labels[index].value for index in train_indices]
            train_counts = Counter(y_train_labels)
            if set(train_counts) != {label.value for label in _LABELS} or any(
                train_counts[label.value] < options.min_class_samples for label in _LABELS
            ):
                raise ValueError(
                    f"Fold {fold.fold_index} / {policy} lacks minimum training support for all "
                    "four labels. The fold is not silently relabeled or dropped."
                )
            y_validation = [labels[index].value for index in validation_indices]
            y_test = [labels[index].value for index in test_indices]
            x_train = training_matrix
            x_validation = numpy.asarray(
                [feature_by_index[index] for index in validation_indices], dtype="float64"
            )
            x_test = numpy.asarray(
                [feature_by_index[index] for index in test_indices], dtype="float64"
            )
            policy_report: dict[str, Any] = {
                "parameters": policy_parameters,
                "train_class_counts": dict(sorted(train_counts.items())),
                "validation_sample_count": len(validation_indices),
                "test_sample_count": len(test_indices),
                "models": {},
            }
            majority_label = Counter(y_train_labels).most_common(1)[0][0]
            fitted_for_backtest: dict[str, Any] = {}
            for model_name in _MODELS:
                wrapper: Any
                if model_name == "random_forest":
                    estimator = sklearn_ensemble.RandomForestClassifier(
                        n_estimators=180,
                        max_depth=7,
                        min_samples_leaf=5,
                        class_weight="balanced_subsample",
                        random_state=options.random_seed + fold.fold_index,
                        n_jobs=1,
                    )
                    estimator.fit(x_train, numpy.asarray(y_train_labels, dtype=str))
                    class_order = tuple(RegimeLabel(str(value)) for value in estimator.classes_)
                    if set(class_order) != set(_LABELS):
                        raise ValueError(
                            "Random Forest did not retain all required target classes."
                        )
                    raw_validation = estimator.predict_proba(x_validation).tolist()
                    raw_test = estimator.predict_proba(x_test).tolist()
                    wrapper = _EstimatorAdapter(
                        estimator,
                        class_order=class_order,
                        temperature=1.0,
                        extractor=extractor,
                        fingerprint="pending",
                    )
                    payload: dict[str, Any] = {
                        "model_family": model_name,
                        "estimator": estimator,
                        "class_order": [label.value for label in class_order],
                        "feature_schema_version": extractor.feature_schema_version,
                        "feature_lookback": extractor.lookback,
                    }
                elif model_name == "hist_gradient_boosting":
                    estimator = sklearn_ensemble.HistGradientBoostingClassifier(
                        max_iter=120,
                        max_leaf_nodes=15,
                        l2_regularization=1.0,
                        learning_rate=0.06,
                        random_state=options.random_seed + fold.fold_index,
                    )
                    estimator.fit(x_train, numpy.asarray(y_train_labels, dtype=str))
                    class_order = tuple(RegimeLabel(str(value)) for value in estimator.classes_)
                    if set(class_order) != set(_LABELS):
                        raise ValueError(
                            "HistGradientBoosting did not retain all required target classes."
                        )
                    raw_validation = estimator.predict_proba(x_validation).tolist()
                    raw_test = estimator.predict_proba(x_test).tolist()
                    wrapper = _EstimatorAdapter(
                        estimator,
                        class_order=class_order,
                        temperature=1.0,
                        extractor=extractor,
                        fingerprint="pending",
                    )
                    payload = {
                        "model_family": model_name,
                        "estimator": estimator,
                        "class_order": [label.value for label in class_order],
                        "feature_schema_version": extractor.feature_schema_version,
                        "feature_lookback": extractor.lookback,
                    }
                else:
                    state_map = numpy.ones((4, 4), dtype="float64")
                    state_labels = numpy.argmax(train_posteriors, axis=1)
                    for state in range(4):
                        subset = [
                            labels[index].value
                            for index, hidden_state in zip(
                                train_indices, state_labels, strict=True
                            )
                            if int(hidden_state) == state
                        ]
                        state_counts = Counter(subset)
                        denominator = len(subset) + len(_LABELS)
                        for label_index, label in enumerate(_LABELS):
                            state_map[state, label_index] = (
                                state_counts[label.value] + 1
                            ) / denominator
                    raw_validation = (val_posteriors @ state_map).tolist()
                    raw_test = (test_posteriors @ state_map).tolist()
                    class_order = _LABELS
                    wrapper = _HMMAdapter(
                        hmm_model,
                        scaler,
                        state_map,
                        extractor=extractor,
                        training_start_index=fold.train.start,
                        temperature=1.0,
                        fingerprint="pending",
                    )
                    payload = {
                        "model_family": model_name,
                        "hmm": hmm_model,
                        "scaler": scaler,
                        "state_to_label_probabilities": state_map.tolist(),
                        "class_order": [label.value for label in _LABELS],
                        "feature_schema_version": extractor.feature_schema_version,
                        "feature_lookback": extractor.lookback,
                    }
                temperature = _fit_temperature(raw_validation, y_validation, class_order)
                wrapper.temperature = temperature
                validation_metrics = _classification_metrics(
                    y_validation, raw_validation, class_order, temperature=temperature
                )
                test_metrics = _classification_metrics(
                    y_test, raw_test, class_order, temperature=temperature
                )
                baseline_metrics = _constant_classifier_metrics(y_test, majority_label, class_order)
                payload["temperature"] = temperature
                payload["dataset_version"] = dataset.version
                payload["fold_index"] = fold.fold_index
                payload["label_policy"] = policy
                payload["train_range"] = {"start": fold.train.start, "end": fold.train.end}
                payload["validation_range"] = {
                    "start": fold.validation.start,
                    "end": fold.validation.end,
                }
                payload["test_range"] = {"start": fold.test.start, "end": fold.test.end}
                model_bytes_input = json.dumps(
                    {
                        "dataset_version": dataset.version,
                        "fold": fold.fold_index,
                        "policy": policy,
                        "model": model_name,
                        "train_range": [fold.train.start, fold.train.end],
                        "validation_range": [fold.validation.start, fold.validation.end],
                        "test_range": [fold.test.start, fold.test.end],
                        "temperature": temperature,
                        "seed": options.random_seed + fold.fold_index,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                model_identity = hashlib.sha256(model_bytes_input).hexdigest()
                wrapper.model_fingerprint = model_identity
                wrapper.model_version = model_identity[:16]
                artifact_path = model_dir / f"fold-{fold.fold_index}-{policy}-{model_name}.joblib"
                joblib.dump(payload, artifact_path, compress=3)
                artifact_sha = _sha256_file(artifact_path)
                model_record = {
                    "artifact_path": str(artifact_path.relative_to(output)),
                    "artifact_sha256": artifact_sha,
                    "model_identity_sha256": model_identity,
                    "model_version": model_identity[:16],
                    "temperature_fitted_on": "validation_only",
                    "validation_metrics": validation_metrics,
                    "test_metrics": test_metrics,
                    "training_only_majority_baseline": baseline_metrics,
                }
                policy_report["models"][model_name] = model_record
                aggregate_key = f"{policy}/{model_name}"
                aggregate_source.setdefault(aggregate_key, []).append(
                    {
                        "fold_index": fold.fold_index,
                        **test_metrics,
                    }
                )
                if policy == "volatility_scaled_triple_barrier":
                    fitted_for_backtest[model_name] = {
                        "wrapper": wrapper,
                        "policy": policy,
                        "artifact_sha256": artifact_sha,
                    }

            # The same untouched fold test interval/cost model is used for each model and baseline.
            ml_backtests: dict[str, Any] = {}
            for model_name, item in fitted_for_backtest.items():
                result = run_backtest(
                    dataset,
                    RegimeLongFlatStrategy(
                        item["wrapper"],
                        __import__("decimal").Decimal(str(options.confidence_threshold)),
                        dataset.version,
                    ),
                    backtest_config,
                    start_index=fold.test.start,
                    end_index=fold.test.end,
                    warmup_start_index=0,
                    segment_name=f"walk-forward-fold-{fold.fold_index}",
                )
                report_path = backtest_store.save(result)
                ml_backtests[model_name] = {
                    "run_id": result.run_id,
                    "report_path": str(report_path.relative_to(output)),
                    "model_artifact_sha256": item["artifact_sha256"],
                    "metrics": _backtest_metrics(
                        result.metrics,
                        volume_capacity_constraint_enabled=(
                            backtest_config.enforce_volume_participation
                        ),
                    ),
                }
            baselines: dict[str, Any] = {}
            for strategy_type in baseline_strategy_types:
                strategy = (
                    strategy_type(fast_window=12, slow_window=48)
                    if strategy_type is SmaCrossoverBaseline
                    else strategy_type()
                )
                result = run_backtest(
                    dataset,
                    strategy,
                    backtest_config,
                    start_index=fold.test.start,
                    end_index=fold.test.end,
                    warmup_start_index=0,
                    segment_name=f"walk-forward-fold-{fold.fold_index}",
                )
                report_path = backtest_store.save(result)
                baselines[strategy.strategy_id] = {
                    "run_id": result.run_id,
                    "report_path": str(report_path.relative_to(output)),
                    "metrics": _backtest_metrics(result.metrics),
                }
            fold_reports.append(
                {
                    "fold_index": fold.fold_index,
                    "hmm_converged": hmm_converged,
                    "hmm_iterations": hmm_iterations,
                    "ranges": {
                        "train": {"start": fold.train.start, "end": fold.train.end},
                        "validation": {
                            "start": fold.validation.start,
                            "end": fold.validation.end,
                        },
                        "test": {"start": fold.test.start, "end": fold.test.end},
                    },
                    "label_purging": {
                        "horizon_bars": horizon,
                        "train_labels_crossing_boundary_removed": True,
                        "validation_labels_crossing_boundary_removed": True,
                        "test_labels_crossing_boundary_removed": True,
                        "feature_inputs_are_trailing_completed_bars": True,
                    },
                    "label_policies": policy_report,
                    "triple_barrier_holdout_backtests": ml_backtests,
                    "same_fold_baseline_backtests": baselines,
                }
            )
    summaries: dict[str, Any] = {}
    for key, values in aggregate_source.items():
        summaries[key] = {}
        for metric in (
            "accuracy",
            "balanced_accuracy",
            "macro_f1",
            "multiclass_brier_score",
            "log_loss",
        ):
            numbers = [float(item[metric]) for item in values]
            summaries[key][metric] = {
                "fold_count": len(numbers),
                "mean": sum(numbers) / len(numbers),
                "population_stddev": math.sqrt(
                    sum((number - (sum(numbers) / len(numbers))) ** 2 for number in numbers)
                    / len(numbers)
                ),
                "minimum": min(numbers),
                "maximum": max(numbers),
                "per_fold": [
                    {"fold_index": item["fold_index"], "value": item[metric]}
                    for item in values
                ],
            }
    report = {
        "schema_version": 1,
        "experiment_id": hashlib.sha256(
            f"{dataset.version}|{options!r}|walk-forward-v1".encode()
        ).hexdigest(),
        "dataset": {
            "dataset_id": dataset.dataset_id,
            "dataset_version": dataset.version,
            "candles_sha256": hashlib.sha256(dataset.candles_csv_bytes()).hexdigest(),
            "candle_count": len(candles),
            "volume_unit": dataset.volume_unit,
            "volume_semantics_status": (
                "UNVERIFIED_NOT_USED_IN_FEATURES" if dataset.volume_unit == "unknown"
                else "DECLARED_NOT_INDEPENDENTLY_VERIFIED"
            ),
            "gap_count": dataset.gap_count,
        },
        "protocol": {
            "mode": "rolling_fixed_window_walk_forward",
            "feature_schema_version": extractor.feature_schema_version,
            "feature_lookback": options.feature_lookback,
            "label_horizon_bars": options.horizon_bars,
            "train_bars": options.train_bars,
            "validation_bars": options.validation_bars,
            "test_bars": options.test_bars,
            "step_bars": options.step_bars,
            "fold_count": len(folds),
            "label_purging": "drop samples whose forward label crosses each partition end",
            "calibration": "temperature fitted on that fold's validation only",
            "models": list(_MODELS),
            "label_policies": list(_POLICIES),
            "hmm_converged_per_fold": [item.get("hmm_converged") for item in fold_reports],
            "test_fold_tuning": False,
            "volume_feature_enabled": False,
            "volume_capacity_status": (
                "UNCONSTRAINED_UNKNOWN_VOLUME_UNIT"
                if not backtest_config.enforce_volume_participation
                else "ENFORCED_BASE_ASSET"
            ),
        },
        "folds": fold_reports,
        "per_fold_metric_summary": summaries,
        "release_disposition": "RESEARCH_ONLY_NOT_APPROVED_FOR_LIVE_TRADING",
        "limitations": [
            "Targets remain weak labels, not human-verified ground truth.",
            "No fold test values are used for fitting, label thresholds, model choice, "
            "or temperature calibration.",
            "The HMM uses forward-filtered posteriors; it does not use "
            "backward-smoothed test states.",
            "UDF volume units are undocumented; volume is excluded from features "
            "and capacity limits are disabled.",
            "With capacity limits disabled, the portfolio simulation does not establish "
            "executable liquidity or capacity.",
            "Historical spread/depth are unavailable from OHLCV; spread and slippage "
            "remain assumptions.",
            "Fold scores are reported individually and as descriptive mean/std/range; "
            "no best-fold selection is performed.",
            "Results are research-only and do not establish future profitability "
            "or live readiness.",
        ],
    }
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "walk-forward-report.json", report)
    return report


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def _backtest_metrics(metrics: Any, *, volume_capacity_constraint_enabled: bool) -> dict[str, Any]:
    return {
        "initial_equity": str(metrics.initial_equity),
        "final_equity": str(metrics.final_equity),
        "net_profit": str(metrics.net_profit),
        "net_return_fraction": str(metrics.net_return_fraction),
        "max_drawdown_fraction": str(metrics.max_drawdown_fraction),
        "closed_trade_count": metrics.closed_trade_count,
        "commission_paid": str(metrics.commission_paid),
        "estimated_spread_cost": str(metrics.estimated_spread_cost),
        "estimated_slippage_cost": str(metrics.estimated_slippage_cost),
        "fill_count": metrics.fill_count,
        "time_in_market_fraction": str(metrics.time_in_market_fraction),
        "open_position_quantity": str(metrics.open_position_quantity),
        "volume_capacity_constraint_enabled": volume_capacity_constraint_enabled,
    }


def _write_json(path: Path, value: object) -> None:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload + b"\n")
