"""Tests for the default USDT-only runtime market policy."""
from __future__ import annotations

import pytest

from evoryn.config import MarketPolicy, NobitexReadOnlyConfig
from evoryn.domain.models import Market


@pytest.mark.parametrize(
    "symbol",
    ["BTC-USDT", "ETH/USDT", "SOL_USDT", "SHIBUSDT"],
)
def test_market_policy_accepts_usdt_quote_symbols(symbol: str) -> None:
    policy = MarketPolicy()
    assert policy.allows_market(Market(symbol, "nobitex", "spot"))


@pytest.mark.parametrize(
    "symbol",
    ["BTC-IRT", "ETH-BTC", "USDT-IRT", "BTC", "BTC/USDT/IRT"],
)
def test_market_policy_rejects_non_usdt_and_ambiguous_symbols(symbol: str) -> None:
    policy = MarketPolicy()
    assert not policy.allows_market(Market(symbol, "nobitex", "spot"))


def test_market_policy_rejects_other_venues_and_market_types() -> None:
    policy = MarketPolicy()
    assert policy.rejection_reason(Market("BTC-USDT", "other", "spot")) == (
        "venue_not_allowed"
    )
    assert policy.rejection_reason(Market("BTC-USDT", "nobitex", "margin")) == (
        "market_type_not_allowed"
    )


def test_runtime_configuration_cannot_expand_the_reviewed_quote_whitelist() -> None:
    with pytest.raises(ValueError, match="Unsupported quote currency"):
        MarketPolicy.from_mapping({"EVORYN_ALLOWED_QUOTE_CURRENCIES": "USDT,IRT"})
    with pytest.raises(ValueError, match="Unsupported quote currency"):
        MarketPolicy.from_mapping({"EVORYN_ALLOWED_QUOTE_CURRENCIES": "BTC"})


def test_empty_quote_whitelist_fails_closed_by_disabling_all_markets() -> None:
    policy = MarketPolicy.from_mapping({"EVORYN_ALLOWED_QUOTE_CURRENCIES": ""})
    assert not policy.allows_market(Market("BTC-USDT", "nobitex", "spot"))


def test_legacy_general_purpose_token_name_is_rejected_without_echoing_value() -> None:
    with pytest.raises(ValueError, match="NOBITEX_READONLY_API_TOKEN"):
        NobitexReadOnlyConfig.from_mapping({"NOBITEX_API_TOKEN": "dummy-test-token"})


def test_readonly_token_is_excluded_from_configuration_repr() -> None:
    config = NobitexReadOnlyConfig.from_mapping(
        {"NOBITEX_READONLY_API_TOKEN": "dummy-test-token"}
    )
    assert "dummy-test-token" not in repr(config)



def test_blank_optional_transport_settings_use_safe_official_defaults() -> None:
    config = NobitexReadOnlyConfig.from_mapping(
        {
            "NOBITEX_BASE_URL": "",
            "NOBITEX_TIMEOUT_SECONDS": "",
            "NOBITEX_READONLY_API_TOKEN": "",
        }
    )

    assert config.base_url == "https://apiv2.nobitex.ir"
    assert config.timeout_seconds == 10.0
    assert config.read_only_api_token is None
