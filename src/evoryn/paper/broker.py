"""A deterministic single-market paper broker with no exchange-write capability."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from threading import RLock

from evoryn.domain.models import (
    Fill,
    Market,
    MarketSnapshot,
    OrderRequest,
    OrderType,
    Side,
)
from evoryn.paper.failure_injection import FailureInjector, FailureMode
from evoryn.ports.contracts import MarketDataPort, RiskPort
from evoryn.risk.models import PortfolioSnapshot, RiskAction, RiskResult


def _runtime_is_instance(value: object, expected: type[object]) -> bool:
    """Runtime guard for dynamically supplied port implementations."""
    return isinstance(value, expected)


class PaperOrderStatus(StrEnum):
    """Exact external paper lifecycle labels; no live venue status is implied."""

    REJECTED = "rejected"
    OPEN = "open"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PaperOrder:
    """Immutable order state returned to callers after each simulation step."""

    order_id: str
    request: OrderRequest
    requested_quantity: Decimal
    status: PaperOrderStatus
    filled_quantity: Decimal
    fills: tuple[Fill, ...]
    created_at: datetime
    updated_at: datetime
    reason: str | None = None
    last_confirmed_status: PaperOrderStatus | None = None

    @property
    def remaining_quantity(self) -> Decimal:
        """Return unfilled quantity from the risk-approved order size."""
        return max(Decimal("0"), self.request.quantity - self.filled_quantity)


@dataclass(frozen=True, slots=True)
class PaperBrokerState:
    """Validated point-in-time broker state for deterministic offline restart recovery."""

    market: Market
    initial_capital: Decimal
    cash: Decimal
    position_quantity: Decimal
    average_entry_price: Decimal
    realized_pnl: Decimal
    orders: tuple[PaperOrder, ...]


class PaperBroker:
    """Risk-gated paper simulator for one configured Spot market.

    Market snapshots must provide Decimal values for bid, ask, last, bid_size and
    ask_size. Liquidity is deliberately limited to visible top-of-book quantity;
    no claim is made about hidden depth or real exchange fills.
    """

    def __init__(
        self,
        *,
        market: Market,
        market_data: MarketDataPort,
        risk_port: RiskPort,
        initial_capital: Decimal,
        max_market_data_age_seconds: int = 10,
        failure_injector: FailureInjector | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Build an isolated paper ledger; it cannot place a real exchange order."""
        if not initial_capital.is_finite() or initial_capital <= 0:
            raise ValueError("initial_capital must be finite and positive.")
        if max_market_data_age_seconds <= 0:
            raise ValueError("max_market_data_age_seconds must be positive.")
        self._market = market
        self._market_data = market_data
        self._risk_port = risk_port
        self._initial_capital = initial_capital
        self._cash = initial_capital
        self._position_quantity = Decimal("0")
        self._average_entry_price = Decimal("0")
        self._realized_pnl = Decimal("0")
        self._orders: dict[str, PaperOrder] = {}
        self._risk_decisions: dict[str, RiskResult] = {}
        self._liquidity_used: dict[tuple[datetime, Side], Decimal] = {}
        self._max_data_age = max_market_data_age_seconds
        self._failures = failure_injector or FailureInjector()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()

    def submit_order(self, request: OrderRequest) -> PaperOrder:
        """Submit an idempotent simulation request only after the independent risk gate."""
        with self._lock:
            existing = self._orders.get(request.client_order_id)
            if existing is not None:
                same_request = (
                    existing.request.market == request.market
                    and existing.request.side is request.side
                    and existing.request.order_type is request.order_type
                    and existing.requested_quantity == request.quantity
                    and existing.request.price == request.price
                )
                if not same_request:
                    raise ValueError("Client order ID is already bound to a different request.")
                return existing

            now = self._now()
            order_id = f"paper:{request.client_order_id}"
            if request.market != self._market:
                return self._store_rejected(
                    order_id, request, now, "requested_market_mismatch"
                )
            if any(
                item.status is PaperOrderStatus.UNKNOWN for item in self._orders.values()
            ):
                self._risk_port.emergency_halt("unresolved_unknown_order")
            if self._risk_port.is_halted:
                return self._store_rejected(
                    order_id, request, now, "risk_halt_latched"
                )

            injected = self._failures.consume("submit")
            if injected in (FailureMode.NETWORK_DISCONNECT, FailureMode.TIMEOUT):
                self._risk_port.emergency_halt(f"paper_submit_{injected.value}")
                return self._store(
                    PaperOrder(
                        order_id=order_id,
                        request=request,
                        requested_quantity=request.quantity,
                        status=PaperOrderStatus.UNKNOWN,
                        filled_quantity=Decimal("0"),
                        fills=(),
                        created_at=now,
                        updated_at=now,
                        reason=f"injected_{injected.value}; order outcome is ambiguous",
                    )
                )
            if injected is FailureMode.INVALID_DATA:
                self._risk_port.emergency_halt("injected_invalid_submit_data")
                return self._store_rejected(
                    order_id, request, now, "injected_invalid_submit_data"
                )

            data_fault = self._failures.consume("market_data")
            if data_fault is not None:
                self._risk_port.emergency_halt(f"market_data_{data_fault.value}")
                return self._store_rejected(
                    order_id, request, now, f"market_data_{data_fault.value}"
                )

            try:
                snapshot = self._market_data.get_snapshot(request.market)
            except Exception:
                self._risk_port.emergency_halt("market_data_read_failure")
                return self._store_rejected(
                    order_id, request, now, "market_data_read_failure"
                )

            fault = self._snapshot_fault(snapshot, now)
            if fault is not None:
                self._risk_port.emergency_halt(fault)
                return self._store_rejected(order_id, request, now, fault)

            portfolio = self._portfolio_snapshot(snapshot)
            try:
                decision = self._risk_port.evaluate(
                    request, portfolio, snapshot, now=now
                )
            except Exception:
                self._risk_port.emergency_halt("risk_engine_failure")
                return self._store_rejected(order_id, request, now, "risk_engine_failure")

            if not _runtime_is_instance(decision, RiskResult):
                self._risk_port.emergency_halt("invalid_risk_decision")
                return self._store_rejected(order_id, request, now, "invalid_risk_decision")
            self._risk_decisions[request.client_order_id] = decision
            if decision.action in (RiskAction.REJECT, RiskAction.HALT):
                return self._store_rejected(
                    order_id, request, now, f"{decision.reason_code}: {decision.explanation}"
                )
            if (
                decision.action not in (RiskAction.ALLOW, RiskAction.REDUCE)
                or decision.approved_quantity is None
                or decision.approved_quantity > request.quantity
                or (
                    decision.action is RiskAction.ALLOW
                    and decision.approved_quantity != request.quantity
                )
                or (
                    decision.action is RiskAction.REDUCE
                    and decision.approved_quantity >= request.quantity
                )
            ):
                self._risk_port.emergency_halt("invalid_risk_decision")
                return self._store_rejected(order_id, request, now, "invalid_risk_decision")

            approved_request = replace(request, quantity=decision.approved_quantity)
            reason = (
                f"{decision.reason_code}: {decision.explanation}"
                if decision.action is RiskAction.REDUCE
                else None
            )
            order = PaperOrder(
                order_id=order_id,
                request=approved_request,
                requested_quantity=request.quantity,
                status=PaperOrderStatus.OPEN,
                filled_quantity=Decimal("0"),
                fills=(),
                created_at=now,
                updated_at=now,
                reason=reason,
            )
            self._orders[request.client_order_id] = order
            return self._match_order(request.client_order_id, snapshot)

    def process_market_update(self, snapshot: MarketSnapshot) -> tuple[PaperOrder, ...]:
        """Process a deterministic market tick against all resting paper orders."""
        with self._lock:
            if any(
                order.status is PaperOrderStatus.UNKNOWN for order in self._orders.values()
            ):
                self._risk_port.emergency_halt("unresolved_unknown_order")
                return tuple(self._orders.values())

            injected = self._failures.consume("market_update")
            if injected in (FailureMode.NETWORK_DISCONNECT, FailureMode.TIMEOUT):
                self._risk_port.emergency_halt(f"paper_market_update_{injected.value}")
                for key, order in tuple(self._orders.items()):
                    if order.status in (PaperOrderStatus.OPEN, PaperOrderStatus.PARTIAL):
                        self._orders[key] = replace(
                            order,
                            status=PaperOrderStatus.UNKNOWN,
                            updated_at=self._now(),
                            reason=f"injected_{injected.value}; recovery required",
                            last_confirmed_status=order.status,
                        )
                return tuple(self._orders.values())
            if injected is FailureMode.INVALID_DATA:
                self._risk_port.emergency_halt("injected_invalid_market_update")
                return tuple(self._orders.values())

            fault = self._snapshot_fault(snapshot, self._now())
            if fault is not None:
                self._risk_port.emergency_halt(fault)
                return tuple(self._orders.values())
            if self._risk_port.is_halted:
                return tuple(self._orders.values())

            for key, order in tuple(self._orders.items()):
                if order.status in (PaperOrderStatus.OPEN, PaperOrderStatus.PARTIAL):
                    self._match_order(key, snapshot)
            return tuple(self._orders.values())

    def cancel_order(self, order_reference: str) -> PaperOrder:
        """Cancel an unfilled or partially-filled order; unknown results stay unknown."""
        with self._lock:
            key = self._resolve_key(order_reference)
            order = self._orders.get(key)
            if order is None:
                raise KeyError("Paper order was not found.")
            if order.status in (PaperOrderStatus.OPEN, PaperOrderStatus.PARTIAL):
                order = replace(
                    order,
                    status=PaperOrderStatus.CANCELED,
                    updated_at=self._now(),
                    reason="canceled_in_paper_ledger",
                )
                self._orders[key] = order
            return order

    def get_order(self, order_reference: str) -> PaperOrder | None:
        """Return an order by client identifier or deterministic paper order id."""
        with self._lock:
            return self._orders.get(self._resolve_key(order_reference))

    def get_risk_decision(self, order_reference: str) -> RiskResult | None:
        """Return the exact deterministic risk result used for this paper order."""
        with self._lock:
            return self._risk_decisions.get(self._resolve_key(order_reference))

    def list_orders(self) -> tuple[PaperOrder, ...]:
        """Return an immutable point-in-time view of every paper order."""
        with self._lock:
            return tuple(self._orders.values())

    def emergency_halt(self, reason: str) -> None:
        """Expose the same latched risk stop to execution/reconciliation orchestration."""
        self._risk_port.emergency_halt(reason)

    def resolve_unknown_order(
        self,
        order_reference: str,
        *,
        confirmed_status: PaperOrderStatus,
        operator_acknowledged: bool,
    ) -> PaperOrder:
        """Reconcile an unknown paper order only after explicit operator confirmation.

        The confirmed state must agree with fills already recorded in the local ledger.
        This method never invents fills; an outcome requiring additional fill records
        remains unresolved and cannot be cleared through this shortcut.
        """
        with self._lock:
            if operator_acknowledged is not True:
                raise PermissionError(
                    "Resolving an unknown order requires operator acknowledgement."
                )
            if confirmed_status is PaperOrderStatus.UNKNOWN:
                raise ValueError("Reconciliation must resolve unknown to a known state.")
            key = self._resolve_key(order_reference)
            order = self._orders.get(key)
            if order is None:
                raise KeyError("Paper order was not found.")
            if order.status is not PaperOrderStatus.UNKNOWN:
                raise ValueError("Only an unknown paper order can be reconciled.")

            filled = order.filled_quantity
            quantity = order.request.quantity
            if confirmed_status is PaperOrderStatus.OPEN and filled != 0:
                raise ValueError("An open order cannot have already recorded fills.")
            if confirmed_status is PaperOrderStatus.PARTIAL and not (
                Decimal("0") < filled < quantity
            ):
                raise ValueError("A partial order requires a positive incomplete fill quantity.")
            if confirmed_status is PaperOrderStatus.FILLED and filled != quantity:
                raise ValueError("A filled order requires complete fill records in the ledger.")
            if confirmed_status is PaperOrderStatus.REJECTED and filled != 0:
                raise ValueError("A rejected order cannot have recorded fills.")
            if confirmed_status not in (
                PaperOrderStatus.OPEN,
                PaperOrderStatus.PARTIAL,
                PaperOrderStatus.FILLED,
                PaperOrderStatus.CANCELED,
                PaperOrderStatus.REJECTED,
            ):
                raise ValueError("Unsupported reconciled order status.")

            resolved = replace(
                order,
                status=confirmed_status,
                updated_at=self._now(),
                reason=f"operator_reconciled_as_{confirmed_status.value}",
                last_confirmed_status=None,
            )
            self._orders[key] = resolved
            return resolved

    @property
    def cash(self) -> Decimal:
        """Current unreserved quote-currency cash after simulated fills."""
        with self._lock:
            return self._cash

    @property
    def position_quantity(self) -> Decimal:
        """Current long-only base quantity in this single-market paper account."""
        with self._lock:
            return self._position_quantity

    @property
    def realized_pnl(self) -> Decimal:
        """Realized profit/loss from closed portions of simulated long positions."""
        with self._lock:
            return self._realized_pnl

    @property
    def average_entry_price(self) -> Decimal:
        """Current weighted average entry price for the Paper long position."""
        with self._lock:
            return self._average_entry_price

    def portfolio_snapshot(self, snapshot: MarketSnapshot) -> PortfolioSnapshot:
        """Return a checked account/exposure view using current marks and order reservations."""
        with self._lock:
            fault = self._snapshot_fault(snapshot, self._now())
            if fault is not None:
                raise ValueError(f"Paper portfolio snapshot rejected: {fault}")
            return self._portfolio_snapshot(snapshot)

    def export_state(self) -> PaperBrokerState:
        """Export immutable broker state for a durable Paper runtime checkpoint."""
        with self._lock:
            return PaperBrokerState(
                market=self._market,
                initial_capital=self._initial_capital,
                cash=self._cash,
                position_quantity=self._position_quantity,
                average_entry_price=self._average_entry_price,
                realized_pnl=self._realized_pnl,
                orders=tuple(self._orders.values()),
            )

    def restore_state(self, state: PaperBrokerState) -> None:
        """Restore only a fresh broker from a committed Paper ledger snapshot.

        This is not an exchange reconciliation mechanism. The runtime rebuilds the
        mock venue independently and reconciles both ledgers before processing.
        """
        if not isinstance(state, PaperBrokerState) or state.market != self._market:
            raise ValueError("Paper restore state has an invalid market identity.")
        if state.initial_capital != self._initial_capital:
            raise ValueError("Paper restore initial capital differs from configuration.")
        for name in ("cash", "position_quantity", "average_entry_price"):
            value = getattr(state, name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"Paper restore field {name} must be finite and non-negative.")
        if not state.realized_pnl.is_finite():
            raise ValueError("Paper restore realized PnL must be finite.")
        seen_client_ids: set[str] = set()
        for order in state.orders:
            if order.request.market != self._market:
                raise ValueError("Paper restore includes an order for another market.")
            if order.request.client_order_id in seen_client_ids:
                raise ValueError("Paper restore has duplicate client order identifiers.")
            seen_client_ids.add(order.request.client_order_id)
            if (
                not order.requested_quantity.is_finite()
                or order.requested_quantity <= 0
                or order.request.quantity > order.requested_quantity
                or not order.filled_quantity.is_finite()
                or order.filled_quantity < 0
                or order.filled_quantity > order.request.quantity
            ):
                raise ValueError("Paper restore order quantities violate the order contract.")
            if sum((fill.quantity for fill in order.fills), Decimal("0")) != order.filled_quantity:
                raise ValueError("Paper restore fill quantity does not match order state.")
            if any(
                not fill.quantity.is_finite()
                or fill.quantity <= 0
                or not fill.price.is_finite()
                or fill.price <= 0
                or fill.occurred_at.tzinfo is None
                or fill.occurred_at.utcoffset() is None
                for fill in order.fills
            ):
                raise ValueError("Paper restore contains an invalid fill.")
        with self._lock:
            if (
                self._orders
                or self._cash != self._initial_capital
                or self._position_quantity != 0
                or self._realized_pnl != 0
            ):
                raise RuntimeError("Paper restore is permitted only on a fresh broker.")
            self._cash = state.cash
            self._position_quantity = state.position_quantity
            self._average_entry_price = state.average_entry_price
            self._realized_pnl = state.realized_pnl
            self._orders = {order.request.client_order_id: order for order in state.orders}
            self._risk_decisions.clear()
            self._liquidity_used.clear()

    def _match_order(self, key: str, snapshot: MarketSnapshot) -> PaperOrder:
        """Fill eligible quantity against the current visible top-of-book liquidity."""
        order = self._orders[key]
        if order.status not in (PaperOrderStatus.OPEN, PaperOrderStatus.PARTIAL):
            return order
        if self._risk_port.is_halted:
            return order
        fault = self._snapshot_fault(snapshot, self._now())
        if fault is not None:
            self._risk_port.emergency_halt(fault)
            return order

        prices = snapshot.values
        if order.request.side is Side.BUY:
            execution_price = prices["ask"]
            available = prices["ask_size"]
            liquidity_key = (snapshot.observed_at, Side.BUY)
            available = max(
                Decimal("0"),
                available - self._liquidity_used.get(liquidity_key, Decimal("0")),
            )
            crosses = (
                order.request.order_type is OrderType.MARKET
                or (
                    order.request.price is not None
                    and order.request.price >= execution_price
                )
            )
        else:
            execution_price = prices["bid"]
            available = prices["bid_size"]
            liquidity_key = (snapshot.observed_at, Side.SELL)
            available = max(
                Decimal("0"),
                available - self._liquidity_used.get(liquidity_key, Decimal("0")),
            )
            crosses = (
                order.request.order_type is OrderType.MARKET
                or (
                    order.request.price is not None
                    and order.request.price <= execution_price
                )
            )
        if not crosses or available <= 0:
            return order

        fill_quantity = min(order.remaining_quantity, available)
        if fill_quantity <= 0:
            return order
        now = self._now()
        fill = Fill(
            order_id=order.order_id,
            quantity=fill_quantity,
            price=execution_price,
            occurred_at=now,
        )
        if not self._apply_fill(order.request.side, fill_quantity, execution_price):
            return order
        liquidity_key = (snapshot.observed_at, order.request.side)
        self._liquidity_used[liquidity_key] = (
            self._liquidity_used.get(liquidity_key, Decimal("0")) + fill_quantity
        )
        if len(self._liquidity_used) > 256:
            active_timestamp = snapshot.observed_at
            self._liquidity_used = {
                key: value
                for key, value in self._liquidity_used.items()
                if key[0] == active_timestamp
            }
        total_filled = order.filled_quantity + fill_quantity
        status = (
            PaperOrderStatus.FILLED
            if total_filled >= order.request.quantity
            else PaperOrderStatus.PARTIAL
        )
        updated = replace(
            order,
            status=status,
            filled_quantity=total_filled,
            fills=(*order.fills, fill),
            updated_at=now,
        )
        self._orders[key] = updated
        return updated

    def _apply_fill(self, side: Side, quantity: Decimal, price: Decimal) -> bool:
        """Apply a confirmed paper fill using Decimal-only accounting."""
        notional = quantity * price
        if side is Side.BUY:
            if notional > self._cash:
                self._risk_port.emergency_halt("paper_ledger_cash_invariant")
                return False
            prior_cost = self._position_quantity * self._average_entry_price
            self._cash -= notional
            self._position_quantity += quantity
            self._average_entry_price = (
                (prior_cost + notional) / self._position_quantity
            )
            return True

        if quantity > self._position_quantity:
            self._risk_port.emergency_halt("paper_ledger_position_invariant")
            return False
        self._cash += notional
        self._realized_pnl += quantity * (price - self._average_entry_price)
        self._position_quantity -= quantity
        if self._position_quantity == 0:
            self._average_entry_price = Decimal("0")
        return True

    def _portfolio_snapshot(self, snapshot: MarketSnapshot) -> PortfolioSnapshot:
        """Build the ledger view including reservations for currently-open orders."""
        mid = (snapshot.values["bid"] + snapshot.values["ask"]) / Decimal("2")
        reserved_buy = Decimal("0")
        reserved_sell = Decimal("0")
        for order in self._orders.values():
            if order.status not in (PaperOrderStatus.OPEN, PaperOrderStatus.PARTIAL):
                continue
            if order.request.side is Side.BUY:
                reference = order.request.price or snapshot.values["ask"]
                reserved_buy += order.remaining_quantity * reference
            else:
                reserved_sell += order.remaining_quantity
        exposure = self._position_quantity * mid + reserved_buy
        unrealized = self._position_quantity * (mid - self._average_entry_price)
        return PortfolioSnapshot(
            free_capital=max(Decimal("0"), self._cash - reserved_buy),
            total_exposure=exposure,
            market_exposure=exposure,
            position_quantity=self._position_quantity,
            reserved_buy_notional=reserved_buy,
            reserved_sell_quantity=reserved_sell,
            realized_pnl=self._realized_pnl,
            unrealized_pnl=unrealized,
            account_reconciled=True,
            execution_error=False,
        )

    def _snapshot_fault(self, snapshot: MarketSnapshot, now: datetime) -> str | None:
        """Reject stale, unrelated, or malformed market data before simulating fills."""
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            return "invalid_paper_clock"
        if snapshot.market != self._market:
            return "paper_market_mismatch"
        if _runtime_is_instance(snapshot.observed_at, datetime) is False or (
            snapshot.observed_at.tzinfo is None
            or snapshot.observed_at.utcoffset() is None
        ):
            return "invalid_market_timestamp"
        age = now - snapshot.observed_at
        if age < timedelta(0) or age.total_seconds() > self._max_data_age:
            return "stale_market_data"
        if not isinstance(snapshot.source, str) or not snapshot.source.strip():
            return "market_data_quality_failure"
        if not _runtime_is_instance(snapshot.quality_flags, tuple):
            return "invalid_market_quality_metadata"
        if snapshot.quality_flags:
            return "market_data_quality_failure"
        if not _runtime_is_instance(snapshot.values, dict):
            return "invalid_market_values"
        required = ("bid", "ask", "last", "bid_size", "ask_size")
        for name in required:
            value = snapshot.values.get(name)
            if not isinstance(value, Decimal) or not value.is_finite():
                return f"invalid_market_value_{name}"
            if name in ("bid", "ask", "last") and value <= 0:
                return f"invalid_market_price_{name}"
            if name in ("bid_size", "ask_size") and value < 0:
                return f"invalid_market_size_{name}"
        if snapshot.values["bid"] > snapshot.values["ask"]:
            return "crossed_market"
        return None

    def _store_rejected(
        self,
        order_id: str,
        request: OrderRequest,
        now: datetime,
        reason: str,
    ) -> PaperOrder:
        """Persist rejected intents for auditability and client-id idempotency."""
        return self._store(
            PaperOrder(
                order_id=order_id,
                request=request,
                requested_quantity=request.quantity,
                status=PaperOrderStatus.REJECTED,
                filled_quantity=Decimal("0"),
                fills=(),
                created_at=now,
                updated_at=now,
                reason=reason,
            )
        )

    def _store(self, order: PaperOrder) -> PaperOrder:
        """Store by client order ID so repeated submissions never create a second order."""
        self._orders[order.request.client_order_id] = order
        return order

    def _resolve_key(self, reference: str) -> str:
        """Map either the stable client ID or paper order ID to internal storage."""
        if reference in self._orders:
            return reference
        prefix = "paper:"
        return reference[len(prefix):] if reference.startswith(prefix) else reference

    def _now(self) -> datetime:
        """Require an aware deterministic clock from production code or tests."""
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Paper broker clock must return timezone-aware timestamps.")
        return value
