"""Deterministic risk gate. This module intentionally has no strategy or AI imports."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from threading import RLock

from evoryn.domain.models import MarketSnapshot, OrderRequest, OrderType, Side
from evoryn.risk.models import (
    PortfolioSnapshot,
    RiskAction,
    RiskLimits,
    RiskResult,
)


class RiskEngine:
    """Stateful, latched fail-closed gate for a single Spot market."""

    def __init__(self, limits: RiskLimits) -> None:
        """Construct the gate from validated, explicit hard limits."""
        self._limits = limits
        self._lock = RLock()
        self._halt_reason: str | None = None
        self._kill_switch_active = False

    @property
    def is_halted(self) -> bool:
        """True while an automatic halt or manual kill switch is latched."""
        with self._lock:
            return self._halt_reason is not None or self._kill_switch_active

    @property
    def halt_reason(self) -> str | None:
        """Return a non-secret description of the latched stop condition."""
        with self._lock:
            if self._kill_switch_active:
                return self._halt_reason or "manual_kill_switch"
            return self._halt_reason

    def activate_kill_switch(self, reason: str = "manual_kill_switch") -> None:
        """Latch an operator-triggered stop; no order can clear it implicitly."""
        with self._lock:
            self._kill_switch_active = True
            self._halt_reason = reason.strip() or "manual_kill_switch"

    def emergency_halt(self, reason: str) -> None:
        """Latch an automatic halt after a data, account, or execution fault."""
        with self._lock:
            if self._halt_reason is None:
                self._halt_reason = reason.strip() or "unspecified_critical_fault"

    def clear_halt(self, *, operator_acknowledged: bool) -> None:
        """Require explicit acknowledgement before clearing a latched stop."""
        if not operator_acknowledged:
            raise PermissionError("Clearing a risk halt requires explicit operator acknowledgement.")
        with self._lock:
            self._halt_reason = None
            self._kill_switch_active = False

    def evaluate(
        self,
        request: OrderRequest,
        portfolio: PortfolioSnapshot,
        market_data: MarketSnapshot,
        *,
        now: datetime,
    ) -> RiskResult:
        """Return exactly ALLOW, REDUCE, REJECT, or HALT for this order request."""
        with self._lock:
            if not isinstance(request, OrderRequest) or not isinstance(
                portfolio, PortfolioSnapshot
            ) or not isinstance(market_data, MarketSnapshot):
                self.emergency_halt("invalid_risk_input")
                return self._result(
                    RiskAction.HALT,
                    "invalid_risk_input",
                    "Risk evaluation inputs do not satisfy the validated contracts.",
                )
            if self._kill_switch_active or self._halt_reason is not None:
                return self._result(
                    RiskAction.HALT,
                    "risk_halt_latched",
                    self._halt_reason or "The manual kill switch is active.",
                )

            unsafe_data = self._data_failure(request, market_data, now)
            if unsafe_data is not None:
                self.emergency_halt(unsafe_data[0])
                return self._result(RiskAction.HALT, unsafe_data[0], unsafe_data[1])

            if portfolio.execution_error is None:
                self.emergency_halt("execution_status_unknown")
                return self._result(
                    RiskAction.HALT,
                    "execution_status_unknown",
                    "Execution-fault state is unknown; further orders are halted.",
                )
            if portfolio.execution_error:
                self.emergency_halt("execution_error")
                return self._result(
                    RiskAction.HALT,
                    "execution_error",
                    "An execution fault is unresolved; further orders are halted.",
                )
            if portfolio.account_reconciled is None:
                self.emergency_halt("account_state_unknown")
                return self._result(
                    RiskAction.HALT,
                    "account_state_unknown",
                    "Account reconciliation state is unknown.",
                )
            if not portfolio.account_reconciled:
                self.emergency_halt("account_mismatch")
                return self._result(
                    RiskAction.HALT,
                    "account_mismatch",
                    "The internal account ledger is not reconciled.",
                )

            net_pnl = portfolio.realized_pnl + portfolio.unrealized_pnl
            if net_pnl <= -self._limits.max_loss:
                self.emergency_halt("max_loss_exceeded")
                return self._result(
                    RiskAction.HALT,
                    "max_loss_exceeded",
                    "The configured maximum loss has been reached or exceeded.",
                )

            prices = market_data.values
            side_price = prices["ask"] if request.side is Side.BUY else prices["bid"]
            risk_price = (
                max(request.price, side_price)
                if request.order_type is OrderType.LIMIT and request.price is not None
                else side_price
            )
            requested = request.quantity

            if request.side is Side.SELL:
                available = portfolio.position_quantity - portfolio.reserved_sell_quantity
                if available <= 0:
                    return self._result(
                        RiskAction.REJECT,
                        "insufficient_position",
                        "No unreserved Spot position is available to sell.",
                    )
                max_by_order = self._limits.max_order_notional / risk_price
                allowed = min(requested, available, max_by_order)
            else:
                free_capital = portfolio.free_capital - portfolio.reserved_buy_notional
                remaining_total = (
                    self._limits.max_total_exposure - portfolio.total_exposure
                )
                remaining_market = (
                    self._limits.max_market_exposure - portfolio.market_exposure
                )
                max_by_capital = max(Decimal("0"), free_capital) / risk_price
                max_by_total = max(Decimal("0"), remaining_total) / risk_price
                max_by_market = max(Decimal("0"), remaining_market) / risk_price
                max_by_order = self._limits.max_order_notional / risk_price
                allowed = min(
                    requested,
                    max_by_capital,
                    max_by_total,
                    max_by_market,
                    max_by_order,
                )

            if not allowed.is_finite() or allowed <= 0:
                return self._result(
                    RiskAction.REJECT,
                    "risk_capacity_exhausted",
                    "Free capital, position, or an exposure limit leaves no safe quantity.",
                )
            if allowed < requested:
                return RiskResult(
                    action=RiskAction.REDUCE,
                    reason_code="hard_limit_reduction",
                    explanation=(
                        "Requested quantity exceeds free capital, position, per-order, "
                        "or exposure capacity; only the bounded quantity is authorized."
                    ),
                    approved_quantity=allowed,
                )
            return RiskResult(
                action=RiskAction.ALLOW,
                reason_code="within_hard_limits",
                explanation="Order is within validated deterministic risk limits.",
                approved_quantity=requested,
            )

    def _data_failure(
        self,
        request: OrderRequest,
        market_data: MarketSnapshot,
        now: datetime,
    ) -> tuple[str, str] | None:
        """Validate all data needed by both the risk calculation and fill simulator."""
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            return ("invalid_evaluation_time", "Evaluation time must include a timezone.")
        if not isinstance(market_data.observed_at, datetime) or (
            market_data.observed_at.tzinfo is None
            or market_data.observed_at.utcoffset() is None
        ):
            return ("invalid_market_timestamp", "Market data timestamp is not timezone-aware.")
        if market_data.market != request.market:
            return ("market_mismatch", "Risk input market does not match the requested market.")
        if not isinstance(market_data.source, str) or not market_data.source.strip():
            return ("missing_market_source", "Market data has no source identifier.")
        if not isinstance(market_data.values, dict):
            return ("invalid_market_values", "Market data values must be a mapping.")
        if market_data.quality_flags:
            return ("market_quality_failure", "Market data has one or more quality failure flags.")

        age = now - market_data.observed_at
        if age < timedelta(0) or age.total_seconds() > self._limits.max_market_data_age_seconds:
            return ("stale_market_data", "Market data is stale or timestamped in the future.")

        for key in ("bid", "ask", "last", "bid_size", "ask_size"):
            value = market_data.values.get(key)
            if not isinstance(value, Decimal) or not value.is_finite():
                return ("invalid_market_value", f"Required market value {key!r} is missing or invalid.")
            if key in ("bid", "ask", "last") and value <= 0:
                return ("invalid_market_price", f"Required market price {key!r} is not positive.")
            if key in ("bid_size", "ask_size") and value < 0:
                return ("invalid_market_size", f"Market liquidity {key!r} is negative.")
        if market_data.values["bid"] > market_data.values["ask"]:
            return ("crossed_market", "Best bid is greater than best ask.")
        return None

    @staticmethod
    def _result(action: RiskAction, code: str, explanation: str) -> RiskResult:
        """Build a decision that does not accidentally authorize a quantity."""
        return RiskResult(
            action=action,
            reason_code=code,
            explanation=explanation,
        )
