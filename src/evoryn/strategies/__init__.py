"""Stateless, deterministic baseline strategies independent of AI and execution."""

from evoryn.strategies.baseline import (
    BaselineStrategy,
    SmaCrossoverBaseline,
    SmaTrendBaseline,
)
from evoryn.strategies.contracts import CausalTargetStrategy

__all__ = [
    "BaselineStrategy",
    "CausalTargetStrategy",
    "SmaCrossoverBaseline",
    "SmaTrendBaseline",
]
