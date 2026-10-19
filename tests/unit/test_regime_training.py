"""Offline tests for label construction and temporal train/calibration/test boundaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from collections.abc import Sequence
from decimal import Decimal

import pytest

from evoryn.domain.models import Market, MarketCandle
from evoryn.intelligence.regime import RegimeLabel
from evoryn.intelligence.regime_training import (
    ForwardOutcome,
    RegimeTrainingConfig,
    assign_forward_regime,
    forward_outcome,
    linear_quantile,
)

MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
INTERVAL = timedelta(hours=1)
START = datetime(2026, 9, 1, tzinfo=UTC)


def candles_from_closes(closes: Sequence[str]) -> tuple[MarketCandle, ...]:
    candles: list[MarketCandle] = []
    for index, raw in enumerate(closes):
        close = Decimal(raw)
        candles.append(
            MarketCandle(
                market=MARKET,
                timestamp=START + INTERVAL * index,
                interval=INTERVAL,
                open=close,
                high=close * Decimal("1.01"),
                low=close * Decimal("0.99"),
                close=close,
                volume=Decimal("10"),
                source="unit-test-fixture",
            )
        )
    return tuple(candles)


def test_forward_outcome_uses_exactly_the_requested_future_horizon() -> None:
    candles = candles_from_closes(["100", "101", "102", "103", "104"])
    outcome = forward_outcome(candles, 0, 3)

    assert outcome.forward_return == pytest.approx(0.03)
    assert outcome.forward_volatility >= 0


def test_forward_outcome_rejects_incomplete_future_horizon() -> None:
    candles = candles_from_closes(["100", "101", "102"])
    with pytest.raises(ValueError, match="complete future horizon"):
        forward_outcome(candles, 1, 2)


def test_quantile_is_deterministic_and_linearly_interpolated() -> None:
    assert linear_quantile([4.0, 1.0, 3.0, 2.0], 0.5) == pytest.approx(2.5)
    assert linear_quantile([1.0, 3.0], 0.25) == pytest.approx(1.5)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (ForwardOutcome(0.04, 0.08), RegimeLabel.BULL_TREND),
        (ForwardOutcome(-0.04, 0.08), RegimeLabel.BEAR_TREND),
        (ForwardOutcome(0.0, 0.12), RegimeLabel.HIGH_VOLATILITY),
        (ForwardOutcome(0.0, 0.01), RegimeLabel.RANGE_BOUND),
    ],
)
def test_weak_label_policy_maps_future_outcomes_transparently(
    outcome: ForwardOutcome,
    expected: RegimeLabel,
) -> None:
    assert assign_forward_regime(
        outcome,
        trend_threshold=0.02,
        volatility_threshold=0.05,
    ) is expected


def test_training_config_rejects_time_leaking_or_unbounded_splits() -> None:
    with pytest.raises(ValueError, match="fractions"):
        RegimeTrainingConfig(train_fraction=0.85, validation_fraction=0.20)
    with pytest.raises(ValueError, match="horizon_bars"):
        RegimeTrainingConfig(horizon_bars=0)
    assert RegimeTrainingConfig().train_fraction == 0.60
