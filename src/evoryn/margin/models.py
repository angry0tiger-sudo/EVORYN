"""Independent domain models for Nobitex delegated Spot Margin positions.

These models intentionally do not reuse EVORYN Spot positions or risk calculations.
They describe the fields exposed by Nobitex's documented Margin API; unknown values
remain unknown rather than being inferred from generic perpetual-futures conventions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class MarginSide(StrEnum):
    """Nobitex Margin opening direction."""

    BUY = "buy"
    SELL = "sell"


class MarginPositionStatus(StrEnum):
    """Documented position states plus an explicit fail-closed unknown state."""

    OPEN = "Open"
    CLOSED = "Closed"
    LIQUIDATED = "Liquidated"
    EXPIRED = "Expired"
    UNKNOWN = "Unknown"


class MarginMode(StrEnum):
    """Margin mode returned by the venue; policy currently enables isolated only."""

    ISOLATED = "Isolated Margin"
    CROSS = "Cross Margin"
    UNKNOWN = "Unknown"


class MarginHealth(StrEnum):
    """Local risk classification; it never changes or predicts the venue position state."""

    HEALTHY = "healthy"
    AT_RISK = "at_risk"
    LIQUIDATION_THRESHOLD_CROSSED = "liquidation_threshold_crossed"
    LIQUIDATED = "liquidated"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"
    UNSUPPORTED_MODE = "unsupported_mode"


class MarginOrderDisposition(StrEnum):
    """Decision for a proposed Margin opening request, not authority to send it."""

    ALLOW = "allow"
    REJECT = "reject"
    HALT = "halt"


@dataclass(frozen=True, slots=True)
class MarginMarketSettings:
    """Normalized result of GET /margin/markets/list.

    The fee rate is a source-provided daily extension-fee rate. It can vary by market
    and asset-value tier; local linear fee calculations are estimates only.
    """

    market_symbol: str
    src_currency: str
    dst_currency: str
    position_fee_rate: Decimal
    max_leverage: Decimal
    buy_enabled: bool
    sell_enabled: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_nonempty(self.market_symbol, "market_symbol")
        _require_nonempty(self.src_currency, "src_currency")
        _require_nonempty(self.dst_currency, "dst_currency")
        if self.src_currency.casefold() == self.dst_currency.casefold():
            raise ValueError("Margin source and destination currencies must differ.")
        _require_aware(self.observed_at, "Market settings timestamp")
        _require_nonnegative(self.position_fee_rate, "position_fee_rate")
        if self.position_fee_rate > Decimal("1"):
            raise ValueError("position_fee_rate must not exceed 1.")
        if (
            not isinstance(self.max_leverage, Decimal)
            or not self.max_leverage.is_finite()
            or self.max_leverage < Decimal("1")
        ):
            raise ValueError("max_leverage must be finite and at least 1.")
        if type(self.buy_enabled) is not bool or type(self.sell_enabled) is not bool:
            raise ValueError("Margin side capabilities must be explicit booleans.")


@dataclass(frozen=True, slots=True)
class DelegationLimit:
    """One row from /margin/v2/delegation-limit for one side and leverage.

    Nobitex documents buy limit in destination/quote currency and sell limit in
    source/base currency. The caller must not compare these limits without respecting
    their side-specific units.
    """

    side: MarginSide
    leverage: Decimal
    limit: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.side, MarginSide):
            raise ValueError("Delegation side must be MarginSide.")
        if (
            not isinstance(self.leverage, Decimal)
            or not self.leverage.is_finite()
            or self.leverage < Decimal("1")
        ):
            raise ValueError("Delegation leverage must be finite and at least 1.")
        _require_nonnegative(self.limit, "delegation limit")


@dataclass(frozen=True, slots=True)
class DelegationLimitSnapshot:
    """Timestamped source response for one market's side-specific remaining limits."""

    market_symbol: str
    observed_at: datetime
    limits: tuple[DelegationLimit, ...]

    def __post_init__(self) -> None:
        _require_nonempty(self.market_symbol, "market_symbol")
        _require_aware(self.observed_at, "Delegation limit timestamp")
        if not isinstance(self.limits, tuple):
            raise ValueError("Delegation limits must be an immutable tuple.")
        seen: set[tuple[MarginSide, Decimal]] = set()
        for item in self.limits:
            if not isinstance(item, DelegationLimit):
                raise ValueError("Delegation rows must use the typed domain model.")
            key = (item.side, item.leverage)
            if key in seen:
                raise ValueError("duplicate side/leverage rows in delegation limit snapshot.")
            seen.add(key)


@dataclass(frozen=True, slots=True)
class MarginPosition:
    """Normalized read-only Nobitex Position object.

    Monetary fields keep their documented units: collateral/total_asset/extension_fee/
    PnL are destination-currency values; delegated_amount/liability are source-currency
    quantities; prices are destination currency per source unit. The model does not
    derive a venue liquidation price or reinterpret liability as perpetual-futures debt.
    """

    position_id: int
    market_symbol: str
    src_currency: str
    dst_currency: str
    side: MarginSide
    status: MarginPositionStatus
    margin_mode: MarginMode
    collateral: Decimal
    leverage: Decimal
    observed_at: datetime
    created_at: datetime | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    liquidation_price: Decimal | None = None
    entry_price: Decimal | None = None
    exit_price: Decimal | None = None
    delegated_amount: Decimal | None = None
    liability: Decimal | None = None
    total_asset: Decimal | None = None
    margin_ratio: Decimal | None = None
    liability_in_order: Decimal | None = None
    asset_in_order: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    unrealized_pnl_percent: Decimal | None = None
    expiration_date: date | None = None
    extension_fee: Decimal | None = None
    mark_price: Decimal | None = None
    realized_pnl: Decimal | None = None
    realized_pnl_percent: Decimal | None = None

    def __post_init__(self) -> None:
        if self.position_id <= 0:
            raise ValueError("position_id must be positive.")
        if not isinstance(self.side, MarginSide):
            raise ValueError("Position side must be MarginSide.")
        if not isinstance(self.status, MarginPositionStatus):
            raise ValueError("Position status must be MarginPositionStatus.")
        if not isinstance(self.margin_mode, MarginMode):
            raise ValueError("Position margin_mode must be MarginMode.")
        for name in ("market_symbol", "src_currency", "dst_currency"):
            _require_nonempty(getattr(self, name), name)
        if self.src_currency.casefold() == self.dst_currency.casefold():
            raise ValueError("Margin source and destination currencies must differ.")
        _require_aware(self.observed_at, "Position observation timestamp")
        for name in ("created_at", "opened_at", "closed_at"):
            value = getattr(self, name)
            if value is not None:
                _require_aware(value, name)
        if (
            not isinstance(self.collateral, Decimal)
            or not self.collateral.is_finite()
            or self.collateral < 0
        ):
            raise ValueError("collateral must be finite and non-negative.")
        if (
            not isinstance(self.leverage, Decimal)
            or not self.leverage.is_finite()
            or self.leverage < Decimal("1")
        ):
            raise ValueError("leverage must be finite and at least 1.")
        for name in (
            "liquidation_price",
            "entry_price",
            "exit_price",
            "mark_price",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite() or value <= 0
            ):
                raise ValueError(f"{name} must be positive and finite when present.")
        for name in (
            "delegated_amount",
            "liability",
            "total_asset",
            "liability_in_order",
            "asset_in_order",
            "extension_fee",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_nonnegative(value, name)
        for name in (
            "margin_ratio",
            "unrealized_pnl",
            "unrealized_pnl_percent",
            "realized_pnl",
            "realized_pnl_percent",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite()
            ):
                raise ValueError(f"{name} must be finite when present.")
        if self.margin_ratio is not None and self.margin_ratio < 0:
            raise ValueError("margin_ratio must not be negative.")


@dataclass(frozen=True, slots=True)
class MarginOpenIntent:
    """Pure request model for evaluating an opening order; it performs no I/O."""

    market_symbol: str
    side: MarginSide
    amount: Decimal
    price: Decimal
    leverage: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_nonempty(self.market_symbol, "market_symbol")
        if not isinstance(self.side, MarginSide):
            raise ValueError("Intent side must be MarginSide.")
        _require_aware(self.observed_at, "Intent timestamp")
        for name in ("amount", "price", "leverage"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be finite and positive.")
        if self.leverage < Decimal("1"):
            raise ValueError("leverage must be at least 1.")


@dataclass(frozen=True, slots=True)
class MarginOrderRiskResult:
    """A validated/rejected Margin request with explicit code and no execution side effect."""

    disposition: MarginOrderDisposition
    reason_code: str
    explanation: str
    estimated_initial_collateral: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, MarginOrderDisposition):
            raise ValueError("Order disposition must be MarginOrderDisposition.")
        _require_nonempty(self.reason_code, "reason_code")
        _require_nonempty(self.explanation, "explanation")
        if self.estimated_initial_collateral is not None:
            _require_nonnegative(
                self.estimated_initial_collateral, "estimated_initial_collateral"
            )


@dataclass(frozen=True, slots=True)
class MarginRiskAssessment:
    """Risk classification for a venue-reported Margin position at one observation time."""

    position_id: int
    health: MarginHealth
    assessed_at: datetime
    reason_code: str
    explanation: str
    new_risk_permitted: bool
    reported_liquidation_price: Decimal | None
    observed_mark_price: Decimal | None
    observed_margin_ratio: Decimal | None

    def __post_init__(self) -> None:
        if self.position_id <= 0:
            raise ValueError("position_id must be positive.")
        if not isinstance(self.health, MarginHealth):
            raise ValueError("Risk health must be MarginHealth.")
        _require_aware(self.assessed_at, "Risk assessment timestamp")
        _require_nonempty(self.reason_code, "reason_code")
        _require_nonempty(self.explanation, "explanation")
        if self.new_risk_permitted and self.health is not MarginHealth.HEALTHY:
            raise ValueError("Only a healthy position assessment may permit new risk.")


@dataclass(frozen=True, slots=True)
class MarginReconciliationResult:
    """Comparison of two normalized snapshots for the same external position."""

    position_id: int
    matched: bool
    differences: tuple[str, ...]


def _require_nonempty(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty.")


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone.")


def _require_nonnegative(value: Decimal, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{label} must be finite and non-negative.")
