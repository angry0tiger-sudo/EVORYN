"""Deterministic quality gates for normalized market candles; no data is fetched here."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from evoryn.domain.models import MarketCandle, Market


class DataQualityCode(StrEnum):
    """Stable machine-readable reasons why a market-data batch is not acceptable."""

    EMPTY_SERIES = "empty_series"
    WRONG_MARKET = "wrong_market"
    WRONG_INTERVAL = "wrong_interval"
    STALE_DATA = "stale_data"
    FUTURE_TIMESTAMP = "future_timestamp"
    DUPLICATE_TIMESTAMP = "duplicate_timestamp"
    OUT_OF_ORDER = "out_of_order"
    TIME_GAP = "time_gap"
    INCOMPLETE_COVERAGE = "incomplete_coverage"


@dataclass(frozen=True, slots=True)
class MarketDataQualityPolicy:
    """Required identity, cadence, freshness, and optional coverage for one data batch."""

    market: Market
    interval: timedelta
    max_age: timedelta
    max_future_skew: timedelta = timedelta(0)
    expected_start: datetime | None = None
    expected_end: datetime | None = None

    def __post_init__(self) -> None:
        """Reject invalid policy durations and timezone-naive coverage boundaries."""
        if self.interval <= timedelta(0):
            raise ValueError("Quality policy interval must be positive.")
        if self.max_age <= timedelta(0):
            raise ValueError("Quality policy max_age must be positive.")
        if self.max_future_skew < timedelta(0):
            raise ValueError("Quality policy max_future_skew must not be negative.")
        for name, boundary in (
            ("expected_start", self.expected_start),
            ("expected_end", self.expected_end),
        ):
            if boundary is not None and (
                boundary.tzinfo is None or boundary.utcoffset() is None
            ):
                raise ValueError(f"{name} must include a timezone.")
        if (
            self.expected_start is not None
            and self.expected_end is not None
            and self.expected_start > self.expected_end
        ):
            raise ValueError("expected_start must not be later than expected_end.")


@dataclass(frozen=True, slots=True)
class DataQualityIssue:
    """One deterministic quality failure with an optional timestamp and gap size."""

    code: DataQualityCode
    message: str
    timestamp: datetime | None = None
    missing_intervals: int = 0


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    """Read-only result of a quality check, including every detected issue."""

    checked_count: int
    issues: tuple[DataQualityIssue, ...]

    @property
    def accepted(self) -> bool:
        """Return true only when the entire batch satisfies every enabled gate."""
        return not self.issues


def evaluate_market_data(
    candles: Sequence[MarketCandle],
    *,
    as_of: datetime,
    policy: MarketDataQualityPolicy,
) -> DataQualityReport:
    """Evaluate identity, freshness, temporal order, duplicates, gaps, and coverage.

    The function never repairs, reorders, deduplicates, or drops rows. Any issue
    causes the whole batch to be rejected so downstream components can fail closed.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include a timezone.")
    now = as_of.astimezone(UTC)
    issues: list[DataQualityIssue] = []

    if not candles:
        return DataQualityReport(
            checked_count=0,
            issues=(
                DataQualityIssue(
                    code=DataQualityCode.EMPTY_SERIES,
                    message="No candles were provided.",
                ),
            ),
        )

    seen_timestamps: set[datetime] = set()
    previous_timestamp: datetime | None = None
    observed_timestamps: list[datetime] = []

    for candle in candles:
        timestamp = candle.timestamp.astimezone(UTC)
        observed_timestamps.append(timestamp)

        if candle.market != policy.market:
            issues.append(
                DataQualityIssue(
                    code=DataQualityCode.WRONG_MARKET,
                    message="Candle market does not match the requested market.",
                    timestamp=timestamp,
                )
            )
        if candle.interval != policy.interval:
            issues.append(
                DataQualityIssue(
                    code=DataQualityCode.WRONG_INTERVAL,
                    message="Candle interval does not match the expected interval.",
                    timestamp=timestamp,
                )
            )
        if timestamp > now + policy.max_future_skew:
            issues.append(
                DataQualityIssue(
                    code=DataQualityCode.FUTURE_TIMESTAMP,
                    message="Candle timestamp is beyond the permitted future skew.",
                    timestamp=timestamp,
                )
            )
        elif now - timestamp > policy.max_age:
            issues.append(
                DataQualityIssue(
                    code=DataQualityCode.STALE_DATA,
                    message="Candle is older than the configured freshness limit.",
                    timestamp=timestamp,
                )
            )

        if timestamp in seen_timestamps:
            issues.append(
                DataQualityIssue(
                    code=DataQualityCode.DUPLICATE_TIMESTAMP,
                    message="Candle timestamp occurs more than once.",
                    timestamp=timestamp,
                )
            )
        seen_timestamps.add(timestamp)

        if previous_timestamp is not None:
            delta = timestamp - previous_timestamp
            if delta < timedelta(0):
                issues.append(
                    DataQualityIssue(
                        code=DataQualityCode.OUT_OF_ORDER,
                        message="Candle timestamps are not monotonically increasing.",
                        timestamp=timestamp,
                    )
                )
            elif delta > policy.interval:
                missing = max(int(delta // policy.interval) - 1, 1)
                issues.append(
                    DataQualityIssue(
                        code=DataQualityCode.TIME_GAP,
                        message=f"Temporal gap detected; at least {missing} interval(s) are missing.",
                        timestamp=timestamp,
                        missing_intervals=missing,
                    )
                )
        previous_timestamp = timestamp

    first_observed = min(observed_timestamps)
    last_observed = max(observed_timestamps)
    if policy.expected_start is not None:
        expected_start = policy.expected_start.astimezone(UTC)
        if first_observed > expected_start:
            missing = max(
                int((first_observed - expected_start) // policy.interval),
                1,
            )
            issues.append(
                DataQualityIssue(
                    code=DataQualityCode.INCOMPLETE_COVERAGE,
                    message="Series starts later than the required coverage boundary.",
                    timestamp=first_observed,
                    missing_intervals=missing,
                )
            )
    if policy.expected_end is not None:
        expected_end = policy.expected_end.astimezone(UTC)
        if last_observed < expected_end:
            missing = max(
                int((expected_end - last_observed) // policy.interval),
                1,
            )
            issues.append(
                DataQualityIssue(
                    code=DataQualityCode.INCOMPLETE_COVERAGE,
                    message="Series ends earlier than the required coverage boundary.",
                    timestamp=last_observed,
                    missing_intervals=missing,
                )
            )

    return DataQualityReport(checked_count=len(candles), issues=tuple(issues))
