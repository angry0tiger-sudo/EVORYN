"""Minimal immutable domain models; this module contains no trading algorithms or I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    """Direction associated with a signal or order intent."""

    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    """Normalized exchange order type supported by the common contract."""

    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(StrEnum):
    """Normalized order lifecycle labels for domain-level representation only."""

    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class RiskDisposition(StrEnum):
    """Possible deterministic risk-gate outcomes."""

    ALLOW = "allow"
    REDUCE = "reduce"
    REJECT = "reject"
    HALT = "halt"


@dataclass(frozen=True, slots=True)
class Market:
    """Canonical market identity without provider-specific behavior."""

    symbol: str
    venue: str
    market_type: str


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Timestamped market-data envelope with explicit source and quality metadata."""

    market: Market
    observed_at: datetime
    source: str
    values: dict[str, Decimal]
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Signal:
    """A proposed signal record; creating one does not authorize execution."""

    market_symbol: str
    side: Side
    rationale: str
    created_at: datetime
    valid_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """An abstract requested order, not an exchange request or live order."""

    market_symbol: str
    side: Side
    quantity: Decimal
    rationale: str


@dataclass(frozen=True, slots=True)
class Order:
    """Normalized order record for storage, tests, and future reconciliation."""

    order_id: str
    market_symbol: str
    side: Side
    quantity: Decimal
    status: OrderStatus
    price: Decimal | None = None
    client_order_id: str | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    """Normalized fill record without assumptions about exchange execution."""

    order_id: str
    quantity: Decimal
    price: Decimal
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class Position:
    """Position snapshot represented with decimal precision."""

    market_symbol: str
    quantity: Decimal
    average_entry_price: Decimal


@dataclass(frozen=True, slots=True)
class ExchangeCapabilities:
    """Declared capabilities for a future adapter; unsupported means unavailable."""

    venue: str
    market_data: bool
    account_read: bool
    order_read: bool
    order_write: bool = False
    candles: bool = False
    order_book: bool = False
    recent_trades: bool = False
    market_stats: bool = False
    order_submit: bool = False
    order_cancel: bool = False


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    """One normalized level in an order-book side."""

    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        """Reject non-finite or non-positive order-book values."""
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("Order-book price must be finite and positive.")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError("Order-book quantity must be finite and positive.")


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    """Timestamped normalized bids and asks for a single canonical market."""

    market: Market
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    observed_at: datetime
    source: str
    last_trade_price: Decimal | None = None

    def __post_init__(self) -> None:
        """Reject missing time/source metadata and invalid optional last price."""
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("Order-book timestamp must include a timezone.")
        if not self.source.strip():
            raise ValueError("Order-book source must not be empty.")
        if self.last_trade_price is not None and (
            not self.last_trade_price.is_finite() or self.last_trade_price <= 0
        ):
            raise ValueError("Last trade price must be finite and positive.")


@dataclass(frozen=True, slots=True)
class MarketTrade:
    """One normalized trade event from an exchange feed."""

    trade_id: str
    market: Market
    side: Side
    price: Decimal
    quantity: Decimal
    occurred_at: datetime

    def __post_init__(self) -> None:
        """Enforce explicit identity, aware event time, and positive trade values."""
        if not self.trade_id.strip():
            raise ValueError("Trade identifier must not be empty.")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("Trade timestamp must include a timezone.")
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("Trade price must be finite and positive.")
        if not self.quantity.is_finite() or self.quantity < 0:
            raise ValueError("Trade quantity must be finite and non-negative.")


@dataclass(frozen=True, slots=True)
class WalletBalance:
    """Normalized wallet balance preserving the provider's balance and blocked fields."""

    currency: str
    balance: Decimal
    blocked: Decimal

    def __post_init__(self) -> None:
        """Reject invalid currency identifiers or negative/non-finite balances."""
        if not self.currency.strip():
            raise ValueError("Wallet currency must not be empty.")
        if not self.balance.is_finite() or self.balance < 0:
            raise ValueError("Wallet balance must be finite and non-negative.")
        if not self.blocked.is_finite() or self.blocked < 0:
            raise ValueError("Blocked balance must be finite and non-negative.")


@dataclass(frozen=True, slots=True)
class MarketStatistics:
    """Normalized market-statistics snapshot with optional provider fields."""

    market: Market
    observed_at: datetime
    latest: Decimal | None = None
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    base_volume: Decimal | None = None
    quote_volume: Decimal | None = None
    day_low: Decimal | None = None
    day_high: Decimal | None = None
    day_open: Decimal | None = None
    day_close: Decimal | None = None
    day_change_percent: Decimal | None = None
    is_closed: bool | None = None

    def __post_init__(self) -> None:
        """Require an aware timestamp and finite values for every supplied statistic."""
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("Market-statistics timestamp must include a timezone.")
        for name in (
            "latest", "best_bid", "best_ask", "base_volume", "quote_volume",
            "day_low", "day_high", "day_open", "day_close", "day_change_percent",
        ):
            value = getattr(self, name)
            if value is not None and not value.is_finite():
                raise ValueError(f"Market statistic {name} must be finite.")


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """Typed order intent presented to an exchange port, not permission to execute."""

    client_order_id: str
    market: Market
    side: Side
    order_type: OrderType
    quantity: Decimal
    price: Decimal | None = None

    def __post_init__(self) -> None:
        """Validate request shape without applying venue-specific trade rules."""
        if not self.client_order_id.strip():
            raise ValueError("Client order identifier must not be empty.")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError("Order quantity must be finite and positive.")
        if self.order_type is OrderType.LIMIT and self.price is None:
            raise ValueError("Limit orders require a price.")
        if self.order_type is OrderType.MARKET and self.price is not None:
            raise ValueError("Market orders must not provide a limit price.")
        if self.price is not None and (not self.price.is_finite() or self.price <= 0):
            raise ValueError("Order price must be finite and positive.")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Structured deterministic risk outcome for a proposed intent."""

    disposition: RiskDisposition
    reason_code: str
    explanation: str


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """A prepared execution description; it does not place an order."""

    intent: OrderIntent
    approved: bool
    plan_reference: str


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Abstract execution outcome shape for future implementations and simulations."""

    order: Order | None
    fills: tuple[Fill, ...] = ()
    reconciliation_required: bool = False


@dataclass(frozen=True, slots=True)
class LearningEvent:
    """Versioned observation supplied to a future learning subsystem."""

    event_id: str
    dataset_version: str
    outcome_summary: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ExperimentProposal:
    """Reviewable experiment proposal; it cannot modify live configuration."""

    proposal_id: str
    hypothesis: str
    dataset_version: str
    evaluation_plan: str


@dataclass(frozen=True, slots=True)
class MarketCandle:
    """Canonical OHLCV candle with explicit market identity and UTC-capable timestamp."""

    market: Market
    timestamp: datetime
    interval: timedelta
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str

    def __post_init__(self) -> None:
        """Reject invalid candle invariants before a record enters the domain."""
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("Candle timestamp must include a timezone.")
        if self.interval <= timedelta(0):
            raise ValueError("Candle interval must be positive.")
        if not self.source.strip():
            raise ValueError("Candle source must not be empty.")

        prices = (self.open, self.high, self.low, self.close)
        if any(not value.is_finite() or value <= 0 for value in prices):
            raise ValueError("OHLC prices must be finite and greater than zero.")
        if not self.volume.is_finite() or self.volume < 0:
            raise ValueError("Volume must be finite and non-negative.")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("High must be at least open, close, and low.")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("Low must be no greater than open, close, and high.")

