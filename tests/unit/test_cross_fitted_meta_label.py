from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from evoryn.domain.models import Market, MarketCandle
from evoryn.learning_lab.cross_fitted_meta_label import (
    CrossFittedMetaConfig,
    TripleBarrierEventPolicy,
    _calibrate_probabilities,
    _fit_probability_calibrator,
    select_validation_threshold,
)
from evoryn.learning_lab.triple_barrier import (
    BarrierOutcome,
    TripleBarrierConfig,
    TripleBarrierEvent,
    build_triple_barrier_events,
    make_purged_event_folds,
)

MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
INTERVAL = timedelta(hours=1)
START = datetime(2025, 1, 1, tzinfo=UTC)


def make_candles(count: int = 720) -> tuple[MarketCandle, ...]:
    result: list[MarketCandle] = []
    previous = 100.0
    for index in range(count):
        close_value = 100.0 + 0.002 * index + 5.0 * math.sin(index / 8.0)
        opening = Decimal(str(previous))
        close = Decimal(str(close_value))
        result.append(
            MarketCandle(
                market=MARKET,
                timestamp=START + index * INTERVAL,
                interval=INTERVAL,
                open=opening,
                high=max(opening, close) + Decimal("0.3"),
                low=min(opening, close) - Decimal("0.3"),
                close=close,
                volume=Decimal("17.3"),
                source="synthetic-cross-fit-meta-label",
            )
        )
        previous = close_value
    return tuple(result)


def make_events() -> tuple[TripleBarrierEvent, ...]:
    return build_triple_barrier_events(
        make_candles(),
        TripleBarrierConfig(
            short_window=6,
            long_window=18,
            volatility_lookback=12,
            vertical_horizon_bars=6,
            profit_taking_multiple=0.3,
            stop_loss_multiple=0.3,
            round_trip_cost_bps=50.0,
        ),
    ).events


def test_protocol_rejects_overlapping_oof_test_folds_and_invalid_gate_limits() -> None:
    with pytest.raises(ValueError, match="must not overlap"):
        CrossFittedMetaConfig(test_event_count=10, step_event_count=5)
    with pytest.raises(ValueError, match="max_drawdown_fraction"):
        CrossFittedMetaConfig(max_drawdown_fraction=1.0)


def test_fold_coverage_fails_when_expected_oof_test_folds_are_missing() -> None:
    events = make_events()
    assert len(events) >= 8
    folds = make_purged_event_folds(
        events[:8],
        train_event_count=3,
        validation_event_count=2,
        test_event_count=1,
        step_event_count=1,
        expected_fold_count=6,
        embargo_bars=0,
    )
    assert folds.actual_fold_count < folds.expected_fold_count
    assert folds.coverage_complete is False


def test_validation_threshold_abstains_when_no_nonoverlapping_cutoff_has_positive_net_return() -> None:
    events = make_events()[:10]
    assert len(events) >= 5
    losing = tuple(replace(event, net_return_fraction=-0.01) for event in events)
    selected = select_validation_threshold(
        losing,
        [0.99 - index * 0.001 for index in range(len(losing))],
        embargo_bars=0,
    )
    assert selected.threshold == 1.01
    assert selected.selected_event_count == 0
    assert selected.abstain is True


def test_validation_threshold_uses_only_passed_probabilities_and_label_outcomes() -> None:
    events = make_events()[:10]
    assert len(events) >= 5
    winners = tuple(replace(event, net_return_fraction=0.02) for event in events)
    selected = select_validation_threshold(
        winners,
        [0.99 - index * 0.001 for index in range(len(winners))],
        embargo_bars=0,
    )
    assert selected.abstain is False
    assert selected.threshold <= 0.99
    assert selected.selected_event_count >= 3
    assert selected.compounded_net_label_return > 0


def test_backtest_policy_uses_frozen_barriers_but_not_realized_label_outcome() -> None:
    candles = make_candles()
    event = next(
        item
        for item in build_triple_barrier_events(
            candles,
            TripleBarrierConfig(
                short_window=6,
                long_window=18,
                volatility_lookback=12,
                vertical_horizon_bars=6,
                profit_taking_multiple=0.3,
                stop_loss_multiple=0.3,
            ),
        ).events
    )
    safe_event = replace(
        event,
        profit_taking_price=event.reference_price * Decimal("2"),
        stop_loss_price=event.reference_price * Decimal("0.5"),
    )
    altered_label = replace(
        safe_event,
        outcome=(
            BarrierOutcome.STOP_LOSS
            if safe_event.outcome is not BarrierOutcome.STOP_LOSS
            else BarrierOutcome.PROFIT_TAKING
        ),
        net_return_fraction=-99.0,
    )
    policy_a = TripleBarrierEventPolicy(
        events_by_index={safe_event.event_index: safe_event},
        allowed_event_indices={safe_event.event_index},
        horizon_bars=6,
    )
    policy_b = TripleBarrierEventPolicy(
        events_by_index={event.event_index: altered_label},
        allowed_event_indices={event.event_index},
        horizon_bars=6,
    )
    at_event = candles[: safe_event.event_index + 1]
    assert policy_a.target_exposure(at_event) == Decimal("1")
    assert policy_b.target_exposure(at_event) == Decimal("1")
    assert policy_a.target_exposure(candles[: safe_event.event_index + 2]) == Decimal("1")
    assert policy_b.target_exposure(candles[: event.event_index + 2]) == Decimal("1")

    triggered = list(candles)
    next_candle = triggered[safe_event.event_index + 1]
    triggered[event.event_index + 1] = MarketCandle(
        market=next_candle.market,
        timestamp=next_candle.timestamp,
        interval=next_candle.interval,
        open=next_candle.open,
        high=max(next_candle.high, safe_event.profit_taking_price),
        low=next_candle.low,
        close=next_candle.close,
        volume=next_candle.volume,
        source=next_candle.source,
    )
    assert policy_a.target_exposure(tuple(triggered[: safe_event.event_index + 2])) == Decimal("0")
    assert policy_b.target_exposure(tuple(triggered[: event.event_index + 2])) == Decimal("0")


def test_platt_calibration_uses_only_valid_separate_calibration_rows() -> None:
    pytest.importorskip("sklearn")
    probabilities = [0.1, 0.15, 0.2, 0.3, 0.65, 0.7, 0.8, 0.85, 0.9, 0.95]
    labels = [0, 0, 0, 0, 0, 1, 1, 1, 1, 1]
    calibrator = _fit_probability_calibrator(probabilities, labels, seed=41)
    calibrated = _calibrate_probabilities(calibrator, probabilities)
    assert len(calibrated) == len(probabilities)
    assert all(0.0 <= probability <= 1.0 for probability in calibrated)
    assert all(math.isfinite(probability) for probability in calibrated)
    assert sum(calibrated[5:]) / 5 > sum(calibrated[:5]) / 5


def test_platt_calibration_rejects_single_class_support() -> None:
    pytest.importorskip("sklearn")
    with pytest.raises(ValueError, match="both classes"):
        _fit_probability_calibrator([0.1] * 12, [0] * 12, seed=41)
