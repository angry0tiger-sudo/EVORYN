"""Research-only causal features and cost-aware SMA meta-label primitives.

No exchange clients, execution ports, or runtime-model promotion are present here.
Feature selection and tuning must run inside each walk-forward training/validation fold.
"""
from __future__ import annotations

import importlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from evoryn.domain.models import MarketCandle

ADVANCED_FEATURE_SCHEMA_VERSION = "1.0.0-mtf-fibonacci-swing-proxy"
_META_MODELS = ("xgboost", "lightgbm")
_FEATURE_NAMES = (
    "return_1", "return_6", "return_24",
    "realized_volatility_6", "realized_volatility_24", "realized_volatility_48",
    "bollinger_percent_b_20", "bollinger_bandwidth_20", "atr_14_pct",
    "fib_distance_236_48", "fib_distance_382_48", "fib_distance_500_48",
    "fib_distance_618_48", "fib_distance_786_48",
    "confirmed_swing_high_count_48", "confirmed_swing_low_count_48",
    "last_swing_direction", "last_swing_amplitude_pct",
    "bars_since_last_swing_fraction",
    "mtf_4h_return_1", "mtf_4h_return_6", "mtf_4h_sma_gap_6",
    "mtf_1d_return_1", "mtf_1d_return_7", "mtf_1d_sma_gap_5",
    "mtf_4h_available", "mtf_1d_available",
)
FIBONACCI_LEVELS = (0.236, 0.382, 0.5, 0.618, 0.786)


@dataclass(frozen=True, slots=True)
class MetaLabelEvent:
    """One primary SMA long-entry event and its cost-aware, forward outcome label."""

    signal_index: int
    entry_index: int
    exit_index: int
    gross_return: float
    net_return_after_costs: float
    round_trip_cost_bps: float
    label: int

    def __post_init__(self) -> None:
        if not (0 <= self.signal_index < self.entry_index <= self.exit_index):
            raise ValueError("Meta-label event indices must be ordered.")
        if self.label not in (0, 1):
            raise ValueError("Meta-label target must be binary.")
        if not all(math.isfinite(value) for value in (
            self.gross_return, self.net_return_after_costs, self.round_trip_cost_bps
        )):
            raise ValueError("Meta-label outcome values must be finite.")


@dataclass(frozen=True, slots=True)
class MetaLabelTuningResult:
    """Validation-only Optuna result; it does not imply test or economic acceptance."""

    model_name: str
    best_params: dict[str, int | float]
    best_validation_average_precision: float
    completed_trials: int
    random_seed: int


def advanced_feature_names() -> tuple[str, ...]:
    """Return the stable, versioned order for advanced features."""
    return _FEATURE_NAMES


def _mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("Cannot calculate the mean of an empty sequence.")
    return sum(values) / len(values)


def _population_std(values: Sequence[float]) -> float:
    center = _mean(values)
    return math.sqrt(_mean(tuple((value - center) ** 2 for value in values)))


def _validate_candles(candles: Sequence[MarketCandle]) -> None:
    if not candles:
        raise ValueError("Advanced feature engineering requires non-empty candles.")
    if any(candle.interval != timedelta(hours=1) for candle in candles):
        raise ValueError("Advanced multi-timeframe features require 1-hour candles.")
    for left, right in zip(candles, candles[1:], strict=False):
        if right.timestamp - left.timestamp != timedelta(hours=1):
            raise ValueError("Advanced feature engineering rejects duplicate or gapped candles.")


def _closed_timeframe_index(
    candles: Sequence[MarketCandle],
    end_index: int,
    timeframe_seconds: int,
    index_by_epoch: dict[int, int],
    grid_offset_seconds: int,
) -> int | None:
    """Find the last constituent hourly close known at end_index, without partial HTF bars."""
    candle_close_epoch = int(candles[end_index].timestamp.timestamp()) + 3600
    last_boundary = (
        (candle_close_epoch - grid_offset_seconds) // timeframe_seconds
    ) * timeframe_seconds + grid_offset_seconds
    final_open_epoch = last_boundary - 3600
    position = index_by_epoch.get(final_open_epoch)
    if position is None or position > end_index:
        return None
    return position


def _higher_timeframe_features(
    candles: Sequence[MarketCandle],
    end_index: int,
    index_by_epoch: dict[int, int],
    grid_offset_seconds: int,
) -> tuple[float, float, float, float, float, float, float, float]:
    """Return 4h/daily returns and SMA gaps from completed bars only, plus availability flags."""
    current_4h = _closed_timeframe_index(
        candles, end_index, 4 * 3600, index_by_epoch, grid_offset_seconds
    )
    current_1d = _closed_timeframe_index(
        candles, end_index, 24 * 3600, index_by_epoch, grid_offset_seconds
    )

    def close_at(index: int) -> float:
        return float(candles[index].close)

    def tf_return(current: int | None, bars_back: int, bars_per_tf: int) -> float:
        if current is None:
            return 0.0
        prior_index = current - bars_back * bars_per_tf
        if prior_index < 0:
            return 0.0
        prior = close_at(prior_index)
        return close_at(current) / prior - 1.0 if prior > 0 else 0.0

    def sma_gap(current: int | None, bars_per_tf: int, window: int) -> float:
        if current is None:
            return 0.0
        indices = [current - offset * bars_per_tf for offset in range(window)]
        if min(indices) < 0:
            return 0.0
        average = _mean([close_at(index) for index in indices])
        return close_at(current) / average - 1.0 if average > 0 else 0.0

    has_4h = current_4h is not None and current_4h >= 5 * 4
    has_1d = current_1d is not None and current_1d >= 7 * 24
    return (
        tf_return(current_4h, 1, 4),
        tf_return(current_4h, 6, 4),
        sma_gap(current_4h, 4, 6),
        tf_return(current_1d, 1, 24),
        tf_return(current_1d, 7, 24),
        sma_gap(current_1d, 24, 5),
        float(has_4h),
        float(has_1d),
    )


def _confirmed_swings(
    highs: Sequence[float],
    lows: Sequence[float],
    start_index: int,
    end_index: int,
    confirmation_bars: int = 2,
) -> list[tuple[int, int, float]]:
    """Return confirmed swing pivots; each pivot uses two bars on its right as confirmation."""
    pivots: list[tuple[int, int, float]] = []
    first = max(start_index + confirmation_bars, start_index + 2)
    last_exclusive = end_index - confirmation_bars + 1
    for index in range(first, last_exclusive):
        left_highs = highs[index - 2 : index]
        right_highs = highs[index + 1 : index + 3]
        left_lows = lows[index - 2 : index]
        right_lows = lows[index + 1 : index + 3]
        is_high = highs[index] > max(left_highs) and highs[index] >= max(right_highs)
        is_low = lows[index] < min(left_lows) and lows[index] <= min(right_lows)
        # An outside bar can qualify as both; omit it rather than inventing an ordering.
        if is_high and not is_low:
            pivots.append((index, 1, highs[index]))
        elif is_low and not is_high:
            pivots.append((index, -1, lows[index]))
    return pivots


def _feature_row(
    candles: Sequence[MarketCandle],
    end_index: int,
    index_by_epoch: dict[int, int],
    grid_offset_seconds: int,
    *,
    lookback: int,
) -> tuple[float, ...]:
    start_index = end_index - lookback + 1
    window = candles[start_index : end_index + 1]
    closes = [float(candle.close) for candle in window]
    highs = [float(candle.high) for candle in window]
    lows = [float(candle.low) for candle in window]
    if len(closes) != lookback or any(
        not math.isfinite(value) or value <= 0 for value in (*closes, *highs, *lows)
    ):
        raise ValueError("Feature window contains invalid OHLC values.")

    returns = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]

    def ret(period: int) -> float:
        return closes[-1] / closes[-1 - period] - 1.0

    vol6 = _population_std(returns[-6:])
    vol24 = _population_std(returns[-24:])
    vol48 = _population_std(returns[-48:])
    bollinger = closes[-20:]
    bb_mean = _mean(bollinger)
    bb_std = _population_std(bollinger)
    bb_percent = (
        (closes[-1] - (bb_mean - 2.0 * bb_std)) / (4.0 * bb_std)
        if bb_std > 1e-15 else 0.5
    )
    bb_bandwidth = 4.0 * bb_std / bb_mean if bb_mean > 0 else 0.0

    atr_ranges: list[float] = []
    for candle_index in range(max(start_index + 1, end_index - 13), end_index + 1):
        current = candles[candle_index]
        previous_close = float(candles[candle_index - 1].close)
        atr_ranges.append(max(
            float(current.high) - float(current.low),
            abs(float(current.high) - previous_close),
            abs(float(current.low) - previous_close),
        ))
    atr_pct = _mean(atr_ranges) / closes[-1] if atr_ranges else 0.0

    high_48, low_48 = max(highs[-48:]), min(lows[-48:])
    price_range = high_48 - low_48
    fib_distances = tuple(
        (closes[-1] - (low_48 + level * price_range)) / closes[-1]
        for level in FIBONACCI_LEVELS
    ) if price_range > 0 else (0.0,) * len(FIBONACCI_LEVELS)

    # These are confirmed swing-shape proxies, not subjective Elliott-wave labels.
    all_highs = [float(c.high) for c in candles]
    all_lows = [float(c.low) for c in candles]
    pivots = _confirmed_swings(
        all_highs, all_lows, max(0, end_index - 47), end_index
    )
    highs_count = sum(direction == 1 for _, direction, _ in pivots)
    lows_count = sum(direction == -1 for _, direction, _ in pivots)
    if len(pivots) >= 2:
        _, last_direction, last_price = pivots[-1]
        _, _, prior_price = pivots[-2]
        swing_amplitude = abs(last_price - prior_price) / closes[-1]
    elif pivots:
        _, last_direction, _ = pivots[-1]
        swing_amplitude = 0.0
    else:
        last_direction, swing_amplitude = 0, 0.0
    bars_since_swing = (end_index - pivots[-1][0] if pivots else lookback) / float(lookback)

    mtf = _higher_timeframe_features(candles, end_index, index_by_epoch, grid_offset_seconds)
    features = (
        ret(1), ret(6), ret(24), vol6, vol24, vol48, bb_percent, bb_bandwidth, atr_pct,
        *fib_distances, float(highs_count), float(lows_count), float(last_direction),
        swing_amplitude, min(1.0, bars_since_swing), *mtf,
    )
    if len(features) != len(_FEATURE_NAMES) or not all(math.isfinite(value) for value in features):
        raise ValueError("Advanced feature row is non-finite or violates its schema.")
    return features


def build_advanced_feature_matrix(
    candles: Sequence[MarketCandle], *, lookback: int = 48
) -> tuple[tuple[str, ...], tuple[int, ...], tuple[tuple[float, ...], ...]]:
    """Build causal OHLC-only features at hourly close; no future prices enter any row.

    The multi-timeframe grid follows the candle timestamp offset. Fibonacci levels are
    rolling-range references. Swing columns are confirmed-pivot shape proxies, not claimed
    Elliott-wave counts or human wave analysis.
    """
    _validate_candles(candles)
    if type(lookback) is not int or lookback < 48:
        raise ValueError("lookback must be an integer of at least 48 hours.")
    if len(candles) <= lookback:
        raise ValueError("Insufficient candles for the requested lookback.")
    index_by_epoch = {int(c.timestamp.timestamp()): i for i, c in enumerate(candles)}
    grid_offset_seconds = int(candles[0].timestamp.timestamp()) % 3600
    indices = tuple(range(lookback - 1, len(candles)))
    rows = tuple(
        _feature_row(candles, index, index_by_epoch, grid_offset_seconds, lookback=lookback)
        for index in indices
    )
    return _FEATURE_NAMES, indices, rows


def select_features_train_only(
    x_train: Sequence[Sequence[float]],
    y_train: Sequence[int],
    feature_names: Sequence[str],
    *,
    top_k: int,
    random_seed: int = 41,
) -> tuple[str, ...]:
    """Rank features with mutual information using training rows only.

    Call only after temporal train/validation/test separation; including validation or test
    rows here biases downstream model-selection claims.
    """
    if type(top_k) is not int or not 1 <= top_k <= len(feature_names):
        raise ValueError("top_k must be between one and the feature count.")
    numpy = importlib.import_module("numpy")
    selection = importlib.import_module("sklearn.feature_selection")
    matrix = numpy.asarray(x_train, dtype="float64")
    target = numpy.asarray(y_train, dtype="int64")
    if (
        matrix.ndim != 2 or matrix.shape[1] != len(feature_names)
        or matrix.shape[0] != target.shape[0] or matrix.shape[0] < 10
        or target.ndim != 1 or len(set(int(value) for value in target)) < 2
        or not numpy.isfinite(matrix).all()
    ):
        raise ValueError("Feature selection requires aligned, finite training data with >=2 classes.")
    scores = selection.mutual_info_classif(
        matrix, target, discrete_features=False, random_state=random_seed
    )
    ranked_indices = sorted(range(len(feature_names)), key=lambda index: (-float(scores[index]), index))
    return tuple(feature_names[index] for index in ranked_indices[:top_k])


def build_sma_meta_labels(
    candles: Sequence[MarketCandle],
    *,
    short_window: int = 12,
    long_window: int = 48,
    horizon_bars: int = 6,
    round_trip_cost_bps: float = 30.0,
) -> tuple[MetaLabelEvent, ...]:
    """Label SMA golden-cross entries by whether next-open-to-horizon return clears costs.

    Signals are known at candle close, entries occur at next candle open, and the label endpoint
    is the close after horizon_bars. Incomplete final horizons are excluded. Cost is a label
    threshold only; final economic acceptance must use the complete BacktestConfig simulator.
    """
    _validate_candles(candles)
    if (
        type(short_window) is not int or type(long_window) is not int
        or short_window < 2 or long_window <= short_window
    ):
        raise ValueError("SMA windows must satisfy 2 <= short_window < long_window.")
    if type(horizon_bars) is not int or horizon_bars < 1:
        raise ValueError("horizon_bars must be positive.")
    if not math.isfinite(round_trip_cost_bps) or not 0 <= round_trip_cost_bps < 10000:
        raise ValueError("round_trip_cost_bps must be finite and in [0, 10000).")

    closes = [float(candle.close) for candle in candles]

    def sma(index: int, window: int) -> float:
        return _mean(closes[index - window + 1 : index + 1])

    cost_rate = round_trip_cost_bps / 10000.0
    events: list[MetaLabelEvent] = []
    for index in range(long_window, len(candles) - horizon_bars):
        previous_short, previous_long = sma(index - 1, short_window), sma(index - 1, long_window)
        current_short, current_long = sma(index, short_window), sma(index, long_window)
        if previous_short > previous_long or current_short <= current_long:
            continue
        entry_index, exit_index = index + 1, index + horizon_bars
        entry_price = float(candles[entry_index].open)
        exit_price = float(candles[exit_index].close)
        gross_return = exit_price / entry_price - 1.0
        net_return = gross_return - cost_rate
        events.append(MetaLabelEvent(
            signal_index=index, entry_index=entry_index, exit_index=exit_index,
            gross_return=gross_return, net_return_after_costs=net_return,
            round_trip_cost_bps=round_trip_cost_bps, label=int(net_return > 0),
        ))
    return tuple(events)


def make_meta_label_classifier(
    model_name: Literal["xgboost", "lightgbm"],
    params: dict[str, int | float],
    *,
    random_seed: int = 41,
) -> Any:
    """Construct a deterministic optional tree learner without connecting it to Runtime."""
    if model_name == "xgboost":
        module = importlib.import_module("xgboost")
        return module.XGBClassifier(
            objective="binary:logistic", eval_metric="logloss", tree_method="hist",
            n_jobs=1, random_state=random_seed, verbosity=0, **params,
        )
    if model_name == "lightgbm":
        module = importlib.import_module("lightgbm")
        return module.LGBMClassifier(
            objective="binary", n_jobs=1, random_state=random_seed, deterministic=True,
            force_col_wise=True, verbosity=-1, **params,
        )
    raise ValueError("model_name must be xgboost or lightgbm.")


def tune_meta_labeler(
    model_name: Literal["xgboost", "lightgbm"],
    x_train: Sequence[Sequence[float]],
    y_train: Sequence[int],
    x_validation: Sequence[Sequence[float]],
    y_validation: Sequence[int],
    *,
    n_trials: int = 30,
    random_seed: int = 41,
) -> MetaLabelTuningResult:
    """Tune XGBoost/LightGBM on a chronological validation block with seeded Optuna.

    Never pass a test block to this function. The objective is validation average precision
    on cost-aware primary-signal labels; net returns and baselines need a separate locked
    out-of-sample economic backtest.
    """
    if model_name not in _META_MODELS:
        raise ValueError("Only XGBoost and LightGBM are supported meta-label challengers.")
    if type(n_trials) is not int or not 5 <= n_trials <= 100:
        raise ValueError("n_trials must be an integer in [5, 100].")
    numpy = importlib.import_module("numpy")
    optuna = importlib.import_module("optuna")
    metrics = importlib.import_module("sklearn.metrics")
    train_x, train_y = numpy.asarray(x_train, dtype="float64"), numpy.asarray(y_train, dtype="int64")
    valid_x, valid_y = numpy.asarray(x_validation, dtype="float64"), numpy.asarray(y_validation, dtype="int64")
    if (
        train_x.ndim != 2 or valid_x.ndim != 2 or train_x.shape[1] == 0
        or train_x.shape[1] != valid_x.shape[1] or len(train_x) != len(train_y)
        or len(valid_x) != len(valid_y) or len(train_y) < 20 or len(valid_y) < 10
        or set(int(value) for value in train_y) != {0, 1}
        or set(int(value) for value in valid_y) != {0, 1}
        or not numpy.isfinite(train_x).all() or not numpy.isfinite(valid_x).all()
    ):
        raise ValueError("Tuning requires finite, aligned train/validation matrices with both labels.")

    def objective(trial: Any) -> float:
        if model_name == "xgboost":
            params: dict[str, int | float] = {
                "n_estimators": trial.suggest_int("n_estimators", 50, 400, step=25),
                "max_depth": trial.suggest_int("max_depth", 2, 8),
                "learning_rate": trial.suggest_float("learning_rate", 0.005, 0.2, log=True),
                "min_child_weight": trial.suggest_float("min_child_weight", 1.0, 20.0, log=True),
                "subsample": trial.suggest_float("subsample", 0.6, 1.0),
                "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
                "reg_alpha": trial.suggest_float("reg_alpha", 1e-5, 10.0, log=True),
                "reg_lambda": trial.suggest_float("reg_lambda", 1e-5, 20.0, log=True),
            }
        else:
            max_depth = trial.suggest_int("max_depth", 3, 10)
            params = {
                "n_estimators": trial.suggest_int("n_estimators", 50, 400, step=25),
                "max_depth": max_depth,
                "num_leaves": trial.suggest_int("num_leaves", 7, min(63, 2 ** max_depth)),
                "learning_rate": trial.suggest_float("learning_rate", 0.005, 0.2, log=True),
                "min_child_samples": trial.suggest_int("min_child_samples", 10, 60),
                "subsample": trial.suggest_float("subsample", 0.6, 1.0),
                "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
                "reg_alpha": trial.suggest_float("reg_alpha", 1e-5, 10.0, log=True),
                "reg_lambda": trial.suggest_float("reg_lambda", 1e-5, 20.0, log=True),
            }
        model = make_meta_label_classifier(model_name, params, random_seed=random_seed + trial.number)
        model.fit(train_x, train_y)
        probability = model.predict_proba(valid_x)[:, 1]
        score = float(metrics.average_precision_score(valid_y, probability))
        if not math.isfinite(score):
            raise ValueError("Optuna objective produced a non-finite validation score.")
        return score

    study = optuna.create_study(
        direction="maximize", sampler=optuna.samplers.TPESampler(seed=random_seed),
        study_name=f"evoryn-{model_name}-sma-meta-label",
    )
    study.optimize(objective, n_trials=n_trials, n_jobs=1, show_progress_bar=False)
    return MetaLabelTuningResult(
        model_name=model_name,
        best_params=dict(study.best_params),
        best_validation_average_precision=float(study.best_value),
        completed_trials=sum(trial.state.name == "COMPLETE" for trial in study.trials),
        random_seed=random_seed,
    )
