"""Mock-only tests for the Nobitex adapter; these tests never call the live exchange."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from evoryn.adapters.exchanges.nobitex import NobitexExchangeAdapter, NobitexExchangePlugin
from evoryn.adapters.exchanges.nobitex_transport import NobitexHTTPTransport
from evoryn.domain.models import (
    ExchangeCapabilities,
    Market,
    OrderRequest,
    OrderType,
    Side,
)
from evoryn.ports.exchange_errors import (
    AuthenticationError,
    InvalidExchangeRequest,
    MalformedExchangeResponse,
    TradingDisabledError,
    UnsupportedExchangeCapability,
)


@dataclass
class FakeNobitexTransport:
    """In-memory response fixture recording every request and returning no real network data."""

    responses: dict[tuple[str, str], dict[str, object]]
    calls: list[
        tuple[
            str,
            str,
            Mapping[str, str] | None,
            Mapping[str, object] | None,
            str | None,
        ]
    ] = field(default_factory=list)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
        token: str | None = None,
    ) -> dict[str, object]:
        """Record a mocked request and return only an explicitly configured fixture."""
        self.calls.append((method, path, params, payload, token))
        response = self.responses.get((method, path))
        if response is None:
            raise AssertionError(f"Unexpected mocked request: {method} {path}")
        return response


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def market() -> Market:
    """Return a synthetic Spot market; no market metadata is fetched."""
    return Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")


@pytest.fixture
def empty_transport() -> FakeNobitexTransport:
    """Return a transport fixture with no configured responses."""
    return FakeNobitexTransport(responses={})


def make_adapter(
    transport: FakeNobitexTransport,
    *,
    token: str | None = "fixture-read-only-token",
) -> NobitexExchangeAdapter:
    """Create the adapter with deterministic time and local rate accounting."""
    from evoryn.infrastructure.exchange.rate_limit import SlidingWindowRateLimiter

    return NobitexExchangeAdapter(
        transport=transport,
        token=token,
        rate_limiter=SlidingWindowRateLimiter(clock=lambda: 100.0),
        clock=lambda: NOW,
    )


def test_nobitex_adapter_declares_reads_but_not_order_writes(
    empty_transport: FakeNobitexTransport,
) -> None:
    """Nobitex implements normalized reads and advertises the hard write boundary."""
    adapter = make_adapter(empty_transport)
    capabilities = adapter.get_capabilities()

    assert isinstance(capabilities, ExchangeCapabilities)
    assert capabilities.market_data
    assert capabilities.account_read
    assert capabilities.order_read
    assert capabilities.candles
    assert capabilities.order_book
    assert capabilities.recent_trades
    assert capabilities.market_stats
    assert not capabilities.order_write
    assert not capabilities.order_submit
    assert not capabilities.order_cancel


def test_candles_are_normalized_from_mock_udf_arrays(
    market: Market,
) -> None:
    """Valid mock UDF arrays become canonical candles without network access."""
    first = int((NOW - timedelta(minutes=2)).timestamp())
    second = int((NOW - timedelta(minutes=1)).timestamp())
    transport = FakeNobitexTransport(
        responses={
            ("GET", "/market/udf/history"): {
                "s": "ok",
                "t": [first, second],
                "o": ["100", "102"],
                "h": ["105", "104"],
                "l": ["99", "101"],
                "c": ["102", "103"],
                "v": ["12.5", "8"],
            }
        }
    )
    adapter = make_adapter(transport)

    candles = adapter.get_market_candles(
        market,
        interval=timedelta(minutes=1),
        start=NOW - timedelta(minutes=2),
        end=NOW,
    )

    assert len(candles) == 2
    assert candles[0].open == Decimal("100")
    assert candles[1].close == Decimal("103")
    assert candles[0].timestamp.tzinfo == UTC
    assert transport.calls[0][2] is not None
    assert transport.calls[0][2]["symbol"] == "BTCUSDT"
    assert transport.calls[0][2]["resolution"] == "1"


def test_candle_no_data_is_a_valid_empty_history_result(
    market: Market,
) -> None:
    """The official no_data UDF result maps to an empty sequence."""
    transport = FakeNobitexTransport(
        responses={("GET", "/market/udf/history"): {"s": "no_data"}}
    )
    adapter = make_adapter(transport)

    candles = adapter.get_market_candles(
        market,
        interval=timedelta(minutes=1),
        start=NOW - timedelta(minutes=2),
        end=NOW,
    )
    assert candles == ()


def test_candle_mismatched_arrays_are_rejected(market: Market) -> None:
    """Inconsistent column lengths fail instead of truncating candle rows."""
    transport = FakeNobitexTransport(
        responses={
            ("GET", "/market/udf/history"): {
                "s": "ok",
                "t": [int(NOW.timestamp())],
                "o": ["100", "101"],
                "h": ["105"],
                "l": ["99"],
                "c": ["102"],
                "v": ["1"],
            }
        }
    )
    adapter = make_adapter(transport)

    with pytest.raises(MalformedExchangeResponse, match="inconsistent lengths"):
        adapter.get_market_candles(
            market,
            interval=timedelta(minutes=1),
            start=NOW - timedelta(minutes=1),
            end=NOW,
        )


def test_order_book_normalizes_bids_asks_and_provider_timestamp(market: Market) -> None:
    """Mock order-book levels and millisecond timestamp become typed domain records."""
    transport = FakeNobitexTransport(
        responses={
            ("GET", "/v3/orderbook/BTCUSDT"): {
                "status": "ok",
                "lastUpdate": int(NOW.timestamp() * 1000),
                "lastTradePrice": "100.5",
                "bids": [["100", "2.5"], ["99", "1"]],
                "asks": [["101", "3"]],
            }
        }
    )
    snapshot = make_adapter(transport).get_order_book(market)

    assert snapshot.bids[0].price == Decimal("100")
    assert snapshot.asks[0].quantity == Decimal("3")
    assert snapshot.last_trade_price == Decimal("100.5")
    assert snapshot.observed_at == NOW


def test_malformed_order_book_level_is_rejected(market: Market) -> None:
    """Malformed levels are not quietly ignored or omitted."""
    transport = FakeNobitexTransport(
        responses={
            ("GET", "/v3/orderbook/BTCUSDT"): {
                "status": "ok",
                "lastUpdate": int(NOW.timestamp() * 1000),
                "bids": [["100"]],
                "asks": [],
            }
        }
    )
    with pytest.raises(MalformedExchangeResponse, match="exactly price and quantity"):
        make_adapter(transport).get_order_book(market)


def test_recent_trades_are_returned_chronologically(market: Market) -> None:
    """Newer-first provider results are normalized and returned oldest-to-newest."""
    older_ms = int((NOW - timedelta(seconds=2)).timestamp() * 1000)
    newer_ms = int((NOW - timedelta(seconds=1)).timestamp() * 1000)
    transport = FakeNobitexTransport(
        responses={
            ("GET", "/v2/trades/BTCUSDT"): {
                "status": "ok",
                "trades": [
                    {"time": newer_ms, "price": "102", "volume": "0.2", "type": "sell"},
                    {"time": older_ms, "price": "101", "volume": "0.1", "type": "buy"},
                ],
            }
        }
    )
    trades = make_adapter(transport).get_recent_trades(market, limit=2)

    assert len(trades) == 2
    assert trades[0].price == Decimal("101")
    assert trades[0].side is Side.BUY
    assert trades[1].side is Side.SELL
    assert trades[0].occurred_at < trades[1].occurred_at


def test_market_stats_select_explicit_base_quote_market(market: Market) -> None:
    """Provider statistics are selected using a normalized explicit pair."""
    transport = FakeNobitexTransport(
        responses={
            ("GET", "/market/stats"): {
                "status": "ok",
                "stats": {
                    "btc-usdt": {
                        "latest": "101.2",
                        "bestBuy": "101",
                        "bestSell": "102",
                        "volumeSrc": "10",
                        "volumeDst": "1012",
                        "dayLow": "90",
                        "dayHigh": "110",
                        "dayOpen": "95",
                        "dayClose": "101.2",
                        "dayChange": "6.5",
                        "isClosed": False,
                    }
                },
            }
        }
    )
    stats = make_adapter(transport).get_market_stats(market)

    assert stats.latest == Decimal("101.2")
    assert stats.best_bid == Decimal("101")
    assert stats.best_ask == Decimal("102")
    assert stats.day_change_percent == Decimal("6.5")
    assert stats.observed_at == NOW


def test_wallet_read_requires_token_before_any_transport_call(
    empty_transport: FakeNobitexTransport,
) -> None:
    """Missing account credentials are rejected without making an HTTP attempt."""
    adapter = make_adapter(empty_transport, token=None)

    with pytest.raises(AuthenticationError, match="NOBITEX_READONLY_API_TOKEN"):
        adapter.get_balances()

    assert empty_transport.calls == []


def test_wallet_balances_request_and_return_only_usdt() -> None:
    """The adapter asks for USDT only and filters other wallet rows defensively."""
    transport = FakeNobitexTransport(
        responses={
            ("GET", "/v2/wallets"): {
                "status": "ok",
                "wallets": {
                    "BTC": {"balance": "1.2500", "blocked": "0.2500"},
                    "USDT": {"balance": "100", "blocked": "10"},
                    "ETH": {"balance": "4", "blocked": "1"},
                },
            }
        }
    )
    balances = make_adapter(transport).get_balances()

    assert len(balances) == 1
    assert balances[0].currency == "USDT"
    assert balances[0].balance == Decimal("100")
    assert balances[0].blocked == Decimal("10")
    assert transport.calls[0][0:2] == ("GET", "/v2/wallets")
    assert transport.calls[0][2] == {"currencies": "usdt", "type": "spot"}
    assert transport.calls[0][4] == "fixture-read-only-token"


def test_usdt_market_discovery_filters_non_usdt_pairs() -> None:
    transport = FakeNobitexTransport(
        responses={
            ("GET", "/market/stats"): {
                "status": "ok",
                "stats": {
                    "btc-usdt": {"latest": "100"},
                    "eth-usdt": {"latest": "20"},
                    "shib-irt": {"latest": "1"},
                    "sol-btc": {"latest": "0.001"},
                    "global": {"latest": "0"},
                },
            }
        }
    )

    markets = make_adapter(transport).get_usdt_markets()

    assert [market.symbol for market in markets] == ["BTC-USDT", "ETH-USDT"]
    assert all(market.venue == "nobitex" for market in markets)
    assert all(market.market_type == "spot" for market in markets)
    assert transport.calls[0][2] == {"dstCurrency": "usdt"}


def test_non_usdt_market_is_rejected_before_market_data_transport_call() -> None:
    transport = FakeNobitexTransport(responses={})
    adapter = make_adapter(transport)
    non_usdt_market = Market(symbol="BTC-IRT", venue="nobitex", market_type="spot")

    with pytest.raises(UnsupportedExchangeCapability, match="USDT-only"):
        adapter.get_order_book(non_usdt_market)

    assert transport.calls == []


def test_transport_blocks_non_usdt_market_and_wallet_reads_before_network_io() -> None:
    transport = NobitexHTTPTransport()

    with pytest.raises(InvalidExchangeRequest, match="USDT-quoted"):
        transport.request("GET", "/market/stats", params={"dstCurrency": "irt"})
    with pytest.raises(InvalidExchangeRequest, match="restricted to USDT"):
        transport.request(
            "GET",
            "/v2/wallets",
            params={"currencies": "btc", "type": "spot"},
        )
    with pytest.raises(InvalidExchangeRequest, match="restricted to USDT"):
        transport.request(
            "GET",
            "/v2/wallets",
            params={"currencies": "usdt", "type": "all"},
        )


def test_order_status_is_read_only_and_normalized(market: Market) -> None:
    """The status query maps a mocked order but never reaches an order-write endpoint."""
    transport = FakeNobitexTransport(
        responses={
            ("POST", "/market/orders/status"): {
                "status": "ok",
                "order": {
                    "id": 123,
                    "market": "BTC-USDT",
                    "type": "buy",
                    "amount": "0.25",
                    "price": "100",
                    "status": "Active",
                    "clientOrderId": "fixture-123",
                },
            }
        }
    )
    order = make_adapter(transport).get_order("123", market)

    assert order is not None
    assert order.order_id == "123"
    assert order.market_symbol == market.symbol
    assert order.quantity == Decimal("0.25")
    assert order.price == Decimal("100")
    assert order.client_order_id == "fixture-123"
    assert [call[1] for call in transport.calls] == ["/market/orders/status"]


def test_invalid_order_status_market_mapping_is_rejected(market: Market) -> None:
    """The adapter rejects a provider response explicitly identifying another market."""
    transport = FakeNobitexTransport(
        responses={
            ("POST", "/market/orders/status"): {
                "status": "ok",
                "order": {
                    "id": 123,
                    "market": "ETH-USDT",
                    "type": "buy",
                    "amount": "0.25",
                    "status": "Active",
                },
            }
        }
    )
    with pytest.raises(MalformedExchangeResponse, match="market mismatches"):
        make_adapter(transport).get_order("123", market)


def test_submit_and_cancel_are_hard_disabled_and_never_call_transport(
    empty_transport: FakeNobitexTransport,
    market: Market,
) -> None:
    """Both write methods always fail closed, independently of environment variables."""
    adapter = make_adapter(empty_transport)
    request = OrderRequest(
        client_order_id="unit-order-1",
        market=market,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.01"),
        price=Decimal("100"),
    )

    with pytest.raises(TradingDisabledError, match="Phases 6/7"):
        adapter.submit_order(request)

    with pytest.raises(TradingDisabledError, match="Phases 6/7"):
        adapter.cancel_order("123")

    assert empty_transport.calls == []


def test_plugin_refuses_enable_real_trading_true() -> None:
    """Setting a live flag cannot enable any route during Phases 6/7."""
    plugin = NobitexExchangePlugin()

    with pytest.raises(TradingDisabledError, match="cannot enable writes"):
        plugin.build_adapter({"ENABLE_REAL_TRADING": "True"})


def test_transport_rejects_write_routes_before_network_io() -> None:
    """The concrete HTTP transport allowlist excludes submit and cancellation URLs."""
    transport = NobitexHTTPTransport()

    with pytest.raises(InvalidExchangeRequest, match="non-allowlisted"):
        transport.request(
            "POST",
            "/market/orders/add",
            payload={"type": "buy", "amount": "0.01"},
        )

    with pytest.raises(InvalidExchangeRequest, match="non-allowlisted"):
        transport.request(
            "POST",
            "/market/orders/update-status",
            payload={"order": 123, "status": "canceled"},
        )


def test_transport_rejects_non_official_hosts() -> None:
    """An arbitrary host cannot be injected as a Nobitex base URL."""
    with pytest.raises(ValueError, match="official HTTPS API host"):
        NobitexHTTPTransport(base_url="https://attacker.invalid")


def test_adapter_rejects_unsupported_candle_ranges_before_request(
    market: Market,
    empty_transport: FakeNobitexTransport,
) -> None:
    """Overlong history requests are rejected rather than silently truncated."""
    adapter = make_adapter(empty_transport)

    with pytest.raises(InvalidExchangeRequest, match="500 candles"):
        adapter.get_market_candles(
            market,
            interval=timedelta(minutes=1),
            start=NOW - timedelta(minutes=501),
            end=NOW,
        )

    assert empty_transport.calls == []
