"""Feature-gated Spot execution orchestration with deterministic risk and SQLite idempotency.

This service is never started by import or by CI. Writes require an explicit local flag, a
second owner acknowledgement, provider credentials, reconciled account state, fresh Nobitex
market data/rules, passing Risk Engine output, and a durable pre-submit journal claim.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from evoryn.adapters.exchanges.nobitex_instrument_rules import (
    NobitexSpotInstrumentRulesProvider,
    SpotInstrumentRules,
)
from evoryn.adapters.exchanges.nobitex_transport import NobitexHTTPTransport
from evoryn.adapters.exchanges.nobitex_spot_trading import NobitexSpotTradingTransport
from evoryn.config import (
    MarketPolicy,
    NobitexReadOnlyConfig,
)
from evoryn.domain.models import (
    Market,
    MarketSnapshot,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)
from evoryn.ports.exchange_errors import (
    AuthenticationError,
    AuthorizationError,
    ExchangeTransportError,
    InvalidExchangeRequest,
    OrderOutcomeUnknown,
    RateLimitExceeded,
    TradingDisabledError,
)
from evoryn.risk.engine import RiskEngine
from evoryn.risk.models import (
    PortfolioSnapshot,
    RiskAction,
    RiskResult,
)


_LIVE_ACKNOWLEDGEMENT = "I_UNDERSTAND_ORDERS_ARE_REAL"
_HARD_MAX_MARKET_SLIPPAGE_BPS = Decimal("100")
_RULES_MAX_AGE_SECONDS = 300.0
_BLOCKING_STATES = frozenset(
    {
        "submitting",
        "unknown",
        "cancel_submitting",
        OrderStatus.PENDING.value,
        OrderStatus.OPEN.value,
        OrderStatus.PARTIALLY_FILLED.value,
    }
)


def _env_true(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class LiveSpotExecutionSettings:
    """Explicit local-only order-write settings; secrets never appear in repr."""

    order_writes_enabled: bool = False
    api_key: str | None = field(default=None, repr=False)
    private_key: str | None = field(default=None, repr=False)
    owner_acknowledgement: str | None = field(default=None, repr=False)
    max_market_slippage_bps: Decimal = Decimal("50")

    def __post_init__(self) -> None:
        if type(self.order_writes_enabled) is not bool:
            raise ValueError("order_writes_enabled must be an explicit boolean.")
        if (
            not self.max_market_slippage_bps.is_finite()
            or self.max_market_slippage_bps <= 0
            or self.max_market_slippage_bps > _HARD_MAX_MARKET_SLIPPAGE_BPS
        ):
            raise ValueError("Market slippage must be positive and no greater than 100 bps.")
        if self.order_writes_enabled:
            if self.owner_acknowledgement != _LIVE_ACKNOWLEDGEMENT:
                raise TradingDisabledError(
                    "Enabling Spot writes requires the explicit local owner acknowledgement."
                )
            if not self.api_key or not self.private_key:
                raise AuthenticationError(
                    "Nobitex Spot write credentials must be supplied through the local secret store."
                )

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> LiveSpotExecutionSettings:
        """Parse the local-only flag; CI cannot enable write-capable runtime configuration."""
        enabled = _env_true(values.get("EVORYN_SPOT_ORDER_WRITES_ENABLED"))
        if enabled and (
            _env_true(values.get("CI"))
            or _env_true(values.get("GITHUB_ACTIONS"))
            or (values.get("EVORYN_ENV", "").strip().casefold() in {"ci", "test"})
        ):
            raise TradingDisabledError("Spot order writes are forbidden in CI and test environments.")
        raw_slippage = values.get("EVORYN_MAX_MARKET_SLIPPAGE_BPS", "50")
        try:
            slippage = Decimal(raw_slippage.strip())
        except (AttributeError, InvalidOperation, ValueError) as exc:
            raise ValueError("EVORYN_MAX_MARKET_SLIPPAGE_BPS must be decimal.") from exc
        if not enabled:
            # Do not even retain unused secrets when the write gate is off.
            return cls(
                order_writes_enabled=False,
                max_market_slippage_bps=slippage,
            )
        return cls(
            order_writes_enabled=True,
            api_key=values.get("NOBITEX_SPOT_API_KEY"),
            private_key=values.get("NOBITEX_SPOT_PRIVATE_KEY"),
            owner_acknowledgement=values.get("EVORYN_LIVE_TRADING_ACK"),
            max_market_slippage_bps=slippage,
        )

    @classmethod
    def from_environment(cls) -> LiveSpotExecutionSettings:
        """Read settings from the local process environment without network I/O."""
        return cls.from_mapping(os.environ)


class SpotTradingPort(Protocol):
    """Narrow write gateway used by the execution service and replaced by mocks in tests."""

    def submit_order(self, request: OrderRequest, *, price_guard: Decimal) -> Order:
        """Submit one already-authorized order exactly once."""
        ...

    def get_order_status(
        self,
        *,
        requested_market: Market,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> Order | None:
        """Query one order without submitting a write."""
        ...

    def cancel_order(self, *, requested_market: Market, known_order: Order) -> Order:
        """Cancel one journaled nonterminal order exactly once."""
        ...


class SpotRulesPort(Protocol):
    """Instrument-rule lookup that fails closed when official rules are unavailable."""

    def get_rules(self, market: Market) -> SpotInstrumentRules:
        """Return current official amount step, price tick and USDT minimum."""
        ...


class ExecutionDisposition(StrEnum):
    SUBMITTED = "submitted"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    RECONCILED = "reconciled"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class LiveSpotExecutionResult:
    """Auditable application result; it never implies that a risk result is live permission."""

    disposition: ExecutionDisposition
    reason_code: str
    order: Order | None = None
    risk: RiskResult | None = None
    reconciliation_required: bool = False


@dataclass(frozen=True, slots=True)
class JournalRecord:
    """Non-secret durable order state needed for idempotency and recovery."""

    client_order_id: str
    market: Market
    side: Side
    order_type: OrderType
    quantity: Decimal
    requested_price: Decimal | None
    price_guard: Decimal
    state: str
    exchange_order_id: str | None
    exchange_status: OrderStatus | None
    exchange_price: Decimal | None
    created_at: datetime
    updated_at: datetime
    reason_code: str | None

    def to_order(self) -> Order:
        if not self.exchange_order_id or self.exchange_status is None:
            raise OrderOutcomeUnknown("Journal has no confirmed exchange order identity.")
        return Order(
            order_id=self.exchange_order_id,
            market_symbol=self.market.symbol,
            side=self.side,
            quantity=self.quantity,
            status=self.exchange_status,
            price=self.exchange_price,
            client_order_id=self.client_order_id,
        )


class SQLiteExecutionJournal:
    """SQLite WAL journal with unique client IDs and a single-active-order safety baseline."""

    def __init__(self, path: str | Path = "data/execution.sqlite3") -> None:
        rendered_path = str(path)
        if rendered_path != ":memory:":
            Path(rendered_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            rendered_path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if rendered_path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS live_spot_orders (
                client_order_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                venue TEXT NOT NULL,
                market_type TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                quantity TEXT NOT NULL,
                requested_price TEXT,
                price_guard TEXT NOT NULL,
                state TEXT NOT NULL,
                exchange_order_id TEXT UNIQUE,
                exchange_status TEXT,
                exchange_price TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reason_code TEXT
            )
            """
        )

    def close(self) -> None:
        """Close the SQLite connection explicitly at process shutdown."""
        with self._lock:
            self._connection.close()

    def claim(self, request: OrderRequest, *, price_guard: Decimal, now: datetime) -> None:
        """Durably claim one unique client ID before any order request leaves the process."""
        _require_aware(now)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                active = self._connection.execute(
                    "SELECT client_order_id FROM live_spot_orders "
                    "WHERE state IN ({}) LIMIT 1".format(
                        ",".join("?" for _ in _BLOCKING_STATES)
                    ),
                    tuple(sorted(_BLOCKING_STATES)),
                ).fetchone()
                if active is not None:
                    raise OrderOutcomeUnknown(
                        "A prior order is active or unresolved; reconcile before another submit."
                    )
                self._connection.execute(
                    """
                    INSERT INTO live_spot_orders (
                        client_order_id, symbol, venue, market_type, side, order_type,
                        quantity, requested_price, price_guard, state, exchange_order_id,
                        exchange_status, exchange_price, created_at, updated_at, reason_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitting', NULL, NULL, NULL, ?, ?, NULL)
                    """,
                    (
                        request.client_order_id,
                        request.market.symbol,
                        request.market.venue,
                        request.market.market_type,
                        request.side.value,
                        request.order_type.value,
                        format(request.quantity, "f"),
                        (
                            format(request.price, "f")
                            if request.price is not None
                            else None
                        ),
                        format(price_guard, "f"),
                        now.astimezone(UTC).isoformat(),
                        now.astimezone(UTC).isoformat(),
                    ),
                )
                self._connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._connection.execute("ROLLBACK")
                raise InvalidExchangeRequest(
                    "client_order_id was already used; durable idempotency forbids reuse."
                ) from exc
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def get(self, client_order_id: str) -> JournalRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM live_spot_orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return None if row is None else self._to_record(row)

    def update(
        self,
        client_order_id: str,
        *,
        state: str,
        now: datetime,
        order: Order | None = None,
        reason_code: str | None = None,
    ) -> None:
        """Update state with sanitized codes; never store request/response bodies or secrets."""
        _require_aware(now)
        if reason_code is not None and (
            not reason_code
            or len(reason_code) > 80
            or not all(char.isalnum() or char in "_-." for char in reason_code)
        ):
            raise ValueError("Journal reason_code must be a short safe identifier.")
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE live_spot_orders
                SET state = ?,
                    exchange_order_id = COALESCE(?, exchange_order_id),
                    exchange_status = COALESCE(?, exchange_status),
                    exchange_price = COALESCE(?, exchange_price),
                    updated_at = ?,
                    reason_code = ?
                WHERE client_order_id = ?
                """,
                (
                    state,
                    order.order_id if order is not None else None,
                    order.status.value if order is not None else None,
                    (
                        format(order.price, "f")
                        if order is not None and order.price is not None
                        else None
                    ),
                    now.astimezone(UTC).isoformat(),
                    reason_code,
                    client_order_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError("Journal order was not found for state update.")

    def blocking_order(self) -> JournalRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM live_spot_orders WHERE state IN ({}) "
                "ORDER BY created_at LIMIT 1".format(
                    ",".join("?" for _ in _BLOCKING_STATES)
                ),
                tuple(sorted(_BLOCKING_STATES)),
            ).fetchone()
        return None if row is None else self._to_record(row)

    @staticmethod
    def _to_record(row: sqlite3.Row) -> JournalRecord:
        return JournalRecord(
            client_order_id=str(row["client_order_id"]),
            market=Market(
                symbol=str(row["symbol"]),
                venue=str(row["venue"]),
                market_type=str(row["market_type"]),
            ),
            side=Side(str(row["side"])),
            order_type=OrderType(str(row["order_type"])),
            quantity=Decimal(str(row["quantity"])),
            requested_price=(
                Decimal(str(row["requested_price"]))
                if row["requested_price"] is not None
                else None
            ),
            price_guard=Decimal(str(row["price_guard"])),
            state=str(row["state"]),
            exchange_order_id=(
                str(row["exchange_order_id"])
                if row["exchange_order_id"] is not None
                else None
            ),
            exchange_status=(
                OrderStatus(str(row["exchange_status"]))
                if row["exchange_status"] is not None
                else None
            ),
            exchange_price=(
                Decimal(str(row["exchange_price"]))
                if row["exchange_price"] is not None
                else None
            ),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            reason_code=(
                str(row["reason_code"]) if row["reason_code"] is not None else None
            ),
        )


@dataclass(slots=True)
class LiveSpotExecutionService:
    """Gate each Spot order through config, current venue rules, account state, and Risk Engine."""

    settings: LiveSpotExecutionSettings
    risk_engine: RiskEngine
    journal: SQLiteExecutionJournal
    rules_provider: SpotRulesPort
    transport: SpotTradingPort | None
    market_policy: MarketPolicy = field(default_factory=MarketPolicy)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)

    def __post_init__(self) -> None:
        if self.settings.order_writes_enabled and self.transport is None:
            raise TradingDisabledError("Enabled Spot writes require an explicit trading transport.")

    def submit_order(
        self,
        request: OrderRequest,
        portfolio: PortfolioSnapshot,
        market_data: MarketSnapshot,
        *,
        now: datetime,
    ) -> LiveSpotExecutionResult:
        """Risk-check and submit once. Ambiguous submission is journaled and never retried."""
        self._require_enabled()
        if not isinstance(request, OrderRequest):
            raise InvalidExchangeRequest("Live Spot execution requires a validated OrderRequest.")
        if self.risk_engine.is_halted:
            return LiveSpotExecutionResult(
                ExecutionDisposition.BLOCKED,
                "risk_halt_latched",
            )
        if portfolio.account_reconciled is not True or portfolio.execution_error is not False:
            self.risk_engine.emergency_halt("account_state_unverified")
            return LiveSpotExecutionResult(
                ExecutionDisposition.BLOCKED,
                "account_state_unverified",
            )
        if market_data.market != request.market or "nobitex" not in market_data.source.casefold():
            return LiveSpotExecutionResult(
                ExecutionDisposition.REJECTED,
                "market_data_identity_or_source_invalid",
            )
        if self.market_policy.rejection_reason(request.market) is not None:
            return LiveSpotExecutionResult(
                ExecutionDisposition.REJECTED,
                "market_not_allowed",
            )
        _require_aware(now)

        rules = self.rules_provider.get_rules(request.market)
        rules_age = (now.astimezone(UTC) - rules.observed_at.astimezone(UTC)).total_seconds()
        if rules_age < 0 or rules_age > _RULES_MAX_AGE_SECONDS:
            return LiveSpotExecutionResult(
                ExecutionDisposition.REJECTED,
                "instrument_rules_stale",
            )
        base, quote = split_usdt_symbol_for_rules(request.market, self.market_policy)
        if rules.symbol != f"{base}{quote}".upper():
            return LiveSpotExecutionResult(
                ExecutionDisposition.REJECTED,
                "instrument_rules_symbol_mismatch",
            )

        try:
            price_guard = self._price_guard(request, market_data, rules)
            if request.quantity % rules.amount_step != 0:
                return LiveSpotExecutionResult(
                    ExecutionDisposition.REJECTED,
                    "amount_precision_invalid",
                )
            if request.order_type is OrderType.LIMIT:
                assert request.price is not None
                if request.price % rules.price_tick != 0:
                    return LiveSpotExecutionResult(
                        ExecutionDisposition.REJECTED,
                        "price_precision_invalid",
                    )
            guarded_values = dict(market_data.values)
            guarded_values["ask" if request.side is Side.BUY else "bid"] = price_guard
            guarded_market_data = replace(market_data, values=guarded_values)
            risk = self.risk_engine.evaluate(
                request,
                portfolio,
                guarded_market_data,
                now=now,
            )
            if risk.action not in {RiskAction.ALLOW, RiskAction.REDUCE}:
                return LiveSpotExecutionResult(
                    ExecutionDisposition.BLOCKED,
                    risk.reason_code,
                    risk=risk,
                    reconciliation_required=risk.action is RiskAction.HALT,
                )
            approved_quantity = risk.approved_quantity
            if approved_quantity is None or not approved_quantity.is_finite():
                self.risk_engine.emergency_halt("risk_quantity_missing")
                return LiveSpotExecutionResult(
                    ExecutionDisposition.BLOCKED,
                    "risk_quantity_missing",
                    risk=risk,
                )
            approved_quantity = (
                (approved_quantity / rules.amount_step).to_integral_value(rounding=ROUND_DOWN)
                * rules.amount_step
            )
            if approved_quantity <= 0 or approved_quantity > request.quantity:
                return LiveSpotExecutionResult(
                    ExecutionDisposition.REJECTED,
                    "risk_quantity_not_executable",
                    risk=risk,
                )
            if approved_quantity * price_guard < rules.min_notional_usdt:
                return LiveSpotExecutionResult(
                    ExecutionDisposition.REJECTED,
                    "minimum_order_notional",
                    risk=risk,
                )
            approved_request = replace(request, quantity=approved_quantity)
        except InvalidExchangeRequest as exc:
            return LiveSpotExecutionResult(
                ExecutionDisposition.REJECTED,
                _safe_reason(type(exc).__name__),
            )

        blocking_order = self.journal.blocking_order()
        if blocking_order is not None:
            known_active = blocking_order.state in {
                OrderStatus.PENDING.value,
                OrderStatus.OPEN.value,
                OrderStatus.PARTIALLY_FILLED.value,
            }
            if not known_active:
                self.risk_engine.emergency_halt("order_outcome_unknown")
            return LiveSpotExecutionResult(
                ExecutionDisposition.BLOCKED if known_active else ExecutionDisposition.UNKNOWN,
                "prior_order_still_active" if known_active else "prior_order_requires_reconciliation",
                reconciliation_required=not known_active,
            )

        claimed_at = self.clock()
        _require_aware(claimed_at)
        try:
            self.journal.claim(approved_request, price_guard=price_guard, now=claimed_at)
        except OrderOutcomeUnknown:
            return LiveSpotExecutionResult(
                ExecutionDisposition.UNKNOWN,
                "prior_order_requires_reconciliation",
                risk=risk,
                reconciliation_required=True,
            )
        except InvalidExchangeRequest:
            return LiveSpotExecutionResult(
                ExecutionDisposition.REJECTED,
                "client_order_id_reuse_or_journal_conflict",
                risk=risk,
            )
        assert self.transport is not None
        try:
            order = self.transport.submit_order(approved_request, price_guard=price_guard)
        except (OrderOutcomeUnknown, ExchangeTransportError) as exc:
            self._mark_unknown(approved_request.client_order_id, type(exc).__name__)
            return LiveSpotExecutionResult(
                ExecutionDisposition.UNKNOWN,
                "order_outcome_unknown",
                risk=risk,
                reconciliation_required=True,
            )
        except (AuthenticationError, AuthorizationError, InvalidExchangeRequest, RateLimitExceeded) as exc:
            self.journal.update(
                approved_request.client_order_id,
                state=OrderStatus.REJECTED.value,
                now=self.clock(),
                reason_code=_safe_reason(type(exc).__name__),
            )
            return LiveSpotExecutionResult(
                ExecutionDisposition.REJECTED,
                _safe_reason(type(exc).__name__),
                risk=risk,
            )
        except Exception as exc:
            # A write-path exception not explicitly classified is treated as ambiguous.
            self._mark_unknown(approved_request.client_order_id, type(exc).__name__)
            return LiveSpotExecutionResult(
                ExecutionDisposition.UNKNOWN,
                "order_outcome_unknown",
                risk=risk,
                reconciliation_required=True,
            )

        try:
            self.journal.update(
                approved_request.client_order_id,
                state=order.status.value,
                now=self.clock(),
                order=order,
                reason_code=None,
            )
        except Exception:
            self.risk_engine.emergency_halt("journal_ack_persist_failed")
            # The row remains SUBMITTING or UNKNOWN and will block new entries after restart.
            return LiveSpotExecutionResult(
                ExecutionDisposition.UNKNOWN,
                "order_ack_journal_persist_failed",
                order=order,
                risk=risk,
                reconciliation_required=True,
            )
        if order.status is OrderStatus.UNKNOWN:
            self._mark_unknown(approved_request.client_order_id, "unmapped_order_status")
            return LiveSpotExecutionResult(
                ExecutionDisposition.UNKNOWN,
                "unmapped_order_status",
                order=order,
                risk=risk,
                reconciliation_required=True,
            )
        if order.status is OrderStatus.REJECTED:
            return LiveSpotExecutionResult(
                ExecutionDisposition.REJECTED,
                "exchange_order_rejected",
                order=order,
                risk=risk,
            )
        return LiveSpotExecutionResult(
            ExecutionDisposition.SUBMITTED,
            "order_acknowledged",
            order=order,
            risk=risk,
        )

    def reconcile(self, client_order_id: str) -> LiveSpotExecutionResult:
        """Query one journaled client ID once; not-found never authorizes resubmission."""
        record = self.journal.get(client_order_id)
        if record is None:
            return LiveSpotExecutionResult(
                ExecutionDisposition.REJECTED,
                "journal_order_not_found",
            )
        if record.state not in {"submitting", "unknown", "cancel_submitting"}:
            order = record.to_order() if record.exchange_order_id and record.exchange_status else None
            return LiveSpotExecutionResult(
                ExecutionDisposition.RECONCILED,
                "journal_state_already_known",
                order=order,
                reconciliation_required=False,
            )
        if self.transport is None:
            return LiveSpotExecutionResult(
                ExecutionDisposition.UNKNOWN,
                "trading_transport_unavailable",
                reconciliation_required=True,
            )
        try:
            order = self.transport.get_order_status(
                requested_market=record.market,
                client_order_id=record.client_order_id,
            )
        except Exception as exc:
            self.risk_engine.emergency_halt("reconciliation_query_failed")
            return LiveSpotExecutionResult(
                ExecutionDisposition.UNKNOWN,
                _safe_reason(type(exc).__name__),
                reconciliation_required=True,
            )
        if order is None:
            # Nobitex clientOrderId lookup may omit closed orders; absence is not proof of no order.
            self.risk_engine.emergency_halt("order_reconciliation_unresolved")
            self.journal.update(
                client_order_id,
                state="unknown",
                now=self.clock(),
                reason_code="order_not_found_requires_manual_review",
            )
            return LiveSpotExecutionResult(
                ExecutionDisposition.UNKNOWN,
                "order_not_found_requires_manual_review",
                reconciliation_required=True,
            )
        self.journal.update(
            client_order_id,
            state=order.status.value,
            now=self.clock(),
            order=order,
            reason_code=None,
        )
        if order.status is OrderStatus.UNKNOWN:
            self.risk_engine.emergency_halt("reconciliation_status_unknown")
            return LiveSpotExecutionResult(
                ExecutionDisposition.UNKNOWN,
                "reconciliation_status_unknown",
                order=order,
                reconciliation_required=True,
            )
        return LiveSpotExecutionResult(
            ExecutionDisposition.RECONCILED,
            "order_reconciled",
            order=order,
            reconciliation_required=False,
        )

    def cancel_order(self, client_order_id: str) -> LiveSpotExecutionResult:
        """Cancel only one journaled, acknowledged, nonterminal order through the same gate."""
        self._require_enabled()
        record = self.journal.get(client_order_id)
        if record is None or record.state not in {
            OrderStatus.PENDING.value,
            OrderStatus.OPEN.value,
            OrderStatus.PARTIALLY_FILLED.value,
        }:
            return LiveSpotExecutionResult(
                ExecutionDisposition.BLOCKED,
                "cancel_requires_journaled_active_order",
                reconciliation_required=record is not None,
            )
        try:
            known_order = record.to_order()
        except OrderOutcomeUnknown:
            return LiveSpotExecutionResult(
                ExecutionDisposition.UNKNOWN,
                "cancel_order_identity_unknown",
                reconciliation_required=True,
            )
        now = self.clock()
        self.journal.update(
            client_order_id,
            state="cancel_submitting",
            now=now,
            reason_code=None,
        )
        assert self.transport is not None
        try:
            cancelled = self.transport.cancel_order(
                requested_market=record.market,
                known_order=known_order,
            )
        except OrderOutcomeUnknown:
            self._mark_unknown(client_order_id, "cancel_outcome_unknown")
            return LiveSpotExecutionResult(
                ExecutionDisposition.UNKNOWN,
                "cancel_outcome_unknown",
                order=known_order,
                reconciliation_required=True,
            )
        except (AuthenticationError, AuthorizationError, InvalidExchangeRequest, RateLimitExceeded) as exc:
            # A conclusive rejection preserves the previously acknowledged order state.
            self.journal.update(
                client_order_id,
                state=known_order.status.value,
                now=self.clock(),
                reason_code=_safe_reason(type(exc).__name__),
            )
            return LiveSpotExecutionResult(
                ExecutionDisposition.REJECTED,
                _safe_reason(type(exc).__name__),
                order=known_order,
            )
        except Exception:
            self._mark_unknown(client_order_id, "cancel_outcome_unknown")
            return LiveSpotExecutionResult(
                ExecutionDisposition.UNKNOWN,
                "cancel_outcome_unknown",
                order=known_order,
                reconciliation_required=True,
            )
        self.journal.update(
            client_order_id,
            state=OrderStatus.CANCELLED.value,
            now=self.clock(),
            order=cancelled,
            reason_code=None,
        )
        return LiveSpotExecutionResult(
            ExecutionDisposition.CANCELLED,
            "cancel_acknowledged",
            order=cancelled,
        )

    def _price_guard(
        self,
        request: OrderRequest,
        market_data: MarketSnapshot,
        rules: SpotInstrumentRules,
    ) -> Decimal:
        if request.order_type is OrderType.LIMIT:
            if request.price is None:
                raise InvalidExchangeRequest("Limit order price is required.")
            if request.price % rules.price_tick != 0:
                raise InvalidExchangeRequest("Limit price does not match the official price tick.")
            return request.price

        key = "ask" if request.side is Side.BUY else "bid"
        reference = market_data.values.get(key)
        if not isinstance(reference, Decimal) or not reference.is_finite() or reference <= 0:
            raise InvalidExchangeRequest("Fresh top-of-book price is required for a guarded market order.")
        slippage_ratio = self.settings.max_market_slippage_bps / Decimal("10000")
        unrounded = (
            reference * (Decimal("1") + slippage_ratio)
            if request.side is Side.BUY
            else reference * (Decimal("1") - slippage_ratio)
        )
        rounding = ROUND_CEILING if request.side is Side.BUY else ROUND_FLOOR
        guard = (unrounded / rules.price_tick).to_integral_value(rounding=rounding) * rules.price_tick
        if guard <= 0:
            raise InvalidExchangeRequest("Market price guard is not positive.")
        actual_bps = (
            (guard / reference - Decimal("1")) * Decimal("10000")
            if request.side is Side.BUY
            else (Decimal("1") - guard / reference) * Decimal("10000")
        )
        if actual_bps > self.settings.max_market_slippage_bps:
            raise InvalidExchangeRequest("Rounded market price guard exceeds the slippage policy.")
        return guard

    def _require_enabled(self) -> None:
        if not self.settings.order_writes_enabled:
            raise TradingDisabledError(
                "Spot order writes are disabled. Set the local feature flag and explicit owner acknowledgement."
            )
        if self.transport is None:
            raise TradingDisabledError("No Spot trading transport is configured.")

    def _mark_unknown(self, client_order_id: str, reason_code: str) -> None:
        try:
            self.journal.update(
                client_order_id,
                state="unknown",
                now=self.clock(),
                reason_code=_safe_reason(reason_code),
            )
        finally:
            self.risk_engine.emergency_halt("order_outcome_unknown")


def split_usdt_symbol_for_rules(market: Market, policy: MarketPolicy) -> tuple[str, str]:
    """Return lower-case base and quote code after applying the central market whitelist."""
    if policy.rejection_reason(market) is not None:
        raise InvalidExchangeRequest("Only Nobitex Spot/USDT is enabled.")
    symbol = market.symbol.strip().upper()
    for separator in ("/", "-", "_"):
        if separator in symbol:
            parts = symbol.split(separator)
            if len(parts) == 2 and parts[0].isalnum() and parts[1] == "USDT":
                return parts[0].lower(), "usdt"
            raise InvalidExchangeRequest("Market symbol must be one BASE/USDT pair.")
    if symbol.endswith("USDT") and symbol[:-4].isalnum():
        return symbol[:-4].lower(), "usdt"
    raise InvalidExchangeRequest("Market symbol is not a recognized BASE/USDT pair.")


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Execution timestamps must be timezone-aware.")


def _safe_reason(value: str) -> str:
    """Convert an exception class or fixed reason into a safe audit identifier."""
    normalized = "".join(
        char for char in value.casefold() if char.isalnum() or char in "_-."
    )
    return normalized[:80] or "execution_error"


def build_live_spot_execution_service(
    *,
    risk_engine: RiskEngine,
    journal: SQLiteExecutionJournal,
    environment: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> LiveSpotExecutionService:
    """Create the local composition root without sending any request or starting a worker."""
    values = environment if environment is not None else os.environ
    settings = LiveSpotExecutionSettings.from_mapping(values)
    read_config = NobitexReadOnlyConfig.from_mapping(values)
    public_transport = NobitexHTTPTransport(
        base_url=read_config.base_url,
        token=None,
        timeout_seconds=read_config.timeout_seconds,
    )
    rules_provider = NobitexSpotInstrumentRulesProvider(
        transport=public_transport,
        market_policy=read_config.market_policy,
        clock=clock,
    )
    trading_transport = (
        NobitexSpotTradingTransport(
            api_key=settings.api_key or "",
            private_key=settings.private_key or "",
            base_url=read_config.base_url,
            timeout_seconds=read_config.timeout_seconds,
            clock=clock,
            market_policy=read_config.market_policy,
        )
        if settings.order_writes_enabled
        else None
    )
    return LiveSpotExecutionService(
        settings=settings,
        risk_engine=risk_engine,
        journal=journal,
        rules_provider=rules_provider,
        transport=trading_transport,
        market_policy=read_config.market_policy,
        clock=clock,
    )
