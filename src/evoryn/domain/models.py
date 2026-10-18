"""Minimal immutable domain models; this module contains no trading algorithms or I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    """Direction associated with a signal or order intent."""

    BUY = "buy"
    SELL = "sell"


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
