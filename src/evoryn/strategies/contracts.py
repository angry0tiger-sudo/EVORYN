"""Causal target-exposure contract for research-only strategy implementations."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol

from evoryn.domain.models import MarketCandle


class CausalTargetStrategy(Protocol):
    """Pure research policy that reads completed history and proposes flat/long exposure.

    Implementations do not receive execution ports, exchange clients or risk authority.
    The backtest engine passes only bars completed before the current simulated open.
    """

    strategy_id: str
    strategy_version: str

    def parameters(self) -> dict[str, str]:
        """Return deterministic, serializable parameters included in a run fingerprint."""
        ...

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        """Return Decimal('0') or Decimal('1') based only on the supplied completed history."""
        ...
