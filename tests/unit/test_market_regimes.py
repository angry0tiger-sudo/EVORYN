"""Offline invariants for causal three-state regime detection."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from evoryn.domain.models import Market, MarketCandle
from evoryn.learning_lab.market_regimes import (
    FEATURE_NAMES,
    MarketRegime,
    RegimeGatedSmaStrategy,
    build_causal_feature_matrix,
    fit_trend_threshold,
    forward_filter_probabilities,
    label_current_regime,
    label_regimes,
)

MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
START = datetime(2025, 1, 1, tzinfo=UTC)
INTERVAL = timedelta(hours=1)


def make_candles(count: int = 240) -> tuple[MarketCandle, ...]:
    candles: list[MarketCandle] = []
    prior = 100.0
    for index in range(count):
        close_value = 100.0 * math.exp(0.0004 * index + 0.025 * math.sin(index / 9.0))
        opening = Decimal(str(prior))
        close = Decimal(str(close_value))
        candles.append(
            MarketCandle(
                market=MARKET,
                timestamp=START + index * INTERVAL,
                interval=INTERVAL,
                open=opening,
                high=max(opening, close) + Decimal("0.5"),
                low=min(opening, close) - Decimal("0.5"),
                close=close,
                volume=Decimal("1"),
                source="synthetic-regime-test",
            )
        )
        prior = close_value
    return tuple(candles)


class ArrayLike:
    def __init__(self, value: Any) -> None:
        self.value = value

    def tolist(self) -> Any:
        return self.value


class FakeHmm:
    startprob_ = ArrayLike([0.6, 0.4])
    transmat_ = ArrayLike([[0.9, 0.1], [0.2, 0.8]])

    def _compute_log_likelihood(self, observations: Any) -> ArrayLike:
        return ArrayLike(
            [[-abs(float(row[0])), -abs(float(row[0]) - 1.0)] for row in observations]
        )


def test_features_are_finite_causal_and_price_only() -> None:
    candles = make_candles()
    indices, rows = build_causal_feature_matrix(candles)

    assert len(indices) == len(rows) == len(candles) - 72
    assert indices[0] == 72
    assert len(rows[0]) == len(FEATURE_NAMES) == 10
    assert all(math.isfinite(value) for row in rows for value in row)
    assert not any("volume" in name or "order_book" in name for name in FEATURE_NAMES)

    cut = 170
    _, prefix_rows = build_causal_feature_matrix(candles[: cut + 1])
    assert rows[indices.index(cut)] == prefix_rows[-1]


def test_feature_extractor_rejects_gaps_invalid_lookback_and_short_data() -> None:
    candles = make_candles(100)
    with pytest.raises(ValueError, match="gapped"):
        build_causal_feature_matrix(candles[:40] + candles[41:])
    with pytest.raises(ValueError, match="lookback"):
        build_causal_feature_matrix(candles, lookback=24)
    with pytest.raises(ValueError, match="lookback"):
        build_causal_feature_matrix(candles[:72])


def test_regime_labels_are_three_state_descriptions_of_trailing_returns() -> None:
    threshold = 0.02
    neutral = [0.0] * len(FEATURE_NAMES)
    up = neutral.copy()
    up[2] = 0.03
    down = neutral.copy()
    down[2] = -0.03

    assert label_current_regime(up, threshold) == MarketRegime.BULL_TREND.value
    assert label_current_regime(down, threshold) == MarketRegime.BEAR_TREND.value
    assert label_current_regime(neutral, threshold) == MarketRegime.RANGE.value
    assert label_regimes((up, down, neutral), threshold) == (
        "BULL_TREND", "BEAR_TREND", "RANGE"
    )


def test_threshold_is_fitted_from_supplied_training_rows_only() -> None:
    rows = []
    for magnitude in (0.01, 0.02, 0.03, 0.04, 0.05):
        row = [0.0] * len(FEATURE_NAMES)
        row[2] = magnitude
        rows.append(row)
    threshold = fit_trend_threshold(rows, quantile=0.5)

    changed_future = rows + [[0.0, 0.0, 100.0] + [0.0] * (len(FEATURE_NAMES) - 3)]
    assert fit_trend_threshold(rows, quantile=0.5) == threshold
    assert fit_trend_threshold(changed_future, quantile=0.5) != threshold


def test_hmm_forward_filter_is_causal_and_probabilities_are_normalized() -> None:
    hmm = FakeHmm()
    observations = ((0.0,), (0.2,), (0.7,), (1.2,))
    all_rows = forward_filter_probabilities(hmm, observations)
    prefix_rows = forward_filter_probabilities(hmm, observations[:3])

    assert len(all_rows) == len(observations)
    assert all(sum(row) == pytest.approx(1.0) for row in all_rows)
    assert all(value >= 0 for row in all_rows for value in row)
    assert all_rows[:3] == prefix_rows


def test_regime_gated_sma_only_takes_spot_exposure_in_bull_state() -> None:
    candles = make_candles(100)
    _, _ = build_causal_feature_matrix(candles)
    predictions = {candle.timestamp: "BEAR_TREND" for candle in candles}
    strategy = RegimeGatedSmaStrategy(predictions)
    assert strategy.target_exposure(candles) == Decimal("0")

    predictions = {candle.timestamp: "BULL_TREND" for candle in candles}
    strategy = RegimeGatedSmaStrategy(predictions)
    assert strategy.target_exposure(candles) == Decimal("1")

