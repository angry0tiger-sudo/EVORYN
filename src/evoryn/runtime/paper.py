"""Restart-aware end-to-end Paper runtime. This module has no live exchange transport."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Protocol, cast

from evoryn.domain.models import (
    Fill,
    Market,
    MarketCandle,
    MarketSnapshot,
    OrderRequest,
    OrderType,
    Side,
)
from evoryn.execution.spot import MockSpotExchangeAdapter, SpotExecutionService, SpotIntent
from evoryn.intelligence.regime import RegimeLabel, RegimePrediction
from evoryn.paper.broker import PaperBroker, PaperBrokerState, PaperOrder, PaperOrderStatus
from evoryn.ports.contracts import MarketDataPort
from evoryn.risk.engine import RiskEngine
from evoryn.risk.models import PortfolioSnapshot, RiskLimits
from evoryn.strategies.plugins import StrategyPluginRegistry
from evoryn.strategies.selector import (
    RegimeStrategySelector,
    StrategySelectionDecision,
    StrategySelectionPolicy,
)

ZERO = Decimal("0")
ONE = Decimal("1")
SEED_HASH = "0" * 64
ACTIVE_STATUSES = frozenset({PaperOrderStatus.OPEN, PaperOrderStatus.PARTIAL})
COMMITTED_EVENTS = frozenset({"BOOTSTRAP_COMMITTED", "CYCLE_COMMITTED"})


class RuntimeLedgerIntegrityError(RuntimeError):
    """Raised if persisted runtime evidence is unreadable or fails its hash chain."""


class RuntimeStatus(StrEnum):
    BOOTSTRAPPED = "bootstrapped"
    PROCESSED = "processed"
    NO_NEW_CANDLE = "no_new_candle"
    ABSTAINED = "abstained"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class RuntimeLedgerEvent:
    sequence: int
    event_type: str
    correlation_id: str
    occurred_at: datetime
    payload: dict[str, object]
    previous_hash: str
    event_hash: str


class SQLitePaperRuntimeLedger:
    """Transactional SQLite event journal with sequence and SHA-256 chain validation."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_runtime_events (
                    sequence INTEGER PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _hash(
        sequence: int,
        event_type: str,
        correlation_id: str,
        occurred_at: str,
        payload_json: str,
        previous_hash: str,
    ) -> str:
        material = json.dumps(
            [
                sequence,
                event_type,
                correlation_id,
                occurred_at,
                payload_json,
                previous_hash,
            ],
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def append(
        self,
        event_type: str,
        *,
        correlation_id: str,
        occurred_at: datetime,
        payload: Mapping[str, object],
    ) -> RuntimeLedgerEvent:
        """Commit an event transactionally before continuing the operation."""
        _require_utc(occurred_at, "ledger timestamp")
        if not event_type.strip() or not correlation_id.strip():
            raise ValueError("Ledger event type and correlation ID are required.")
        payload_json = json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        timestamp = occurred_at.astimezone(UTC).isoformat()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                prior = connection.execute(
                    "SELECT sequence, event_hash FROM paper_runtime_events "
                    "ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                sequence = int(prior["sequence"]) + 1 if prior is not None else 1
                previous_hash = str(prior["event_hash"]) if prior is not None else SEED_HASH
                digest = self._hash(
                    sequence,
                    event_type,
                    correlation_id,
                    timestamp,
                    payload_json,
                    previous_hash,
                )
                connection.execute(
                    "INSERT INTO paper_runtime_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        sequence,
                        event_type,
                        correlation_id,
                        timestamp,
                        payload_json,
                        previous_hash,
                        digest,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        return RuntimeLedgerEvent(
            sequence,
            event_type,
            correlation_id,
            occurred_at.astimezone(UTC),
            dict(payload),
            previous_hash,
            digest,
        )

    def events(self) -> tuple[RuntimeLedgerEvent, ...]:
        """Read all events, rejecting sequence gaps, malformed rows, or chain corruption."""
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM paper_runtime_events ORDER BY sequence"
            ).fetchall()
        finally:
            connection.close()

        result: list[RuntimeLedgerEvent] = []
        previous_hash = SEED_HASH
        for expected_sequence, row in enumerate(rows, start=1):
            try:
                sequence = int(row["sequence"])
                event_type = str(row["event_type"])
                correlation_id = str(row["correlation_id"])
                timestamp_text = str(row["occurred_at"])
                payload_json = str(row["payload_json"])
                stored_previous = str(row["previous_hash"])
                stored_hash = str(row["event_hash"])
                payload_value = json.loads(payload_json)
                timestamp = datetime.fromisoformat(timestamp_text)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeLedgerIntegrityError("Runtime event cannot be decoded.") from exc
            if not isinstance(payload_value, dict):
                raise RuntimeLedgerIntegrityError("Runtime event payload must be an object.")
            _require_utc(timestamp, "stored event timestamp")
            actual_hash = self._hash(
                sequence,
                event_type,
                correlation_id,
                timestamp_text,
                payload_json,
                previous_hash,
            )
            if (
                sequence != expected_sequence
                or stored_previous != previous_hash
                or stored_hash != actual_hash
            ):
                raise RuntimeLedgerIntegrityError("Runtime ledger sequence/hash-chain mismatch.")
            result.append(
                RuntimeLedgerEvent(
                    sequence,
                    event_type,
                    correlation_id,
                    timestamp.astimezone(UTC),
                    cast(dict[str, object], payload_value),
                    stored_previous,
                    stored_hash,
                )
            )
            previous_hash = stored_hash
        return tuple(result)

    def verify(self) -> bool:
        self.events()
        return True


class PaperRuntimeFeed(MarketDataPort, Protocol):
    """Mock-only feed contract. Live providers must never claim simulation_only=True."""

    simulation_only: bool

    def get_completed_candles(
        self,
        market: Market,
        interval: timedelta,
        *,
        now: datetime,
    ) -> Sequence[MarketCandle]:
        """Return completed bars in strict chronological order."""
        ...


class RegimeClassifier(Protocol):
    """Advisory classifier boundary shared by the live-independent runtime."""

    def classify(
        self,
        completed_history: Sequence[MarketCandle],
        *,
        now: datetime,
    ) -> RegimePrediction:
        ...


@dataclass(frozen=True, slots=True)
class AccountPortfolioState:
    """Strategy-visible balances, marked exposures, and active/unresolved order identifiers."""

    market: Market
    as_of: datetime
    base_asset: str
    quote_asset: str
    quote_available: Decimal
    base_quantity: Decimal
    reference_price: Decimal
    equity: Decimal
    portfolio: PortfolioSnapshot
    active_order_ids: tuple[str, ...]
    unresolved_order_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_aware(self.as_of, "portfolio timestamp")
        for name in ("quote_available", "base_quantity", "reference_price", "equity"):
            value = getattr(self, name)
            if not value.is_finite() or value < ZERO:
                raise ValueError(f"{name} must be finite and non-negative.")


class AccountPortfolioBuilder:
    """Build a same-cycle account view from canonical PaperBroker state."""

    def __init__(
        self,
        broker: PaperBroker,
        market: Market,
        base_asset: str,
        quote_asset: str,
    ) -> None:
        if market.market_type.casefold() != "spot":
            raise ValueError("Paper portfolio builder accepts Spot only.")
        if (
            not base_asset.strip()
            or not quote_asset.strip()
            or base_asset.upper() == quote_asset.upper()
        ):
            raise ValueError("Distinct non-empty base and quote assets are required.")
        self._broker = broker
        self._market = market
        self._base_asset = base_asset.upper()
        self._quote_asset = quote_asset.upper()

    def build(
        self,
        snapshot: MarketSnapshot,
        *,
        reconciled: bool,
    ) -> AccountPortfolioState:
        if snapshot.market != self._market:
            raise ValueError("Portfolio snapshot market mismatch.")
        portfolio = replace(
            self._broker.portfolio_snapshot(snapshot),
            account_reconciled=reconciled,
        )
        mid = (snapshot.values["bid"] + snapshot.values["ask"]) / Decimal("2")
        orders = self._broker.list_orders()
        active = tuple(order.order_id for order in orders if order.status in ACTIVE_STATUSES)
        unresolved = tuple(
            order.order_id for order in orders if order.status is PaperOrderStatus.UNKNOWN
        )
        return AccountPortfolioState(
            market=self._market,
            as_of=snapshot.observed_at.astimezone(UTC),
            base_asset=self._base_asset,
            quote_asset=self._quote_asset,
            quote_available=self._broker.cash,
            base_quantity=self._broker.position_quantity,
            reference_price=mid,
            equity=self._broker.cash + self._broker.position_quantity * mid,
            portfolio=portfolio,
            active_order_ids=active,
            unresolved_order_ids=unresolved,
        )


@dataclass(frozen=True, slots=True)
class ProtectiveExitPolicy:
    """Deterministic adverse-regime and drawdown policy; orders still pass the Risk Engine."""

    max_adverse_move_fraction: Decimal = Decimal("0.05")
    exit_regimes: frozenset[RegimeLabel] = frozenset(
        {
            RegimeLabel.BEAR_TREND,
            RegimeLabel.HIGH_VOLATILITY,
            RegimeLabel.UNKNOWN,
        }
    )
    exit_on_abstention: bool = True

    def __post_init__(self) -> None:
        if (
            not self.max_adverse_move_fraction.is_finite()
            or not ZERO < self.max_adverse_move_fraction < ONE
        ):
            raise ValueError("Protective loss fraction must be in (0, 1).")
        if not isinstance(self.exit_regimes, frozenset) or any(
            not isinstance(label, RegimeLabel) for label in self.exit_regimes
        ):
            raise ValueError("Protective exit regimes must be a valid immutable set.")
        if type(self.exit_on_abstention) is not bool:
            raise ValueError("exit_on_abstention must be an explicit boolean.")

    def should_exit(
        self,
        account: AccountPortfolioState,
        *,
        average_entry_price: Decimal,
        prediction: RegimePrediction | None,
    ) -> bool:
        if account.base_quantity <= ZERO:
            return False
        if not average_entry_price.is_finite() or average_entry_price <= ZERO:
            return True
        basis = account.base_quantity * average_entry_price
        if basis <= ZERO:
            return True
        if account.portfolio.unrealized_pnl / basis <= -self.max_adverse_move_fraction:
            return True
        if prediction is None:
            return False
        if self.exit_on_abstention and prediction.abstained:
            return True
        return prediction.label in self.exit_regimes


@dataclass(frozen=True, slots=True)
class PaperRuntimeResult:
    status: RuntimeStatus
    correlation_id: str
    reason_code: str
    candle_timestamp: datetime | None = None
    regime_label: str | None = None
    regime_confidence: Decimal | None = None
    selected_strategy_id: str | None = None
    target_exposure: Decimal | None = None
    order_id: str | None = None
    order_status: str | None = None
    filled_quantity: Decimal = ZERO
    protective_exit: bool = False
    reconciliation_matched: bool | None = None
    account: AccountPortfolioState | None = None


class PaperStrategyRuntime:
    """Feed -> regime brain -> strategy selector -> intent -> Paper fill -> reconciliation."""

    def __init__(
        self,
        *,
        market: Market,
        base_asset: str,
        quote_asset: str,
        feed: PaperRuntimeFeed,
        regime_classifier: RegimeClassifier,
        registry: StrategyPluginRegistry,
        initial_capital: Decimal,
        risk_limits: RiskLimits,
        journal: SQLitePaperRuntimeLedger,
        selector_policy: StrategySelectionPolicy | None = None,
        protective_exit_policy: ProtectiveExitPolicy | None = None,
        allocation_fraction: Decimal = Decimal("0.25"),
        quantity_step: Decimal = Decimal("0.001"),
        rebalance_tolerance_fraction: Decimal = Decimal("0.02"),
        preferred_strategy_by_regime: Mapping[RegimeLabel, tuple[str, ...]] | None = None,
        interval: timedelta = timedelta(hours=1),
        history_limit: int = 256,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if getattr(feed, "simulation_only", None) is not True:
            raise ValueError("PaperStrategyRuntime requires feed.simulation_only=True.")
        if (
            market.venue.casefold() != "nobitex"
            or market.market_type.casefold() != "spot"
            or not market.symbol.upper().endswith("-USDT")
            or quote_asset.upper() != "USDT"
        ):
            raise ValueError("Paper runtime scope is Nobitex Spot/USDT only.")
        if not allocation_fraction.is_finite() or not ZERO < allocation_fraction <= ONE:
            raise ValueError("allocation_fraction must be finite and in (0, 1].")
        if not quantity_step.is_finite() or quantity_step <= ZERO:
            raise ValueError("quantity_step must be finite and positive.")
        if (
            not rebalance_tolerance_fraction.is_finite()
            or not ZERO <= rebalance_tolerance_fraction < ONE
        ):
            raise ValueError("rebalance_tolerance_fraction must be in [0, 1).")
        if interval <= timedelta(0) or history_limit < 2:
            raise ValueError("Runtime interval and history_limit must be positive.")
        if not initial_capital.is_finite() or initial_capital <= ZERO:
            raise ValueError("Initial Paper capital must be finite and positive.")
        if not base_asset.strip() or base_asset.upper() == quote_asset.upper():
            raise ValueError("Paper base and quote assets must be distinct.")

        self.market = market
        self.interval = interval
        self.feed = feed
        self.regime_classifier = regime_classifier
        self.registry = registry
        self.journal = journal
        self.allocation_fraction = allocation_fraction
        self.quantity_step = quantity_step
        self.rebalance_tolerance_fraction = rebalance_tolerance_fraction
        self.history_limit = history_limit
        self._max_market_data_age_seconds = risk_limits.max_market_data_age_seconds
        self.protective_exit_policy = protective_exit_policy or ProtectiveExitPolicy()
        self._clock_source = clock or (lambda: datetime.now(UTC))
        self._effective_now = _require_utc(self._clock_source(), "runtime clock")
        def component_clock() -> datetime:
            return self._effective_now

        self.risk_engine = RiskEngine(risk_limits)
        self.broker = PaperBroker(
            market=market,
            market_data=feed,
            risk_port=self.risk_engine,
            initial_capital=initial_capital,
            max_market_data_age_seconds=self._max_market_data_age_seconds,
            clock=component_clock,
        )
        self.mock_venue = MockSpotExchangeAdapter(
            base_asset=base_asset,
            quote_asset=quote_asset,
            initial_quote_balance=initial_capital,
            clock=component_clock,
        )
        self.execution = SpotExecutionService(
            market=market,
            base_asset=base_asset,
            quote_asset=quote_asset,
            broker=self.broker,
            mock_venue=self.mock_venue,
            clock=component_clock,
        )
        self.account_builder = AccountPortfolioBuilder(
            self.broker,
            market,
            base_asset,
            quote_asset,
        )
        self.selector = RegimeStrategySelector(
            registry,
            policy=selector_policy,
            preferred_strategy_by_regime=preferred_strategy_by_regime,
            decision_ledger=self.execution.ledger,
        )
        self._history: deque[MarketCandle] = deque(maxlen=history_limit)
        self._last_candle_at: datetime | None = None
        self._bootstrapped = False
        self._halted = False
        self._halt_reason: str | None = None
        self._restore_from_journal()

    @property
    def history(self) -> tuple[MarketCandle, ...]:
        return tuple(self._history)

    @property
    def halted(self) -> bool:
        return self._halted or self.risk_engine.is_halted

    @property
    def halt_reason(self) -> str | None:
        return self._halt_reason or self.risk_engine.halt_reason

    def _risk_is_halted(self) -> bool:
        """Read the mutable RiskEngine latch after operations that may change it."""
        return self.risk_engine.is_halted

    def bootstrap(self, *, now: datetime | None = None) -> PaperRuntimeResult:
        observed_at = self._set_now(now)
        correlation_id = self._cycle_id("bootstrap", observed_at)
        if self.halted:
            return self._halted_result(correlation_id)
        if self._bootstrapped:
            return PaperRuntimeResult(
                RuntimeStatus.BOOTSTRAPPED,
                correlation_id,
                "RECOVERED_COMMITTED_STATE",
                candle_timestamp=self._last_candle_at,
                reconciliation_matched=True,
            )
        try:
            candles = self._validated_candles(observed_at)
            self._history.extend(candles[-self.history_limit :])
            self._last_candle_at = self._history[-1].timestamp
            self._bootstrapped = True
            self.journal.append(
                "BOOTSTRAP_COMMITTED",
                correlation_id=correlation_id,
                occurred_at=observed_at,
                payload=self._state_payload({"reason_code": "HISTORY_SEEDED_NO_ORDERS"}),
            )
            account = self.account_builder.build(
                self.feed.get_snapshot(self.market),
                reconciled=True,
            )
            return PaperRuntimeResult(
                RuntimeStatus.BOOTSTRAPPED,
                correlation_id,
                "HISTORY_SEEDED_NO_ORDERS",
                candle_timestamp=self._last_candle_at,
                reconciliation_matched=True,
                account=account,
            )
        except Exception as exc:
            return self._latch_halt(
                f"bootstrap_failed:{type(exc).__name__}",
                correlation_id,
                observed_at,
            )

    def step(self, *, now: datetime | None = None) -> PaperRuntimeResult:
        """Run exactly one supervised cycle; no hidden loop and no live order transport."""
        observed_at = self._set_now(now)
        correlation_id = self._cycle_id("cycle", observed_at)
        if self.halted:
            return self._halted_result(correlation_id)
        if not self._bootstrapped:
            return self.bootstrap(now=observed_at)

        try:
            snapshot = self.feed.get_snapshot(self.market)
            self._validate_snapshot(snapshot, observed_at)
            candles = self._validated_candles(observed_at)
            unseen = tuple(
                candle
                for candle in candles
                if self._last_candle_at is None or candle.timestamp > self._last_candle_at
            )
            if len(unseen) > 1:
                return self._latch_halt(
                    "multiple_unprocessed_candles_requires_reconciliation",
                    correlation_id,
                    observed_at,
                )
            if (
                unseen
                and self._last_candle_at is not None
                and unseen[0].timestamp != self._last_candle_at + self.interval
            ):
                return self._latch_halt(
                    "candle_gap_requires_reconciliation",
                    correlation_id,
                    observed_at,
                )

            self.journal.append(
                "MARKET_UPDATE_STARTED",
                correlation_id=correlation_id,
                occurred_at=observed_at,
                payload={
                    "market": self.market.symbol,
                    "snapshot_at": snapshot.observed_at.astimezone(UTC).isoformat(),
                    "new_candle_at": unseen[0].timestamp.isoformat() if unseen else None,
                },
            )
            _, reconciliation = self.execution.process_market_update(snapshot)
            self.journal.append(
                "MARKET_UPDATE_RESOLVED",
                correlation_id=correlation_id,
                occurred_at=observed_at,
                payload={
                    "matched": reconciliation.matched,
                    "differences": list(reconciliation.differences),
                    "compared_orders": reconciliation.compared_orders,
                },
            )
            if not reconciliation.matched or self.risk_engine.is_halted:
                self._commit_cycle(
                    observed_at,
                    correlation_id,
                    {"reason_code": "MARKET_RECONCILIATION_FAILED"},
                )
                return self._latch_halt(
                    self.risk_engine.halt_reason or "market_reconciliation_failed",
                    correlation_id,
                    observed_at,
                )

            new_candle = unseen[0] if unseen else None
            prediction: RegimePrediction | None = None
            decision: StrategySelectionDecision | None = None
            selected_strategy_id: str | None = None
            target_exposure: Decimal | None = None
            protective_exit = False
            reason_code = "NO_NEW_CANDLE"

            if new_candle is not None:
                self._history.append(new_candle)
                self._last_candle_at = new_candle.timestamp
                raw_prediction: object = self.regime_classifier.classify(
                    tuple(self._history),
                    now=observed_at,
                )
                if not isinstance(raw_prediction, RegimePrediction):
                    return self._latch_halt(
                        "invalid_regime_prediction",
                        correlation_id,
                        observed_at,
                    )
                prediction = raw_prediction
                decision = self.selector.select(
                    prediction,
                    now=observed_at,
                    correlation_id=correlation_id,
                    market_symbol=self.market.symbol,
                )
                if decision.allow_new_signals and decision.selected_strategy_id is not None:
                    selected_strategy_id = decision.selected_strategy_id
                    strategy = self.registry.get(selected_strategy_id).strategy
                    raw_target: object = strategy.target_exposure(tuple(self._history))
                    if (
                        not isinstance(raw_target, Decimal)
                        or not raw_target.is_finite()
                        or raw_target not in (ZERO, ONE)
                    ):
                        return self._latch_halt(
                            "invalid_strategy_target_exposure",
                            correlation_id,
                            observed_at,
                        )
                    target_exposure = raw_target
                    reason_code = "STRATEGY_TARGET_READY"
                else:
                    reason_code = decision.reason_code

            account = self.account_builder.build(snapshot, reconciled=True)
            protective_exit = self.protective_exit_policy.should_exit(
                account,
                average_entry_price=self.broker.average_entry_price,
                prediction=prediction,
            )
            if protective_exit:
                target_exposure = ZERO
                reason_code = "PROTECTIVE_EXIT"
            elif new_candle is None and account.base_quantity > ZERO:
                # Monitor stops even between new completed-candle decisions.
                if self.protective_exit_policy.should_exit(
                    account,
                    average_entry_price=self.broker.average_entry_price,
                    prediction=None,
                ):
                    protective_exit = True
                    target_exposure = ZERO
                    reason_code = "PROTECTIVE_STOP_LOSS"

            order_result = None
            request: OrderRequest | None = None
            if target_exposure is not None:
                if account.unresolved_order_ids:
                    self._commit_cycle(
                        observed_at,
                        correlation_id,
                        {"reason_code": "UNKNOWN_ORDER"},
                    )
                    return self._latch_halt(
                        "unknown_order_state",
                        correlation_id,
                        observed_at,
                    )
                if account.active_order_ids:
                    target_exposure = None
                    reason_code = "ACTIVE_ORDER_RECONCILIATION_PENDING"
                else:
                    request = self._make_order_request(
                        target_exposure,
                        account,
                        force=protective_exit,
                    )

            if request is not None:
                order_correlation = f"{correlation_id}:order"
                self.journal.append(
                    "INTENT_PREPARED",
                    correlation_id=order_correlation,
                    occurred_at=observed_at,
                    payload={
                        "client_order_id": request.client_order_id,
                        "market": request.market.symbol,
                        "side": request.side.value,
                        "quantity": str(request.quantity),
                        "target_exposure": str(target_exposure),
                        "protective_exit": protective_exit,
                    },
                )
                try:
                    order_result = self.execution.execute(
                        SpotIntent(
                            correlation_id=order_correlation,
                            request=request,
                            created_at=observed_at,
                        )
                    )
                except Exception as exc:
                    self._commit_cycle(
                        observed_at,
                        correlation_id,
                        {"reason_code": "PAPER_EXECUTION_EXCEPTION"},
                    )
                    return self._latch_halt(
                        f"paper_execution_failed:{type(exc).__name__}",
                        correlation_id,
                        observed_at,
                    )
                self.journal.append(
                    "INTENT_RESOLVED",
                    correlation_id=order_correlation,
                    occurred_at=observed_at,
                    payload={
                        "client_order_id": request.client_order_id,
                        "order_id": order_result.order.order_id,
                        "status": order_result.order.status.value,
                        "filled_quantity": str(order_result.order.filled_quantity),
                        "reconciliation_matched": order_result.reconciliation.matched,
                    },
                )
                if (
                    not order_result.reconciliation.matched
                    or order_result.order.status is PaperOrderStatus.UNKNOWN
                ):
                    self._commit_cycle(
                        observed_at,
                        correlation_id,
                        {"reason_code": "ORDER_RECONCILIATION_FAILED"},
                    )
                    return self._latch_halt(
                        "order_reconciliation_failed",
                        correlation_id,
                        observed_at,
                    )
                if self._risk_is_halted():
                    self._commit_cycle(
                        observed_at,
                        correlation_id,
                        {"reason_code": "RISK_ENGINE_HALTED"},
                    )
                    return self._latch_halt(
                        self.risk_engine.halt_reason or "risk_engine_halted",
                        correlation_id,
                        observed_at,
                    )

            final_reconciliation = (
                order_result.reconciliation if order_result is not None else reconciliation
            )
            final_account = self.account_builder.build(
                snapshot,
                reconciled=final_reconciliation.matched,
            )
            self._commit_cycle(
                observed_at,
                correlation_id,
                {
                    "reason_code": reason_code,
                    "candle_timestamp": (
                        self._last_candle_at.isoformat() if self._last_candle_at else None
                    ),
                    "regime": self._prediction_payload(prediction) if prediction else None,
                    "selection": self._decision_payload(decision) if decision else None,
                    "selected_strategy_id": selected_strategy_id,
                    "target_exposure": (
                        str(target_exposure) if target_exposure is not None else None
                    ),
                    "protective_exit": protective_exit,
                    "order_id": order_result.order.order_id if order_result else None,
                    "order_status": order_result.order.status.value if order_result else None,
                    "filled_quantity": (
                        str(order_result.order.filled_quantity) if order_result else "0"
                    ),
                    "reconciliation_matched": final_reconciliation.matched,
                },
            )
            if target_exposure is not None:
                status = RuntimeStatus.PROCESSED
            elif new_candle is None:
                status = RuntimeStatus.NO_NEW_CANDLE
            else:
                status = RuntimeStatus.ABSTAINED
            return PaperRuntimeResult(
                status=status,
                correlation_id=correlation_id,
                reason_code=reason_code,
                candle_timestamp=self._last_candle_at,
                regime_label=prediction.label.value if prediction else None,
                regime_confidence=prediction.confidence if prediction else None,
                selected_strategy_id=selected_strategy_id,
                target_exposure=target_exposure,
                order_id=order_result.order.order_id if order_result else None,
                order_status=order_result.order.status.value if order_result else None,
                filled_quantity=(
                    order_result.order.filled_quantity if order_result else ZERO
                ),
                protective_exit=protective_exit,
                reconciliation_matched=final_reconciliation.matched,
                account=final_account,
            )
        except Exception as exc:
            return self._latch_halt(
                f"runtime_cycle_failed:{type(exc).__name__}",
                correlation_id,
                observed_at,
            )

    def halt(self, reason: str, *, now: datetime | None = None) -> PaperRuntimeResult:
        observed_at = self._set_now(now)
        return self._latch_halt(
            reason.strip() or "runtime_stopped",
            self._cycle_id("halt", observed_at),
            observed_at,
        )

    def _make_order_request(
        self,
        target_exposure: Decimal,
        account: AccountPortfolioState,
        *,
        force: bool,
    ) -> OrderRequest | None:
        current_quantity = account.base_quantity
        if target_exposure == ZERO:
            desired_quantity = ZERO
        else:
            desired_quantity = (
                account.equity * self.allocation_fraction / account.reference_price
            )
            desired_quantity = (
                desired_quantity / self.quantity_step
            ).to_integral_value(rounding=ROUND_DOWN) * self.quantity_step
        delta = desired_quantity - current_quantity
        if delta == ZERO:
            return None
        if target_exposure == ONE and not force:
            tolerance = (
                account.equity
                * self.allocation_fraction
                * self.rebalance_tolerance_fraction
            )
            if abs(delta) * account.reference_price <= tolerance:
                return None
        quantity = (
            (abs(delta) / self.quantity_step).to_integral_value(rounding=ROUND_DOWN)
            * self.quantity_step
        )
        if quantity <= ZERO:
            return None
        side = Side.BUY if delta > ZERO else Side.SELL
        candle_key = self._last_candle_at.isoformat() if self._last_candle_at else "no-candle"
        suffix = hashlib.sha256(
            f"{self.market.symbol}|{candle_key}|{side.value}".encode()
        ).hexdigest()[:28]
        return OrderRequest(
            client_order_id=f"paper-{suffix}",
            market=self.market,
            side=side,
            order_type=OrderType.MARKET,
            quantity=quantity,
        )

    def _validated_candles(self, now: datetime) -> tuple[MarketCandle, ...]:
        candles = tuple(
            self.feed.get_completed_candles(self.market, self.interval, now=now)
        )
        if not candles:
            raise ValueError("Mock feed has no completed candles.")
        previous: MarketCandle | None = None
        for candle in candles:
            if candle.market != self.market or candle.interval != self.interval:
                raise ValueError("Mock candle market/interval mismatch.")
            _require_utc(candle.timestamp, "candle timestamp")
            if candle.timestamp + candle.interval > now:
                raise ValueError("Mock feed returned a partial or future candle.")
            if previous is not None and candle.timestamp != previous.timestamp + self.interval:
                raise ValueError("Mock candle history has a gap, duplicate or ordering error.")
            previous = candle
        if now - (candles[-1].timestamp + candles[-1].interval) > self.interval * 2:
            raise ValueError("Latest completed mock candle is stale.")
        return candles

    def _validate_snapshot(self, snapshot: MarketSnapshot, now: datetime) -> None:
        if snapshot.market != self.market:
            raise ValueError("Mock snapshot market mismatch.")
        _require_aware(snapshot.observed_at, "snapshot timestamp")
        age = now - snapshot.observed_at
        if age < timedelta(0) or age.total_seconds() > self._max_market_data_age_seconds:
            raise ValueError("Mock snapshot is stale or future-dated.")

    def _state_payload(self, details: Mapping[str, object]) -> dict[str, object]:
        state = self.broker.export_state()
        return {
            "market": _market_payload(state.market),
            "initial_capital": str(state.initial_capital),
            "cash": str(state.cash),
            "position_quantity": str(state.position_quantity),
            "average_entry_price": str(state.average_entry_price),
            "realized_pnl": str(state.realized_pnl),
            "orders": [_order_payload(order) for order in state.orders],
            "history": [_candle_payload(candle) for candle in self._history],
            "last_candle_at": (
                self._last_candle_at.isoformat() if self._last_candle_at else None
            ),
            "details": dict(details),
        }

    def _commit_cycle(
        self,
        observed_at: datetime,
        correlation_id: str,
        details: Mapping[str, object],
    ) -> None:
        self.journal.append(
            "CYCLE_COMMITTED",
            correlation_id=correlation_id,
            occurred_at=observed_at,
            payload=self._state_payload(details),
        )

    def _restore_from_journal(self) -> None:
        events = self.journal.events()
        if not events:
            return
        committed = next(
            (event for event in reversed(events) if event.event_type in COMMITTED_EVENTS),
            None,
        )
        if committed is not None:
            self.broker.restore_state(_broker_state_from_payload(committed.payload))
            raw_history = committed.payload.get("history")
            if not isinstance(raw_history, list):
                raise RuntimeLedgerIntegrityError("Committed state has no candle history.")
            self._history.extend(_candle_from_payload(item) for item in raw_history)
            if self._history:
                self._last_candle_at = self._history[-1].timestamp
                self._bootstrapped = True
            for order in sorted(
                self.broker.list_orders(),
                key=lambda item: (item.created_at, item.order_id),
            ):
                self.mock_venue.observe_order(order)
            report = self.execution.reconcile_all(
                correlation_id=f"startup-reconcile:{committed.sequence}"
            )
            if not report.matched:
                self._latch_halt(
                    "restart_state_reconciliation_failed",
                    f"startup-reconcile:{committed.sequence}",
                    self._effective_now,
                )
        trailing = [
            event for event in events
            if committed is None or event.sequence > committed.sequence
        ]
        interrupted = any(
            event.event_type in {"MARKET_UPDATE_STARTED", "INTENT_PREPARED"}
            for event in trailing
        )
        previously_halted = any(event.event_type == "RUNTIME_HALTED" for event in events)
        if interrupted or previously_halted:
            reason = (
                "interrupted_operation_requires_reconciliation"
                if interrupted
                else "previous_halt_requires_operator_review"
            )
            self._latch_halt(
                reason,
                f"startup-recovery:{events[-1].sequence}",
                self._effective_now,
            )

    def _latch_halt(
        self,
        reason: str,
        correlation_id: str,
        occurred_at: datetime,
    ) -> PaperRuntimeResult:
        if not self._halted:
            self._halted = True
            self._halt_reason = reason
            self.risk_engine.emergency_halt(reason)
            try:
                self.journal.append(
                    "RUNTIME_HALTED",
                    correlation_id=correlation_id,
                    occurred_at=occurred_at,
                    payload={"reason_code": reason, "manual_clear_required": True},
                )
            except Exception as exc:
                # The in-memory stop remains latched even if durable halt evidence fails.
                self._halt_reason = (
                    f"{reason}:halt_journal_write_failed:{type(exc).__name__}"
                )
        return self._halted_result(correlation_id)

    def _halted_result(self, correlation_id: str) -> PaperRuntimeResult:
        return PaperRuntimeResult(
            RuntimeStatus.HALTED,
            correlation_id,
            self.halt_reason or "runtime_halted",
            candle_timestamp=self._last_candle_at,
            reconciliation_matched=False,
        )

    def _set_now(self, value: datetime | None) -> datetime:
        self._effective_now = _require_utc(
            value if value is not None else self._clock_source(),
            "runtime time",
        )
        return self._effective_now

    def _cycle_id(self, prefix: str, occurred_at: datetime) -> str:
        material = f"{prefix}|{self.market.symbol}|{occurred_at.isoformat()}"
        return f"{prefix}:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"

    @staticmethod
    def _prediction_payload(value: RegimePrediction) -> dict[str, object]:
        return {
            "label": value.label.value,
            "confidence": str(value.confidence),
            "as_of": value.as_of.astimezone(UTC).isoformat(),
            "model_id": value.model_id,
            "model_version": value.model_version,
            "model_fingerprint": value.model_fingerprint,
            "feature_schema_version": value.feature_schema_version,
            "abstained": value.abstained,
            "reason_code": value.reason_code,
        }

    @staticmethod
    def _decision_payload(value: StrategySelectionDecision) -> dict[str, object]:
        return {
            "selected_strategy_id": value.selected_strategy_id,
            "candidate_strategy_id": value.candidate_strategy_id,
            "status": value.status.value,
            "reason_code": value.reason_code,
            "allow_new_signals": value.allow_new_signals,
            "registry_fingerprint": value.registry_fingerprint,
            "routing_fingerprint": value.routing_fingerprint,
            "evidence_count": value.evidence_count,
        }


class PaperRuntimeSupervisor:
    """Supervise bounded cycles and reconstruct from disk after errors, without resubmitting."""

    def __init__(self, factory: Callable[[], PaperStrategyRuntime]) -> None:
        self._factory = factory
        self.runtime = factory()

    def step(self, *, now: datetime | None = None) -> PaperRuntimeResult:
        try:
            return self.runtime.step(now=now)
        except Exception as exc:
            result = self.runtime.halt(f"supervisor_exception:{type(exc).__name__}", now=now)
            self.runtime = self._factory()
            return result

    def restart(self) -> PaperStrategyRuntime:
        self.runtime = self._factory()
        return self.runtime


def _market_payload(value: Market) -> dict[str, object]:
    return {"symbol": value.symbol, "venue": value.venue, "market_type": value.market_type}


def _market_from_payload(value: object) -> Market:
    data = _object(value, "market")
    return Market(
        symbol=_string(data, "symbol"),
        venue=_string(data, "venue"),
        market_type=_string(data, "market_type"),
    )


def _candle_payload(value: MarketCandle) -> dict[str, object]:
    return {
        "market": _market_payload(value.market),
        "timestamp": value.timestamp.astimezone(UTC).isoformat(),
        "interval_microseconds": int(value.interval.total_seconds() * 1_000_000),
        "open": str(value.open),
        "high": str(value.high),
        "low": str(value.low),
        "close": str(value.close),
        "volume": str(value.volume),
        "source": value.source,
    }


def _candle_from_payload(value: object) -> MarketCandle:
    data = _object(value, "candle")
    return MarketCandle(
        market=_market_from_payload(data.get("market")),
        timestamp=_datetime(data, "timestamp"),
        interval=timedelta(microseconds=_integer(data, "interval_microseconds")),
        open=_decimal(data, "open"),
        high=_decimal(data, "high"),
        low=_decimal(data, "low"),
        close=_decimal(data, "close"),
        volume=_decimal(data, "volume"),
        source=_string(data, "source"),
    )


def _order_payload(value: PaperOrder) -> dict[str, object]:
    request = value.request
    return {
        "order_id": value.order_id,
        "request": {
            "client_order_id": request.client_order_id,
            "market": _market_payload(request.market),
            "side": request.side.value,
            "order_type": request.order_type.value,
            "quantity": str(request.quantity),
            "price": str(request.price) if request.price is not None else None,
        },
        "requested_quantity": str(value.requested_quantity),
        "status": value.status.value,
        "filled_quantity": str(value.filled_quantity),
        "fills": [
            {
                "order_id": fill.order_id,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "occurred_at": fill.occurred_at.astimezone(UTC).isoformat(),
            }
            for fill in value.fills
        ],
        "created_at": value.created_at.astimezone(UTC).isoformat(),
        "updated_at": value.updated_at.astimezone(UTC).isoformat(),
        "reason": value.reason,
        "last_confirmed_status": (
            value.last_confirmed_status.value if value.last_confirmed_status else None
        ),
    }


def _order_from_payload(value: object) -> PaperOrder:
    data = _object(value, "paper order")
    request_data = _object(data.get("request"), "order request")
    raw_fills = data.get("fills")
    if not isinstance(raw_fills, list):
        raise RuntimeLedgerIntegrityError("Paper order fills must be a list.")
    fills: list[Fill] = []
    for item in raw_fills:
        fill = _object(item, "fill")
        fills.append(
            Fill(
                order_id=_string(fill, "order_id"),
                quantity=_decimal(fill, "quantity"),
                price=_decimal(fill, "price"),
                occurred_at=_datetime(fill, "occurred_at"),
            )
        )
    raw_price = request_data.get("price")
    raw_reason = data.get("reason")
    raw_confirmed = data.get("last_confirmed_status")
    return PaperOrder(
        order_id=_string(data, "order_id"),
        request=OrderRequest(
            client_order_id=_string(request_data, "client_order_id"),
            market=_market_from_payload(request_data.get("market")),
            side=Side(_string(request_data, "side")),
            order_type=OrderType(_string(request_data, "order_type")),
            quantity=_decimal(request_data, "quantity"),
            price=Decimal(str(raw_price)) if raw_price is not None else None,
        ),
        requested_quantity=_decimal(data, "requested_quantity"),
        status=PaperOrderStatus(_string(data, "status")),
        filled_quantity=_decimal(data, "filled_quantity"),
        fills=tuple(fills),
        created_at=_datetime(data, "created_at"),
        updated_at=_datetime(data, "updated_at"),
        reason=raw_reason if isinstance(raw_reason, str) else None,
        last_confirmed_status=(
            PaperOrderStatus(str(raw_confirmed)) if raw_confirmed is not None else None
        ),
    )


def _broker_state_from_payload(payload: Mapping[str, object]) -> PaperBrokerState:
    raw_orders = payload.get("orders")
    if not isinstance(raw_orders, list):
        raise RuntimeLedgerIntegrityError("Committed runtime state has no order list.")
    return PaperBrokerState(
        market=_market_from_payload(payload.get("market")),
        initial_capital=_decimal(payload, "initial_capital"),
        cash=_decimal(payload, "cash"),
        position_quantity=_decimal(payload, "position_quantity"),
        average_entry_price=_decimal(payload, "average_entry_price"),
        realized_pnl=_decimal(payload, "realized_pnl"),
        orders=tuple(_order_from_payload(item) for item in raw_orders),
    )


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeLedgerIntegrityError(f"{name} must be a JSON object.")
    return cast(dict[str, object], value)


def _string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeLedgerIntegrityError(f"Required text field {key!r} is invalid.")
    return value


def _integer(data: Mapping[str, object], key: str) -> int:
    value = data.get(key)
    if type(value) is not int:
        raise RuntimeLedgerIntegrityError(f"Required integer field {key!r} is invalid.")
    return value


def _decimal(data: Mapping[str, object], key: str) -> Decimal:
    value = data.get(key)
    if not isinstance(value, str):
        raise RuntimeLedgerIntegrityError(f"Required decimal field {key!r} is invalid.")
    try:
        result = Decimal(value)
    except Exception as exc:
        raise RuntimeLedgerIntegrityError(f"Invalid decimal field {key!r}.") from exc
    if not result.is_finite():
        raise RuntimeLedgerIntegrityError(f"Non-finite decimal field {key!r}.")
    return result


def _datetime(data: Mapping[str, object], key: str) -> datetime:
    value = data.get(key)
    if not isinstance(value, str):
        raise RuntimeLedgerIntegrityError(f"Required timestamp field {key!r} is invalid.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeLedgerIntegrityError(f"Invalid timestamp field {key!r}.") from exc
    _require_aware(result, key)
    return result.astimezone(UTC)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")


def _require_utc(value: datetime, name: str) -> datetime:
    _require_aware(value, name)
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC.")
    return value.astimezone(UTC)
