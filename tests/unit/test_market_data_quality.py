"""Unit tests for market-data normalization and fail-closed quality gates."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from evoryn.domain.models import Market, MarketCandle
from evoryn.infrastructure.data.normalization import normalize_candle, normalize_candles
from evoryn.infrastructure.data.quality import (
    DataQualityCode,
    MarketDataQualityPolicy,
    evaluate_market_data,
)


@pytest.fixture
def market() -> Market:
    """Return a synthetic market identity without a live venue dependency."""
    return Market(symbol="BTC-USDT", venue="fixture", market_type="spot")


@pytest.fixture
def as_of() -> datetime:
    """Return a fixed timezone-aware reference time for deterministic freshness tests."""
    return datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _raw_row(timestamp: datetime, **overrides: object) -> dict[str, object]:
    """Build one synthetic OHLCV row and apply explicit test overrides."""
    row: dict[str, object] = {
        "timestamp": timestamp.isoformat(),
        "open": "100.00",
        "high": "105.00",
        "low": "99.00",
        "close": "102.00",
        "volume": "12.50",
    }
    row.update(overrides)
    row["timestamp"] = timestamp.isoformat()
    return row


@pytest.fixture
def raw_rows(as_of: datetime) -> tuple[dict[str, object], ...]:
    """Return two valid, consecutive synthetic one-minute candles."""
    return (
        _raw_row(as_of - timedelta(minutes=2)),
        _raw_row(
            as_of - timedelta(minutes=1),
            open="102.00",
            high="104.00",
            low="101.00",
            close="103.00",
            volume="8.00",
        ),
    )


@pytest.fixture
def normalized_candles(
    market: Market, raw_rows: tuple[dict[str, object], ...]
) -> tuple[MarketCandle, ...]:
    """Normalize the standard synthetic fixture through the production helper."""
    return normalize_candles(
        raw_rows,
        market=market,
        interval=timedelta(minutes=1),
        source="unit-test-fixture",
    )


@pytest.fixture
def policy(market: Market, as_of: datetime) -> MarketDataQualityPolicy:
    """Return a policy that accepts the two-row synthetic fixture."""
    return MarketDataQualityPolicy(
        market=market,
        interval=timedelta(minutes=1),
        max_age=timedelta(minutes=5),
        expected_start=as_of - timedelta(minutes=2),
        expected_end=as_of - timedelta(minutes=1),
    )


def test_normalization_converts_decimal_values_and_utc_timestamp(market: Market) -> None:
    """ISO timestamps ending in Z and decimal strings normalize without float math."""
    candle = normalize_candle(
        {
            "timestamp": "2026-10-18T11:58:00Z",
            "open": "100.10",
            "high": "101.20",
            "low": "99.90",
            "close": "100.75",
            "volume": "12.345",
        },
        market=market,
        interval=timedelta(minutes=1),
        source="fixture",
    )

    assert candle.timestamp == datetime(2026, 10, 18, 11, 58, tzinfo=UTC)
    assert candle.open == Decimal("100.10")
    assert candle.volume == Decimal("12.345")


def test_normalization_rejects_missing_required_fields(market: Market) -> None:
    """A missing OHLCV field rejects the row rather than fabricating a value."""
    with pytest.raises(ValueError, match="Missing required market fields"):
        normalize_candle(
            {"timestamp": "2026-10-18T11:58:00Z", "open": "100"},
            market=market,
            interval=timedelta(minutes=1),
            source="fixture",
        )


def test_normalization_rejects_timezone_naive_timestamps(market: Market) -> None:
    """Naive timestamps are rejected instead of assuming a local timezone."""
    with pytest.raises(ValueError, match="explicit timezone"):
        normalize_candle(
            {
                "timestamp": "2026-10-18T11:58:00",
                "open": "100",
                "high": "101",
                "low": "99",
                "close": "100",
                "volume": "1",
            },
            market=market,
            interval=timedelta(minutes=1),
            source="fixture",
        )


def test_normalization_rejects_impossible_ohlc_and_non_finite_values(market: Market) -> None:
    """Malformed high/low relationships and NaN values cannot enter the normalized model."""
    with pytest.raises(ValueError, match="High must be"):
        normalize_candle(
            _raw_row(
                datetime(2026, 10, 18, 11, 58, tzinfo=UTC),
                high="98",
            ),
            market=market,
            interval=timedelta(minutes=1),
            source="fixture",
        )

    with pytest.raises(ValueError, match="must be finite"):
        normalize_candle(
            _raw_row(
                datetime(2026, 10, 18, 11, 58, tzinfo=UTC),
                close="NaN",
            ),
            market=market,
            interval=timedelta(minutes=1),
            source="fixture",
        )


def test_quality_gate_accepts_valid_complete_fresh_series(
    normalized_candles: tuple[MarketCandle, ...],
    as_of: datetime,
    policy: MarketDataQualityPolicy,
) -> None:
    """Consecutive candles matching policy identity and required coverage pass."""
    report = evaluate_market_data(normalized_candles, as_of=as_of, policy=policy)

    assert report.accepted
    assert report.checked_count == 2
    assert report.issues == ()


def test_quality_gate_rejects_empty_series(
    as_of: datetime, policy: MarketDataQualityPolicy
) -> None:
    """An empty input is explicitly rejected."""
    report = evaluate_market_data((), as_of=as_of, policy=policy)

    assert not report.accepted
    assert report.issues[0].code is DataQualityCode.EMPTY_SERIES


def test_quality_gate_rejects_stale_data(
    normalized_candles: tuple[MarketCandle, ...],
    as_of: datetime,
    policy: MarketDataQualityPolicy,
) -> None:
    """Rows older than the configured freshness limit cannot pass."""
    stale_policy = replace(policy, max_age=timedelta(seconds=30))
    report = evaluate_market_data(normalized_candles, as_of=as_of, policy=stale_policy)

    assert not report.accepted
    assert any(issue.code is DataQualityCode.STALE_DATA for issue in report.issues)


def test_quality_gate_detects_duplicate_timestamps(
    normalized_candles: tuple[MarketCandle, ...],
    as_of: datetime,
    policy: MarketDataQualityPolicy,
) -> None:
    """Duplicate timestamps are reported and the batch is rejected."""
    duplicate_series = (normalized_candles[0], normalized_candles[0])
    report = evaluate_market_data(duplicate_series, as_of=as_of, policy=policy)

    assert not report.accepted
    assert any(issue.code is DataQualityCode.DUPLICATE_TIMESTAMP for issue in report.issues)


def test_quality_gate_detects_out_of_order_candles(
    normalized_candles: tuple[MarketCandle, ...],
    as_of: datetime,
    policy: MarketDataQualityPolicy,
) -> None:
    """The gate reports unordered input instead of silently sorting it."""
    report = evaluate_market_data(tuple(reversed(normalized_candles)), as_of=as_of, policy=policy)

    assert not report.accepted
    assert any(issue.code is DataQualityCode.OUT_OF_ORDER for issue in report.issues)


def test_quality_gate_detects_temporal_gaps(
    market: Market, as_of: datetime
) -> None:
    """Missing one-minute candles are reported with an interval count."""
    rows = (
        _raw_row(as_of - timedelta(minutes=4)),
        _raw_row(as_of - timedelta(minutes=1)),
    )
    candles = normalize_candles(
        rows,
        market=market,
        interval=timedelta(minutes=1),
        source="fixture",
    )
    policy = MarketDataQualityPolicy(
        market=market,
        interval=timedelta(minutes=1),
        max_age=timedelta(minutes=10),
    )
    report = evaluate_market_data(candles, as_of=as_of, policy=policy)
    gap_issues = tuple(
        issue for issue in report.issues if issue.code is DataQualityCode.TIME_GAP
    )

    assert not report.accepted
    assert len(gap_issues) == 1
    assert gap_issues[0].missing_intervals == 2


def test_quality_gate_detects_future_timestamps(
    market: Market, as_of: datetime
) -> None:
    """A candle beyond permitted clock skew is rejected."""
    candle = normalize_candle(
        _raw_row(as_of + timedelta(minutes=1)),
        market=market,
        interval=timedelta(minutes=1),
        source="fixture",
    )
    policy = MarketDataQualityPolicy(
        market=market,
        interval=timedelta(minutes=1),
        max_age=timedelta(minutes=5),
    )
    report = evaluate_market_data((candle,), as_of=as_of, policy=policy)

    assert not report.accepted
    assert any(issue.code is DataQualityCode.FUTURE_TIMESTAMP for issue in report.issues)


def test_quality_gate_checks_market_and_interval_identity(
    market: Market, as_of: datetime
) -> None:
    """Wrong market identity and candle cadence are independently reported."""
    other_market = Market(symbol="ETH-USDT", venue="fixture", market_type="spot")
    candle = normalize_candle(
        _raw_row(as_of - timedelta(minutes=1)),
        market=other_market,
        interval=timedelta(minutes=2),
        source="fixture",
    )
    policy = MarketDataQualityPolicy(
        market=market,
        interval=timedelta(minutes=1),
        max_age=timedelta(minutes=5),
    )
    report = evaluate_market_data((candle,), as_of=as_of, policy=policy)
    codes = {issue.code for issue in report.issues}

    assert not report.accepted
    assert DataQualityCode.WRONG_MARKET in codes
    assert DataQualityCode.WRONG_INTERVAL in codes


def test_quality_gate_detects_incomplete_coverage(
    normalized_candles: tuple[MarketCandle, ...],
    as_of: datetime,
    policy: MarketDataQualityPolicy,
) -> None:
    """An optional expected start boundary detects missing leading intervals."""
    incomplete_policy = replace(
        policy,
        expected_start=as_of - timedelta(minutes=4),
        expected_end=None,
    )
    report = evaluate_market_data(normalized_candles, as_of=as_of, policy=incomplete_policy)

    assert not report.accepted
    assert any(
        issue.code is DataQualityCode.INCOMPLETE_COVERAGE for issue in report.issues
    )
