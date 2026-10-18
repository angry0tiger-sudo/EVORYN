"""Typed, strategy-independent risk contracts used before paper execution."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class RiskAction(StrEnum):
    """The only actions the deterministic risk gate may return."""

    ALLOW = "ALLOW"
    REDUCE = "REDUCE"
    REJECT = "REJECT"
    HALT = "HALT"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Hard portfolio and freshness limits. Values are explicit, never inferred."""

    max_total_exposure: Decimal
    max_market_exposure: Decimal
    max_order_notional: Decimal
    max_loss: Decimal
    max_market_data_age_seconds: int = 10

    def __post_init__(self) -> None:
        """Reject nonsensical limits instead of silently repairing configuration."""
        for name in (
            "max_total_exposure",
            "max_market_exposure",
            "max_order_notional",
            "max_loss",
        ):
            value = getattr(self, name)
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be finite and positive.")
        if self.max_market_data_age_seconds <= 0:
            raise ValueError("max_market_data_age_seconds must be positive.")


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """Sufficient, caller-supplied ledger state for one configured Spot market."""

    free_capital: Decimal
    total_exposure: Decimal
    market_exposure: Decimal
    position_quantity: Decimal
    reserved_buy_notional: Decimal = Decimal("0")
    reserved_sell_quantity: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    account_reconciled: bool | None = None
    execution_error: bool | None = None

    def __post_init__(self) -> None:
        """Prevent non-finite or negative balances and exposures entering the gate."""
        for name in (
            "free_capital",
            "total_exposure",
            "market_exposure",
            "position_quantity",
            "reserved_buy_notional",
            "reserved_sell_quantity",
        ):
            value = getattr(self, name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative.")
        for name in ("realized_pnl", "unrealized_pnl"):
            if not getattr(self, name).is_finite():
                raise ValueError(f"{name} must be finite.")


@dataclass(frozen=True, slots=True)
class RiskResult:
    """A single gate action plus a reason and quantity for ALLOW/REDUCE."""

    action: RiskAction
    reason_code: str
    explanation: str
    approved_quantity: Decimal | None = None

    def __post_init__(self) -> None:
        """Ensure only a positive, finite quantity can be authorized."""
        if self.approved_quantity is not None and (
            not self.approved_quantity.is_finite() or self.approved_quantity <= 0
        ):
            raise ValueError("approved_quantity must be finite and positive.")
        if self.action in (RiskAction.ALLOW, RiskAction.REDUCE) and (
            self.approved_quantity is None
        ):
            raise ValueError("ALLOW and REDUCE decisions require approved_quantity.")
        if self.action in (RiskAction.REJECT, RiskAction.HALT) and (
            self.approved_quantity is not None
        ):
            raise ValueError("REJECT and HALT decisions must not authorize quantity.")
        if not self.reason_code.strip() or not self.explanation.strip():
            raise ValueError("Risk results require a reason code and explanation.")
