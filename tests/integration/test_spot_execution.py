"""End-to-end Phase 10 checks using a simulated broker and in-memory mock venue only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from evoryn.domain.models import (
    Market,
    MarketSnapshot,
    OrderRequest,
    OrderType,
    Side,
)
from evoryn.execution.spot import (
    ExecutionLedger,
    ExecutionState,
    MockSpotExchangeAdapter,
    SpotExecutionService,
    SpotIntent,
)
from evoryn.paper.broker import PaperBroker, PaperOrder, PaperOrderStatus
from evoryn.paper.failure_injection import FailureInjector, FailureMode
from evoryn.risk.engine import RiskEngine
from evoryn.risk.models import RiskAction, RiskLimits

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
INITIAL_CASH = Decimal("10000")


class MutableClock:
    """Test-only aware clock that advances explicitly, never by sleeping."""

    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


class ExplodingPaperBroker(PaperBroker):
    """Test double that proves unexpected broker errors are correlated and fail closed."""

    def submit_order(self, request: OrderRequest) -> PaperOrder:
        del request
        raise RuntimeError("synthetic broker fault")


class StaticMarketData:
    """Minimal deterministic market-data port; it never connects to a provider."""

    def __init__(self, current: MarketSnapshot) -> None:
        self.current = current
        self.read_count = 0

    def get_snapshot(self, market: Market) -> MarketSnapshot:
        self.read_count += 1
        if market != self.current.market:
            raise ValueError("Fixture market mismatch.")
        return self.current

    def get_history(
        self, market: Market, start: datetime, end: datetime
    ) -> tuple[MarketSnapshot, ...]:
        del market, start, end
        return ()


def snapshot(
    *,
    observed_at: datetime = NOW,
    bid: str = "99",
    ask: str = "101",
    bid_size: str = "10",
    ask_size: str = "10",
) -> MarketSnapshot:
    """Build a normalized synthetic book for one test."""
    return MarketSnapshot(
        market=MARKET,
        observed_at=observed_at,
        source="synthetic-integration-fixture",
        values={
            "bid": Decimal(bid),
            "ask": Decimal(ask),
            "last": (Decimal(bid) + Decimal(ask)) / Decimal("2"),
            "bid_size": Decimal(bid_size),
            "ask_size": Decimal(ask_size),
        },
    )


def build_system(
    *,
    initial_cash: Decimal = INITIAL_CASH,
    market_snapshot: MarketSnapshot | None = None,
    failures: FailureInjector | None = None,
    broker_type: type[PaperBroker] = PaperBroker,
) -> tuple[
    SpotExecutionService,
    PaperBroker,
    RiskEngine,
    MockSpotExchangeAdapter,
    ExecutionLedger,
    MutableClock,
    StaticMarketData,
]:
    """Wire the real deterministic risk gate to the paper broker and mock venue."""
    clock = MutableClock()
    feed = StaticMarketData(market_snapshot or snapshot())
    risk = RiskEngine(
        RiskLimits(
            max_total_exposure=Decimal("50000"),
            max_market_exposure=Decimal("25000"),
            max_order_notional=Decimal("5000"),
            max_loss=Decimal("1000"),
            max_market_data_age_seconds=10,
        )
    )
    broker = broker_type(
        market=MARKET,
        market_data=feed,
        risk_port=risk,
        initial_capital=initial_cash,
        failure_injector=failures,
        clock=clock,
    )
    mock_venue = MockSpotExchangeAdapter(
        base_asset="BTC",
        quote_asset="USDT",
        initial_quote_balance=initial_cash,
        clock=clock,
    )
    ledger = ExecutionLedger()
    service = SpotExecutionService(
        market=MARKET,
        base_asset="BTC",
        quote_asset="USDT",
        broker=broker,
        mock_venue=mock_venue,
        ledger=ledger,
        clock=clock,
    )
    return service, broker, risk, mock_venue, ledger, clock, feed


def intent(
    correlation_id: str,
    client_order_id: str,
    *,
    side: Side = Side.BUY,
    quantity: str = "0.1",
    order_type: OrderType = OrderType.MARKET,
    price: str | None = None,
    created_at: datetime = NOW,
) -> SpotIntent:
    """Build one validated order intent with stable identifiers."""
    return SpotIntent(
        correlation_id=correlation_id,
        request=OrderRequest(
            client_order_id=client_order_id,
            market=MARKET,
            side=side,
            order_type=order_type,
            quantity=Decimal(quantity),
            price=Decimal(price) if price is not None else None,
        ),
        created_at=created_at,
    )


def test_spot_intent_runs_risk_order_fill_balance_and_reconciliation() -> None:
    service, broker, risk, venue, ledger, _, _ = build_system()

    result = service.execute(intent("corr-buy-1", "buy-1"))

    assert result.state is ExecutionState.FILLED
    assert result.risk is not None
    assert result.risk.action is RiskAction.ALLOW
    assert result.order.filled_quantity == Decimal("0.1")
    assert result.balance.base_available == Decimal("0.1")
    assert result.balance.quote_available == Decimal("9989.9")
    assert result.reconciliation.matched
    assert result.reconciliation.compared_orders == 1
    assert venue.get_order("buy-1") is not None
    assert broker.get_order("buy-1") == result.order
    assert not risk.is_halted

    assert len(ledger.intents) == 1
    assert len(ledger.risk_decisions) == 1
    assert len(ledger.executions) == 1
    assert len(ledger.balances) == 1
    assert len(ledger.reconciliations) == 1
    assert {
        ledger.intents[0].correlation_id,
        ledger.risk_decisions[0].correlation_id,
        ledger.executions[0].correlation_id,
        ledger.balances[0].correlation_id,
        ledger.reconciliations[0].correlation_id,
    } == {"corr-buy-1"}


def test_risk_rejection_is_ledgered_and_never_creates_a_fill() -> None:
    service, broker, risk, _, ledger, _, _ = build_system()

    result = service.execute(
        intent("corr-sell-empty", "sell-empty", side=Side.SELL, quantity="0.5")
    )

    assert result.state is ExecutionState.REJECTED
    assert result.risk is not None
    assert result.risk.action is RiskAction.REJECT
    assert result.order.fills == ()
    assert result.balance.base_available == Decimal("0")
    assert result.reconciliation.matched
    assert ledger.risk_decisions[0].action is RiskAction.REJECT
    assert not risk.is_halted
    assert broker.position_quantity == Decimal("0")


def test_partial_fills_update_balances_once_and_reconcile_after_a_market_tick() -> None:
    service, broker, risk, venue, ledger, clock, _ = build_system(
        market_snapshot=snapshot(ask_size="0.2")
    )

    first = service.execute(intent("corr-partial", "partial-1", quantity="0.25"))
    assert first.state is ExecutionState.PARTIAL
    assert first.order.filled_quantity == Decimal("0.2")
    assert first.balance.quote_available == Decimal("9979.8")
    assert first.reconciliation.matched

    clock.current = NOW + timedelta(seconds=1)
    updated = snapshot(observed_at=clock.current, ask_size="0.1")
    orders, report = service.process_market_update(updated)

    order = broker.get_order("partial-1")
    assert order is not None
    assert order.status is PaperOrderStatus.FILLED
    assert order.filled_quantity == Decimal("0.25")
    assert len(order.fills) == 2
    assert report.matched
    assert venue.get_order("partial-1") is not None
    assert broker.cash == Decimal("9974.75")
    assert venue.get_balances(captured_at=clock.current).quote_available == Decimal(
        "9974.75"
    )
    assert not risk.is_halted
    assert len(ledger.executions) == 2
    assert len(orders) == 1


def test_unknown_submit_outcome_is_not_retried_and_latches_halt() -> None:
    failures = FailureInjector()
    failures.inject_next("submit", FailureMode.TIMEOUT)
    service, broker, risk, venue, ledger, _, _ = build_system(failures=failures)
    proposed = intent("corr-timeout", "timeout-1")

    first = service.execute(proposed)
    repeated = service.execute(proposed)

    assert first is repeated
    assert first.state is ExecutionState.UNKNOWN
    assert first.reconciliation_required
    assert not first.reconciliation.matched
    assert any("unknown_order_outcome" in item for item in first.reconciliation.differences)
    assert broker.get_order("timeout-1") is not None
    assert venue.get_order("timeout-1") is not None
    assert risk.is_halted
    assert len(ledger.intents) == 1
    assert len(ledger.executions) == 1


def test_balance_mismatch_latches_risk_halt_and_blocks_new_orders() -> None:
    service, broker, risk, venue, _, _, _ = build_system()
    service.execute(intent("corr-normal", "normal-1"))
    venue.inject_balance_drift("USDT", Decimal("1"))

    report = service.reconcile_all(correlation_id="corr-audit-mismatch")

    assert not report.matched
    assert "balance_mismatch:quote:USDT" in report.differences
    assert risk.is_halted
    blocked = service.execute(intent("corr-blocked", "blocked-1"))
    assert blocked.state is ExecutionState.REJECTED
    assert blocked.order.fills == ()
    assert broker.get_order("blocked-1") is not None


def test_missing_venue_order_is_detected_even_when_balances_match() -> None:
    service, broker, risk, venue, _, _, _ = build_system()
    service.execute(intent("corr-missing", "missing-1"))
    venue.forget_order("missing-1")

    report = service.reconcile_all(correlation_id="corr-audit-missing")

    assert not report.matched
    assert any("order_missing_from_mock_venue" in item for item in report.differences)
    assert risk.is_halted
    assert broker.get_order("missing-1") is not None


def test_unexpected_broker_error_is_recorded_and_latches_halt() -> None:
    service, _, risk, _, ledger, _, _ = build_system(
        broker_type=ExplodingPaperBroker
    )

    with pytest.raises(RuntimeError, match="synthetic broker fault"):
        service.execute(intent("corr-broker-error", "broker-error-1"))

    assert risk.is_halted
    assert len(ledger.intents) == 1
    assert len(ledger.errors) == 1
    assert ledger.errors[0].correlation_id == "corr-broker-error"
    assert ledger.errors[0].client_order_id == "broker-error-1"
    assert ledger.errors[0].component == "paper_broker"
    assert ledger.errors[0].reason_code == "spot_execution_exception"
    assert ledger.executions == ()


def test_correlation_and_client_order_ids_cannot_be_reused_for_new_payloads() -> None:
    service, _, _, _, _, _, _ = build_system()
    original = intent("corr-idempotent", "stable-client-id")
    first = service.execute(original)

    assert service.execute(original) is first
    with pytest.raises(ValueError, match="Correlation ID was reused"):
        service.execute(
            intent("corr-idempotent", "stable-client-id", quantity="0.2")
        )
    with pytest.raises(ValueError, match="another correlation ID"):
        service.execute(intent("corr-other", "stable-client-id"))
