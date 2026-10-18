"""Unit tests for deterministic portfolio risk enforcement."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from evoryn.domain.models import Market, MarketSnapshot, OrderRequest, OrderType, Side
from evoryn.risk.engine import RiskEngine
from evoryn.risk.models import PortfolioSnapshot, RiskAction, RiskLimits


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")


def market_snapshot(
    *,
    observed_at: datetime = NOW,
    values: dict[str, Decimal] | None = None,
    quality_flags: tuple[str, ...] = (),
) -> MarketSnapshot:
    """Build a fully synthetic top-of-book record; no exchange call is made."""
    return MarketSnapshot(
        market=MARKET,
        observed_at=observed_at,
        source="unit-test-fixture",
        values=values
        or {
            "bid": Decimal("99"),
            "ask": Decimal("101"),
            "last": Decimal("100"),
            "bid_size": Decimal("10"),
            "ask_size": Decimal("10"),
        },
        quality_flags=quality_flags,
    )


def portfolio(
    *,
    free_capital: Decimal = Decimal("10000"),
    total_exposure: Decimal = Decimal("0"),
    market_exposure: Decimal = Decimal("0"),
    position_quantity: Decimal = Decimal("0"),
    reserved_buy_notional: Decimal = Decimal("0"),
    reserved_sell_quantity: Decimal = Decimal("0"),
    realized_pnl: Decimal = Decimal("0"),
    unrealized_pnl: Decimal = Decimal("0"),
    account_reconciled: bool | None = True,
    execution_error: bool | None = False,
) -> PortfolioSnapshot:
    """Return a safe synthetic ledger that tests can override one field at a time."""
    return PortfolioSnapshot(
        free_capital=free_capital,
        total_exposure=total_exposure,
        market_exposure=market_exposure,
        position_quantity=position_quantity,
        reserved_buy_notional=reserved_buy_notional,
        reserved_sell_quantity=reserved_sell_quantity,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        account_reconciled=account_reconciled,
        execution_error=execution_error,
    )


def request(quantity: str = "0.1") -> OrderRequest:
    """Create a validated Spot market buy request."""
    return OrderRequest(
        client_order_id="risk-test-1",
        market=MARKET,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal(quantity),
    )


def engine() -> RiskEngine:
    """Return deliberately generous limits unless a test targets a particular cap."""
    return RiskEngine(
        RiskLimits(
            max_total_exposure=Decimal("5000"),
            max_market_exposure=Decimal("2500"),
            max_order_notional=Decimal("1000"),
            max_loss=Decimal("250"),
            max_market_data_age_seconds=10,
        )
    )


def test_order_inside_every_hard_limit_is_allowed() -> None:
    """Normal, fresh and reconciled input returns the ALLOW action."""
    result = engine().evaluate(request(), portfolio(), market_snapshot(), now=NOW)

    assert result.action is RiskAction.ALLOW
    assert result.approved_quantity == Decimal("0.1")


def test_free_capital_and_exposure_caps_reduce_quantity() -> None:
    """The engine calculates the tightest permitted quantity instead of overrunning."""
    risk = RiskEngine(
        RiskLimits(
            max_total_exposure=Decimal("500"),
            max_market_exposure=Decimal("500"),
            max_order_notional=Decimal("1000"),
            max_loss=Decimal("250"),
        )
    )
    context = portfolio(free_capital=Decimal("30"))

    result = risk.evaluate(request("1"), context, market_snapshot(), now=NOW)

    assert result.action is RiskAction.REDUCE
    assert result.approved_quantity is not None
    assert result.approved_quantity < Decimal("1")
    assert result.approved_quantity * Decimal("101") <= Decimal("30")


def test_sell_quantity_is_capped_to_unreserved_spot_position() -> None:
    """Risk checks forbid an unintended short position in the Spot paper account."""
    sell = OrderRequest(
        client_order_id="sell-test",
        market=MARKET,
        side=Side.SELL,
        order_type=OrderType.MARKET,
        quantity=Decimal("2"),
    )
    risk = engine()

    rejected = risk.evaluate(sell, portfolio(), market_snapshot(), now=NOW)
    reduced = risk.evaluate(
        sell,
        portfolio(
            position_quantity=Decimal("0.5"),
            reserved_sell_quantity=Decimal("0.1"),
        ),
        market_snapshot(),
        now=NOW,
    )

    assert rejected.action is RiskAction.REJECT
    assert reduced.action is RiskAction.REDUCE
    assert reduced.approved_quantity == Decimal("0.4")


def test_stale_or_quality_flagged_data_latches_halt() -> None:
    """Stale/flagged market data halts both this and every later evaluation."""
    risk = engine()
    stale = market_snapshot(observed_at=NOW - timedelta(seconds=11))

    first = risk.evaluate(request(), portfolio(), stale, now=NOW)
    second = risk.evaluate(request(), portfolio(), market_snapshot(), now=NOW)

    assert first.action is RiskAction.HALT
    assert first.reason_code == "stale_market_data"
    assert second.action is RiskAction.HALT
    assert risk.is_halted


def test_account_mismatch_execution_error_and_max_loss_auto_halt() -> None:
    """Each critical account/exec/PnL condition independently trips Auto-Halt."""
    for context, expected in (
        (portfolio(account_reconciled=False), "account_mismatch"),
        (portfolio(account_reconciled=None), "account_state_unknown"),
        (portfolio(execution_error=True), "execution_error"),
        (portfolio(execution_error=None), "execution_status_unknown"),
        (portfolio(realized_pnl=Decimal("-250")), "max_loss_exceeded"),
    ):
        risk = engine()
        result = risk.evaluate(request(), context, market_snapshot(), now=NOW)
        assert result.action is RiskAction.HALT
        assert result.reason_code == expected
        assert risk.is_halted


def test_missing_required_market_value_fails_closed() -> None:
    """An incomplete snapshot cannot be interpreted as a zero price or zero risk."""
    risk = engine()
    incomplete = market_snapshot(values={"bid": Decimal("99")})

    result = risk.evaluate(request(), portfolio(), incomplete, now=NOW)

    assert result.action is RiskAction.HALT
    assert result.reason_code == "invalid_market_value"
    assert risk.is_halted


def test_manual_kill_switch_requires_explicit_operator_acknowledgement() -> None:
    """Manual stop is latched and cannot be cleared by ordinary order evaluation."""
    risk = engine()
    risk.activate_kill_switch("operator_stop")

    stopped = risk.evaluate(request(), portfolio(), market_snapshot(), now=NOW)
    assert stopped.action is RiskAction.HALT

    try:
        risk.clear_halt(operator_acknowledged=False)
    except PermissionError:
        pass
    else:
        raise AssertionError("Unacknowledged kill-switch clear must fail.")

    risk.clear_halt(operator_acknowledged=True)
    assert not risk.is_halted


def test_total_and_market_exposure_each_reduce_order_size() -> None:
    """A pre-existing position at either exposure cap leaves only residual capacity."""
    risk = RiskEngine(
        RiskLimits(
            max_total_exposure=Decimal("100"),
            max_market_exposure=Decimal("100"),
            max_order_notional=Decimal("1000"),
            max_loss=Decimal("250"),
        )
    )

    total_limited = risk.evaluate(
        request("0.1"),
        portfolio(total_exposure=Decimal("95")),
        market_snapshot(),
        now=NOW,
    )
    market_limited = risk.evaluate(
        request("0.1"),
        portfolio(market_exposure=Decimal("95")),
        market_snapshot(),
        now=NOW,
    )

    assert total_limited.action is RiskAction.REDUCE
    assert total_limited.approved_quantity is not None
    assert total_limited.approved_quantity * Decimal("101") <= Decimal("5")
    assert market_limited.action is RiskAction.REDUCE
    assert market_limited.approved_quantity is not None
    assert market_limited.approved_quantity * Decimal("101") <= Decimal("5")


def test_risk_action_vocabulary_is_exactly_four_uppercase_values() -> None:
    """No advisory, unknown, or lowercase disposition may enter the RiskPort."""
    assert {action.value for action in RiskAction} == {
        "ALLOW",
        "REDUCE",
        "REJECT",
        "HALT",
    }
