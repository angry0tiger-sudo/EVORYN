"""Deterministic, network-free tests for the Paper Broker."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from evoryn.domain.models import Market, MarketSnapshot, OrderRequest, OrderType, Side
from evoryn.paper.broker import PaperBroker, PaperOrderStatus
from evoryn.paper.failure_injection import FailureInjector, FailureMode
from evoryn.ports.contracts import MarketDataPort
from evoryn.risk.engine import RiskEngine
from evoryn.risk.models import RiskLimits


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")


def snapshot(
    *,
    observed_at: datetime = NOW,
    bid: str = "99",
    ask: str = "101",
    last: str = "100",
    bid_size: str = "1",
    ask_size: str = "1",
) -> MarketSnapshot:
    """Create one synthetic normalized book tick."""
    return MarketSnapshot(
        market=MARKET,
        observed_at=observed_at,
        source="paper-test-fixture",
        values={
            "bid": Decimal(bid),
            "ask": Decimal(ask),
            "last": Decimal(last),
            "bid_size": Decimal(bid_size),
            "ask_size": Decimal(ask_size),
        },
    )


class StaticMarketData:
    """Minimal in-memory implementation of the Phase 5 market-data port."""

    def __init__(self, current: MarketSnapshot) -> None:
        self.current = current
        self.reads = 0

    def get_snapshot(self, market: Market) -> MarketSnapshot:
        self.reads += 1
        if market != self.current.market:
            raise ValueError("unknown test market")
        return self.current

    def get_history(
        self, market: Market, start: datetime, end: datetime
    ) -> Sequence[MarketSnapshot]:
        del market, start, end
        return (self.current,)


def build_broker(
    current: MarketSnapshot | None = None,
    *,
    capital: str = "10000",
    failures: FailureInjector | None = None,
) -> tuple[PaperBroker, StaticMarketData, RiskEngine]:
    """Build an isolated in-memory broker with no API credentials or network."""
    feed = StaticMarketData(current or snapshot())
    risk = RiskEngine(
        RiskLimits(
            max_total_exposure=Decimal("5000"),
            max_market_exposure=Decimal("2500"),
            max_order_notional=Decimal("1000"),
            max_loss=Decimal("250"),
            max_market_data_age_seconds=10,
        )
    )
    broker = PaperBroker(
        market=MARKET,
        market_data=feed,
        risk_port=risk,
        initial_capital=Decimal(capital),
        failure_injector=failures,
        clock=lambda: NOW,
    )
    return broker, feed, risk


def order(
    client_id: str,
    *,
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
    quantity: str = "0.1",
    price: str | None = None,
) -> OrderRequest:
    """Return a validated immutable request."""
    return OrderRequest(
        client_order_id=client_id,
        market=MARKET,
        side=side,
        order_type=order_type,
        quantity=Decimal(quantity),
        price=Decimal(price) if price is not None else None,
    )


def test_market_order_partially_fills_then_completes_from_new_market_data() -> None:
    """Market order fills only visible ask liquidity and accounts for every fill."""
    broker, feed, risk = build_broker(
        snapshot(ask_size="0.2", observed_at=NOW - timedelta(seconds=1))
    )
    submitted = broker.submit_order(order("market-partial", quantity="0.25"))

    assert submitted.status is PaperOrderStatus.PARTIAL
    assert submitted.filled_quantity == Decimal("0.2")
    assert len(submitted.fills) == 1
    assert submitted.fills[0].price == Decimal("101")
    assert broker.position_quantity == Decimal("0.2")
    assert broker.cash == Decimal("9979.8")

    filled = broker.process_market_update(
        snapshot(ask_size="0.1", observed_at=NOW)
    )[0]

    assert filled.status is PaperOrderStatus.FILLED
    assert filled.filled_quantity == Decimal("0.25")
    assert len(filled.fills) == 2
    assert broker.position_quantity == Decimal("0.25")
    assert broker.cash == Decimal("9974.75")
    assert not risk.is_halted
    assert feed.reads == 1


def test_limit_order_waits_then_fills_when_market_crosses_its_limit() -> None:
    """A non-marketable limit rests open and fills only when ask reaches its limit."""
    broker, _, _ = build_broker()
    resting = broker.submit_order(
        order("limit-wait", order_type=OrderType.LIMIT, quantity="0.1", price="99")
    )
    assert resting.status is PaperOrderStatus.OPEN
    assert resting.fills == ()

    crossed = broker.process_market_update(
        snapshot(bid="97", ask="98", last="98", ask_size="1")
    )[0]

    assert crossed.status is PaperOrderStatus.FILLED
    assert crossed.fills[0].price == Decimal("98")
    assert broker.position_quantity == Decimal("0.1")


def test_open_order_can_be_canceled_and_cancel_is_idempotent() -> None:
    """Cancel releases an open paper order without contacting a venue."""
    broker, _, _ = build_broker()
    resting = broker.submit_order(
        order("cancel-me", order_type=OrderType.LIMIT, quantity="0.1", price="90")
    )

    canceled = broker.cancel_order(resting.order_id)

    assert canceled.status is PaperOrderStatus.CANCELED
    assert broker.cancel_order("cancel-me").status is PaperOrderStatus.CANCELED


def test_risk_reduction_is_enforced_by_the_broker() -> None:
    """The broker uses approved size rather than the larger originally requested size."""
    broker, _, _ = build_broker(capital="10")
    reduced = broker.submit_order(order("reduce-me", quantity="0.5"))

    assert reduced.requested_quantity == Decimal("0.5")
    assert reduced.request.quantity < reduced.requested_quantity
    assert reduced.status is PaperOrderStatus.FILLED
    assert reduced.filled_quantity * Decimal("101") <= Decimal("10")


def test_timeout_injection_marks_outcome_unknown_and_latches_halt() -> None:
    """An ambiguous submit result is never retried or represented as safely rejected."""
    injector = FailureInjector()
    injector.inject_next("submit", FailureMode.TIMEOUT)
    broker, feed, risk = build_broker(failures=injector)

    unknown = broker.submit_order(order("timeout-order"))
    duplicate = broker.submit_order(order("timeout-order"))

    assert unknown.status is PaperOrderStatus.UNKNOWN
    assert duplicate.order_id == unknown.order_id
    assert duplicate.status is PaperOrderStatus.UNKNOWN
    assert feed.reads == 0
    assert risk.is_halted

    try:
        broker.submit_order(order("timeout-order", quantity="0.2"))
    except ValueError:
        pass
    else:
        raise AssertionError("Reusing a client ID with a different payload must fail.")

    risk.clear_halt(operator_acknowledged=True)
    broker.process_market_update(snapshot())
    assert risk.is_halted, "An unresolved unknown order must relatch Auto-Halt."


def test_network_failure_unknown_order_requires_manual_reconciliation() -> None:
    """A lost update latches the halt until status is reconciled and separately cleared."""
    injector = FailureInjector()
    broker, _, risk = build_broker(failures=injector)
    resting = broker.submit_order(
        order("unknown-update", order_type=OrderType.LIMIT, price="90")
    )
    assert resting.status is PaperOrderStatus.OPEN

    injector.inject_next("market_update", FailureMode.NETWORK_DISCONNECT)
    result = broker.process_market_update(snapshot())
    assert result[0].status is PaperOrderStatus.UNKNOWN
    assert result[0].last_confirmed_status is PaperOrderStatus.OPEN
    assert risk.is_halted

    try:
        broker.resolve_unknown_order(
            resting.order_id,
            confirmed_status=PaperOrderStatus.OPEN,
            operator_acknowledged=False,
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("Unknown order resolution must require operator acknowledgement.")

    try:
        broker.resolve_unknown_order(
            resting.order_id,
            confirmed_status=PaperOrderStatus.FILLED,
            operator_acknowledged=True,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("The broker must not invent missing fill records.")

    reconciled = broker.resolve_unknown_order(
        resting.order_id,
        confirmed_status=PaperOrderStatus.OPEN,
        operator_acknowledged=True,
    )
    assert reconciled.status is PaperOrderStatus.OPEN
    assert risk.is_halted

    risk.clear_halt(operator_acknowledged=True)
    assert not risk.is_halted


def test_invalid_data_injection_halts_without_filling_resting_orders() -> None:
    """Invalid market input is fail-closed and cannot trigger a simulated fill."""
    injector = FailureInjector()
    broker, _, risk = build_broker(failures=injector)
    resting = broker.submit_order(
        order("bad-data-order", order_type=OrderType.LIMIT, price="90")
    )
    assert resting.status is PaperOrderStatus.OPEN
    injector.inject_next("market_update", FailureMode.INVALID_DATA)

    result = broker.process_market_update(snapshot(ask="89", bid="88"))

    assert result[0].status is PaperOrderStatus.OPEN
    assert result[0].filled_quantity == Decimal("0")
    assert risk.is_halted


def test_malformed_market_data_is_rejected_before_order_bookkeeping() -> None:
    """Missing data halts the engine and rejects before any paper fill is recorded."""
    malformed = MarketSnapshot(
        market=MARKET,
        observed_at=NOW,
        source="bad-fixture",
        values={"bid": Decimal("99")},
    )
    broker, _, risk = build_broker(malformed)

    rejected = broker.submit_order(order("malformed-order"))

    assert rejected.status is PaperOrderStatus.REJECTED
    assert rejected.fills == ()
    assert risk.is_halted



def test_multiple_orders_share_the_same_snapshot_liquidity() -> None:
    """Two orders cannot both consume the same displayed top-of-book volume."""
    broker, _, _ = build_broker(snapshot(ask_size="1"))

    first = broker.submit_order(
        order("shared-liquidity-1", order_type=OrderType.LIMIT, quantity="0.8", price="110")
    )
    second = broker.submit_order(
        order("shared-liquidity-2", order_type=OrderType.LIMIT, quantity="0.8", price="110")
    )

    assert first.status is PaperOrderStatus.FILLED
    assert first.filled_quantity == Decimal("0.8")
    assert second.status is PaperOrderStatus.PARTIAL
    assert second.filled_quantity == Decimal("0.2")
    assert first.filled_quantity + second.filled_quantity == Decimal("1")
