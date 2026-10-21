from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from evoryn.domain.models import Market, MarketCandle
from evoryn.learning_lab.triple_barrier import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    BarrierOutcome,
    TripleBarrierConfig,
    build_triple_barrier_events,
    make_purged_event_folds,
    purge_overlapping_events,
)

MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
INTERVAL = timedelta(hours=1)
START = datetime(2025, 1, 1, tzinfo=UTC)


def make_candles(count: int = 360) -> tuple[MarketCandle, ...]:
    result: list[MarketCandle] = []
    previous_close = 100.0
    for index in range(count):
        close_value = 100.0 + 0.002 * index + 5.0 * math.sin(index / 8.0)
        opening = Decimal(str(previous_close))
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
                source="synthetic-triple-barrier",
            )
        )
        previous_close = close_value
    return tuple(result)


def test_events_freeze_barriers_at_event_time_and_include_complete_label_intervals() -> None:
    candles = make_candles()
    options = TripleBarrierConfig(vertical_horizon_bars=12)
    built = build_triple_barrier_events(candles, options)

    assert built.detected_bullish_crossovers >= len(built.events) > 0
    assert all(len(event.event_id) == 64 for event in built.events)
    assert all(event.label_start_index == event.event_index for event in built.events)
    assert all(event.event_index < event.label_end_index for event in built.events)
    assert all(
        event.holding_bars == event.label_end_index - event.event_index
        for event in built.events
    )
    assert all(
        event.label_end_index <= event.event_index + options.vertical_horizon_bars
        for event in built.events
    )
    assert all(
        event.stop_loss_price < event.reference_price < event.profit_taking_price
        for event in built.events
    )
    assert all(
        event.net_return_fraction == pytest.approx(event.gross_return_fraction - 0.003)
        for event in built.events
    )
    assert sum(built.outcome_counts.values()) == built.emitted_event_count
    assert built.outcome_counts[BarrierOutcome.VERTICAL_EXPIRY.value] >= 0


def test_event_features_are_prefix_invariant_and_future_data_only_changes_labels() -> None:
    candles = make_candles()
    base = build_triple_barrier_events(candles, TripleBarrierConfig(vertical_horizon_bars=12))
    target = base.events[0]
    enough_prefix = candles[: target.event_index + 13]
    prefix_result = build_triple_barrier_events(
        enough_prefix, TripleBarrierConfig(vertical_horizon_bars=12)
    )
    prefix_event = next(event for event in prefix_result.events if event.event_id == target.event_id)

    assert target.feature_schema_version == FEATURE_SCHEMA_VERSION
    assert target.feature_names == FEATURE_NAMES
    assert target.feature_values == prefix_event.feature_values
    assert target.profit_taking_price == prefix_event.profit_taking_price
    assert target.stop_loss_price == prefix_event.stop_loss_price

    # Widening only a post-event high cannot rewrite the already-frozen feature row/barriers.
    mutated = list(candles)
    future_index = target.event_index + 1
    future = mutated[future_index]
    mutated[future_index] = MarketCandle(
        market=future.market,
        timestamp=future.timestamp,
        interval=future.interval,
        open=future.open,
        high=max(future.high, target.profit_taking_price + Decimal("100")),
        low=future.low,
        close=future.close,
        volume=future.volume,
        source=future.source,
    )
    changed = build_triple_barrier_events(
        tuple(mutated), TripleBarrierConfig(vertical_horizon_bars=12)
    )
    changed_event = next(event for event in changed.events if event.event_id == target.event_id)
    assert changed_event.feature_values == target.feature_values
    assert changed_event.profit_taking_price == target.profit_taking_price
    assert changed_event.stop_loss_price == target.stop_loss_price


def test_ambiguous_same_bar_touch_is_resolved_adversely_as_stop_loss() -> None:
    prices = [100.0] * 50 + [110.0] + [110.0] * 8
    candles: list[MarketCandle] = []
    previous = 100.0
    for index, value in enumerate(prices):
        opening = Decimal(str(previous))
        close = Decimal(str(value))
        high = max(opening, close) + Decimal("0.2")
        low = min(opening, close) - Decimal("0.2")
        if index == 51:
            high = Decimal("116")
            low = Decimal("100")
        candles.append(
            MarketCandle(
                market=MARKET,
                timestamp=START + index * INTERVAL,
                interval=INTERVAL,
                open=opening,
                high=high,
                low=low,
                close=close,
                volume=Decimal("1"),
                source="synthetic-ambiguous-touch",
            )
        )
        previous = value

    built = build_triple_barrier_events(
        tuple(candles),
        TripleBarrierConfig(
            short_window=12,
            long_window=48,
            volatility_lookback=48,
            vertical_horizon_bars=3,
        ),
    )
    event = next(event for event in built.events if event.event_index == 50)
    assert event.outcome is BarrierOutcome.STOP_LOSS
    assert event.ambiguous_horizontal_touch is True
    assert event.exit_price <= event.stop_loss_price


def test_incomplete_horizons_and_gapped_or_non_usdt_data_fail_closed() -> None:
    flat_tail: list[MarketCandle] = []
    previous_close = Decimal("100")
    for index in range(80):
        close = Decimal("110") if index == 79 else Decimal("100")
        opening = previous_close
        flat_tail.append(
            MarketCandle(
                market=MARKET,
                timestamp=START + index * INTERVAL,
                interval=INTERVAL,
                open=opening,
                high=max(opening, close) + Decimal("0.2"),
                low=min(opening, close) - Decimal("0.2"),
                close=close,
                volume=Decimal("1"),
                source="synthetic-incomplete-horizon",
            )
        )
        previous_close = close
    options = TripleBarrierConfig(vertical_horizon_bars=12)
    tail_signal = build_triple_barrier_events(tuple(flat_tail), options)
    assert tail_signal.detected_bullish_crossovers == 1
    assert tail_signal.incomplete_horizon_skipped == 1
    assert tail_signal.emitted_event_count == 0

    candles = make_candles(80)
    with pytest.raises(ValueError, match="gapped"):
        build_triple_barrier_events(candles[:50] + candles[51:], options)

    wrong_market = Market(symbol="BTC-EUR", venue="nobitex", market_type="spot")
    wrong = tuple(
        MarketCandle(
            market=wrong_market,
            timestamp=candle.timestamp,
            interval=candle.interval,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            source=candle.source,
        )
        for candle in candles
    )
    with pytest.raises(ValueError, match="Spot/USDT"):
        build_triple_barrier_events(wrong, options)


def test_overlap_purge_and_embargo_remove_training_events_without_randomization() -> None:
    events = build_triple_barrier_events(
        make_candles(),
        TripleBarrierConfig(
            short_window=6,
            long_window=18,
            volatility_lookback=12,
            vertical_horizon_bars=24,
            profit_taking_multiple=0.3,
            stop_loss_multiple=0.3,
        ),
    ).events
    assert len(events) >= 6
    training = events[:3]
    evaluation = (events[3],)
    purged = purge_overlapping_events(training, evaluation, embargo_bars=100_000)
    assert purged == ()
    assert purge_overlapping_events(training, (), embargo_bars=0) == training


def test_purged_fold_coverage_is_explicit_and_missing_folds_do_not_pass() -> None:
    events = build_triple_barrier_events(
        make_candles(720),
        TripleBarrierConfig(
            short_window=6,
            long_window=18,
            volatility_lookback=12,
            vertical_horizon_bars=6,
            profit_taking_multiple=0.2,
            stop_loss_multiple=0.2,
        ),
    ).events
    assert len(events) >= 8
    result = make_purged_event_folds(
        events,
        train_event_count=3,
        validation_event_count=1,
        test_event_count=1,
        step_event_count=1,
        expected_fold_count=3,
        embargo_bars=0,
    )
    assert result.actual_fold_count == 3
    assert result.coverage_complete is all(fold.status == "EVALUABLE" for fold in result.folds)

    incomplete = make_purged_event_folds(
        events[:5],
        train_event_count=3,
        validation_event_count=1,
        test_event_count=1,
        step_event_count=1,
        expected_fold_count=3,
        embargo_bars=0,
    )
    assert incomplete.actual_fold_count == 1
    assert incomplete.coverage_complete is False
    with pytest.raises(ValueError, match="test folds disjoint"):
        make_purged_event_folds(
            events,
            train_event_count=3,
            validation_event_count=1,
            test_event_count=2,
            step_event_count=1,
            expected_fold_count=2,
            embargo_bars=0,
        )


def test_invalid_barrier_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="short_window"):
        TripleBarrierConfig(short_window=48, long_window=12)
    with pytest.raises(ValueError, match="round_trip_cost_bps"):
        TripleBarrierConfig(round_trip_cost_bps=10_000)
