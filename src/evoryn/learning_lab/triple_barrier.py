"""Causal, research-only Triple-Barrier event labels with purged chronological splits.

Only completed candles at or before an event determine its signal, features, volatility,
and frozen barrier prices. Later OHLC observations are read solely to assign the label.
The module has no exchange client, model loader, runtime promotion, or order interface.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from evoryn.domain.models import MarketCandle

TRIPLE_BARRIER_SCHEMA_VERSION = "1.0.0-sma12-48-closed-bar"
FEATURE_SCHEMA_VERSION = "1.0.0-event-time-only"
FEATURE_NAMES = (
    "sma_short_gap",
    "sma_spread_fraction",
    "return_1",
    "return_6",
    "trailing_volatility",
    "utc_hour_sin",
    "utc_hour_cos",
)


class BarrierOutcome(StrEnum):
    """First barrier outcome for a bullish, long-only research event."""

    PROFIT_TAKING = "profit_taking"
    STOP_LOSS = "stop_loss"
    VERTICAL_EXPIRY = "vertical_expiry"


@dataclass(frozen=True, slots=True)
class TripleBarrierConfig:
    """Predeclared label geometry; values are never tuned on test outcomes."""

    short_window: int = 12
    long_window: int = 48
    volatility_lookback: int = 48
    vertical_horizon_bars: int = 24
    profit_taking_multiple: float = 1.5
    stop_loss_multiple: float = 1.0
    round_trip_cost_bps: float = 30.0

    def __post_init__(self) -> None:
        for name in (
            "short_window",
            "long_window",
            "volatility_lookback",
            "vertical_horizon_bars",
        ):
            value = getattr(self, name)
            minimum = 2 if name == "volatility_lookback" else 1
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}.")
        if self.short_window >= self.long_window:
            raise ValueError("short_window must be smaller than long_window.")
        for name in ("profit_taking_multiple", "stop_loss_multiple"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive.")
        if (
            not math.isfinite(self.round_trip_cost_bps)
            or not 0 <= self.round_trip_cost_bps < 10_000
        ):
            raise ValueError("round_trip_cost_bps must be finite and in [0, 10000).")


@dataclass(frozen=True, slots=True)
class TripleBarrierEvent:
    """One event with an immutable event-time feature row and an inclusive label interval."""

    event_id: str
    event_index: int
    event_timestamp: datetime
    label_start_index: int
    label_end_index: int
    label_start_timestamp: datetime
    label_end_timestamp: datetime
    reference_price: Decimal
    profit_taking_price: Decimal
    stop_loss_price: Decimal
    trailing_volatility: float
    feature_names: tuple[str, ...]
    feature_values: tuple[float, ...]
    feature_schema_version: str
    outcome: BarrierOutcome
    exit_price: Decimal
    gross_return_fraction: float
    net_return_fraction: float
    round_trip_cost_bps: float
    holding_bars: int
    ambiguous_horizontal_touch: bool

    def __post_init__(self) -> None:
        if not self.event_id or len(self.event_id) != 64:
            raise ValueError("event_id must be a SHA-256 hex digest.")
        if self.event_index < 0 or self.label_start_index != self.event_index:
            raise ValueError("Label start must equal the non-negative event index.")
        if self.label_end_index < self.label_start_index:
            raise ValueError("Label interval end must not precede its start.")
        if self.holding_bars != self.label_end_index - self.event_index:
            raise ValueError("holding_bars must match the inclusive event interval geometry.")
        if self.label_start_timestamp != self.event_timestamp:
            raise ValueError("Label start timestamp must equal event timestamp.")
        if self.label_end_timestamp < self.label_start_timestamp:
            raise ValueError("Label end timestamp must not precede event time.")
        prices = (
            self.reference_price,
            self.profit_taking_price,
            self.stop_loss_price,
            self.exit_price,
        )
        if any(not value.is_finite() or value <= 0 for value in prices):
            raise ValueError("Triple-barrier prices must be finite and positive.")
        if not self.stop_loss_price < self.reference_price < self.profit_taking_price:
            raise ValueError("Frozen barriers must surround the event reference price.")
        numeric = (
            self.trailing_volatility,
            self.gross_return_fraction,
            self.net_return_fraction,
            self.round_trip_cost_bps,
            *self.feature_values,
        )
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("Triple-barrier feature and outcome values must be finite.")
        if self.trailing_volatility <= 0:
            raise ValueError("An event must have positive trailing volatility.")
        if self.feature_names != FEATURE_NAMES or len(self.feature_values) != len(FEATURE_NAMES):
            raise ValueError("Feature schema does not match the versioned event-time contract.")
        if self.feature_schema_version != FEATURE_SCHEMA_VERSION:
            raise ValueError("Unsupported event-time feature schema version.")
        if not isinstance(self.outcome, BarrierOutcome):
            raise ValueError("Unknown Triple-Barrier outcome.")
        if type(self.ambiguous_horizontal_touch) is not bool:
            raise ValueError("ambiguous_horizontal_touch must be boolean.")


@dataclass(frozen=True, slots=True)
class TripleBarrierBuildResult:
    """Build output includes dropped-event reasons so incomplete labels are not hidden."""

    events: tuple[TripleBarrierEvent, ...]
    detected_bullish_crossovers: int
    incomplete_horizon_skipped: int
    zero_volatility_skipped: int
    invalid_barrier_geometry_skipped: int

    @property
    def emitted_event_count(self) -> int:
        """Count labels that have a complete, valid forward interval."""
        return len(self.events)

    @property
    def outcome_counts(self) -> dict[str, int]:
        """Return deterministic counts for all three label outcomes, including zero counts."""
        counts = Counter(event.outcome.value for event in self.events)
        return {outcome.value: counts.get(outcome.value, 0) for outcome in BarrierOutcome}


@dataclass(frozen=True, slots=True)
class PurgedEventFold:
    """One chronological fold after interval purging and boundary embargo."""

    fold_index: int
    train_events: tuple[TripleBarrierEvent, ...]
    validation_events: tuple[TripleBarrierEvent, ...]
    test_events: tuple[TripleBarrierEvent, ...]
    purged_train_count: int
    purged_validation_count: int
    embargo_bars: int
    status: str


@dataclass(frozen=True, slots=True)
class PurgedEventFoldSet:
    """Fold manifest whose coverage gate explicitly fails on missing or empty folds."""

    folds: tuple[PurgedEventFold, ...]
    expected_fold_count: int
    actual_fold_count: int

    @property
    def coverage_complete(self) -> bool:
        """True only when every expected fold exists and every split is evaluable."""
        return (
            self.actual_fold_count == self.expected_fold_count
            and all(fold.status == "EVALUABLE" for fold in self.folds)
        )


def _validate_candles(candles: Sequence[MarketCandle]) -> None:
    if not candles:
        raise ValueError("Triple-Barrier labeling requires non-empty closed candles.")
    market = candles[0].market
    interval = candles[0].interval
    if market.market_type.casefold() != "spot" or not market.symbol.upper().endswith("-USDT"):
        raise ValueError("Triple-Barrier research is restricted to Spot/USDT datasets.")
    for candle in candles:
        if candle.market != market:
            raise ValueError("Triple-Barrier input cannot mix markets.")
        if candle.interval != interval:
            raise ValueError("Triple-Barrier input cannot mix candle intervals.")
        if candle.timestamp.tzinfo is None or candle.timestamp.utcoffset() is None:
            raise ValueError("Candle timestamps must be timezone-aware.")
        if any(
            not price.is_finite() or price <= 0
            for price in (candle.open, candle.high, candle.low, candle.close)
        ):
            raise ValueError("Triple-Barrier OHLC values must be finite and positive.")
    for left, right in zip(candles, candles[1:], strict=False):
        if right.timestamp - left.timestamp != interval:
            raise ValueError("Triple-Barrier research rejects duplicate or gapped candles.")


def _mean_decimal(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("Cannot calculate a mean from an empty sequence.")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _sample_std(values: Sequence[float]) -> float:
    if len(values) < 2:
        raise ValueError("At least two returns are required for volatility estimation.")
    center = sum(values) / len(values)
    variance = sum((value - center) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def _returns_through_event(candles: Sequence[MarketCandle], index: int, count: int) -> list[float]:
    closes = candles[index - count : index + 1]
    if len(closes) != count + 1:
        raise ValueError("Insufficient event-time candle history for returns.")
    return [
        math.log(float(right.close / left.close))
        for left, right in zip(closes, closes[1:], strict=False)
    ]


def _feature_snapshot(
    candles: Sequence[MarketCandle],
    index: int,
    config: TripleBarrierConfig,
    trailing_volatility: float,
) -> tuple[float, ...]:
    """Every field is a function of candles[:index + 1] and the event timestamp only."""
    current_close = candles[index].close
    short_sma = _mean_decimal(
        [candle.close for candle in candles[index - config.short_window + 1 : index + 1]]
    )
    long_sma = _mean_decimal(
        [candle.close for candle in candles[index - config.long_window + 1 : index + 1]]
    )
    return_1 = float(current_close / candles[index - 1].close - Decimal("1"))
    return_6 = float(current_close / candles[index - 6].close - Decimal("1"))
    event_close_time = (candles[index].timestamp + candles[index].interval).astimezone(UTC)
    fractional_hour = event_close_time.hour + event_close_time.minute / 60.0
    angle = 2.0 * math.pi * fractional_hour / 24.0
    row = (
        float(current_close / short_sma - Decimal("1")),
        float(short_sma / long_sma - Decimal("1")),
        return_1,
        return_6,
        trailing_volatility,
        math.sin(angle),
        math.cos(angle),
    )
    if any(not math.isfinite(value) for value in row):
        raise ValueError("Event-time feature generation produced a non-finite value.")
    return row


def _is_bullish_sma_cross(
    candles: Sequence[MarketCandle], index: int, config: TripleBarrierConfig
) -> bool:
    """Detect a completed-bar-only SMA cross, using no values after index."""
    previous_short = _mean_decimal(
        [candle.close for candle in candles[index - config.short_window : index]]
    )
    previous_long = _mean_decimal(
        [candle.close for candle in candles[index - config.long_window : index]]
    )
    current_short = _mean_decimal(
        [candle.close for candle in candles[index - config.short_window + 1 : index + 1]]
    )
    current_long = _mean_decimal(
        [candle.close for candle in candles[index - config.long_window + 1 : index + 1]]
    )
    return previous_short <= previous_long and current_short > current_long


def _event_identifier(
    candle: MarketCandle,
    event_index: int,
    config: TripleBarrierConfig,
) -> str:
    canonical = {
        "schema_version": TRIPLE_BARRIER_SCHEMA_VERSION,
        "market": {
            "symbol": candle.market.symbol,
            "venue": candle.market.venue,
            "market_type": candle.market.market_type,
        },
        "interval_microseconds": candle.interval // timedelta(microseconds=1),
        "event_index": event_index,
        "event_close_time": (candle.timestamp + candle.interval).astimezone(UTC).isoformat(),
        "config": asdict(config),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_triple_barrier_events(
    candles: Sequence[MarketCandle],
    config: TripleBarrierConfig | None = None,
) -> TripleBarrierBuildResult:
    """Generate bullish SMA-12/48 events and label the first frozen barrier touched.

    The reference and all barriers are fixed to the event candle close. Only later candles
    may affect the outcome. If a candle hits both horizontal barriers, STOP_LOSS is assigned
    and the exit uses the adverse stop gap when applicable. Target gaps never earn more than
    the frozen target price. Incomplete vertical horizons are counted and excluded.
    """
    options = config or TripleBarrierConfig()
    _validate_candles(candles)
    detected = 0
    incomplete = 0
    zero_volatility = 0
    invalid_geometry = 0
    events: list[TripleBarrierEvent] = []
    first_index = max(options.long_window, options.volatility_lookback, 6)
    for index in range(first_index, len(candles)):
        if not _is_bullish_sma_cross(candles, index, options):
            continue
        detected += 1
        if index + options.vertical_horizon_bars >= len(candles):
            incomplete += 1
            continue
        returns = _returns_through_event(candles, index, options.volatility_lookback)
        volatility = _sample_std(returns)
        if not math.isfinite(volatility) or volatility <= 0:
            zero_volatility += 1
            continue
        horizon_scale = math.sqrt(options.vertical_horizon_bars)
        profit_fraction = volatility * horizon_scale * options.profit_taking_multiple
        stop_fraction = volatility * horizon_scale * options.stop_loss_multiple
        if (
            not math.isfinite(profit_fraction)
            or not math.isfinite(stop_fraction)
            or profit_fraction <= 0
            or stop_fraction <= 0
            or stop_fraction >= 1
        ):
            invalid_geometry += 1
            continue

        reference_price = candles[index].close
        profit_price = reference_price * (Decimal("1") + Decimal(str(profit_fraction)))
        stop_price = reference_price * (Decimal("1") - Decimal(str(stop_fraction)))
        exit_index = index + options.vertical_horizon_bars
        outcome = BarrierOutcome.VERTICAL_EXPIRY
        exit_price = candles[exit_index].close
        ambiguous = False
        for future_index in range(index + 1, index + options.vertical_horizon_bars + 1):
            future = candles[future_index]
            profit_hit = future.high >= profit_price
            stop_hit = future.low <= stop_price
            if profit_hit and stop_hit:
                outcome = BarrierOutcome.STOP_LOSS
                exit_index = future_index
                exit_price = min(future.open, stop_price)
                ambiguous = True
                break
            if stop_hit:
                outcome = BarrierOutcome.STOP_LOSS
                exit_index = future_index
                exit_price = min(future.open, stop_price)
                break
            if profit_hit:
                outcome = BarrierOutcome.PROFIT_TAKING
                exit_index = future_index
                # Conservatively cap favorable gaps at the pre-frozen target level.
                exit_price = profit_price
                break

        gross_return = float(exit_price / reference_price - Decimal("1"))
        net_return = gross_return - options.round_trip_cost_bps / 10_000.0
        event_time = (candles[index].timestamp + candles[index].interval).astimezone(UTC)
        exit_time = (candles[exit_index].timestamp + candles[exit_index].interval).astimezone(UTC)
        events.append(
            TripleBarrierEvent(
                event_id=_event_identifier(candles[index], index, options),
                event_index=index,
                event_timestamp=event_time,
                label_start_index=index,
                label_end_index=exit_index,
                label_start_timestamp=event_time,
                label_end_timestamp=exit_time,
                reference_price=reference_price,
                profit_taking_price=profit_price,
                stop_loss_price=stop_price,
                trailing_volatility=volatility,
                feature_names=FEATURE_NAMES,
                feature_values=_feature_snapshot(candles, index, options, volatility),
                feature_schema_version=FEATURE_SCHEMA_VERSION,
                outcome=outcome,
                exit_price=exit_price,
                gross_return_fraction=gross_return,
                net_return_fraction=net_return,
                round_trip_cost_bps=options.round_trip_cost_bps,
                holding_bars=exit_index - index,
                ambiguous_horizontal_touch=ambiguous,
            )
        )
    return TripleBarrierBuildResult(
        events=tuple(events),
        detected_bullish_crossovers=detected,
        incomplete_horizon_skipped=incomplete,
        zero_volatility_skipped=zero_volatility,
        invalid_barrier_geometry_skipped=invalid_geometry,
    )


def purge_overlapping_events(
    training_events: Sequence[TripleBarrierEvent],
    evaluation_events: Sequence[TripleBarrierEvent],
    *,
    embargo_bars: int = 0,
) -> tuple[TripleBarrierEvent, ...]:
    """Remove training events overlapping evaluation label intervals expanded by embargo.

    Each label interval is inclusive: [label_start_index, label_end_index]. Embargo expands
    every evaluation interval on both sides in bar-index space. The function is conservative
    and agnostic about whether the evaluation partition is validation or test.
    """
    if type(embargo_bars) is not int or embargo_bars < 0:
        raise ValueError("embargo_bars must be a non-negative integer.")
    if not training_events or not evaluation_events:
        return tuple(training_events)
    kept: list[TripleBarrierEvent] = []
    for candidate in training_events:
        overlaps = any(
            candidate.label_start_index <= target.label_end_index + embargo_bars
            and target.label_start_index - embargo_bars <= candidate.label_end_index
            for target in evaluation_events
        )
        if not overlaps:
            kept.append(candidate)
    return tuple(kept)


def make_purged_event_folds(
    events: Sequence[TripleBarrierEvent],
    *,
    train_event_count: int,
    validation_event_count: int,
    test_event_count: int,
    step_event_count: int,
    expected_fold_count: int,
    embargo_bars: int = 0,
) -> PurgedEventFoldSet:
    """Create expanding chronological folds and make incomplete coverage explicit.

    Fold geometry is specified in event counts, while purge/embargo use original candle
    indices. No random split is used. Events are not silently sorted or backfilled.
    """
    for name, value in (
        ("train_event_count", train_event_count),
        ("validation_event_count", validation_event_count),
        ("test_event_count", test_event_count),
        ("step_event_count", step_event_count),
        ("expected_fold_count", expected_fold_count),
    ):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer.")
    if type(embargo_bars) is not int or embargo_bars < 0:
        raise ValueError("embargo_bars must be a non-negative integer.")
    if step_event_count < test_event_count:
        raise ValueError("step_event_count must be >= test_event_count to keep test folds disjoint.")
    indices = [event.event_index for event in events]
    if indices != sorted(indices) or len(indices) != len(set(indices)):
        raise ValueError("Events must be in strict chronological order with unique bar indices.")
    ids = [event.event_id for event in events]
    if len(ids) != len(set(ids)):
        raise ValueError("Event IDs must be unique.")

    folds: list[PurgedEventFold] = []
    first_possible_test_start = train_event_count + validation_event_count
    if len(events) < first_possible_test_start + test_event_count:
        return PurgedEventFoldSet(
            folds=(), expected_fold_count=expected_fold_count, actual_fold_count=0
        )
    available_fold_count = (
        (len(events) - test_event_count - first_possible_test_start) // step_event_count
    ) + 1
    actual_fold_count = min(expected_fold_count, available_fold_count)
    # Anchor the planned folds at the latest event; older observations are retained in the
    # expanding training windows rather than creating unplanned extra folds.
    first_test_start = len(events) - test_event_count - (actual_fold_count - 1) * step_event_count
    for fold_number in range(actual_fold_count):
        test_start = first_test_start + fold_number * step_event_count
        validation_start = test_start - validation_event_count
        train_candidates = tuple(events[:validation_start])
        validation_candidates = tuple(events[validation_start:test_start])
        test = tuple(events[test_start : test_start + test_event_count])
        evaluation_for_train = (*validation_candidates, *test)
        train = purge_overlapping_events(
            train_candidates, evaluation_for_train, embargo_bars=embargo_bars
        )
        validation = purge_overlapping_events(
            validation_candidates, test, embargo_bars=embargo_bars
        )
        status = (
            "EVALUABLE"
            if train and validation and len(test) == test_event_count
            else "NOT_EVALUABLE"
        )
        folds.append(
            PurgedEventFold(
                fold_index=len(folds),
                train_events=train,
                validation_events=validation,
                test_events=test,
                purged_train_count=len(train_candidates) - len(train),
                purged_validation_count=len(validation_candidates) - len(validation),
                embargo_bars=embargo_bars,
                status=status,
            )
        )
    return PurgedEventFoldSet(
        folds=tuple(folds),
        expected_fold_count=expected_fold_count,
        actual_fold_count=len(folds),
    )


def label_distribution(events: Sequence[TripleBarrierEvent]) -> dict[str, float | int | None]:
    """Summarize event labels and cost-adjusted returns without treating them as strategy P&L."""
    counts = Counter(event.outcome.value for event in events)
    total = len(events)
    net_returns = sorted(event.net_return_fraction for event in events)
    holding = sorted(event.holding_bars for event in events)

    def median(values: Sequence[float] | Sequence[int]) -> float | None:
        if not values:
            return None
        middle = len(values) // 2
        if len(values) % 2:
            return float(values[middle])
        return (float(values[middle - 1]) + float(values[middle])) / 2.0

    return {
        "event_count": total,
        "profit_taking_count": counts.get(BarrierOutcome.PROFIT_TAKING.value, 0),
        "stop_loss_count": counts.get(BarrierOutcome.STOP_LOSS.value, 0),
        "vertical_expiry_count": counts.get(BarrierOutcome.VERTICAL_EXPIRY.value, 0),
        "ambiguous_horizontal_touch_count": sum(
            event.ambiguous_horizontal_touch for event in events
        ),
        "mean_holding_bars": (
            sum(event.holding_bars for event in events) / total if total else None
        ),
        "median_holding_bars": median(holding),
        "mean_gross_event_return_fraction": (
            sum(event.gross_return_fraction for event in events) / total if total else None
        ),
        "mean_net_event_return_fraction_after_assumed_costs": (
            sum(net_returns) / total if total else None
        ),
        "median_net_event_return_fraction_after_assumed_costs": median(net_returns),
        "net_positive_event_fraction": (
            sum(value > 0 for value in net_returns) / total if total else None
        ),
    }
