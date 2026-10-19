"""Official Nobitex Spot instrument rules from the public /v2/options endpoint."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Callable, Protocol

from evoryn.config import MarketPolicy
from evoryn.domain.models import Market
from evoryn.ports.exchange_errors import (
    InvalidExchangeRequest,
    MalformedExchangeResponse,
)


class NobitexOptionsReadPort(Protocol):
    """Read-only contract for the official, public Nobitex options endpoint."""

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
        token: str | None = None,
    ) -> dict[str, object]:
        """Request one allowlisted read-only route."""
        ...


@dataclass(frozen=True, slots=True)
class SpotInstrumentRules:
    """Exchange-provided amount step, price tick and USDT minimum notional."""

    symbol: str
    amount_step: Decimal
    price_tick: Decimal
    min_notional_usdt: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.symbol.strip() or self.symbol != self.symbol.upper():
            raise ValueError("Instrument symbol must be a normalized uppercase pair.")
        for name in ("amount_step", "price_tick", "min_notional_usdt"):
            value = getattr(self, name)
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be finite and positive.")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("Instrument-rules timestamp must be timezone-aware.")


@dataclass(slots=True)
class NobitexSpotInstrumentRulesProvider:
    """Read and validate rules for one explicitly whitelisted USDT Spot instrument."""

    transport: NobitexOptionsReadPort
    market_policy: MarketPolicy
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def get_rules(self, market: Market) -> SpotInstrumentRules:
        """Fetch current public options once and fail closed if required fields are absent."""
        if self.market_policy.rejection_reason(market) is not None:
            raise InvalidExchangeRequest("Instrument rules are restricted to Nobitex Spot/USDT.")
        symbol = _compact_symbol(market.symbol)
        response = self.transport.request("GET", "/v2/options")
        if response.get("status") not in {"ok", "Ok"}:
            raise MalformedExchangeResponse("Nobitex options response status is not ok.")
        settings = response.get("nobitex")
        if not isinstance(settings, Mapping):
            raise MalformedExchangeResponse("Nobitex options response lacks the settings object.")
        amount_steps = settings.get("amountPrecisions")
        price_ticks = settings.get("pricePrecisions")
        minimums = settings.get("minOrders")
        if not isinstance(amount_steps, Mapping) or not isinstance(price_ticks, Mapping):
            raise MalformedExchangeResponse("Nobitex options response lacks precision maps.")
        if not isinstance(minimums, Mapping):
            raise MalformedExchangeResponse("Nobitex options response lacks minOrders.")
        amount_step = _required_positive_decimal(
            amount_steps.get(symbol), field_name=f"amountPrecisions.{symbol}"
        )
        price_tick = _required_positive_decimal(
            price_ticks.get(symbol), field_name=f"pricePrecisions.{symbol}"
        )
        minimum = _required_positive_decimal(minimums.get("usdt"), field_name="minOrders.usdt")
        observed_at = self.clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise RuntimeError("Instrument-rules clock must return a timezone-aware datetime.")
        return SpotInstrumentRules(
            symbol=symbol,
            amount_step=amount_step,
            price_tick=price_tick,
            min_notional_usdt=minimum,
            observed_at=observed_at.astimezone(UTC),
        )


def _compact_symbol(symbol: str) -> str:
    """Normalize the validated canonical symbol to the exact API map key."""
    normalized = symbol.strip().upper()
    for separator in ("/", "-", "_"):
        if separator in normalized:
            parts = normalized.split(separator)
            if len(parts) != 2 or not all(part.isalnum() for part in parts):
                raise InvalidExchangeRequest("Market symbol must contain one BASE/USDT pair.")
            if parts[1] != "USDT":
                raise InvalidExchangeRequest("Only USDT-quoted Spot instruments are supported.")
            return "".join(parts)
    if normalized.endswith("USDT") and normalized[:-4].isalnum():
        return normalized
    raise InvalidExchangeRequest("Market symbol is not a recognized BASE/USDT pair.")


def _required_positive_decimal(value: object, *, field_name: str) -> Decimal:
    """Parse public provider precision values without binary float conversion."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise MalformedExchangeResponse(
            f"Nobitex options field {field_name!r} is missing or invalid."
        )
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MalformedExchangeResponse(
            f"Nobitex options field {field_name!r} is not decimal."
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise MalformedExchangeResponse(
            f"Nobitex options field {field_name!r} must be finite and positive."
        )
    return parsed
