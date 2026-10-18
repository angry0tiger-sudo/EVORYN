"""Nobitex Margin unit tests using synthetic payloads only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from evoryn.margin.adapter import NobitexMarginAdapter
from evoryn.margin.models import (
    DelegationLimit,
    DelegationLimitSnapshot,
    MarginHealth,
    MarginMarketSettings,
    MarginMode,
    MarginOpenIntent,
    MarginOrderDisposition,
    MarginPosition,
    MarginPositionStatus,
    MarginSide,
)
from evoryn.margin.risk import MarginCalculator, MarginReconciler, MarginRiskPolicy


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def settings(
    *,
    buy_enabled: bool = True,
    sell_enabled: bool = True,
    observed_at: datetime = NOW,
) -> MarginMarketSettings:
    return MarginMarketSettings(
        market_symbol="BTCUSDT",
        src_currency="btc",
        dst_currency="usdt",
        position_fee_rate=Decimal("0.001"),
        max_leverage=Decimal("5"),
        buy_enabled=buy_enabled,
        sell_enabled=sell_enabled,
        observed_at=observed_at,
    )


def position(
    *,
    side: MarginSide = MarginSide.BUY,
    status: MarginPositionStatus = MarginPositionStatus.OPEN,
    margin_mode: MarginMode = MarginMode.ISOLATED,
    margin_ratio: Decimal | None = Decimal("2"),
    mark_price: Decimal | None = Decimal("100"),
    liquidation_price: Decimal | None = Decimal("80"),
    observed_at: datetime = NOW,
    collateral: str = "200",
) -> MarginPosition:
    return MarginPosition(
        position_id=128,
        market_symbol="BTCUSDT",
        src_currency="BTC",
        dst_currency="USDT",
        side=side,
        status=status,
        margin_mode=margin_mode,
        collateral=Decimal(collateral),
        leverage=Decimal("2"),
        observed_at=observed_at,
        created_at=NOW,
        opened_at=NOW,
        liquidation_price=liquidation_price,
        entry_price=Decimal("100"),
        delegated_amount=Decimal("2"),
        liability=Decimal("2.001"),
        total_asset=Decimal("400"),
        margin_ratio=margin_ratio,
        liability_in_order=Decimal("0"),
        asset_in_order=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        expiration_date=NOW.date() + timedelta(days=10),
        extension_fee=Decimal("0.2"),
        mark_price=mark_price,
    )


def intent(
    *,
    side: MarginSide = MarginSide.BUY,
    amount: str = "2",
    price: str = "100",
    leverage: str = "2",
    observed_at: datetime = NOW,
) -> MarginOpenIntent:
    return MarginOpenIntent(
        market_symbol="BTCUSDT",
        side=side,
        amount=Decimal(amount),
        price=Decimal(price),
        leverage=Decimal(leverage),
        observed_at=observed_at,
    )


def test_market_settings_parse_exact_fields_without_defaulting() -> None:
    normalized = NobitexMarginAdapter.normalize_market_settings(
        "BTCUSDT",
        {
            "srcCurrency": "btc",
            "dstCurrency": "usdt",
            "positionFeeRate": "0.001",
            "maxLeverage": "5",
            "buyEnabled": True,
            "sellEnabled": True,
        },
        observed_at=NOW,
    )

    assert normalized.position_fee_rate == Decimal("0.001")
    assert normalized.max_leverage == Decimal("5")
    assert normalized.buy_enabled
    assert normalized.sell_enabled


def test_market_settings_missing_capability_fails_closed() -> None:
    with pytest.raises(ValueError, match="buyEnabled"):
        NobitexMarginAdapter.normalize_market_settings(
            "BTCUSDT",
            {
                "srcCurrency": "btc",
                "dstCurrency": "usdt",
                "positionFeeRate": "0.001",
                "maxLeverage": "5",
                "sellEnabled": True,
            },
            observed_at=NOW,
        )


def test_position_parser_normalizes_open_margin_record() -> None:
    normalized = NobitexMarginAdapter.normalize_position(
        {
            "id": 128,
            "createdAt": "2026-10-18T12:00:00+00:00",
            "srcCurrency": "btc",
            "dstCurrency": "usdt",
            "side": "sell",
            "status": "Open",
            "marginType": "Isolated Margin",
            "collateral": "200",
            "leverage": "2",
            "liquidationPrice": "120",
            "entryPrice": "100",
            "exitPrice": None,
            "delegatedAmount": "2",
            "liability": "2.001",
            "totalAsset": "400",
            "marginRatio": "2",
            "liabilityInOrder": "0",
            "assetInOrder": "0",
            "unrealizedPNL": "-1.25",
            "expirationDate": "2026-10-28",
            "extensionFee": "0.2",
            "markPrice": "100",
        },
        observed_at=NOW,
        market_symbol="BTCUSDT",
    )

    assert normalized.market_symbol == "BTCUSDT"
    assert normalized.side is MarginSide.SELL
    assert normalized.margin_mode is MarginMode.ISOLATED
    assert normalized.liquidation_price == Decimal("120")
    assert normalized.unrealized_pnl == Decimal("-1.25")
    assert normalized.expiration_date == NOW.date() + timedelta(days=10)


def test_float_payload_is_rejected_to_preserve_decimal_precision() -> None:
    with pytest.raises(ValueError, match="float"):
        NobitexMarginAdapter.normalize_market_settings(
            "BTCUSDT",
            {
                "srcCurrency": "btc",
                "dstCurrency": "usdt",
                "positionFeeRate": 0.001,
                "maxLeverage": "5",
                "buyEnabled": True,
                "sellEnabled": True,
            },
            observed_at=NOW,
        )


def test_delegation_limits_retain_distinct_buy_quote_and_sell_base_values() -> None:
    limits = NobitexMarginAdapter.normalize_delegation_limits(
        {
            "limits": {
                "buy": [{"leverage": "2", "limit": "2500"}],
                "sell": [{"leverage": "2", "limit": "0.042"}],
            }
        },
        market_symbol="BTCUSDT",
        observed_at=NOW,
    )

    assert limits.market_symbol == "BTCUSDT"
    assert limits.observed_at == NOW
    assert limits.limits == (
        DelegationLimit(MarginSide.BUY, Decimal("2"), Decimal("2500")),
        DelegationLimit(MarginSide.SELL, Decimal("2"), Decimal("0.042")),
    )


def test_initial_collateral_estimate_and_extension_fee_math() -> None:
    assert MarginCalculator.estimate_initial_collateral(
        Decimal("1000"), Decimal("5")
    ) == Decimal("200")
    assert MarginCalculator.estimate_extension_fee(
        Decimal("2"), Decimal("100"), Decimal("0.001"), Decimal("3")
    ) == Decimal("0.600")
    assert MarginCalculator.liability_value_in_quote(
        Decimal("2.5"), Decimal("100")
    ) == Decimal("250.0")


@pytest.mark.parametrize(
    ("side", "mark", "liquidation", "crossed"),
    [
        (MarginSide.BUY, "79", "80", True),
        (MarginSide.BUY, "81", "80", False),
        (MarginSide.SELL, "121", "120", True),
        (MarginSide.SELL, "119", "120", False),
    ],
)
def test_liquidation_threshold_scenarios(
    side: MarginSide, mark: str, liquidation: str, crossed: bool
) -> None:
    result = MarginRiskPolicy().assess_position(
        position(
            side=side,
            mark_price=Decimal(mark),
            liquidation_price=Decimal(liquidation),
        ),
        now=NOW,
    )
    if crossed:
        assert result.health is MarginHealth.LIQUIDATION_THRESHOLD_CROSSED
        assert not result.new_risk_permitted
    else:
        assert result.health is MarginHealth.HEALTHY
        assert result.new_risk_permitted


def test_margin_ratio_at_minimum_trips_at_risk_even_before_threshold_crossing() -> None:
    result = MarginRiskPolicy().assess_position(
        position(margin_ratio=Decimal("1.1"), mark_price=Decimal("90")),
        now=NOW,
    )

    assert result.health is MarginHealth.AT_RISK
    assert result.reason_code == "maintenance_margin_ratio_breached"
    assert not result.new_risk_permitted


def test_stale_snapshot_unknown_liquidation_price_and_unsupported_cross_fail_closed() -> None:
    policy = MarginRiskPolicy(max_data_age_seconds=30)

    stale = policy.assess_position(
        position(observed_at=NOW - timedelta(seconds=31)), now=NOW
    )
    missing = policy.assess_position(
        position(liquidation_price=None, margin_ratio=Decimal("1.5")), now=NOW
    )
    cross = policy.assess_position(
        position(margin_mode=MarginMode.CROSS), now=NOW
    )

    assert stale.health is MarginHealth.UNKNOWN
    assert stale.reason_code == "stale_margin_position"
    assert missing.health is MarginHealth.UNKNOWN
    assert missing.reason_code == "liquidation_price_missing"
    assert cross.health is MarginHealth.UNSUPPORTED_MODE


def test_reported_liquidated_closed_expired_and_unknown_statuses_are_not_reinterpreted() -> None:
    policy = MarginRiskPolicy()

    liquidated = policy.assess_position(
        position(status=MarginPositionStatus.LIQUIDATED), now=NOW
    )
    closed = policy.assess_position(
        position(status=MarginPositionStatus.CLOSED), now=NOW
    )
    expired = policy.assess_position(
        position(status=MarginPositionStatus.EXPIRED), now=NOW
    )
    unknown = policy.assess_position(
        position(status=MarginPositionStatus.UNKNOWN), now=NOW
    )

    assert liquidated.health is MarginHealth.LIQUIDATED
    assert closed.health is MarginHealth.INACTIVE
    assert expired.health is MarginHealth.UNKNOWN
    assert expired.reason_code == "expiry_settlement_pending"
    assert not expired.new_risk_permitted
    assert unknown.health is MarginHealth.UNKNOWN


def test_buy_order_uses_quote_limit_and_sell_order_uses_base_limit() -> None:
    policy = MarginRiskPolicy()
    limits = DelegationLimitSnapshot(
        market_symbol="BTCUSDT",
        observed_at=NOW,
        limits=(
            DelegationLimit(MarginSide.BUY, Decimal("2"), Decimal("250")),
            DelegationLimit(MarginSide.SELL, Decimal("2"), Decimal("0.05")),
        ),
    )

    buy_ok = policy.evaluate_open_intent(
        intent(side=MarginSide.BUY, amount="2", price="100"), settings(), limits, now=NOW
    )
    buy_rejected = policy.evaluate_open_intent(
        intent(side=MarginSide.BUY, amount="3", price="100"), settings(), limits, now=NOW
    )
    sell_ok = policy.evaluate_open_intent(
        intent(side=MarginSide.SELL, amount="0.04"), settings(), limits, now=NOW
    )
    sell_rejected = policy.evaluate_open_intent(
        intent(side=MarginSide.SELL, amount="0.06"), settings(), limits, now=NOW
    )

    assert buy_ok.disposition is MarginOrderDisposition.ALLOW
    assert buy_ok.estimated_initial_collateral == Decimal("100")
    assert buy_rejected.reason_code == "delegation_limit_exceeded"
    assert sell_ok.disposition is MarginOrderDisposition.ALLOW
    assert sell_rejected.reason_code == "delegation_limit_exceeded"


def test_disabled_direction_stale_settings_and_unknown_limit_fail_closed() -> None:
    policy = MarginRiskPolicy()
    buy_limit = DelegationLimitSnapshot(
        market_symbol="BTCUSDT",
        observed_at=NOW,
        limits=(DelegationLimit(MarginSide.BUY, Decimal("2"), Decimal("1000")),),
    )

    disabled = policy.evaluate_open_intent(
        intent(), settings(buy_enabled=False), buy_limit, now=NOW
    )
    stale = policy.evaluate_open_intent(
        intent(),
        settings(observed_at=NOW - timedelta(minutes=2)),
        buy_limit,
        now=NOW,
    )
    missing = policy.evaluate_open_intent(intent(leverage="1.5"), settings(), buy_limit, now=NOW)

    assert disabled.reason_code == "margin_side_disabled"
    assert stale.disposition is MarginOrderDisposition.HALT
    assert stale.reason_code == "stale_margin_inputs"
    assert missing.disposition is MarginOrderDisposition.HALT
    assert missing.reason_code == "delegation_limit_unknown"


def test_delegation_limit_snapshot_must_be_fresh_and_same_market() -> None:
    policy = MarginRiskPolicy()
    fresh_settings = settings()
    valid_intent = intent()
    current_limit = DelegationLimit(
        MarginSide.BUY, Decimal("2"), Decimal("1000")
    )
    stale_limits = DelegationLimitSnapshot(
        market_symbol="BTCUSDT",
        observed_at=NOW - timedelta(seconds=31),
        limits=(current_limit,),
    )
    wrong_market_limits = DelegationLimitSnapshot(
        market_symbol="ETHUSDT",
        observed_at=NOW,
        limits=(current_limit,),
    )

    stale = policy.evaluate_open_intent(
        valid_intent, fresh_settings, stale_limits, now=NOW
    )
    wrong_market = policy.evaluate_open_intent(
        valid_intent, fresh_settings, wrong_market_limits, now=NOW
    )

    assert stale.disposition is MarginOrderDisposition.HALT
    assert stale.reason_code == "stale_margin_inputs"
    assert wrong_market.disposition is MarginOrderDisposition.REJECT
    assert wrong_market.reason_code == "margin_market_mismatch"


def test_duplicate_side_leverage_rows_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        DelegationLimitSnapshot(
            market_symbol="BTCUSDT",
            observed_at=NOW,
            limits=(
                DelegationLimit(MarginSide.BUY, Decimal("2"), Decimal("100")),
                DelegationLimit(MarginSide.BUY, Decimal("2"), Decimal("200")),
            ),
        )


def test_margin_position_reconciliation_reports_every_material_mismatch() -> None:
    expected = position()
    observed = position(collateral="199", margin_ratio=Decimal("1.2"))

    result = MarginReconciler.compare(expected, observed)

    assert not result.matched
    assert "collateral_mismatch" in result.differences
    assert "margin_ratio_mismatch" in result.differences
    assert MarginReconciler.compare(expected, expected).matched
