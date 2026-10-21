"""Authenticated, read-only Spot/USDT order and trade snapshots for recovery research.

This module does not submit/cancel orders and is deliberately not wired as a live-runtime
reconciler yet. A response is a page, not proof of complete account history: order pagination
is provider-dependent, and user-trade history is limited by Nobitex's documented window.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol

from evoryn.adapters.exchanges.nobitex_transport import NobitexHTTPTransport
from evoryn.config import MarketPolicy, NobitexReadOnlyConfig
from evoryn.domain.models import Market, OrderStatus, Side
from evoryn.infrastructure.exchange.rate_limit import SlidingWindowRateLimiter
from evoryn.ports.contracts import RateLimiterPort
from evoryn.ports.exchange_errors import (
    AuthenticationError,
    InvalidExchangeRequest,
    MalformedExchangeResponse,
    UnsupportedExchangeCapability,
)
from evoryn.ports.rate_limits import RateLimitPolicy


class NobitexReconciliationTransport(Protocol):
    """The minimal read-only request surface used by this provider."""

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
        token: str | None = None,
    ) -> dict[str, object]:
        """Perform one allowlisted API request."""
        ...


class UserOrderStatus(StrEnum):
    """Documented user-order list filters; these are query filters, not order states."""

    ALL = "all"
    OPEN = "open"
    DONE = "done"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class NobitexUserOrder:
    """Validated provider order fields without pretending a partial response is complete."""

    order_id: str
    market: Market
    side: Side
    status: OrderStatus
    raw_status: str
    execution: str
    quantity: Decimal
    matched_quantity: Decimal
    unmatched_quantity: Decimal
    price: Decimal | None
    average_price: Decimal | None
    total_price: Decimal | None
    fee: Decimal | None
    client_order_id: str | None
    trade_type: str | None
    created_at: datetime | None

    @property
    def reconciliation_ready(self) -> bool:
        """Only explicitly identified Spot rows are eligible for later reconciliation."""
        return (
            self.trade_type == "spot"
            and self.status is not OrderStatus.UNKNOWN
            and self.quantity == self.matched_quantity + self.unmatched_quantity
        )


@dataclass(frozen=True, slots=True)
class NobitexUserTrade:
    """One provider fill-like trade record with the original order ID and fee."""

    trade_id: str
    order_id: str
    market: Market
    side: Side
    quantity: Decimal
    price: Decimal
    total: Decimal
    fee: Decimal
    occurred_at: datetime
    trade_type: str | None

    @property
    def reconciliation_ready(self) -> bool:
        """Do not treat a trade as a Spot ledger entry when provider type is absent/unknown."""
        return self.trade_type == "spot"


@dataclass(frozen=True, slots=True)
class NobitexOrderPage:
    """One filtered order-list response; None means the provider gave no continuation flag."""

    market: Market
    requested_status: UserOrderStatus
    requested_from_id: int | None
    orders: tuple[NobitexUserOrder, ...]
    has_next: bool | None
    observed_at: datetime

    @property
    def reconciliation_ready(self) -> bool:
        """True only when completeness and every row's Spot identity are explicit."""
        return self.has_next is False and all(
            order.reconciliation_ready for order in self.orders
        )


@dataclass(frozen=True, slots=True)
class NobitexTradePage:
    """One of Nobitex's user-trade pages; this is not a full historical ledger by itself."""

    market: Market
    requested_from_id: int | None
    trades: tuple[NobitexUserTrade, ...]
    has_next: bool
    observed_at: datetime

    @property
    def reconciliation_ready(self) -> bool:
        """True only for a terminal page whose rows all explicitly identify as Spot."""
        return not self.has_next and all(
            trade.reconciliation_ready for trade in self.trades
        )


_ORDER_LIST_LIMIT = RateLimitPolicy(max_requests=30, window_seconds=60.0)
_USER_TRADE_LIST_LIMIT = RateLimitPolicy(max_requests=20, window_seconds=60.0)


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MalformedExchangeResponse(
            f"Nobitex {field_name} is missing or is not text."
        )
    return value.strip()


def _identifier(value: object, *, field_name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MalformedExchangeResponse(f"Nobitex {field_name} is not an identifier.")
    result = str(value).strip()
    if not result.isascii() or not result.isdigit() or len(result) > 19 or int(result) < 1:
        raise MalformedExchangeResponse(f"Nobitex {field_name} is not a positive integer.")
    return result


def _decimal(
    value: object,
    *,
    field_name: str,
    allow_zero: bool,
    market_word_allowed: bool = False,
) -> Decimal | None:
    if market_word_allowed and isinstance(value, str) and value.strip().casefold() == "market":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise MalformedExchangeResponse(f"Nobitex {field_name} is not numeric.")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MalformedExchangeResponse(f"Nobitex {field_name} is not numeric.") from exc
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise MalformedExchangeResponse(f"Nobitex {field_name} is outside its valid range.")
    return result


def _required_decimal(
    value: object, *, field_name: str, allow_zero: bool
) -> Decimal:
    """Parse a mandatory decimal and fail closed if a future parser permits None."""
    result = _decimal(value, field_name=field_name, allow_zero=allow_zero)
    if result is None:
        raise MalformedExchangeResponse(f"Nobitex {field_name} is required.")
    return result


def _optional_decimal(
    value: object, *, field_name: str, market_word_allowed: bool = False
) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _decimal(
        value,
        field_name=field_name,
        allow_zero=True,
        market_word_allowed=market_word_allowed,
    )


def _timestamp(value: object, *, field_name: str) -> datetime:
    raw = _required_text(value, field_name=field_name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MalformedExchangeResponse(f"Nobitex {field_name} is not ISO-8601.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MalformedExchangeResponse(f"Nobitex {field_name} lacks a timezone.")
    return parsed.astimezone(UTC)


def _normalized_market_symbol(value: str) -> str:
    return value.strip().upper().replace("/", "").replace("-", "").replace("_", "")


def _status(value: str) -> OrderStatus:
    key = value.casefold().replace("_", "").replace(" ", "")
    mapping = {
        "active": OrderStatus.OPEN,
        "open": OrderStatus.OPEN,
        "inactive": OrderStatus.PENDING,
        "pending": OrderStatus.PENDING,
        "partiallymatched": OrderStatus.PARTIALLY_FILLED,
        "partiallyfilled": OrderStatus.PARTIALLY_FILLED,
        "partial": OrderStatus.PARTIALLY_FILLED,
        "done": OrderStatus.FILLED,
        "filled": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELLED,
        "cancelled": OrderStatus.CANCELLED,
        "rejected": OrderStatus.REJECTED,
    }
    return mapping.get(key, OrderStatus.UNKNOWN)


@dataclass(slots=True)
class NobitexReconciliationReader:
    """Request validated Spot/USDT user-order and user-trade pages without write capability."""

    transport: NobitexReconciliationTransport
    token: str | None = field(default=None, repr=False)
    rate_limiter: RateLimiterPort = field(
        default_factory=SlidingWindowRateLimiter, repr=False
    )
    clock: Callable[[], datetime] = field(
        default_factory=lambda: lambda: datetime.now(UTC), repr=False
    )
    market_policy: MarketPolicy = field(
        default_factory=MarketPolicy.from_environment, repr=False
    )

    @classmethod
    def from_config(
        cls,
        config: NobitexReadOnlyConfig,
        *,
        rate_limiter: RateLimiterPort | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> NobitexReconciliationReader:
        """Build from validated read-only API configuration without network I/O."""

        transport = NobitexHTTPTransport(
            base_url=config.base_url,
            token=config.read_only_api_token,
            timeout_seconds=config.timeout_seconds,
        )
        return cls(
            transport=transport,
            token=config.read_only_api_token,
            rate_limiter=rate_limiter or SlidingWindowRateLimiter(),
            clock=clock or (lambda: datetime.now(UTC)),
            market_policy=config.market_policy,
        )

    def _validate_market(self, market: Market) -> tuple[str, str]:
        if market.venue.casefold() != "nobitex" or market.market_type.casefold() != "spot":
            raise UnsupportedExchangeCapability(
                "Nobitex reconciliation is restricted to its Spot venue."
            )
        reason = self.market_policy.rejection_reason(market)
        if reason is not None:
            raise UnsupportedExchangeCapability(
                "Nobitex reconciliation is restricted to allowlisted USDT markets."
            )
        symbol = market.symbol.strip().upper()
        parts: list[str] = []
        for separator in ("/", "-", "_"):
            if separator in symbol:
                split = symbol.split(separator)
                if len(split) != 2 or not all(item.isalnum() for item in split):
                    raise InvalidExchangeRequest("Market must be one BASE/USDT pair.")
                parts = split
                break
        if not parts:
            if not symbol.endswith("USDT") or len(symbol) <= 4:
                raise InvalidExchangeRequest("Reconciliation requires an explicit BASE/USDT pair.")
            parts = [symbol[:-4], "USDT"]
        if parts[1] != "USDT" or not parts[0].isalnum():
            raise InvalidExchangeRequest("Reconciliation only supports BASE/USDT markets.")
        return parts[0].lower(), "usdt"

    def _require_token(self) -> str:
        token = (self.token or "").strip()
        if not token:
            raise AuthenticationError(
                "A read-only NOBITEX_READONLY_API_TOKEN is required for order reconciliation reads."
            )
        return token

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("Reconciliation reader clock must return an aware datetime.")
        return now.astimezone(UTC)

    def get_orders_page(
        self,
        market: Market,
        *,
        status: UserOrderStatus = UserOrderStatus.OPEN,
        from_id: int | None = None,
    ) -> NobitexOrderPage:
        """Fetch one authenticated order-list page; does not claim pagination is complete."""
        base, quote = self._validate_market(market)
        token = self._require_token()
        if not isinstance(status, UserOrderStatus):
            raise InvalidExchangeRequest("Order-list status must be an explicit UserOrderStatus.")
        if from_id is not None and (type(from_id) is not int or from_id < 1):
            raise InvalidExchangeRequest("from_id must be a positive integer when supplied.")
        params = {
            "srcCurrency": base,
            "dstCurrency": quote,
            "details": "2",
            "status": status.value,
        }
        if from_id is not None:
            params["fromId"] = str(from_id)
        self.rate_limiter.acquire("nobitex:user_order_list", _ORDER_LIST_LIMIT)
        response = self.transport.request(
            "GET", "/market/orders/list", params=params, token=token
        )
        raw_orders = response.get("orders")
        if not isinstance(raw_orders, list):
            raise MalformedExchangeResponse("Nobitex order-list response lacks an orders array.")
        has_next_raw = response.get("hasNext")
        if has_next_raw is not None and not isinstance(has_next_raw, bool):
            raise MalformedExchangeResponse(
                "Nobitex order-list hasNext must be boolean when present."
            )
        orders = tuple(self._normalize_order(row, market) for row in raw_orders)
        return NobitexOrderPage(
            market=market,
            requested_status=status,
            requested_from_id=from_id,
            orders=orders,
            has_next=has_next_raw,
            observed_at=self._now(),
        )

    def _normalize_order(self, raw: object, market: Market) -> NobitexUserOrder:
        if not isinstance(raw, dict):
            raise MalformedExchangeResponse("Nobitex order-list row must be an object.")
        order_id = _identifier(raw.get("id"), field_name="order.id")
        side_raw = _required_text(raw.get("type"), field_name="order.type").casefold()
        try:
            side = Side(side_raw)
        except ValueError as exc:
            raise MalformedExchangeResponse("Nobitex order side is unsupported.") from exc
        raw_status = _required_text(raw.get("status"), field_name="order.status")
        execution = _required_text(raw.get("execution"), field_name="order.execution").casefold()
        remote_market = raw.get("market")
        if isinstance(remote_market, str) and (
            _normalized_market_symbol(remote_market)
            != _normalized_market_symbol(market.symbol)
        ):
            raise MalformedExchangeResponse(
                "Nobitex order-list row mismatches the requested market."
            )
        trade_type_raw = raw.get("tradeType")
        trade_type = (
            _required_text(trade_type_raw, field_name="order.tradeType").casefold()
            if trade_type_raw is not None
            else None
        )
        if trade_type is not None and trade_type != "spot":
            raise MalformedExchangeResponse("Nobitex returned a non-Spot order in a Spot query.")
        amount = _required_decimal(
            raw.get("amount"), field_name="order.amount", allow_zero=False
        )
        matched = _required_decimal(
            raw.get("matchedAmount"), field_name="order.matchedAmount", allow_zero=True
        )
        unmatched = _required_decimal(
            raw.get("unmatchedAmount"), field_name="order.unmatchedAmount", allow_zero=True
        )
        price = _optional_decimal(
            raw.get("price"), field_name="order.price", market_word_allowed=True
        )
        average_price = _optional_decimal(
            raw.get("averagePrice"), field_name="order.averagePrice"
        )
        total_price = _optional_decimal(raw.get("totalPrice"), field_name="order.totalPrice")
        fee = _optional_decimal(raw.get("fee"), field_name="order.fee")
        client_id_raw = raw.get("clientOrderId")
        client_order_id = (
            _required_text(client_id_raw, field_name="order.clientOrderId")
            if client_id_raw is not None
            else None
        )
        created_at_raw = raw.get("created_at", raw.get("createdAt"))
        created_at = (
            _timestamp(created_at_raw, field_name="order.created_at")
            if created_at_raw is not None
            else None
        )
        return NobitexUserOrder(
            order_id=order_id,
            market=market,
            side=side,
            status=_status(raw_status),
            raw_status=raw_status,
            execution=execution,
            quantity=amount,
            matched_quantity=matched,
            unmatched_quantity=unmatched,
            price=price,
            average_price=average_price,
            total_price=total_price,
            fee=fee,
            client_order_id=client_order_id,
            trade_type=trade_type,
            created_at=created_at,
        )

    def get_user_trades_page(
        self,
        market: Market,
        *,
        from_id: int | None = None,
    ) -> NobitexTradePage:
        """Fetch one authenticated recent trade page; history is provider-window-limited."""
        base, quote = self._validate_market(market)
        token = self._require_token()
        if from_id is not None and (type(from_id) is not int or from_id < 1):
            raise InvalidExchangeRequest("from_id must be a positive integer when supplied.")
        params = {"srcCurrency": base, "dstCurrency": quote}
        if from_id is not None:
            params["fromId"] = str(from_id)
        self.rate_limiter.acquire("nobitex:user_trade_list", _USER_TRADE_LIST_LIMIT)
        response = self.transport.request(
            "GET", "/market/trades/list", params=params, token=token
        )
        raw_trades = response.get("trades")
        has_next_raw = response.get("hasNext")
        if not isinstance(raw_trades, list) or not isinstance(has_next_raw, bool):
            raise MalformedExchangeResponse(
                "Nobitex user-trade response requires a trades array and boolean hasNext."
            )
        trades = tuple(self._normalize_trade(row, market) for row in raw_trades)
        return NobitexTradePage(
            market=market,
            requested_from_id=from_id,
            trades=trades,
            has_next=has_next_raw,
            observed_at=self._now(),
        )

    def _normalize_trade(self, raw: object, market: Market) -> NobitexUserTrade:
        if not isinstance(raw, dict):
            raise MalformedExchangeResponse("Nobitex user-trade row must be an object.")
        trade_id = _identifier(raw.get("id"), field_name="trade.id")
        order_id = _identifier(raw.get("orderId"), field_name="trade.orderId")
        side_raw = _required_text(raw.get("type"), field_name="trade.type").casefold()
        try:
            side = Side(side_raw)
        except ValueError as exc:
            raise MalformedExchangeResponse("Nobitex user-trade side is unsupported.") from exc
        remote_market = raw.get("market")
        if isinstance(remote_market, str) and (
            _normalized_market_symbol(remote_market)
            != _normalized_market_symbol(market.symbol)
        ):
            raise MalformedExchangeResponse("Nobitex trade row mismatches the requested market.")
        trade_type_raw = raw.get("tradeType")
        trade_type = (
            _required_text(trade_type_raw, field_name="trade.tradeType").casefold()
            if trade_type_raw is not None
            else None
        )
        if trade_type is not None and trade_type != "spot":
            raise MalformedExchangeResponse("Nobitex returned a non-Spot trade in a Spot query.")
        quantity = _required_decimal(
            raw.get("amount"), field_name="trade.amount", allow_zero=False
        )
        price = _required_decimal(
            raw.get("price"), field_name="trade.price", allow_zero=False
        )
        total = _required_decimal(
            raw.get("total"), field_name="trade.total", allow_zero=True
        )
        fee = _required_decimal(raw.get("fee"), field_name="trade.fee", allow_zero=True)
        return NobitexUserTrade(
            trade_id=trade_id,
            order_id=order_id,
            market=market,
            side=side,
            quantity=quantity,
            price=price,
            total=total,
            fee=fee,
            occurred_at=_timestamp(raw.get("timestamp"), field_name="trade.timestamp"),
            trade_type=trade_type,
        )
