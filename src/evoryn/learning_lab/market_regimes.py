"""Causal three-state market-regime primitives for offline research only.

Regime labels describe the trailing completed-price window. They are not targets derived
from future returns and are not forecasts of the next price move.
"""

from __future__ import annotations

import importlib
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from evoryn.domain.models import MarketCandle

REGIME_SCHEMA_VERSION = "1.0.0-trailing-price-regimes"
REGIME_NAMES = ("BULL_TREND", "BEAR_TREND", "RANGE")
FEATURE_NAMES = (
    "return_1",
    "return_6",
    "return_24",
    "return_72",
    "realized_volatility_24",
    "sma_gap_12",
    "sma_gap_48",
    "range_position_24",
    "efficiency_ratio_24",
    "regression_return_24",
)
_ZERO = Decimal("0")
_ONE = Decimal("1")


class MarketRegime(StrEnum):
    """Coarse current market states from trailing completed bars."""

    BULL_TREND = "BULL_TREND"
    BEAR_TREND = "BEAR_TREND"
    RANGE = "RANGE"


def build_causal_feature_matrix(
    candles: Sequence[MarketCandle], *, lookback: int = 72
) -> tuple[tuple[int, ...], tuple[tuple[float, ...], ...]]:
    """Create finite price-only features; each feature row ends at its indexed closed candle."""
    if type(lookback) is not int or lookback < 72:
        raise ValueError("lookback must be an integer >= 72.")
    if len(candles) <= lookback:
        raise ValueError("At least lookback + 1 completed candles are required.")
    interval = candles[0].interval
    if interval.total_seconds() <= 0:
        raise ValueError("Candle interval must be positive.")
    for index, candle in enumerate(candles):
        close = float(candle.close)
        high = float(candle.high)
        low = float(candle.low)
        if not all(math.isfinite(value) and value > 0 for value in (close, high, low)):
            raise ValueError("Candle prices must be finite and positive.")
        if candle.interval != interval:
            raise ValueError("Candle intervals must be consistent.")
        if index and candle.timestamp - candles[index - 1].timestamp != interval:
            raise ValueError("Candle history is gapped or not chronologically ordered.")
    closes = [float(candle.close) for candle in candles]
    highs = [float(candle.high) for candle in candles]
    lows = [float(candle.low) for candle in candles]
    indices: list[int] = []
    rows: list[tuple[float, ...]] = []
    for index in range(lookback, len(candles)):
        close = closes[index]
        return_1 = close / closes[index - 1] - 1.0
        return_6 = close / closes[index - 6] - 1.0
        return_24 = close / closes[index - 24] - 1.0
        return_72 = close / closes[index - 72] - 1.0
        daily_returns = [
            closes[offset] / closes[offset - 1] - 1.0
            for offset in range(index - 23, index + 1)
        ]
        daily_mean = sum(daily_returns) / len(daily_returns)
        volatility = math.sqrt(
            sum((value - daily_mean) ** 2 for value in daily_returns) / len(daily_returns)
        )
        sma_12 = sum(closes[index - 11 : index + 1]) / 12.0
        sma_48 = sum(closes[index - 47 : index + 1]) / 48.0
        recent_high = max(highs[index - 23 : index + 1])
        recent_low = min(lows[index - 23 : index + 1])
        price_range = recent_high - recent_low
        range_position = (close - recent_low) / price_range if price_range > 0 else 0.5
        absolute_path = sum(
            abs(closes[offset] - closes[offset - 1])
            for offset in range(index - 23, index + 1)
        )
        efficiency = (
            (close - closes[index - 24]) / absolute_path if absolute_path > 0 else 0.0
        )
        log_prices = [math.log(value) for value in closes[index - 23 : index + 1]]
        mean_x = 11.5
        mean_y = sum(log_prices) / 24.0
        covariance = sum((offset - mean_x) * (value - mean_y)
                         for offset, value in enumerate(log_prices))
        variance_x = sum((offset - mean_x) ** 2 for offset in range(24))
        slope_return = math.exp((covariance / variance_x) * 23.0) - 1.0
        row = (
            return_1,
            return_6,
            return_24,
            return_72,
            volatility,
            close / sma_12 - 1.0,
            close / sma_48 - 1.0,
            range_position,
            max(-1.0, min(1.0, efficiency)),
            slope_return,
        )
        if not all(math.isfinite(value) for value in row):
            raise ValueError("Feature construction produced a non-finite value.")
        indices.append(index)
        rows.append(row)
    return tuple(indices), tuple(rows)


def fit_trend_threshold(
    training_rows: Sequence[Sequence[float]], *, quantile: float = 0.55
) -> float:
    """Fit a symmetric directional threshold on training-only trailing 24-bar returns."""
    if not training_rows or not 0.5 <= quantile <= 0.9:
        raise ValueError("Training rows must be non-empty and quantile must be in [0.5, 0.9].")
    magnitudes: list[float] = []
    for row in training_rows:
        if len(row) != len(FEATURE_NAMES) or not math.isfinite(row[2]):
            raise ValueError("Training feature row has an invalid schema or return_24.")
        magnitudes.append(abs(row[2]))
    ordered = sorted(magnitudes)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    fraction = position - lower
    threshold = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
    return max(threshold, 0.002)


def label_current_regime(row: Sequence[float], trend_threshold: float) -> str:
    """Assign a weak, descriptive state from the trailing 24-bar return in this row."""
    if (
        len(row) != len(FEATURE_NAMES)
        or not math.isfinite(trend_threshold)
        or trend_threshold <= 0
        or not math.isfinite(row[2])
    ):
        raise ValueError("Regime label requires a valid feature row and positive threshold.")
    if row[2] >= trend_threshold:
        return MarketRegime.BULL_TREND.value
    if row[2] <= -trend_threshold:
        return MarketRegime.BEAR_TREND.value
    return MarketRegime.RANGE.value


def label_regimes(
    rows: Sequence[Sequence[float]], trend_threshold: float
) -> tuple[str, ...]:
    """Label rows using a threshold frozen from the training partition."""
    return tuple(label_current_regime(row, trend_threshold) for row in rows)


def forward_filter_probabilities(
    model: Any, observations: Sequence[Sequence[float]]
) -> tuple[tuple[float, ...], ...]:
    """Run an HMM forward filter; unlike smoothed posteriors, it never sees future rows."""
    if len(observations) == 0:
        return ()
    start_probabilities = [float(value) for value in model.startprob_.tolist()]
    transition = [
        [float(value) for value in row] for row in model.transmat_.tolist()
    ]
    numpy = importlib.import_module("numpy")
    numeric_observations = numpy.asarray(observations, dtype="float64")
    if numeric_observations.ndim != 2:
        raise ValueError("HMM observations must form a two-dimensional numeric matrix.")
    emissions_array = model._compute_log_likelihood(numeric_observations)
    emissions = [[float(value) for value in row] for row in emissions_array.tolist()]
    if not emissions or len(emissions) != len(observations):
        raise ValueError("HMM returned an invalid emission matrix.")
    tiny = 1e-300
    log_start = [math.log(max(value, tiny)) for value in start_probabilities]
    log_transition = [
        [math.log(max(value, tiny)) for value in row] for row in transition
    ]
    alpha: list[float] = []
    output: list[tuple[float, ...]] = []
    for index, emission in enumerate(emissions):
        if index == 0:
            alpha = [log_start[state] + emission[state] for state in range(len(log_start))]
        else:
            next_alpha: list[float] = []
            for target_state in range(len(log_start)):
                candidates = [
                    alpha[source_state] + log_transition[source_state][target_state]
                    for source_state in range(len(log_start))
                ]
                next_alpha.append(_logsumexp(candidates) + emission[target_state])
            alpha = next_alpha
        normalizer = _logsumexp(alpha)
        alpha = [value - normalizer for value in alpha]
        probabilities = tuple(math.exp(value) for value in alpha)
        total = sum(probabilities)
        output.append(tuple(value / total for value in probabilities))
    return tuple(output)


def _logsumexp(values: Sequence[float]) -> float:
    maximum = max(values)
    if not math.isfinite(maximum):
        return maximum
    return maximum + math.log(sum(math.exp(value - maximum) for value in values))


class RegimeGatedSmaStrategy:
    """Spot long/flat SMA trend filter enabled only during a detected bull regime."""

    strategy_id = "market-regime-gated-sma-long-flat"
    strategy_version = REGIME_SCHEMA_VERSION

    def __init__(
        self,
        predicted_regimes: Mapping[datetime, str],
        *,
        fast_window: int = 12,
        slow_window: int = 48,
        model_id: str = "unknown",
    ) -> None:
        if type(fast_window) is not int or type(slow_window) is not int:
            raise ValueError("SMA windows must be integers.")
        if fast_window <= 0 or slow_window <= fast_window:
            raise ValueError("SMA windows must satisfy 0 < fast_window < slow_window.")
        self._predicted_regimes = dict(predicted_regimes)
        self._fast_window = fast_window
        self._slow_window = slow_window
        self._model_id = model_id
        self.strategy_id = f"regime-gated-sma-{model_id}"

    def parameters(self) -> dict[str, str]:
        return {
            "regime_model": self._model_id,
            "regime_policy": "long_only_when_predicted_bull",
            "fast_window": str(self._fast_window),
            "slow_window": str(self._slow_window),
            "signal_price": "last_completed_close",
            "volume_capacity_constraint": "disabled_unknown_volume_units",
        }

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        if len(history) < self._slow_window:
            return _ZERO
        last = history[-1]
        regime = self._predicted_regimes.get(last.timestamp)
        if regime != MarketRegime.BULL_TREND.value:
            return _ZERO
        fast = sum(float(candle.close) for candle in history[-self._fast_window :])
        slow = sum(float(candle.close) for candle in history[-self._slow_window :])
        fast /= self._fast_window
        slow /= self._slow_window
        return _ONE if fast > slow else _ZERO
