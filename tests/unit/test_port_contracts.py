"""Unit tests verify the presence and shape of the core port contracts."""

from __future__ import annotations

import inspect

from evoryn.ports.contracts import (
    ExchangePort,
    ExecutionPort,
    LearningPort,
    MarketDataPort,
    RiskPort,
    StrategyPort,
)


def test_market_data_port_declares_snapshot_and_history() -> None:
    """Market data contracts expose timestamped snapshot and history queries."""
    assert callable(getattr(MarketDataPort, "get_snapshot", None))
    assert callable(getattr(MarketDataPort, "get_history", None))
    assert tuple(inspect.signature(MarketDataPort.get_snapshot).parameters) == ("self", "market")


def test_exchange_port_declares_capability_and_order_queries() -> None:
    """Exchange contract declares capabilities and normalized order lookup only."""
    assert callable(getattr(ExchangePort, "get_capabilities", None))
    assert callable(getattr(ExchangePort, "get_order", None))


def test_strategy_port_has_no_execution_method() -> None:
    """Strategy contract proposes signals and has no order submission method."""
    assert callable(getattr(StrategyPort, "propose_signal", None))
    assert not hasattr(StrategyPort, "execute")
    assert not hasattr(StrategyPort, "submit_order")


def test_risk_port_returns_structured_decision_contract() -> None:
    """Risk contract accepts an intent and position context."""
    parameters = tuple(inspect.signature(RiskPort.evaluate).parameters)
    assert parameters == ("self", "intent", "positions")


def test_execution_port_only_declares_prepare_and_reconcile() -> None:
    """Execution boundary contains plan and reconciliation contracts, not an adapter."""
    assert callable(getattr(ExecutionPort, "prepare", None))
    assert callable(getattr(ExecutionPort, "reconcile", None))
    assert not hasattr(ExecutionPort, "submit_order")


def test_learning_port_cannot_mutate_live_configuration_by_contract() -> None:
    """Learning interface is limited to recording outcomes and proposing experiments."""
    assert callable(getattr(LearningPort, "record_outcome", None))
    assert callable(getattr(LearningPort, "propose_experiment", None))
    assert not hasattr(LearningPort, "update_live_configuration")
