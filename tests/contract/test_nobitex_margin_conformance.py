"""Offline conformance tests for Nobitex's documented Spot/Margin boundaries.

These tests use synthetic snapshots only. They do not call Nobitex endpoints, create
orders, change collateral, or use exchange credentials.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from evoryn.adapters.exchanges.nobitex import NobitexExchangePlugin
from evoryn.domain.models import Market, OrderRequest, OrderType, Side
from evoryn.margin.adapter import NobitexMarginAdapter
from evoryn.margin.models import (
    DelegationLimit,
    DelegationLimitSnapshot,
    MarginHealth,
    MarginLiquidityPoolSnapshot,
    MarginLiquidityPoolStatus,
    MarginMarketSettings,
    MarginMode,
    MarginOpenIntent,
    MarginOrderDisposition,
    MarginPosition,
    MarginPositionStatus,
    MarginSide,
)
from evoryn.margin.risk import MarginRiskPolicy
from evoryn.ports.exchange_errors import TradingDisabledError, UnsupportedExchangeCapability

NOW = datetime(2026, 10, 19, 13, tzinfo=UTC)


def market_settings(*, buy_enabled: bool = True, max_leverage: str = "5") -> MarginMarketSettings:
    return MarginMarketSettings(
        market_symbol="BTCUSDT",
        src_currency="btc",
        dst_currency="usdt",
        position_fee_rate=Decimal("0.001"),
        max_leverage=Decimal(max_leverage),
        buy_enabled=buy_enabled,
        sell_enabled=True,
        observed_at=NOW,
    )


def delegation_limits() -> DelegationLimitSnapshot:
    return DelegationLimitSnapshot(
        market_symbol="BTCUSDT",
        observed_at=NOW,
        limits=(
            DelegationLimit(MarginSide.BUY, Decimal("2"), Decimal("250")),
            DelegationLimit(MarginSide.SELL, Decimal("2"), Decimal("0.05")),
        ),
    )


def active_pool(
    *, capacity: str = "100", filled: str = "0", observed_at: datetime = NOW
) -> MarginLiquidityPoolSnapshot:
    return MarginLiquidityPoolSnapshot(
        currency="btc",
        status=MarginLiquidityPoolStatus.ACTIVE,
        observed_at=observed_at,
        capacity=Decimal(capacity),
        filled_capacity=Decimal(filled),
    )


def open_intent(
    *, side: MarginSide, amount: str, price: str = "100", leverage: str = "2"
) -> MarginOpenIntent:
    return MarginOpenIntent(
        market_symbol="BTCUSDT",
        side=side,
        amount=Decimal(amount),
        price=Decimal(price),
        leverage=Decimal(leverage),
        observed_at=NOW,
    )


@pytest.mark.parametrize(
    ("side", "amount", "price", "expected"),
    [
        (MarginSide.BUY, "2.5", "100", MarginOrderDisposition.ALLOW),
        (MarginSide.BUY, "2.500001", "100", MarginOrderDisposition.REJECT),
        (MarginSide.SELL, "0.05", "100", MarginOrderDisposition.ALLOW),
        (MarginSide.SELL, "0.050001", "100", MarginOrderDisposition.REJECT),
    ],
)
def test_nobitex_delegation_caps_apply_in_the_documented_side_specific_units(
    side: MarginSide,
    amount: str,
    price: str,
    expected: MarginOrderDisposition,
) -> None:
    result = MarginRiskPolicy().evaluate_open_intent(
        open_intent(side=side, amount=amount, price=price),
        market_settings(),
        delegation_limits(),
        active_pool(),
        now=NOW,
    )

    assert result.disposition is expected
    if expected is MarginOrderDisposition.REJECT:
        assert result.reason_code == "delegation_limit_exceeded"


def test_nobitex_market_side_and_account_specific_leverage_are_not_inferred() -> None:
    policy = MarginRiskPolicy()
    disabled_buy = policy.evaluate_open_intent(
        open_intent(side=MarginSide.BUY, amount="1"),
        market_settings(buy_enabled=False),
        delegation_limits(),
        active_pool(),
        now=NOW,
    )
    leverage_above_market_max = policy.evaluate_open_intent(
        open_intent(side=MarginSide.SELL, amount="0.01", leverage="5.5"),
        market_settings(),
        delegation_limits(),
        active_pool(),
        now=NOW,
    )
    missing_account_leverage_row = policy.evaluate_open_intent(
        open_intent(side=MarginSide.SELL, amount="0.01", leverage="1.5"),
        market_settings(),
        delegation_limits(),
        active_pool(),
        now=NOW,
    )

    assert disabled_buy.disposition is MarginOrderDisposition.REJECT
    assert disabled_buy.reason_code == "margin_side_disabled"
    assert leverage_above_market_max.reason_code == "leverage_exceeds_market_limit"
    assert missing_account_leverage_row.disposition is MarginOrderDisposition.HALT
    assert missing_account_leverage_row.reason_code == "delegation_limit_unknown"


def test_unknown_or_stale_source_data_never_authorizes_margin_opening() -> None:
    policy = MarginRiskPolicy()
    limits = delegation_limits()
    proposed = open_intent(side=MarginSide.BUY, amount="1")
    unknown_pool = MarginLiquidityPoolSnapshot(
        currency="btc",
        status=MarginLiquidityPoolStatus.UNKNOWN,
        observed_at=NOW,
    )
    stale_pool = active_pool(observed_at=NOW - timedelta(seconds=31))

    unknown = policy.evaluate_open_intent(
        proposed, market_settings(), limits, unknown_pool, now=NOW
    )
    stale = policy.evaluate_open_intent(
        proposed, market_settings(), limits, stale_pool, now=NOW
    )

    assert unknown.disposition is MarginOrderDisposition.HALT
    assert unknown.reason_code == "margin_pool_state_unknown"
    assert stale.disposition is MarginOrderDisposition.HALT
    assert stale.reason_code == "stale_margin_inputs"


def test_healthy_position_assessment_is_not_an_additional_risk_authorization() -> None:
    position = MarginPosition(
        position_id=128,
        market_symbol="BTCUSDT",
        src_currency="BTC",
        dst_currency="USDT",
        side=MarginSide.BUY,
        status=MarginPositionStatus.OPEN,
        margin_mode=MarginMode.ISOLATED,
        collateral=Decimal("200"),
        leverage=Decimal("2"),
        observed_at=NOW,
        liquidation_price=Decimal("80"),
        entry_price=Decimal("100"),
        delegated_amount=Decimal("2"),
        liability=Decimal("2.001"),
        total_asset=Decimal("400"),
        margin_ratio=Decimal("2"),
        mark_price=Decimal("100"),
    )

    result = MarginRiskPolicy().assess_position(position, now=NOW)

    assert result.health is MarginHealth.HEALTHY
    assert not result.new_risk_permitted


def test_spot_exchange_plugin_does_not_accept_margin_market_data_or_writes() -> None:
    """The current ExchangePort plugin stays Spot-read-only; Margin writes are absent."""
    plugin = NobitexExchangePlugin()
    adapter = plugin.build_adapter({"ENABLE_REAL_TRADING": "False"})

    margin_market = Market(symbol="BTCUSDT", venue="nobitex", market_type="margin")
    with pytest.raises(UnsupportedExchangeCapability):
        adapter.get_market_candles(
            margin_market,
            interval=timedelta(minutes=1),
            start=NOW - timedelta(minutes=1),
            end=NOW,
        )

    spot_market = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
    request = OrderRequest(
        client_order_id="phase21b-offline-test",
        market=spot_market,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        price=Decimal("100"),
    )
    with pytest.raises(TradingDisabledError):
        adapter.submit_order(request)
    with pytest.raises(TradingDisabledError):
        adapter.cancel_order("offline-test-order")


def test_margin_payload_adapter_is_normalization_only() -> None:
    """Margin normalization exposes no HTTP client or operation-writing surface."""
    adapter = NobitexMarginAdapter()

    assert callable(adapter.normalize_market_settings)
    assert callable(adapter.normalize_delegation_limits)
    assert callable(adapter.normalize_liquidity_pool)
    assert callable(adapter.normalize_position)
    assert not hasattr(adapter, "request")
    assert not hasattr(adapter, "submit_order")
    assert not hasattr(adapter, "close_position")
