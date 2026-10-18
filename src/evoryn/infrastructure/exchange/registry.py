"""Exchange plugin registry with explicit registration and Python entry-point discovery."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from re import fullmatch
from typing import cast

from evoryn.ports.contracts import ExchangePlugin, ExchangePort


@dataclass(slots=True)
class ExchangePluginRegistry:
    """Resolve exchange adapters by plugin name without core exchange-specific branches."""

    _plugins: dict[str, ExchangePlugin] = field(default_factory=dict, init=False, repr=False)

    def register(self, plugin: ExchangePlugin) -> None:
        """Register one plugin instance, rejecting invalid or duplicate identifiers."""
        name = plugin.name.strip().lower()
        if fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) is None:
            raise ValueError("Exchange plugin name must be a lowercase identifier.")
        if name in self._plugins:
            raise ValueError(f"Exchange plugin '{name}' is already registered.")
        self._plugins[name] = plugin

    def discover_installed(self) -> tuple[str, ...]:
        """Load installed plugins from the 'evoryn.exchanges' entry-point group.

        Each entry point must resolve to a no-argument callable that creates an
        ExchangePlugin instance. Loading a plugin executes installed Python code,
        so only install reviewed and trusted plugin distributions.
        """
        discovered: list[str] = []
        for entry_point in entry_points(group="evoryn.exchanges"):
            factory = cast(Callable[[], ExchangePlugin], entry_point.load())
            plugin = factory()
            self.register(plugin)
            discovered.append(plugin.name.strip().lower())
        return tuple(discovered)

    def available_plugins(self) -> tuple[str, ...]:
        """Return registered plugin identifiers in stable alphabetical order."""
        return tuple(sorted(self._plugins))

    def create(
        self,
        name: str,
        config: Mapping[str, str] | None = None,
    ) -> ExchangePort:
        """Instantiate a registered provider adapter from an explicit configuration map."""
        normalized = name.strip().lower()
        plugin = self._plugins.get(normalized)
        if plugin is None:
            raise KeyError(f"Exchange plugin '{normalized}' is not registered.")
        return plugin.build_adapter(dict(config or {}))
