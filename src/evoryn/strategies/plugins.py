"""Versioned Strategy Plugin API and a validated, deterministic registry.

Plugins are pure research policies. They never receive exchange, broker, portfolio or risk objects.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Protocol

from evoryn.domain.models import MarketCandle
from evoryn.intelligence.regime import RegimeLabel
from evoryn.strategies.contracts import CausalTargetStrategy


class StrategyPlugin(Protocol):
    """Public plugin contract for strategies that can be composed into a selector."""

    plugin_id: str
    plugin_version: str
    description: str
    strategy: CausalTargetStrategy
    supported_regimes: frozenset[RegimeLabel]
    enabled: bool


@dataclass(frozen=True, slots=True)
class StrategyPluginRegistration:
    """Immutable registration metadata surrounding a pure strategy implementation."""

    plugin_id: str
    plugin_version: str
    description: str
    strategy: CausalTargetStrategy
    supported_regimes: frozenset[RegimeLabel]
    enabled: bool = True

    def __post_init__(self) -> None:
        for name in ("plugin_id", "plugin_version", "description"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty.")
        if self.plugin_id != self.strategy.strategy_id:
            raise ValueError("plugin_id must equal the wrapped strategy_id.")
        if self.plugin_version != self.strategy.strategy_version:
            raise ValueError("plugin_version must equal the wrapped strategy_version.")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be an explicit boolean.")
        if not isinstance(self.supported_regimes, frozenset) or not self.supported_regimes:
            raise ValueError("supported_regimes must be a non-empty frozenset.")
        if RegimeLabel.UNKNOWN in self.supported_regimes:
            raise ValueError("UNKNOWN is abstention and cannot be a routable regime.")
        if any(not isinstance(label, RegimeLabel) for label in self.supported_regimes):
            raise ValueError("Every supported regime must be a canonical RegimeLabel.")
        if not callable(getattr(self.strategy, "target_exposure", None)):
            raise ValueError("A strategy plugin must expose target_exposure(history).")
        if not callable(getattr(self.strategy, "parameters", None)):
            raise ValueError("A strategy plugin must expose deterministic parameters().")
        parameters = self.strategy.parameters()
        if not isinstance(parameters, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in parameters.items()
        ):
            raise ValueError("Strategy parameters must be a string-to-string dictionary.")

    def manifest(self) -> dict[str, object]:
        """Return stable, JSON-compatible metadata for registry fingerprints."""
        return {
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "description": self.description,
            "enabled": self.enabled,
            "supported_regimes": sorted(label.value for label in self.supported_regimes),
            "strategy_parameters": dict(sorted(self.strategy.parameters().items())),
            "contract": "CausalTargetStrategy/v1",
        }


class StrategyPluginRegistry:
    """Thread-safe registry with unique identities and a deterministic content fingerprint."""

    def __init__(self, plugins: tuple[StrategyPluginRegistration, ...] = ()) -> None:
        self._lock = RLock()
        self._plugins: dict[str, StrategyPluginRegistration] = {}
        for plugin in plugins:
            self.register(plugin)

    def register(self, plugin: StrategyPluginRegistration) -> None:
        """Register a plugin once; duplicate IDs fail instead of silently replacing behavior."""
        if not isinstance(plugin, StrategyPluginRegistration):
            raise TypeError("Only validated StrategyPluginRegistration objects can be registered.")
        with self._lock:
            if plugin.plugin_id in self._plugins:
                raise ValueError(f"Strategy plugin {plugin.plugin_id!r} is already registered.")
            self._plugins[plugin.plugin_id] = plugin

    def get(self, plugin_id: str) -> StrategyPluginRegistration:
        """Return a registered plugin or raise a clear error."""
        with self._lock:
            try:
                return self._plugins[plugin_id]
            except KeyError as exc:
                raise KeyError(f"Unknown strategy plugin: {plugin_id}") from exc

    def options_for(self, regime: RegimeLabel) -> tuple[StrategyPluginRegistration, ...]:
        """Return enabled plugins that explicitly declare support for the given regime."""
        if regime is RegimeLabel.UNKNOWN:
            return ()
        with self._lock:
            return tuple(
                sorted(
                    (
                        plugin
                        for plugin in self._plugins.values()
                        if plugin.enabled and regime in plugin.supported_regimes
                    ),
                    key=lambda plugin: plugin.plugin_id,
                )
            )

    @property
    def plugin_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._plugins))

    @property
    def fingerprint(self) -> str:
        """Hash the current plugin/configuration set, including implementation parameters."""
        with self._lock:
            payload = [
                self._plugins[plugin_id].manifest()
                for plugin_id in sorted(self._plugins)
            ]
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


def build_baseline_registry() -> StrategyPluginRegistry:
    """Create transparent built-in routing candidates; these are hypotheses, not winners."""
    from evoryn.strategies.baseline import (
        FlatBaseline,
        SmaCrossoverBaseline,
        SmaTrendBaseline,
    )

    crossover = SmaCrossoverBaseline()
    trend = SmaTrendBaseline()
    flat = FlatBaseline()
    return StrategyPluginRegistry(
        plugins=(
            StrategyPluginRegistration(
                plugin_id=crossover.strategy_id,
                plugin_version=crossover.strategy_version,
                description="Fast/slow SMA crossover; bull-trend research candidate.",
                strategy=crossover,
                supported_regimes=frozenset({RegimeLabel.BULL_TREND}),
            ),
            StrategyPluginRegistration(
                plugin_id=trend.strategy_id,
                plugin_version=trend.strategy_version,
                description="Close-versus-SMA trend baseline; range-regime comparator.",
                strategy=trend,
                supported_regimes=frozenset({RegimeLabel.RANGE_BOUND}),
            ),
            StrategyPluginRegistration(
                plugin_id=flat.strategy_id,
                plugin_version=flat.strategy_version,
                description="Always-flat control for bear, high-volatility or defensive regimes.",
                strategy=flat,
                supported_regimes=frozenset(
                    {RegimeLabel.BEAR_TREND, RegimeLabel.HIGH_VOLATILITY}
                ),
            ),
        )
    )
