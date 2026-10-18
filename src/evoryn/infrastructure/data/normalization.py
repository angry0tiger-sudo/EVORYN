"""Pure normalization helpers for already-acquired market data; no network I/O occurs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from evoryn.domain.models import Market, MarketCandle


def _parse_decimal(value: object, *, field_name: str) -> Decimal:
    """Convert supported scalar input to a finite Decimal without binary-float arithmetic."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{field_name} must be a decimal-compatible scalar.")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} is not a valid decimal value.") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite.")
    return parsed


def _parse_timestamp(value: object) -> datetime:
    """Parse a timezone-aware datetime or ISO-8601 string and normalize it to UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("timestamp must be a valid ISO-8601 string.") from exc
    else:
        raise ValueError("timestamp must be a timezone-aware datetime or ISO-8601 string.")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include an explicit timezone.")
    return parsed.astimezone(UTC)


def normalize_candle(
    raw: Mapping[str, object],
    *,
    market: Market,
    interval: timedelta,
    source: str,
) -> MarketCandle:
    """Normalize one OHLCV row into a validated canonical candle.

    The row must contain timestamp, open, high, low, close, and volume fields.
    Provider-specific aliases are intentionally not guessed; map them explicitly at
    a future adapter boundary before calling this function.
    """
    if interval <= timedelta(0):
        raise ValueError("interval must be positive.")
    if not source.strip():
        raise ValueError("source must not be empty.")

    required_fields = ("timestamp", "open", "high", "low", "close", "volume")
    missing_fields = tuple(field for field in required_fields if field not in raw)
    if missing_fields:
        raise ValueError(f"Missing required market fields: {', '.join(missing_fields)}.")

    return MarketCandle(
        market=market,
        timestamp=_parse_timestamp(raw["timestamp"]),
        interval=interval,
        open=_parse_decimal(raw["open"], field_name="open"),
        high=_parse_decimal(raw["high"], field_name="high"),
        low=_parse_decimal(raw["low"], field_name="low"),
        close=_parse_decimal(raw["close"], field_name="close"),
        volume=_parse_decimal(raw["volume"], field_name="volume"),
        source=source,
    )


def normalize_candles(
    rows: Sequence[Mapping[str, object]],
    *,
    market: Market,
    interval: timedelta,
    source: str,
) -> tuple[MarketCandle, ...]:
    """Normalize a batch strictly, preserving input order and rejecting any bad row.

    No row is silently dropped and no sorting or deduplication is performed here;
    these conditions belong to the explicit quality gate.
    """
    return tuple(
        normalize_candle(row, market=market, interval=interval, source=source)
        for row in rows
    )
