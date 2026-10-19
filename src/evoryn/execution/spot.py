"""Paper-only Spot execution orchestration, append-only ledgers, and reconciliation.

This module deliberately depends on PaperBroker rather than ExchangePort. It has no
network client and cannot place real exchange orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from threading import RLock
from typing import Callable

from evoryn.domain.models import Fill, Market, MarketSnapshot, OrderRequest, Side
from evoryn.domain.strategy_decisions import StrategySelectionLedgerEntry
from evoryn.paper.broker import PaperBroker, PaperOrder, PaperOrderStatus
from evoryn.risk.models import RiskAction, RiskResult


class ExecutionState(StrEnum):
    """Normalized state exposed by the paper-only orchestration boundary."""

    REJECTED = "rejected"
    OPEN = "open"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SpotIntent:
    """A correlation-scoped Spot order intent; it is not an execution authorization."""

    correlation_id: str
    request: OrderRequest
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.correlation_id.strip():
            raise ValueError("Intent correlation_id must not be empty.")
        if not isinstance(self.request, OrderRequest):
            raise ValueError("SpotIntent requires a validated OrderRequest.")
        _require_aware(self.created_at, "Intent timestamp")


@dataclass(frozen=True, slots=True)
class BalanceSnapshot:
    """A point-in-time view of the two assets used by a single Spot market."""

    captured_at: datetime
    base_asset: str
    base_available: Decimal
    quote_asset: str
    quote_available: Decimal

    def __post_init__(self) -> None:
        _require_aware(self.captured_at, "Balance timestamp")
        if not self.base_asset.strip() or not self.quote_asset.strip():
            raise ValueError("Balance asset names must not be empty.")
        if self.base_asset.upper() == self.quote_asset.upper():
            raise ValueError("Base and quote assets must be different.")
        for name in ("base_available", "quote_available"):
            value = getattr(self, name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative.")


@dataclass(frozen=True, slots=True)
class IntentLedgerEntry:
    """Immutable intent-ledger row that links a request to one correlation ID."""

    event_id: str
    correlation_id: str
    recorded_at: datetime
    client_order_id: str
    market_symbol: str
    side: Side
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class RiskLedgerEntry:
    """The exact risk result used by PaperBroker before simulated order matching."""

    event_id: str
    correlation_id: str
    recorded_at: datetime
    client_order_id: str
    action: RiskAction | None
    reason_code: str
    explanation: str
    requested_quantity: Decimal
    approved_quantity: Decimal | None


@dataclass(frozen=True, slots=True)
class ExecutionLedgerEntry:
    """Append-only paper order state transition, including the fills known at that time."""

    event_id: str
    correlation_id: str
    recorded_at: datetime
    client_order_id: str
    paper_order_id: str
    status: PaperOrderStatus
    requested_quantity: Decimal
    approved_quantity: Decimal
    filled_quantity: Decimal
    fills: tuple[Fill, ...]
    reason: str | None


@dataclass(frozen=True, slots=True)
class BalanceLedgerEntry:
    """The internal paper balances recorded alongside a correlated execution event."""

    event_id: str
    correlation_id: str
    recorded_at: datetime
    balance: BalanceSnapshot


@dataclass(frozen=True, slots=True)
class ExecutionErrorEntry:
    """Sanitized, correlation-linked record of a fail-closed execution error."""

    event_id: str
    correlation_id: str
    occurred_at: datetime
    client_order_id: str
    component: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Immutable result comparing the internal paper ledger with the mock venue mirror."""

    event_id: str
    correlation_id: str
    checked_at: datetime
    matched: bool
    differences: tuple[str, ...]
    compared_orders: int
    internal_balance: BalanceSnapshot
    venue_balance: BalanceSnapshot


@dataclass(frozen=True, slots=True)
class MockVenueOrder:
    """Separate immutable order representation held by the in-memory mock venue."""

    order_id: str
    client_order_id: str
    request: OrderRequest
    requested_quantity: Decimal
    status: PaperOrderStatus
    filled_quantity: Decimal
    fills: tuple[Fill, ...]
    reason: str | None


@dataclass(frozen=True, slots=True)
class SpotExecutionResult:
    """Result of one correlation-scoped paper intent and its immediate reconciliation."""

    intent: SpotIntent
    order: PaperOrder
    risk: RiskResult | None
    balance: BalanceSnapshot
    reconciliation: ReconciliationReport

    @property
    def state(self) -> ExecutionState:
        """Translate the broker's constrained state into the orchestration vocabulary."""
        return ExecutionState(self.order.status.value)

    @property
    def reconciliation_required(self) -> bool:
        """Unknown outcomes and any comparison difference must be resolved before reuse."""
        return self.order.status is PaperOrderStatus.UNKNOWN or not self.reconciliation.matched


class ExecutionLedger:
    """Thread-safe in-memory append-only ledgers; persistence is a separate future concern."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._intents: list[IntentLedgerEntry] = []
        self._risk_decisions: list[RiskLedgerEntry] = []
        self._executions: list[ExecutionLedgerEntry] = []
        self._balances: list[BalanceLedgerEntry] = []
        self._errors: list[ExecutionErrorEntry] = []
        self._reconciliations: list[ReconciliationReport] = []
        self._strategy_selections: list[StrategySelectionLedgerEntry] = []

    @property
    def intents(self) -> tuple[IntentLedgerEntry, ...]:
        with self._lock:
            return tuple(self._intents)

    @property
    def risk_decisions(self) -> tuple[RiskLedgerEntry, ...]:
        with self._lock:
            return tuple(self._risk_decisions)

    @property
    def executions(self) -> tuple[ExecutionLedgerEntry, ...]:
        with self._lock:
            return tuple(self._executions)

    @property
    def balances(self) -> tuple[BalanceLedgerEntry, ...]:
        with self._lock:
            return tuple(self._balances)

    @property
    def errors(self) -> tuple[ExecutionErrorEntry, ...]:
        with self._lock:
            return tuple(self._errors)

    @property
    def reconciliations(self) -> tuple[ReconciliationReport, ...]:
        with self._lock:
            return tuple(self._reconciliations)

    @property
    def strategy_selections(self) -> tuple[StrategySelectionLedgerEntry, ...]:
        """Return the append-only strategy-selection ledger in recorded order."""
        with self._lock:
            return tuple(self._strategy_selections)

    def record_error(self, entry: ExecutionErrorEntry) -> None:
        with self._lock:
            self._errors.append(entry)

    def record_intent(self, entry: IntentLedgerEntry) -> None:
        with self._lock:
            if any(old.correlation_id == entry.correlation_id for old in self._intents):
                raise ValueError("A correlation ID may identify only one Spot intent.")
            self._intents.append(entry)

    def record_risk_decision(self, entry: RiskLedgerEntry) -> None:
        with self._lock:
            self._risk_decisions.append(entry)

    def record_execution(self, entry: ExecutionLedgerEntry) -> None:
        with self._lock:
            self._executions.append(entry)

    def record_balance(self, entry: BalanceLedgerEntry) -> None:
        with self._lock:
            self._balances.append(entry)

    def record_reconciliation(self, report: ReconciliationReport) -> None:
        with self._lock:
            self._reconciliations.append(report)

    def record_strategy_selection(self, entry: StrategySelectionLedgerEntry) -> None:
        """Append a selection/abstention outcome without granting execution authority."""
        with self._lock:
            if any(old.event_id == entry.event_id for old in self._strategy_selections):
                raise ValueError("A strategy-selection event ID must be unique.")
            self._strategy_selections.append(entry)


class MockSpotExchangeAdapter:
    """Independent in-memory Spot ledger used for integration tests and paper reconciliation.

    This adapter performs no I/O. New fills are applied exactly once to its own balances.
    Explicit drift and missing-order helpers make reconciliation failures reproducible.
    """

    def __init__(
        self,
        *,
        base_asset: str,
        quote_asset: str,
        initial_quote_balance: Decimal,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not base_asset.strip() or not quote_asset.strip():
            raise ValueError("Mock adapter asset names must not be empty.")
        if base_asset.upper() == quote_asset.upper():
            raise ValueError("Mock adapter base and quote assets must be different.")
        if not initial_quote_balance.is_finite() or initial_quote_balance < 0:
            raise ValueError("Initial quote balance must be finite and non-negative.")
        self._base_asset = base_asset.upper()
        self._quote_asset = quote_asset.upper()
        self._quote_balance = initial_quote_balance
        self._base_balance = Decimal("0")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._orders: dict[str, MockVenueOrder] = {}
        self._client_to_order: dict[str, str] = {}

    def observe_order(self, order: PaperOrder) -> MockVenueOrder:
        """Mirror a paper order without sharing the internal broker's mutable state."""
        with self._lock:
            old = self._orders.get(order.order_id)
            mapped_id = self._client_to_order.get(order.request.client_order_id)
            if mapped_id is not None and mapped_id != order.order_id:
                raise ValueError("Mock venue client order ID collision.")
            old_fills = old.fills if old is not None else ()
            if len(order.fills) < len(old_fills) or order.fills[: len(old_fills)] != old_fills:
                raise ValueError("Mock venue observed a regressive or conflicting fill history.")
            if old is not None and old.request != order.request:
                raise ValueError("Mock venue observed a changed order request.")

            new_quote = self._quote_balance
            new_base = self._base_balance
            for fill in order.fills[len(old_fills) :]:
                if (
                    not fill.quantity.is_finite()
                    or fill.quantity <= 0
                    or not fill.price.is_finite()
                    or fill.price <= 0
                ):
                    raise ValueError("Mock venue received an invalid fill.")
                notional = fill.quantity * fill.price
                if order.request.side is Side.BUY:
                    if notional > new_quote:
                        raise ValueError("Mock venue quote balance invariant failed.")
                    new_quote -= notional
                    new_base += fill.quantity
                else:
                    if fill.quantity > new_base:
                        raise ValueError("Mock venue base balance invariant failed.")
                    new_quote += notional
                    new_base -= fill.quantity

            mirrored = MockVenueOrder(
                order_id=order.order_id,
                client_order_id=order.request.client_order_id,
                request=order.request,
                requested_quantity=order.requested_quantity,
                status=order.status,
                filled_quantity=order.filled_quantity,
                fills=tuple(order.fills),
                reason=order.reason,
            )
            self._quote_balance = new_quote
            self._base_balance = new_base
            self._orders[order.order_id] = mirrored
            self._client_to_order[order.request.client_order_id] = order.order_id
            return mirrored

    def get_order(self, order_reference: str) -> MockVenueOrder | None:
        with self._lock:
            order_id = self._client_to_order.get(order_reference, order_reference)
            return self._orders.get(order_id)

    def get_orders(self) -> tuple[MockVenueOrder, ...]:
        with self._lock:
            return tuple(self._orders.values())

    def get_balances(self, *, captured_at: datetime | None = None) -> BalanceSnapshot:
        timestamp = captured_at if captured_at is not None else self._clock()
        _require_aware(timestamp, "Mock venue balance timestamp")
        with self._lock:
            return BalanceSnapshot(
                captured_at=timestamp,
                base_asset=self._base_asset,
                base_available=self._base_balance,
                quote_asset=self._quote_asset,
                quote_available=self._quote_balance,
            )

    def inject_balance_drift(self, asset: str, delta: Decimal) -> None:
        """Inject a deterministic mock-only mismatch; never targets a real account."""
        if not delta.is_finite():
            raise ValueError("Injected balance drift must be finite.")
        with self._lock:
            normalized = asset.upper()
            if normalized == self._base_asset:
                candidate = self._base_balance + delta
                if candidate < 0:
                    raise ValueError("Injected drift cannot make base balance negative.")
                self._base_balance = candidate
            elif normalized == self._quote_asset:
                candidate = self._quote_balance + delta
                if candidate < 0:
                    raise ValueError("Injected drift cannot make quote balance negative.")
                self._quote_balance = candidate
            else:
                raise ValueError("Drift asset is not part of this mock Spot market.")

    def forget_order(self, order_reference: str) -> None:
        """Simulate a venue lookup that no longer returns an internally recorded order."""
        with self._lock:
            order_id = self._client_to_order.pop(order_reference, order_reference)
            order = self._orders.pop(order_id, None)
            if order is not None:
                self._client_to_order.pop(order.client_order_id, None)


class SpotExecutionService:
    """Connect intent -> RiskEngine/PaperBroker -> fills -> balances -> reconciliation."""

    def __init__(
        self,
        *,
        market: Market,
        base_asset: str,
        quote_asset: str,
        broker: PaperBroker,
        mock_venue: MockSpotExchangeAdapter,
        ledger: ExecutionLedger | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if market.market_type.casefold() != "spot":
            raise ValueError("SpotExecutionService accepts Spot markets only.")
        if not base_asset.strip() or not quote_asset.strip():
            raise ValueError("Base and quote asset names must not be empty.")
        if base_asset.upper() == quote_asset.upper():
            raise ValueError("Base and quote assets must be different.")
        self._market = market
        self._base_asset = base_asset.upper()
        self._quote_asset = quote_asset.upper()
        self._broker = broker
        self._mock_venue = mock_venue
        self.ledger = ledger or ExecutionLedger()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._results: dict[str, SpotExecutionResult] = {}
        self._correlation_by_client_id: dict[str, str] = {}
        self._last_order_fingerprints: dict[str, tuple[object, ...]] = {}
        self._execution_sequence = 0
        self._balance_sequence = 0
        self._error_sequence = 0
        self._reconciliation_sequence = 0

    def execute(self, intent: SpotIntent) -> SpotExecutionResult:
        """Submit one idempotent intent to PaperBroker and immediately reconcile its state."""
        with self._lock:
            existing = self._results.get(intent.correlation_id)
            if existing is not None:
                if existing.intent.request != intent.request:
                    raise ValueError("Correlation ID was reused for a different request.")
                return existing
            if intent.request.market != self._market:
                raise ValueError("Intent market does not match this Spot execution service.")
            bound_correlation = self._correlation_by_client_id.get(
                intent.request.client_order_id
            )
            if bound_correlation is not None and bound_correlation != intent.correlation_id:
                raise ValueError("Client order ID is already bound to another correlation ID.")

            now = self._now()
            self.ledger.record_intent(
                IntentLedgerEntry(
                    event_id=f"{intent.correlation_id}:intent",
                    correlation_id=intent.correlation_id,
                    recorded_at=now,
                    client_order_id=intent.request.client_order_id,
                    market_symbol=intent.request.market.symbol,
                    side=intent.request.side,
                    quantity=intent.request.quantity,
                )
            )
            self._correlation_by_client_id[intent.request.client_order_id] = (
                intent.correlation_id
            )

            try:
                order = self._broker.submit_order(intent.request)
            except Exception:
                self._broker.emergency_halt("spot_execution_exception")
                self._record_error(
                    intent.correlation_id,
                    intent.request.client_order_id,
                    "paper_broker",
                    "spot_execution_exception",
                )
                raise
            try:
                self._mock_venue.observe_order(order)
            except Exception:
                self._broker.emergency_halt("mock_venue_mirror_failure")
                self._record_error(
                    intent.correlation_id,
                    intent.request.client_order_id,
                    "mock_venue",
                    "mock_venue_mirror_failure",
                )
                raise

            risk = self._broker.get_risk_decision(intent.request.client_order_id)
            self._record_risk_decision(intent, risk, order)
            self._record_order_state(intent.correlation_id, order)
            balance = self._internal_balance(self._now())
            reconciliation = self.reconcile_all(correlation_id=intent.correlation_id)
            result = SpotExecutionResult(
                intent=intent,
                order=order,
                risk=risk,
                balance=balance,
                reconciliation=reconciliation,
            )
            self._results[intent.correlation_id] = result
            return result

    def process_market_update(
        self, snapshot: MarketSnapshot
    ) -> tuple[tuple[PaperOrder, ...], ReconciliationReport]:
        """Advance resting paper orders, mirror their state, then reconcile the full ledger."""
        with self._lock:
            try:
                orders = self._broker.process_market_update(snapshot)
            except Exception:
                self._broker.emergency_halt("paper_market_update_exception")
                self._record_error(
                    f"market-tick:{snapshot.observed_at.isoformat()}",
                    "<market-update>",
                    "paper_broker",
                    "paper_market_update_exception",
                )
                raise
            for order in orders:
                try:
                    self._mock_venue.observe_order(order)
                except Exception:
                    self._broker.emergency_halt("mock_venue_mirror_failure")
                    self._record_error(
                        self._correlation_by_client_id.get(
                            order.request.client_order_id,
                            f"orphan:{order.request.client_order_id}",
                        ),
                        order.request.client_order_id,
                        "mock_venue",
                        "mock_venue_mirror_failure",
                    )
                    raise
                correlation_id = self._correlation_by_client_id.get(
                    order.request.client_order_id,
                    f"orphan:{order.request.client_order_id}",
                )
                self._record_order_state(correlation_id, order)
            correlation_id = f"market-tick:{snapshot.observed_at.isoformat()}"
            report = self.reconcile_all(correlation_id=correlation_id)
            return orders, report

    def reconcile_all(self, *, correlation_id: str) -> ReconciliationReport:
        """Compare internal balances and every order with the separate mock venue ledger.

        Call this method from the host's periodic scheduler. This service deliberately
        does not create background threads or silently choose an operating interval.
        """
        if not correlation_id.strip():
            raise ValueError("Reconciliation correlation_id must not be empty.")
        with self._lock:
            checked_at = self._now()
            try:
                internal_balance = self._internal_balance(checked_at)
                venue_balance = self._mock_venue.get_balances(captured_at=checked_at)
                internal_orders = {
                    order.order_id: order for order in self._broker.list_orders()
                }
                venue_orders = {
                    order.order_id: order for order in self._mock_venue.get_orders()
                }
            except Exception:
                self._broker.emergency_halt("paper_reconciliation_read_failure")
                self._record_error(
                    correlation_id,
                    "<reconciliation>",
                    "reconciliation",
                    "paper_reconciliation_read_failure",
                )
                raise
            differences = self._balance_differences(internal_balance, venue_balance)
            for order_id in sorted(internal_orders.keys() - venue_orders.keys()):
                differences.append(f"order_missing_from_mock_venue:{order_id}")
            for order_id in sorted(venue_orders.keys() - internal_orders.keys()):
                differences.append(f"unexpected_mock_venue_order:{order_id}")

            compared_orders = 0
            for order_id in sorted(internal_orders.keys() & venue_orders.keys()):
                local = internal_orders[order_id]
                remote = venue_orders[order_id]
                compared_orders += 1
                if (
                    local.request != remote.request
                    or local.requested_quantity != remote.requested_quantity
                    or local.status is not remote.status
                    or local.filled_quantity != remote.filled_quantity
                ):
                    differences.append(f"order_state_mismatch:{order_id}")
                if local.fills != remote.fills:
                    differences.append(f"order_fill_mismatch:{order_id}")
                if local.status is PaperOrderStatus.UNKNOWN:
                    differences.append(f"unknown_order_outcome:{order_id}")

            normalized = tuple(sorted(set(differences)))
            self._reconciliation_sequence += 1
            report = ReconciliationReport(
                event_id=f"{correlation_id}:reconcile:{self._reconciliation_sequence}",
                correlation_id=correlation_id,
                checked_at=checked_at,
                matched=not normalized,
                differences=normalized,
                compared_orders=compared_orders,
                internal_balance=internal_balance,
                venue_balance=venue_balance,
            )
            self.ledger.record_reconciliation(report)
            if normalized:
                self._broker.emergency_halt("paper_reconciliation_mismatch")
            return report

    def _record_risk_decision(
        self,
        intent: SpotIntent,
        risk: RiskResult | None,
        order: PaperOrder,
    ) -> None:
        now = self._now()
        self.ledger.record_risk_decision(
            RiskLedgerEntry(
                event_id=f"{intent.correlation_id}:risk",
                correlation_id=intent.correlation_id,
                recorded_at=now,
                client_order_id=intent.request.client_order_id,
                action=risk.action if risk is not None else None,
                reason_code=(
                    risk.reason_code
                    if risk is not None
                    else self._reason_code(order.reason, order.status)
                ),
                explanation=(
                    risk.explanation
                    if risk is not None
                    else order.reason or "Risk evaluation was not reached."
                ),
                requested_quantity=intent.request.quantity,
                approved_quantity=risk.approved_quantity if risk is not None else None,
            )
        )

    def _record_order_state(self, correlation_id: str, order: PaperOrder) -> None:
        fingerprint: tuple[object, ...] = (
            order.status,
            order.request.quantity,
            order.requested_quantity,
            order.filled_quantity,
            order.fills,
            order.reason,
        )
        if self._last_order_fingerprints.get(order.request.client_order_id) == fingerprint:
            return
        self._last_order_fingerprints[order.request.client_order_id] = fingerprint
        self._execution_sequence += 1
        now = self._now()
        self.ledger.record_execution(
            ExecutionLedgerEntry(
                event_id=f"{correlation_id}:execution:{self._execution_sequence}",
                correlation_id=correlation_id,
                recorded_at=now,
                client_order_id=order.request.client_order_id,
                paper_order_id=order.order_id,
                status=order.status,
                requested_quantity=order.requested_quantity,
                approved_quantity=order.request.quantity,
                filled_quantity=order.filled_quantity,
                fills=tuple(order.fills),
                reason=order.reason,
            )
        )
        self._balance_sequence += 1
        self.ledger.record_balance(
            BalanceLedgerEntry(
                event_id=f"{correlation_id}:balance:{self._balance_sequence}",
                correlation_id=correlation_id,
                recorded_at=now,
                balance=self._internal_balance(now),
            )
        )

    def _record_error(
        self,
        correlation_id: str,
        client_order_id: str,
        component: str,
        reason_code: str,
    ) -> None:
        self._error_sequence += 1
        self.ledger.record_error(
            ExecutionErrorEntry(
                event_id=f"{correlation_id}:error:{self._error_sequence}",
                correlation_id=correlation_id,
                occurred_at=self._now(),
                client_order_id=client_order_id,
                component=component,
                reason_code=reason_code,
            )
        )

    def _internal_balance(self, captured_at: datetime) -> BalanceSnapshot:
        return BalanceSnapshot(
            captured_at=captured_at,
            base_asset=self._base_asset,
            base_available=self._broker.position_quantity,
            quote_asset=self._quote_asset,
            quote_available=self._broker.cash,
        )

    @staticmethod
    def _balance_differences(
        internal: BalanceSnapshot, venue: BalanceSnapshot
    ) -> list[str]:
        differences: list[str] = []
        if (
            internal.base_asset != venue.base_asset
            or internal.base_available != venue.base_available
        ):
            differences.append(f"balance_mismatch:base:{internal.base_asset}")
        if (
            internal.quote_asset != venue.quote_asset
            or internal.quote_available != venue.quote_available
        ):
            differences.append(f"balance_mismatch:quote:{internal.quote_asset}")
        return differences

    @staticmethod
    def _reason_code(reason: str | None, status: PaperOrderStatus) -> str:
        if reason:
            return reason.split(":", maxsplit=1)[0].strip() or status.value
        return "risk_evaluation_not_reached"

    def _now(self) -> datetime:
        value = self._clock()
        _require_aware(value, "Execution service clock")
        return value


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone.")
