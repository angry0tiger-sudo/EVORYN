"""Stateless, deterministic baseline strategies independent of AI and execution."""

from evoryn.strategies.baseline import (
    BaselineStrategy,
    FlatBaseline,
    SmaCrossoverBaseline,
    SmaTrendBaseline,
)
from evoryn.strategies.contracts import CausalTargetStrategy

__all__ = [
    "BaselineStrategy",
    "CausalTargetStrategy",
    "FlatBaseline",
    "SmaCrossoverBaseline",
    "SmaTrendBaseline",
]
