"""Phase 21 conformance checks for declared capability boundaries."""
from __future__ import annotations

from evoryn.adapters.exchanges.nobitex import NobitexExchangePlugin
from evoryn.domain.market_capabilities import (
    CapabilityFeature,
    CapabilityMatrix,
    CapabilityStatus,
    MarketType,
)
from evoryn.ports.contracts import ExchangePort


def test_nobitex_plugin_still_conforms_to_existing_exchange_port() -> None:
    """The new capability model does not relax the established plugin interface."""
    adapter = NobitexExchangePlugin().build_adapter(
        {"ENABLE_REAL_TRADING": "False", "NOBITEX_API_TOKEN": ""}
    )

    assert isinstance(adapter, ExchangePort)
    legacy = adapter.get_capabilities()
    assert legacy.venue == "nobitex"
    assert legacy.order_write is False
    assert legacy.order_submit is False
    assert legacy.order_cancel is False


def test_legacy_spot_capability_does_not_imply_futures_support() -> None:
    """Legacy broad booleans cannot establish a market-specific Futures capability."""
    matrix = CapabilityMatrix(entries=())

    assert (
        matrix.status_for(
            "nobitex", MarketType.FUTURES, CapabilityFeature.MARKET_DATA_READ
        )
        is CapabilityStatus.UNKNOWN
    )
