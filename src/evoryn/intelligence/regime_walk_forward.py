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
from evoryn.intelligence.regime import RegimeLabel
from evoryn.intelligence.regime_research_models import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    SEQUENCE_LENGTH,
    VOLATILITY_FEATURE_INDEX,
    EstimatorAdapter,
    HMMAdapter,
    LSTMAdapter,
    LongFlatResearchStrategy,
    bootstrap_mean_interval,
    fit_lstm,
    lstm_probabilities,
    research_feature_vector,
    sequence_matrix,
)
from evoryn.intelligence.regime_training import (
    BuyAndHoldStrategy,
    _classification_metrics,
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
_MODELS = ("random_forest", "hist_gradient_boosting", "xgboost", "gaussian_hmm", "lstm")


@dataclass(frozen=True, slots=True)
class WalkForwardResearchConfig:
    """Fixed rolling windows and thresholds; no parameter search on fold test blocks."""

    feature_lookback: int = 48
    horizon_bars: int = 6
    train_bars: int = 1728
    validation_bars: int = 432
    test_bars: int = 216
    step_bars: int = 216
    lstm_epochs: int = 18
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
        if self.feature_lookback < 48 or self.horizon_bars < 1:
            raise ValueError("Feature lookback must be >= 48 and horizon must be positive.")
        if type(self.lstm_epochs) is not int or not 1 <= self.lstm_epochs <= 100:
            raise ValueError("lstm_epochs must be an integer in [1, 100].")
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


def run_walk_forward_research(
    dataset: HistoricalDataset,
    *,
    output_dir: str | Path,
    config: WalkForwardResearchConfig | None = None,
) -> dict[str, Any]:
    """Run price-only technical-feature models on purged, non-overlapping walk-forward folds."""
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
    if len(folds) < 10:
        raise ValueError(
            f"At least ten non-overlapping test folds are required; got {len(folds)}."
        )
    feature_by_index: dict[int, tuple[float, ...]] = {
        index: research_feature_vector(candles, index, lookback=options.feature_lookback)
        for index in range(options.feature_lookback, len(candles))
    }
    output = Path(output_dir).expanduser().resolve()
    model_dir = output / "models"
    backtest_store = BacktestResultStore(output / "backtests")
    model_dir.mkdir(parents=True, exist_ok=True)
    numpy = importlib.import_module("numpy")
    sklearn_ensemble = importlib.import_module("sklearn.ensemble")
    sklearn_preprocessing = importlib.import_module("sklearn.preprocessing")
    sklearn_hmm = importlib.import_module("hmmlearn.hmm")
    xgboost = importlib.import_module("xgboost")
    joblib = importlib.import_module("joblib")
    fold_reports: list[dict[str, Any]] = []
    aggregate_source: dict[str, list[dict[str, Any]]] = {}
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
    for fold in folds:
        horizon = options.horizon_bars
        first_train_sample = max(
            fold.train.start + options.feature_lookback + SEQUENCE_LENGTH - 1,
            options.feature_lookback + SEQUENCE_LENGTH - 1,
        )
        train_indices = list(range(first_train_sample, fold.train.end - horizon))
        validation_indices = list(range(fold.validation.start, fold.validation.end - horizon))
        test_indices = list(range(fold.test.start, fold.test.end - horizon))
        if min(len(train_indices), len(validation_indices), len(test_indices)) < 30:
            raise ValueError(f"Fold {fold.fold_index} is too small after label purging.")
        indices = (*train_indices, *validation_indices, *test_indices)
        outcomes = {index: forward_outcome(candles, index, horizon) for index in indices}
        absolute_returns = [abs(outcomes[index].forward_return) for index in train_indices]
        forward_volatility = [
            outcomes[index].forward_volatility for index in train_indices
        ]
        trend_threshold = max(
            linear_quantile(absolute_returns, options.trend_quantile), 0.001
        )
        volatility_threshold = linear_quantile(
            forward_volatility, options.volatility_quantile
        )

        train_matrix = numpy.asarray(
            [feature_by_index[index] for index in train_indices], dtype="float64"
        )
        scaler = sklearn_preprocessing.StandardScaler()
        scaler.fit(train_matrix)
        all_feature_indices = sorted(feature_by_index)
        all_features = numpy.asarray(
            [feature_by_index[index] for index in all_feature_indices], dtype="float64"
        )
        scaled_all = scaler.transform(all_features)
        scaled_feature_by_index = {
            index: scaled_all[offset]
            for offset, index in enumerate(all_feature_indices)
        }
        scaled_train = numpy.asarray(
            [scaled_feature_by_index[index] for index in train_indices],
            dtype="float64",
        )

        train_sequences = sequence_matrix(
            scaled_feature_by_index, train_indices, SEQUENCE_LENGTH, numpy
        )
        validation_sequences = sequence_matrix(
            scaled_feature_by_index, validation_indices, SEQUENCE_LENGTH, numpy
        )
        test_sequences = sequence_matrix(
            scaled_feature_by_index, test_indices, SEQUENCE_LENGTH, numpy
        )

        hmm_model: Any | None = None
        hmm_fit_error: str | None = None
        hmm_converged = False
        hmm_iterations = 0
        filtered_history: Any | None = None
        train_posteriors: Any | None = None
        validation_posteriors: Any | None = None
        test_posteriors: Any | None = None
        try:
            hmm_model = sklearn_hmm.GaussianHMM(
                n_components=4,
                covariance_type="diag",
                n_iter=160,
                tol=0.01,
                min_covar=1e-4,
                random_state=options.random_seed + fold.fold_index,
                implementation="log",
            )
            hmm_model.fit(scaled_train)
            hmm_converged = bool(hmm_model.monitor_.converged)
            hmm_iterations = int(hmm_model.monitor_.iter)
            if hmm_converged:
                hmm_indices = list(range(train_indices[0], test_indices[-1] + 1))
                hmm_input = numpy.asarray(
                    [scaled_feature_by_index[index] for index in hmm_indices],
                    dtype="float64",
                )
                filtered_history = filtered_gaussian_hmm_probabilities(
                    hmm_model, hmm_input
                )
                train_posteriors = filtered_history[
                    [index - hmm_indices[0] for index in train_indices]
                ]
                validation_posteriors = filtered_history[
                    [index - hmm_indices[0] for index in validation_indices]
                ]
                test_posteriors = filtered_history[
                    [index - hmm_indices[0] for index in test_indices]
                ]
            else:
                hmm_fit_error = "HMM_DID_NOT_CONVERGE"
        except Exception as exc:
            hmm_model = None
            hmm_fit_error = f"HMM_FIT_FAILED:{type(exc).__name__}"

        fold_policy_reports: dict[str, dict[str, Any]] = {}
        fold_backtests: dict[str, Any] = {}
        for policy in _POLICIES:
            if policy == "forward_thresholds":
                labels = {
                    index: assign_forward_regime(
                        outcomes[index],
                        trend_threshold=trend_threshold,
                        volatility_threshold=volatility_threshold,
                    )
                    for index in indices
                }
                policy_parameters: dict[str, Any] = {
                    "method": "train-quantile forward-return and volatility thresholds",
                    "trend_threshold_abs_return": trend_threshold,
                    "high_forward_volatility_threshold": volatility_threshold,
                    "threshold_fit_scope": "training_partition_only",
                }
            else:
                labels = {
                    index: volatility_scaled_triple_barrier_label(
                        candles,
                        index,
                        trailing_volatility=feature_by_index[index][
                            VOLATILITY_FEATURE_INDEX
                        ],
                        horizon_bars=horizon,
                        barrier_multiplier=options.triple_barrier_multiplier,
                        high_volatility_threshold=volatility_threshold,
                    )
                    for index in indices
                }
                policy_parameters = {
                    "method": "volatility-scaled OHLC first-touch triple barrier",
                    "barrier": "symmetric_volatility_scaled",
                    "barrier_multiplier": options.triple_barrier_multiplier,
                    "volatility_feature": FEATURE_NAMES[VOLATILITY_FEATURE_INDEX],
                    "barrier_floor_abs_return": 0.001,
                    "high_forward_volatility_threshold": volatility_threshold,
                    "threshold_fit_scope": "training_partition_only",
                    "same_bar_double_touch": "high_volatility_conservative_label",
                }
            y_train_labels = [labels[index].value for index in train_indices]
            train_counts = Counter(y_train_labels)
            if set(train_counts) != {label.value for label in _LABELS} or any(
                train_counts[label.value] < options.min_class_samples
                for label in _LABELS
            ):
                raise ValueError(
                    f"Fold {fold.fold_index} / {policy} has insufficient class support; "
                    "the fold is not silently relabeled or dropped."
                )
            y_validation = [labels[index].value for index in validation_indices]
            y_test = [labels[index].value for index in test_indices]
            x_train = train_matrix
            x_validation = numpy.asarray(
                [feature_by_index[index] for index in validation_indices], dtype="float64"
            )
            x_test = numpy.asarray(
                [feature_by_index[index] for index in test_indices], dtype="float64"
            )
            policy_report: dict[str, Any] = {
                "parameters": policy_parameters,
                "train_class_counts": dict(sorted(train_counts.items())),
                "validation_class_counts": dict(
                    sorted(Counter(y_validation).items())
                ),
                "test_class_counts": dict(sorted(Counter(y_test).items())),
                "train_sample_count": len(train_indices),
                "validation_sample_count": len(validation_indices),
                "test_sample_count": len(test_indices),
                "feature_names": list(FEATURE_NAMES),
                "features_exclude_unverified_volume": True,
                "models": {},
                "backtests": {},
            }
            majority_label = Counter(y_train_labels).most_common(1)[0][0]
            majority_probabilities = [
                [1.0 if label.value == majority_label else 0.0 for label in _LABELS]
                for _ in y_test
            ]
            majority_metrics = _classification_metrics(
                y_test, majority_probabilities, _LABELS, temperature=1.0
            )
            majority_metrics.update(
                {
                    "baseline_type": "training_partition_majority_class",
                    "constant_label": majority_label,
                }
            )
            fitted_for_backtest: dict[str, dict[str, Any]] = {}

            for model_name in _MODELS:
                wrapper: Any
                payload: dict[str, Any]
                if model_name == "random_forest":
                    estimator = sklearn_ensemble.RandomForestClassifier(
                        n_estimators=220,
                        max_depth=7,
                        min_samples_leaf=5,
                        class_weight="balanced_subsample",
                        random_state=options.random_seed + fold.fold_index,
                        n_jobs=1,
                    )
                    estimator.fit(x_train, numpy.asarray(y_train_labels, dtype=str))
                    class_order = tuple(
                        RegimeLabel(str(value)) for value in estimator.classes_
                    )
                    if set(class_order) != set(_LABELS):
                        raise ValueError("Random Forest did not retain all four classes.")
                    raw_validation = estimator.predict_proba(x_validation).tolist()
                    raw_test = estimator.predict_proba(x_test).tolist()
                    wrapper = EstimatorAdapter(
                        estimator,
                        class_order=class_order,
                        temperature=1.0,
                        lookback=options.feature_lookback,
                        model_name=model_name,
                        fingerprint="pending",
                    )
                    payload = {
                        "estimator": estimator,
                        "class_order": [label.value for label in class_order],
                    }
                elif model_name == "hist_gradient_boosting":
                    estimator = sklearn_ensemble.HistGradientBoostingClassifier(
                        max_iter=160,
                        max_leaf_nodes=15,
                        l2_regularization=1.0,
                        learning_rate=0.045,
                        random_state=options.random_seed + fold.fold_index,
                    )
                    estimator.fit(x_train, numpy.asarray(y_train_labels, dtype=str))
                    class_order = tuple(
                        RegimeLabel(str(value)) for value in estimator.classes_
                    )
                    if set(class_order) != set(_LABELS):
                        raise ValueError("HistGradientBoosting did not retain all classes.")
                    raw_validation = estimator.predict_proba(x_validation).tolist()
                    raw_test = estimator.predict_proba(x_test).tolist()
                    wrapper = EstimatorAdapter(
                        estimator,
                        class_order=class_order,
                        temperature=1.0,
                        lookback=options.feature_lookback,
                        model_name=model_name,
                        fingerprint="pending",
                    )
                    payload = {
                        "estimator": estimator,
                        "class_order": [label.value for label in class_order],
                    }
                elif model_name == "xgboost":
                    estimator = xgboost.XGBClassifier(
                        objective="multi:softprob",
                        num_class=len(_LABELS),
                        eval_metric="mlogloss",
                        n_estimators=220,
                        max_depth=4,
                        learning_rate=0.04,
                        subsample=0.85,
                        colsample_bytree=0.85,
                        reg_lambda=2.0,
                        tree_method="hist",
                        n_jobs=1,
                        random_state=options.random_seed + fold.fold_index,
                    )
                    class_index_by_value = {label.value: index for index, label in enumerate(_LABELS)}
                    y_train_encoded = numpy.asarray(
                        [class_index_by_value[value] for value in y_train_labels], dtype="int64"
                    )
                    estimator.fit(x_train, y_train_encoded)
                    class_order = _LABELS
                    raw_validation = estimator.predict_proba(x_validation).tolist()
                    raw_test = estimator.predict_proba(x_test).tolist()
                    wrapper = EstimatorAdapter(
                        estimator,
                        class_order=class_order,
                        temperature=1.0,
                        lookback=options.feature_lookback,
                        model_name=model_name,
                        fingerprint="pending",
                    )
                    payload = {
                        "estimator": estimator,
                        "class_order": [label.value for label in class_order],
                        "label_encoding": {
                            str(index): label.value
                            for index, label in enumerate(_LABELS)
                        },
                    }
                elif model_name == "lstm":
                    label_index = {label.value: index for index, label in enumerate(_LABELS)}
                    y_train_encoded = numpy.asarray(
                        [label_index[value] for value in y_train_labels], dtype="int64"
                    )
                    estimator, torch = fit_lstm(
                        train_sequences,
                        y_train_encoded,
                        random_seed=options.random_seed + fold.fold_index,
                        epochs=options.lstm_epochs,
                    )
                    raw_validation = lstm_probabilities(
                        estimator, torch, validation_sequences, numpy
                    ).tolist()
                    raw_test = lstm_probabilities(
                        estimator, torch, test_sequences, numpy
                    ).tolist()
                    class_order = _LABELS
                    wrapper = LSTMAdapter(
                        estimator,
                        torch,
                        scaler,
                        lookback=options.feature_lookback,
                        sequence_length=SEQUENCE_LENGTH,
                        temperature=1.0,
                        fingerprint="pending",
                    )
                    payload = {
                        "model_family": "compact_torch_lstm",
                        "state_dict_numpy": {
                            name: tensor.detach().cpu().numpy()
                            for name, tensor in estimator.state_dict().items()
                        },
                        "input_feature_count": len(FEATURE_NAMES),
                        "sequence_length": SEQUENCE_LENGTH,
                        "hidden_size": 32,
                        "epochs": options.lstm_epochs,
                        "scaler_mean": scaler.mean_.tolist(),
                        "scaler_scale": scaler.scale_.tolist(),
                        "class_order": [label.value for label in _LABELS],
                    }
                else:
                    if not hmm_converged or hmm_model is None:
                        policy_report["models"][model_name] = {
                            "status": hmm_fit_error or "HMM_NOT_CONVERGED",
                            "fold_converged": False,
                            "test_metrics": None,
                        }
                        continue
                    if (
                        train_posteriors is None
                        or validation_posteriors is None
                        or test_posteriors is None
                    ):
                        raise ValueError("Converged HMM is missing fold posterior arrays.")
                    state_map = numpy.ones((len(_LABELS), len(_LABELS)), dtype="float64")
                    state_ids = numpy.argmax(train_posteriors, axis=1)
                    for state in range(len(_LABELS)):
                        observed = [
                            labels[index].value
                            for index, state_id in zip(train_indices, state_ids, strict=True)
                            if int(state_id) == state
                        ]
                        observed_counts = Counter(observed)
                        denominator = len(observed) + len(_LABELS)
                        for label_index, label in enumerate(_LABELS):
                            state_map[state, label_index] = (
                                observed_counts[label.value] + 1
                            ) / denominator
                    raw_validation = (validation_posteriors @ state_map).tolist()
                    raw_test = (test_posteriors @ state_map).tolist()
                    class_order = _LABELS
                    wrapper = HMMAdapter(
                        hmm_model,
                        scaler,
                        state_map,
                        lookback=options.feature_lookback,
                        first_index=train_indices[0],
                        temperature=1.0,
                        fingerprint="pending",
                    )
                    payload = {
                        "hmm": hmm_model,
                        "scaler": scaler,
                        "state_to_label_probabilities": state_map.tolist(),
                        "class_order": [label.value for label in _LABELS],
                    }

                temperature = _fit_temperature(raw_validation, y_validation, class_order)
                wrapper.temperature = temperature
                validation_metrics = _classification_metrics(
                    y_validation, raw_validation, class_order, temperature=temperature
                )
                test_metrics = _classification_metrics(
                    y_test, raw_test, class_order, temperature=temperature
                )
                model_identity_input = json.dumps(
                    {
                        "dataset_version": dataset.version,
                        "fold": fold.fold_index,
                        "policy": policy,
                        "model": model_name,
                        "train_range": [fold.train.start, fold.train.end],
                        "validation_range": [fold.validation.start, fold.validation.end],
                        "test_range": [fold.test.start, fold.test.end],
                        "feature_schema": FEATURE_SCHEMA_VERSION,
                        "feature_names": FEATURE_NAMES,
                        "temperature": temperature,
                        "seed": options.random_seed + fold.fold_index,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                identity = hashlib.sha256(model_identity_input).hexdigest()
                wrapper.model_fingerprint = identity
                wrapper.model_version = identity[:16]
                payload.update(
                    {
                        "schema_version": 2,
                        "model_family": model_name,
                        "temperature": temperature,
                        "dataset_version": dataset.version,
                        "fold_index": fold.fold_index,
                        "label_policy": policy,
                        "train_range": {"start": fold.train.start, "end": fold.train.end},
                        "validation_range": {
                            "start": fold.validation.start,
                            "end": fold.validation.end,
                        },
                        "test_range": {"start": fold.test.start, "end": fold.test.end},
                        "feature_schema_version": FEATURE_SCHEMA_VERSION,
                        "feature_names": list(FEATURE_NAMES),
                        "feature_lookback": options.feature_lookback,
                    }
                )
                artifact_path = model_dir / (
                    f"fold-{fold.fold_index}-{policy}-{model_name}.joblib"
                )
                joblib.dump(payload, artifact_path, compress=3)
                artifact_sha = _sha256_file(artifact_path)
                model_record = {
                    "status": "EVALUATED",
                    "artifact_path": str(artifact_path.relative_to(output)),
                    "artifact_sha256": artifact_sha,
                    "model_identity_sha256": identity,
                    "model_version": identity[:16],
                    "temperature_fitted_on": "validation_only",
                    "validation_metrics": validation_metrics,
                    "test_metrics": test_metrics,
                    "training_only_majority_baseline": majority_metrics,
                }
                policy_report["models"][model_name] = model_record
                aggregate_source.setdefault(f"{policy}/{model_name}", []).append(
                    {
                        "fold_index": fold.fold_index,
                        **test_metrics,
                        "majority_accuracy": majority_metrics["accuracy"],
                        "majority_balanced_accuracy": majority_metrics["balanced_accuracy"],
                        "majority_macro_f1": majority_metrics["macro_f1"],
                    }
                )
                if policy == "volatility_scaled_triple_barrier":
                    fitted_for_backtest[model_name] = {
                        "wrapper": wrapper,
                        "artifact_sha256": artifact_sha,
                    }

            if policy == "volatility_scaled_triple_barrier":
                for model_name, item in fitted_for_backtest.items():
                    result = run_backtest(
                        dataset,
                        LongFlatResearchStrategy(
                            item["wrapper"], options.confidence_threshold
                        ),
                        backtest_config,
                        start_index=fold.test.start,
                        end_index=fold.test.end,
                        warmup_start_index=0,
                        segment_name=f"walk-forward-fold-{fold.fold_index}",
                    )
                    path = backtest_store.save(result)
                    policy_report["backtests"][model_name] = {
                        "run_id": result.run_id,
                        "report_path": str(path.relative_to(output)),
                        "model_artifact_sha256": item["artifact_sha256"],
                        "metrics": _backtest_metrics(
                            result.metrics,
                            volume_capacity_constraint_enabled=(
                                backtest_config.enforce_volume_participation
                            ),
                        ),
                    }
            fold_policy_reports[policy] = policy_report
            if policy == "volatility_scaled_triple_barrier":
                fold_backtests = policy_report["backtests"]

        baselines: dict[str, Any] = {}
        baseline_strategies = (
            (SmaCrossoverBaseline, "sma-crossover-long-flat"),
            (BuyAndHoldStrategy, "buy-and-hold-reference"),
        )
        for strategy_type, strategy_name in baseline_strategies:
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
            path = backtest_store.save(result)
            baselines[strategy_name] = {
                "run_id": result.run_id,
                "report_path": str(path.relative_to(output)),
                "metrics": _backtest_metrics(
                    result.metrics,
                    volume_capacity_constraint_enabled=(
                        backtest_config.enforce_volume_participation
                    ),
                ),
            }
        fold_reports.append(
            {
                "fold_index": fold.fold_index,
                "hmm_converged": hmm_converged,
                "hmm_iterations": hmm_iterations,
                "hmm_failure_reason": hmm_fit_error,
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
                    "features_use_only_trailing_completed_bars": True,
                },
                "label_policies": fold_policy_reports,
                "triple_barrier_holdout_backtests": fold_backtests,
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
        if values:
            for metric in ("accuracy", "balanced_accuracy", "macro_f1"):
                difference_name = f"{metric}_difference_vs_majority"
                differences = [
                    float(item[metric]) - float(item[f"majority_{metric}"])
                    for item in values
                ]
                summaries[key][difference_name] = bootstrap_mean_interval(
                    differences, random_seed=options.random_seed + len(key)
                )

    candidate_gates: dict[str, Any] = {}
    triple_policy = "volatility_scaled_triple_barrier"
    for model_name in _MODELS:
        accuracy_differences: list[float] = []
        balanced_differences: list[float] = []
        f1_differences: list[float] = []
        return_differences: list[float] = []
        trade_count = 0
        evaluated_folds = 0
        for fold_report in fold_reports:
            model_report = (
                fold_report["label_policies"][triple_policy]["models"].get(model_name)
            )
            model_backtest = fold_report["triple_barrier_holdout_backtests"].get(model_name)
            if (
                not isinstance(model_report, dict)
                or model_report.get("status") != "EVALUATED"
                or not isinstance(model_backtest, dict)
            ):
                continue
            metrics = model_report["test_metrics"]
            majority = model_report["training_only_majority_baseline"]
            accuracy_differences.append(
                float(metrics["accuracy"]) - float(majority["accuracy"])
            )
            balanced_differences.append(
                float(metrics["balanced_accuracy"])
                - float(majority["balanced_accuracy"])
            )
            f1_differences.append(float(metrics["macro_f1"]) - float(majority["macro_f1"]))
            model_return = float(model_backtest["metrics"]["net_return_fraction"])
            hold_return = float(
                fold_report["same_fold_baseline_backtests"]["buy-and-hold-reference"][
                    "metrics"
                ]["net_return_fraction"]
            )
            return_differences.append(model_return - hold_return)
            trade_count += int(model_backtest["metrics"]["closed_trade_count"])
            evaluated_folds += 1
        if not evaluated_folds:
            candidate_gates[model_name] = {
                "status": "NOT_EVALUATED",
                "evaluated_folds": 0,
                "runtime_promotion_allowed": False,
            }
            continue
        acc_ci = bootstrap_mean_interval(
            accuracy_differences, random_seed=options.random_seed + 101
        )
        balanced_ci = bootstrap_mean_interval(
            balanced_differences, random_seed=options.random_seed + 102
        )
        f1_ci = bootstrap_mean_interval(f1_differences, random_seed=options.random_seed + 103)
        return_ci = bootstrap_mean_interval(
            return_differences, random_seed=options.random_seed + 104
        )
        classification_gate = (
            acc_ci["ci95_lower"] > 0
            and balanced_ci["ci95_lower"] > 0
            and f1_ci["ci95_lower"] > 0
        )
        economic_gate = return_ci["ci95_lower"] > 0 and trade_count >= 20
        all_folds_gate = evaluated_folds == len(folds) and len(folds) >= 10
        candidate_gates[model_name] = {
            "status": "EVALUATED" if all_folds_gate else "INCOMPLETE_FOLD_COVERAGE",
            "evaluated_folds": evaluated_folds,
            "closed_trades_total": trade_count,
            "accuracy_difference_vs_majority": acc_ci,
            "balanced_accuracy_difference_vs_majority": balanced_ci,
            "macro_f1_difference_vs_majority": f1_ci,
            "net_return_difference_vs_buy_and_hold": return_ci,
            "classification_gate_passed": classification_gate,
            "economic_gate_passed": economic_gate,
            "all_folds_gate_passed": all_folds_gate,
            "empirical_candidate_gate_passed": (
                classification_gate and economic_gate and all_folds_gate
            ),
            "runtime_promotion_allowed": False,
            "runtime_promotion_blockers": [
                "Model selection was performed on these same walk-forward windows; an untouched "
                "confirmation period is required.",
                "The new research feature schema has no independently pinned runtime artifact "
                "loader or production-approved manifest.",
                "UDF volume semantics and provider data-use rights remain unresolved.",
                "Windows Task Scheduler registration and native-host acceptance remain open.",
            ],
        }
    report = {
        "schema_version": 2,
        "experiment_id": hashlib.sha256(
            f"{dataset.version}|{options!r}|walk-forward-v2".encode()
        ).hexdigest(),
        "dataset": {
            "dataset_id": dataset.dataset_id,
            "dataset_version": dataset.version,
            "candles_sha256": hashlib.sha256(dataset.candles_csv_bytes()).hexdigest(),
            "candle_count": len(candles),
            "volume_unit": dataset.volume_unit,
            "volume_semantics_status": (
                "UNVERIFIED_NOT_USED_IN_FEATURES"
                if dataset.volume_unit == "unknown"
                else "DECLARED_NOT_INDEPENDENTLY_VERIFIED"
            ),
            "gap_count": dataset.gap_count,
        },
        "protocol": {
            "mode": "rolling_fixed_window_walk_forward",
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "feature_names": list(FEATURE_NAMES),
            "feature_count": len(FEATURE_NAMES),
            "feature_lookback": options.feature_lookback,
            "sequence_model": "one-layer CPU LSTM",
            "sequence_length": SEQUENCE_LENGTH,
            "lstm_epochs": options.lstm_epochs,
            "volume_feature_enabled": False,
            "label_horizon_bars": options.horizon_bars,
            "train_bars": options.train_bars,
            "validation_bars": options.validation_bars,
            "test_bars": options.test_bars,
            "step_bars": options.step_bars,
            "fold_count": len(folds),
            "label_purging": "drop samples whose forward label crosses each partition end",
            "calibration": "temperature fitted on each fold's validation only",
            "models": list(_MODELS),
            "label_policies": list(_POLICIES),
            "hmm_converged_per_fold": [
                item["hmm_converged"] for item in fold_reports
            ],
            "test_fold_tuning": False,
            "volume_capacity_status": (
                "UNCONSTRAINED_UNKNOWN_VOLUME_UNIT"
                if not backtest_config.enforce_volume_participation
                else "ENFORCED_BASE_ASSET"
            ),
            "backtest_costs_bps": {
                "commission": 10.0,
                "spread": 10.0,
                "slippage": 10.0,
            },
        },
        "folds": fold_reports,
        "per_fold_metric_summary": summaries,
        "candidate_gates": candidate_gates,
        "model_selection": {
            "selected_model_for_runtime": None,
            "reason": (
                "Walk-forward results are challenger evidence only. Even if a candidate "
                "passes the descriptive gates, it must be confirmed on a fresh untouched "
                "period before any runtime model promotion."
            ),
        },
        "release_disposition": "RESEARCH_ONLY_NOT_APPROVED_FOR_LIVE_TRADING",
        "limitations": [
            "Targets are weak future-outcome labels, not independently verified ground truth.",
            "All technical features use trailing completed OHLC bars; volume is excluded.",
            "XGBoost and LSTM are optional research dependencies. Runtime does not auto-load them.",
            "No fold test values are used for fitting, label thresholds, model selection within "
            "or temperature calibration.",
            "HMM posterior probabilities use forward filtering, never backward smoothing.",
            "If a fold's HMM does not converge, that model/fold is recorded as not evaluated.",
            "OHLC does not reveal the order of high/low touches within a candle; these labels "
            "are conservatively set to high_volatility.",
            "Paired-fold bootstrap intervals are descriptive; adjacent folds may be dependent.",
            "Disabling unknown-unit volume caps means backtests do not establish executable " +
            "liquidity.",
            "Historical spread/depth and provider data-use rights remain unresolved.",
            "All model choices require an untouched confirmation period and independent review.",
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
