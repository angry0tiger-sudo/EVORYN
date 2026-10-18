"""Unit tests for entry-point based multi-exchange plugin discovery."""

from __future__ import annotations

import pytest

from evoryn.adapters.exchanges.nobitex import NobitexExchangePlugin
from evoryn.domain.models import ExchangeCapabilities
from evoryn.infrastructure.exchange.registry import ExchangePluginRegistry
from evoryn.ports.contracts import ExchangePort


def test_installed_nobitex_plugin_is_discovered_without_core_branching() -> None:
    """The setuptools entry point makes the Nobitex adapter discoverable by name."""
    registry = ExchangePluginRegistry()
    discovered = registry.discover_installed()

    assert "nobitex" in discovered
    assert "nobitex" in registry.available_plugins()

    adapter = registry.create(
        "NOBITEX",
        {"ENABLE_REAL_TRADING": "False", "NOBITEX_API_TOKEN": ""},
    )
    assert isinstance(adapter, ExchangePort)
    assert adapter.get_capabilities().order_write is False


def test_duplicate_plugin_registration_is_rejected() -> None:
    """A second plugin cannot silently shadow the registered provider."""
    registry = ExchangePluginRegistry()
    plugin = NobitexExchangePlugin()
    registry.register(plugin)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(plugin)


def test_plugin_name_must_be_a_safe_identifier() -> None:
    """Plugin names are identifiers, not import expressions or arbitrary paths."""
    registry = ExchangePluginRegistry()

    class InvalidNamePlugin:
        """Minimal invalid plugin object for registration validation."""

        name = "../nobitex"

        def build_adapter(self, config: dict[str, str]) -> ExchangePort:
            """No adapter is constructed by this invalid test plugin."""
            del config
            raise AssertionError("Invalid plugin must never be invoked.")

    with pytest.raises(ValueError, match="lowercase identifier"):
        registry.register(InvalidNamePlugin())  # type: ignore[arg-type]


def test_unknown_plugin_fails_explicitly() -> None:
    """An absent exchange plugin is not silently replaced with a default provider."""
    registry = ExchangePluginRegistry()

    with pytest.raises(KeyError, match="not registered"):
        registry.create("unknown-exchange")

