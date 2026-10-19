"""Stateless, deterministic baseline strategies independent of AI and execution."""

from evoryn.strategies.baseline import (
    BaselineStrategy,
    FlatBaseline,
    SmaCrossoverBaseline,
    SmaTrendBaseline,
)
from evoryn.strategies.contracts import CausalTargetStrategy
from evoryn.strategies.plugins import (
    StrategyPlugin,
    StrategyPluginRegistration,
    StrategyPluginRegistry,
    build_baseline_registry,
)

__all__ = [
    "BaselineStrategy",
    "CausalTargetStrategy",
    "FlatBaseline",
    "StrategyPlugin",
    "StrategyPluginRegistration",
    "StrategyPluginRegistry",
    "build_baseline_registry",
    "SmaCrossoverBaseline",
    "SmaTrendBaseline",
]
