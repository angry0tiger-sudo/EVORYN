"""Unit tests for purged walk-forward label policies and causal HMM filtering."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from evoryn.domain.models import Market, MarketCandle
from evoryn.intelligence.regime import RegimeLabel
from evoryn.intelligence.regime_walk_forward import (
    filtered_gaussian_hmm_probabilities,
    volatility_scaled_triple_barrier_label,
)

MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
INTERVAL = timedelta(hours=1)
START = datetime(2026, 1, 1, tzinfo=UTC)


def candles_with_future_path(
    future_high: str,
    future_low: str,
    *,
    future_close: str = "100",
) -> tuple[MarketCandle, ...]:
    rows = (
        ("100", "100", "100"),
        (future_close, future_high, future_low),
        ("100", "101", "99"),
        ("100", "101", "99"),
    )
    return tuple(
        MarketCandle(
            market=MARKET,
            timestamp=START + INTERVAL * index,
            interval=INTERVAL,
            open=Decimal(close),
            high=Decimal(high),
            low=Decimal(low),
            close=Decimal(close),
            volume=Decimal("10"),
            source="walk-forward-unit-test",
        )
        for index, (close, high, low) in enumerate(rows)
    )


@pytest.mark.parametrize(
    ("high", "low", "expected"),
    [
        ("104", "99", RegimeLabel.BULL_TREND),
        ("101", "96", RegimeLabel.BEAR_TREND),
        ("104", "96", RegimeLabel.HIGH_VOLATILITY),
        ("101", "99", RegimeLabel.RANGE_BOUND),
    ],
)
def test_volatility_scaled_triple_barrier_uses_ohlc_first_touch(
    high: str,
    low: str,
    expected: RegimeLabel,
) -> None:
    candles = candles_with_future_path(high, low)
    label = volatility_scaled_triple_barrier_label(
        candles,
        0,
        trailing_volatility=0.01,
        horizon_bars=2,
        barrier_multiplier=1.5,
        high_volatility_threshold=0.02,
    )
    assert label is expected


def test_triple_barrier_rejects_invalid_thresholds_and_incomplete_horizon() -> None:
    candles = candles_with_future_path("101", "99")
    with pytest.raises(ValueError, match="valid range"):
        volatility_scaled_triple_barrier_label(
            candles,
            0,
            trailing_volatility=-0.01,
            horizon_bars=2,
            barrier_multiplier=1.5,
            high_volatility_threshold=0.02,
        )
    with pytest.raises(ValueError, match="complete future horizon"):
        volatility_scaled_triple_barrier_label(
            candles,
            2,
            trailing_volatility=0.01,
            horizon_bars=2,
            barrier_multiplier=1.5,
            high_volatility_threshold=0.02,
        )


def test_hmm_filtering_is_causal_and_prefix_probabilities_do_not_change() -> None:
    numpy = pytest.importorskip("numpy")

    class FittedDiagonalGaussianHMM:
        startprob_ = numpy.asarray([0.5, 0.5], dtype="float64")
        transmat_ = numpy.asarray([[0.9, 0.1], [0.1, 0.9]], dtype="float64")
        means_ = numpy.asarray([[-1.0, 0.0], [1.0, 0.0]], dtype="float64")
        covars_ = numpy.asarray(
            [numpy.eye(2, dtype="float64"), numpy.eye(2, dtype="float64")]
        )

    model = FittedDiagonalGaussianHMM()
    prefix = numpy.asarray(
        [[-1.0, 0.0], [-0.8, 0.1], [0.9, 0.0]],
        dtype="float64",
    )
    extended = numpy.vstack(
        [prefix, numpy.asarray([[100.0, -100.0]], dtype="float64")]
    )
    prefix_result = filtered_gaussian_hmm_probabilities(model, prefix)
    extended_result = filtered_gaussian_hmm_probabilities(model, extended)
    assert numpy.allclose(prefix_result, extended_result[: len(prefix)], atol=1e-12)
    assert numpy.allclose(
        extended_result.sum(axis=1),
        numpy.ones(len(extended)),
    )
    assert numpy.isfinite(extended_result).all()


def _technical_fixture(count: int = 80) -> tuple[MarketCandle, ...]:
    from decimal import Decimal

    from evoryn.domain.models import Market, MarketCandle

    market = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
    interval = timedelta(hours=1)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows: list[MarketCandle] = []
    prior_close = 100.0
    for index in range(count):
        close_value = 100.0 + index * 0.18 + math.sin(index / 3.0) * 0.7
        close = Decimal(str(close_value))
        opening = Decimal(str(prior_close))
        rows.append(
            MarketCandle(
                market=market,
                timestamp=start + index * interval,
                interval=interval,
                open=opening,
                high=max(opening, close) + Decimal("0.6"),
                low=min(opening, close) - Decimal("0.6"),
                close=close,
                volume=Decimal("100"),
                source="synthetic-technical-features",
            )
        )
        prior_close = close_value
    return tuple(rows)


def test_research_technical_features_are_causal_finite_and_volume_free() -> None:
    from evoryn.intelligence.regime_research_models import (
        FEATURE_NAMES,
        FEATURE_SCHEMA_VERSION,
        research_feature_vector,
    )

    candles = _technical_fixture()
    from_full = research_feature_vector(candles, 60, lookback=48)
    from_prefix = research_feature_vector(candles[:61], 60, lookback=48)

    assert FEATURE_SCHEMA_VERSION == "3.0.0-price-technical-only"
    assert len(from_full) == len(FEATURE_NAMES) == 21
    assert from_full == from_prefix
    assert all(math.isfinite(value) for value in from_full)
    assert not any("volume" in name.casefold() for name in FEATURE_NAMES)
    assert "rsi_14_scaled" in FEATURE_NAMES
    assert "macd_histogram_pct" in FEATURE_NAMES
    assert "bollinger_percent_b" in FEATURE_NAMES
    assert "atr_14_pct" in FEATURE_NAMES


def test_lstm_training_produces_normalized_probabilities_when_torch_is_available() -> None:
    pytest.importorskip("torch")
    numpy = pytest.importorskip("numpy")
    from evoryn.intelligence.regime_research_models import fit_lstm, lstm_probabilities

    generator = numpy.random.default_rng(41)
    features = generator.normal(size=(20, 12, 21)).astype("float32")
    targets = numpy.asarray([0, 1, 2, 3] * 5, dtype="int64")
    model, torch_module = fit_lstm(
        features,
        targets,
        random_seed=41,
        epochs=1,
    )
    probabilities = lstm_probabilities(model, torch_module, features[:4], numpy)

    assert probabilities.shape == (4, 4)
    assert numpy.isfinite(probabilities).all()
    assert numpy.allclose(probabilities.sum(axis=1), numpy.ones(4), atol=1e-6)
