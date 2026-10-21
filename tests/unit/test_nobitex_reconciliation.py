"""Offline tests for authenticated, read-only Nobitex order/fill snapshot pages."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from evoryn.adapters.exchanges.nobitex_reconciliation import (
    NobitexReconciliationReader,
    UserOrderStatus,
)
from evoryn.adapters.exchanges.nobitex_transport import NobitexHTTPTransport
from evoryn.config import MarketPolicy
from evoryn.domain.models import Market, OrderStatus, Side
from evoryn.infrastructure.exchange.rate_limit import SlidingWindowRateLimiter
from evoryn.ports.exchange_errors import (
    AuthenticationError,
    InvalidExchangeRequest,
    MalformedExchangeResponse,
    UnsupportedExchangeCapability,
)

NOW = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")


@dataclass
class FakeReconciliationTransport:
    responses: dict[str, dict[str, object]]
    calls: list[tuple[str, str, Mapping[str, str] | None, str | None]] = field(
        default_factory=list
    )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
        token: str | None = None,
    ) -> dict[str, object]:
        assert payload is None
        self.calls.append((method, path, params, token))
        response = self.responses.get(path)
        if response is None:
            raise AssertionError(f"Unexpected mocked request: {method} {path}")
        return response


def make_reader(
    transport: FakeReconciliationTransport,
    *,
    token: str | None = "mock-read-token",
) -> NobitexReconciliationReader:
    return NobitexReconciliationReader(
        transport=transport,
        token=token,
        rate_limiter=SlidingWindowRateLimiter(clock=lambda: 10.0),
        clock=lambda: NOW,
        market_policy=MarketPolicy(),
    )


def order_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 12345,
        "market": "BTC-USDT",
        "type": "buy",
        "execution": "Limit",
        "status": "Active",
        "tradeType": "Spot",
        "amount": "1.25",
        "matchedAmount": "0.25",
        "unmatchedAmount": "1.00",
        "price": "100.5",
        "averagePrice": "100.4",
        "totalPrice": "25.1",
        "fee": "0.1",
        "clientOrderId": "evoryn-001",
        "created_at": "2026-10-21T07:59:00+00:00",
    }
    row.update(overrides)
    return row


def trade_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 990,
        "orderId": 12345,
        "market": "BTC-USDT",
        "type": "buy",
        "tradeType": "Spot",
        "price": "100.4",
        "amount": "0.25",
        "total": "25.1",
        "fee": "0.1",
        "timestamp": "2026-10-21T07:59:10+00:00",
    }
    row.update(overrides)
    return row


def test_order_page_is_authenticated_scoped_and_preserves_partial_fill() -> None:
    transport = FakeReconciliationTransport({"/market/orders/list": {"status": "ok", "orders": [order_row()], "hasNext": False}})
    reader = make_reader(transport)

    page = reader.get_orders_page(MARKET, status=UserOrderStatus.OPEN, from_id=100)

    order = page.orders[0]
    assert page.has_next is False
    assert page.market == MARKET
    assert order.status is OrderStatus.OPEN
    assert order.side is Side.BUY
    assert order.quantity == Decimal("1.25")
    assert order.matched_quantity == Decimal("0.25")
    assert order.unmatched_quantity == Decimal("1.00")
    assert order.client_order_id == "evoryn-001"
    assert order.reconciliation_ready
    assert transport.calls == [
        (
            "GET",
            "/market/orders/list",
            {"srcCurrency": "btc", "dstCurrency": "usdt", "details": "2", "status": "open", "fromId": "100"},
            "mock-read-token",
        )
    ]


def test_order_page_does_not_claim_complete_when_provider_omits_has_next() -> None:
    transport = FakeReconciliationTransport({"/market/orders/list": {"status": "ok", "orders": [order_row()]}})
    page = make_reader(transport).get_orders_page(MARKET)
    assert page.has_next is None
    assert page.reconciliation_ready is False


def test_order_page_rejects_non_spot_or_mismatched_market_rows() -> None:
    for row in (order_row(tradeType="Margin"), order_row(market="ETH-USDT")):
        transport = FakeReconciliationTransport({"/market/orders/list": {"status": "ok", "orders": [row], "hasNext": False}})
        with pytest.raises(MalformedExchangeResponse):
            make_reader(transport).get_orders_page(MARKET)


def test_user_trade_page_preserves_fill_fields_and_pagination() -> None:
    transport = FakeReconciliationTransport({
        "/market/trades/list": {"status": "ok", "trades": [trade_row()], "hasNext": True}
    })
    page = make_reader(transport).get_user_trades_page(MARKET, from_id=990)
    trade = page.trades[0]

    assert page.has_next is True
    assert trade.trade_id == "990"
    assert trade.order_id == "12345"
    assert trade.quantity == Decimal("0.25")
    assert trade.price == Decimal("100.4")
    assert trade.fee == Decimal("0.1")
    assert trade.occurred_at == datetime(2026, 10, 21, 7, 59, 10, tzinfo=UTC)
    assert page.reconciliation_ready is False
    assert transport.calls[0][2] == {
        "srcCurrency": "btc", "dstCurrency": "usdt", "fromId": "990"
    }


def test_trade_page_requires_boolean_has_next() -> None:
    transport = FakeReconciliationTransport({
        "/market/trades/list": {"status": "ok", "trades": [trade_row()]}
    })
    with pytest.raises(MalformedExchangeResponse, match="boolean hasNext"):
        make_reader(transport).get_user_trades_page(MARKET)


def test_private_reader_rejects_missing_token_and_non_usdt_before_request() -> None:
    transport = FakeReconciliationTransport({})
    reader = make_reader(transport, token=None)
    with pytest.raises(AuthenticationError):
        reader.get_orders_page(MARKET)
    with pytest.raises(AuthenticationError):
        reader.get_user_trades_page(MARKET)
    non_usdt = Market(symbol="BTC-IRT", venue="nobitex", market_type="spot")
    with pytest.raises(UnsupportedExchangeCapability, match="allowlisted USDT"):
        make_reader(transport).get_user_trades_page(non_usdt)
    assert transport.calls == []


def test_transport_allows_only_scoped_private_read_routes() -> None:
    transport = NobitexHTTPTransport()
    assert transport._is_allowed_route("GET", "/market/orders/list")
    assert transport._is_allowed_route("GET", "/market/trades/list")
    assert not transport._is_allowed_route("POST", "/market/orders/list")
    assert not transport._is_allowed_route("POST", "/market/trades/list")
    with pytest.raises(AuthenticationError, match="read-only Nobitex API token"):
        transport.request(
            "GET",
            "/market/orders/list",
            params={"srcCurrency": "btc", "dstCurrency": "usdt", "details": "2", "status": "open"},
        )
    with pytest.raises(InvalidExchangeRequest, match="Spot/USDT query"):
        transport.request(
            "GET", "/market/orders/list",
            params={"srcCurrency": "btc", "dstCurrency": "irt", "details": "2", "status": "open"},
            token="mock-token",
        )
    with pytest.raises(InvalidExchangeRequest, match="Spot/USDT query"):
        transport.request(
            "GET", "/market/trades/list",
            params={"srcCurrency": "btc", "dstCurrency": "usdt", "myTradesOnly": "no"},
            token="mock-token",
        )
