"""Transparent rule-based baselines. Strategies only observe completed historical candles."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from evoryn.domain.models import MarketCandle


class BaselineStrategy(Protocol):
    """Pure policy contract: history ends at the last completed bar, never the fill bar."""

    strategy_id: str

    def parameters(self) -> dict[str, str]:
        """Return the exact parameters that must be recorded with every experiment."""
        ...

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        """Return target exposure in [0, 1] using only candles supplied by the caller."""
        ...


@dataclass(frozen=True, slots=True)
class SmaCrossoverBaseline:
    """Long/flat baseline: hold long when the fast SMA exceeds the slow SMA."""

    fast_window: int = 5
    slow_window: int = 20
    strategy_id: str = "sma-crossover-long-flat"

    def __post_init__(self) -> None:
        if self.fast_window <= 0 or self.slow_window <= 0:
            raise ValueError("SMA windows must be positive integers.")
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be less than slow_window.")

    def parameters(self) -> dict[str, str]:
        return {
            "fast_window": str(self.fast_window),
            "slow_window": str(self.slow_window),
            "position_policy": "long_flat",
            "signal_price": "completed_close",
        }

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        """Compute trailing means including only completed candles in history."""
        if len(history) < self.slow_window:
            return Decimal("0")
        fast = _mean(tuple(c.close for c in history[-self.fast_window :]))
        slow = _mean(tuple(c.close for c in history[-self.slow_window :]))
        return Decimal("1") if fast > slow else Decimal("0")


@dataclass(frozen=True, slots=True)
class SmaTrendBaseline:
    """Long/flat baseline: hold long when the latest completed close exceeds its SMA."""

    window: int = 20
    strategy_id: str = "sma-trend-long-flat"

    def __post_init__(self) -> None:
        if self.window <= 0:
            raise ValueError("SMA window must be a positive integer.")

    def parameters(self) -> dict[str, str]:
        return {
            "window": str(self.window),
            "position_policy": "long_flat",
            "signal_price": "completed_close",
        }

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        """Return flat during warm-up; no current/future bar is available to this method."""
        if len(history) < self.window:
            return Decimal("0")
        mean = _mean(tuple(c.close for c in history[-self.window :]))
        return Decimal("1") if history[-1].close > mean else Decimal("0")


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        raise ValueError("Cannot calculate a moving average from an empty sequence.")
    return sum(values, Decimal("0")) / Decimal(len(values))
