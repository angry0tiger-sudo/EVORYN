"""Typed port contracts separating EVORYN core logic from providers and plugins."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from evoryn.domain.models import (
    ExchangeCapabilities,
    ExecutionPlan,
    ExecutionResult,
    ExperimentProposal,
    LearningEvent,
    Market,
    MarketCandle,
    MarketSnapshot,
    MarketStatistics,
    MarketTrade,
    Order,
    OrderBookSnapshot,
    OrderIntent,
    OrderRequest,
    RiskDecision,
    Signal,
    WalletBalance,
)
from evoryn.ports.rate_limits import RateLimitPolicy
from evoryn.risk.models import PortfolioSnapshot, RiskResult


@runtime_checkable
class MarketDataPort(Protocol):
    """Read canonical market snapshots with source, timestamp, and quality metadata."""

    def get_snapshot(self, market: Market) -> MarketSnapshot:
        """Return one normalized snapshot for the requested market."""
        ...

    def get_history(
        self, market: Market, start: datetime, end: datetime
    ) -> Sequence[MarketSnapshot]:
        """Return timestamped snapshots within the requested interval."""
        ...


@runtime_checkable
class ExchangePort(Protocol):
    """Full common exchange contract; each implementation declares its real capabilities.

    The contract includes writes for architecture completeness. An implementation
    must fail closed for unsupported writes, and the EVORYN Phase 6/7 Nobitex adapter
    intentionally does not implement an order-writing transport path.
    """

    def get_capabilities(self) -> ExchangeCapabilities:
        """Return explicit capabilities; omitted or false capabilities are unavailable."""
        ...

    def get_market_candles(
        self,
        market: Market,
        *,
        interval: timedelta,
        start: datetime,
        end: datetime,
    ) -> Sequence[MarketCandle]:
        """Read and normalize OHLCV candles for a market and closed time range."""
        ...

    def get_order_book(self, market: Market) -> OrderBookSnapshot:
        """Read a normalized point-in-time order-book snapshot."""
        ...

    def get_recent_trades(self, market: Market, *, limit: int) -> Sequence[MarketTrade]:
        """Read a bounded set of normalized recent trades."""
        ...

    def get_market_stats(self, market: Market) -> MarketStatistics:
        """Read normalized market statistics."""
        ...

    def get_balances(self) -> Sequence[WalletBalance]:
        """Read balances for the authenticated account; never access withdrawal APIs."""
        ...

    def get_order(self, order_id: str, market: Market) -> Order | None:
        """Read one order's current state, using caller-supplied canonical market identity."""
        ...

    def submit_order(self, request: OrderRequest) -> Order:
        """Submit an order only if a future separately reviewed execution gate permits it."""
        ...

    def cancel_order(self, order_id: str) -> Order:
        """Cancel an order only if a future separately reviewed execution gate permits it."""
        ...


@runtime_checkable
class ExchangePlugin(Protocol):
    """Provider plugin contract discovered through the 'evoryn.exchanges' entry-point group."""

    @property
    def name(self) -> str:
        """Return the stable lowercase plugin identifier."""
        ...

    def build_adapter(self, config: Mapping[str, str]) -> ExchangePort:
        """Build an adapter from non-secret configuration plus explicitly supplied secrets."""
        ...


@runtime_checkable
class RateLimiterPort(Protocol):
    """Endpoint-independent rate-budget contract shared by exchange adapters."""

    def acquire(self, key: str, policy: RateLimitPolicy) -> None:
        """Consume one request slot or raise RateLimitExceeded with a retry delay."""
        ...


@runtime_checkable
class StrategyPort(Protocol):
    """Propose a signal from a snapshot without accessing order APIs."""

    def propose_signal(self, snapshot: MarketSnapshot) -> Signal | None:
        """Return a candidate signal; this contract grants no execution authority."""
        ...


@runtime_checkable
class RiskPort(Protocol):
    """Deterministic, model-independent gate required before execution."""

    @property
    def is_halted(self) -> bool:
        """Return whether automatic halt or the manual kill switch is latched."""
        ...

    def evaluate(
        self,
        request: OrderRequest,
        portfolio: PortfolioSnapshot,
        market_data: MarketSnapshot,
        *,
        now: datetime,
    ) -> RiskResult:
        """Return exactly ALLOW, REDUCE, REJECT, or HALT."""
        ...

    def emergency_halt(self, reason: str) -> None:
        """Latch a fail-closed stop after a critical data or execution fault."""
        ...


@runtime_checkable
class ExecutionPort(Protocol):
    """Define an execution boundary without implementing or enabling live order submission."""

    def prepare(self, intent: OrderIntent, decision: RiskDecision) -> ExecutionPlan:
        """Prepare a reviewable plan from an intent and explicit risk decision."""
        ...

    def reconcile(self, order_id: str) -> ExecutionResult:
        """Return a normalized result shape for reconciliation workflows."""
        ...


@runtime_checkable
class LearningPort(Protocol):
    """Record versioned outcomes and propose experiments without changing live settings."""

    def record_outcome(self, event: LearningEvent) -> None:
        """Persist or forward a learning event in a future implementation."""
        ...

    def propose_experiment(self, dataset_version: str) -> ExperimentProposal | None:
        """Return a reviewable experiment proposal for a versioned dataset."""
        ...
