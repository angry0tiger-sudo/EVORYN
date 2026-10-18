"""Stateless, deterministic baseline strategies independent of AI and execution."""

from evoryn.strategies.baseline import (
    BaselineStrategy,
    SmaCrossoverBaseline,
    SmaTrendBaseline,
)

__all__ = ["BaselineStrategy", "SmaCrossoverBaseline", "SmaTrendBaseline"]
