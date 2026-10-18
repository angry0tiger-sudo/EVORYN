"""Typed port contracts only; no exchange adapter or execution implementation exists here."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from evoryn.domain.models import (
    ExchangeCapabilities,
    ExecutionPlan,
    ExecutionResult,
    ExperimentProposal,
    LearningEvent,
    Market,
    MarketSnapshot,
    Order,
    OrderIntent,
    Position,
    RiskDecision,
    Signal,
)


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
    """Describe provider capabilities and normalized read-only query contracts."""

    def get_capabilities(self) -> ExchangeCapabilities:
        """Return an explicit capability declaration for this provider."""
        ...

    def get_order(self, order_id: str) -> Order | None:
        """Return a normalized known order, or None when it is not found."""
        ...


@runtime_checkable
class StrategyPort(Protocol):
    """Propose a signal from a snapshot without accessing order APIs."""

    def propose_signal(self, snapshot: MarketSnapshot) -> Signal | None:
        """Return a candidate signal; this contract grants no execution authority."""
        ...


@runtime_checkable
class RiskPort(Protocol):
    """Evaluate an intent against portfolio context using deterministic policy."""

    def evaluate(self, intent: OrderIntent, positions: Sequence[Position]) -> RiskDecision:
        """Return a structured allow, reduce, reject, or halt decision."""
        ...


@runtime_checkable
class ExecutionPort(Protocol):
    """Define an execution boundary without implementing live order submission."""

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
