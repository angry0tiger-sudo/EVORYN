"""Mock-only security and execution tests; no live API requests are made."""
from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from urllib.error import URLError
from urllib.request import Request

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from evoryn.adapters.exchanges.nobitex_instrument_rules import (
    NobitexSpotInstrumentRulesProvider,
    SpotInstrumentRules,
)
from evoryn.adapters.exchanges.nobitex_spot_trading import (
    NobitexSpotTradingTransport,
)
from evoryn.config import MarketPolicy
from evoryn.domain.models import (
    Market,
    MarketSnapshot,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)
from evoryn.execution.live_spot import (
    ExecutionDisposition,
    LiveSpotExecutionService,
    LiveSpotExecutionSettings,
    SQLiteExecutionJournal,
)
from evoryn.ports.exchange_errors import (
    AuthenticationError,
    InvalidExchangeRequest,
    MalformedExchangeResponse,
    OrderOutcomeUnknown,
    TradingDisabledError,
)
from evoryn.risk.engine import RiskEngine
from evoryn.risk.models import (
    PortfolioSnapshot,
    RiskLimits,
)

NOW = datetime(2026, 10, 19, 18, 0, tzinfo=UTC)
MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
ACK = "I_UNDERSTAND_ORDERS_ARE_REAL"


def _credentials() -> tuple[str, str, Ed25519PrivateKey]:
    private = Ed25519PrivateKey.generate()
    private_bytes = private.private_bytes(
        encoding=Encoding.Raw,
        format=PrivateFormat.Raw,
        encryption_algorithm=NoEncryption(),
    )
    public_bytes = private.public_key().public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )
    private_key = base64.urlsafe_b64encode(private_bytes).decode("ascii").rstrip("=")
    api_key = base64.urlsafe_b64encode(public_bytes).decode("ascii").rstrip("=")
    return api_key, private_key, private


class FakeResponse:
    def __init__(self, payload: Mapping[str, object]) -> None:
        self._body = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool:
        del exc_type, exc, traceback
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]


class FakeOpener:
    """Fake urllib opener; all requests terminate in memory."""

    def __init__(
        self,
        responses: list[Mapping[str, object] | Exception],
    ) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[Request, float]] = []

    def open(self, request: Request, *, timeout: float) -> FakeResponse:
        self.calls.append((request, timeout))
        if not self.responses:
            raise AssertionError("Unexpected additional HTTP request.")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)


def transport(
    opener: FakeOpener,
) -> tuple[NobitexSpotTradingTransport, Ed25519PrivateKey]:
    api_key, private_key, signer = _credentials()
    instance = NobitexSpotTradingTransport(
        api_key=api_key,
        private_key=private_key,
        base_url="https://apiv2.nobitex.ir",
        timeout_seconds=2,
        clock=lambda: NOW,
        opener=opener,
        market_policy=MarketPolicy(),
    )
    return instance, signer


def order_request(
    *,
    client_order_id: str = "evoryn-test-001",
    order_type: OrderType = OrderType.LIMIT,
    quantity: str = "1",
    price: str | None = "100",
) -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id,
        market=MARKET,
        side=Side.BUY,
        order_type=order_type,
        quantity=Decimal(quantity),
        price=Decimal(price) if price is not None else None,
    )


def test_signed_submission_uses_official_headers_and_one_fixed_route() -> None:
    opener = FakeOpener(
        [
            {
                "status": "ok",
                "order": {
                    "id": 12345,
                    "type": "buy",
                    "execution": "limit",
                    "status": "active",
                    "amount": "1",
                    "price": "100",
                    "clientOrderId": "evoryn-test-001",
                    "market": "BTCUSDT",
                },
            }
        ]
    )
    gateway, signer = transport(opener)

    result = gateway.submit_order(order_request(), price_guard=Decimal("100"))
    request, timeout = opener.calls[0]
    headers = {name.casefold(): value for name, value in request.header_items()}
    body = request.data.decode("utf-8") if request.data is not None else ""
    signature = base64.urlsafe_b64decode(headers["nobitex-signature"].encode("ascii"))
    signing_text = (
        f"{headers['nobitex-timestamp']}POST/market/orders/add{body}"
    ).encode("utf-8")
    signer.public_key().verify(signature, signing_text)

    assert result.order_id == "12345"
    assert result.status is OrderStatus.OPEN
    assert result.client_order_id == "evoryn-test-001"
    assert request.get_method() == "POST"
    assert request.full_url == "https://apiv2.nobitex.ir/market/orders/add"
    assert headers["nobitex-key"]
    assert headers["nobitex-timestamp"] == str(int(NOW.timestamp()))
    assert timeout == 2
    assert len(opener.calls) == 1


def test_submission_network_timeout_is_unknown_and_never_retried() -> None:
    opener = FakeOpener([URLError("synthetic timeout")])
    gateway, _ = transport(opener)

    with pytest.raises(OrderOutcomeUnknown):
        gateway.submit_order(order_request(), price_guard=Decimal("100"))

    assert len(opener.calls) == 1


def test_non_usdt_write_is_rejected_before_transport() -> None:
    opener = FakeOpener([])
    gateway, _ = transport(opener)
    request = OrderRequest(
        client_order_id="evoryn-irt",
        market=Market("BTC-IRT", "nobitex", "spot"),
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        price=Decimal("100"),
    )

    with pytest.raises(InvalidExchangeRequest):
        gateway.submit_order(request, price_guard=Decimal("100"))

    assert opener.calls == []


def test_bad_public_private_key_pair_is_rejected() -> None:
    api_key, _, _ = _credentials()
    other_api_key, private_key, _ = _credentials()
    assert api_key != other_api_key

    with pytest.raises(AuthenticationError):
        NobitexSpotTradingTransport(
            api_key=api_key,
            private_key=private_key,
            opener=FakeOpener([]),
        )


def test_cancel_requires_known_order_and_conclusive_acknowledgement() -> None:
    opener = FakeOpener([{"status": "ok", "updatedStatus": "canceled"}])
    gateway, _ = transport(opener)
    active = Order(
        order_id="12345",
        market_symbol=MARKET.symbol,
        side=Side.BUY,
        quantity=Decimal("1"),
        status=OrderStatus.OPEN,
        price=Decimal("100"),
        client_order_id="evoryn-test-001",
    )

    cancelled = gateway.cancel_order(requested_market=MARKET, known_order=active)

    assert cancelled.status is OrderStatus.CANCELLED
    assert len(opener.calls) == 1
    assert opener.calls[0][0].full_url.endswith("/market/orders/update-status")


class FakeOptionsTransport:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[tuple[str, str, Mapping[str, str] | None]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
        token: str | None = None,
    ) -> dict[str, object]:
        self.calls.append((method, path, params))
        assert payload is None
        assert token is None
        return self.response


def test_instrument_rules_are_read_from_options_without_guessing() -> None:
    reader = FakeOptionsTransport(
        {
            "status": "ok",
            "nobitex": {
                "amountPrecisions": {"BTCUSDT": "0.0001"},
                "pricePrecisions": {"BTCUSDT": "0.01"},
                "minOrders": {"usdt": "11"},
            },
        }
    )
    provider = NobitexSpotInstrumentRulesProvider(
        transport=reader,
        market_policy=MarketPolicy(),
        clock=lambda: NOW,
    )

    rules = provider.get_rules(MARKET)

    assert rules.amount_step == Decimal("0.0001")
    assert rules.price_tick == Decimal("0.01")
    assert rules.min_notional_usdt == Decimal("11")
    assert rules.symbol == "BTCUSDT"
    assert reader.calls == [("GET", "/v2/options", None)]


def test_missing_instrument_rule_fails_closed() -> None:
    provider = NobitexSpotInstrumentRulesProvider(
        transport=FakeOptionsTransport(
            {
                "status": "ok",
                "nobitex": {
                    "amountPrecisions": {},
                    "pricePrecisions": {},
                    "minOrders": {"usdt": "11"},
                },
            }
        ),
        market_policy=MarketPolicy(),
        clock=lambda: NOW,
    )

    with pytest.raises(MalformedExchangeResponse):
        provider.get_rules(MARKET)


def settings(*, enabled: bool = True) -> LiveSpotExecutionSettings:
    return LiveSpotExecutionSettings(
        order_writes_enabled=enabled,
        api_key="dummy-public-key",
        private_key="dummy-private-key",
        owner_acknowledgement=ACK,
        max_market_slippage_bps=Decimal("50"),
    )


class FakeRulesProvider:
    def __init__(self, rules: SpotInstrumentRules) -> None:
        self.rules = rules
        self.calls = 0

    def get_rules(self, market: Market) -> SpotInstrumentRules:
        self.calls += 1
        assert market == MARKET
        return self.rules


class FakeTradingGateway:
    def __init__(self) -> None:
        self.submissions: list[tuple[OrderRequest, Decimal]] = []
        self.cancellations: list[Order] = []
        self.status_response: Order | None = None
        self.submit_error: Exception | None = None

    def submit_order(self, request: OrderRequest, *, price_guard: Decimal) -> Order:
        self.submissions.append((request, price_guard))
        if self.submit_error is not None:
            raise self.submit_error
        return Order(
            order_id="9001",
            market_symbol=request.market.symbol,
            side=request.side,
            quantity=request.quantity,
            status=OrderStatus.OPEN,
            price=price_guard,
            client_order_id=request.client_order_id,
        )

    def get_order_status(
        self,
        *,
        requested_market: Market,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> Order | None:
        del requested_market, order_id, client_order_id
        return self.status_response

    def cancel_order(self, *, requested_market: Market, known_order: Order) -> Order:
        del requested_market
        self.cancellations.append(known_order)
        return replace_order_status(known_order, OrderStatus.CANCELLED)


def replace_order_status(order: Order, status: OrderStatus) -> Order:
    return Order(
        order_id=order.order_id,
        market_symbol=order.market_symbol,
        side=order.side,
        quantity=order.quantity,
        status=status,
        price=order.price,
        client_order_id=order.client_order_id,
    )


def risk_engine() -> RiskEngine:
    return RiskEngine(
        RiskLimits(
            max_total_exposure=Decimal("10000"),
            max_market_exposure=Decimal("10000"),
            max_order_notional=Decimal("2000"),
            max_loss=Decimal("500"),
            max_market_data_age_seconds=10,
        ),
        market_policy=MarketPolicy(),
    )


def portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        free_capital=Decimal("1000"),
        total_exposure=Decimal("0"),
        market_exposure=Decimal("0"),
        position_quantity=Decimal("10"),
        account_reconciled=True,
        execution_error=False,
    )


def snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        market=MARKET,
        observed_at=NOW,
        source="nobitex",
        values={
            "bid": Decimal("99.90"),
            "ask": Decimal("100.00"),
            "last": Decimal("99.95"),
            "bid_size": Decimal("10"),
            "ask_size": Decimal("10"),
        },
    )


def service(
    gateway: FakeTradingGateway,
    journal: SQLiteExecutionJournal,
    *,
    enabled: bool = True,
) -> tuple[LiveSpotExecutionService, FakeRulesProvider, RiskEngine]:
    rules_provider = FakeRulesProvider(
        SpotInstrumentRules(
            symbol="BTCUSDT",
            amount_step=Decimal("0.001"),
            price_tick=Decimal("0.01"),
            min_notional_usdt=Decimal("11"),
            observed_at=NOW,
        )
    )
    risk = risk_engine()
    instance = LiveSpotExecutionService(
        settings=settings(enabled=enabled),
        risk_engine=risk,
        journal=journal,
        rules_provider=rules_provider,
        transport=gateway if enabled else None,
        market_policy=MarketPolicy(),
        clock=lambda: NOW,
    )
    return instance, rules_provider, risk


def test_disabled_feature_flag_stops_before_rules_or_transport() -> None:
    gateway = FakeTradingGateway()
    journal = SQLiteExecutionJournal(":memory:")
    runner, rules, _risk = service(gateway, journal, enabled=False)

    with pytest.raises(TradingDisabledError):
        runner.submit_order(
            order_request(order_type=OrderType.MARKET, price=None),
            portfolio(),
            snapshot(),
            now=NOW,
        )

    assert rules.calls == 0
    assert gateway.submissions == []
    assert journal.blocking_order() is None


def test_settings_require_explicit_arm_and_refuse_ci_write_configuration() -> None:
    with pytest.raises(TradingDisabledError):
        LiveSpotExecutionSettings.from_mapping(
            {
                "EVORYN_SPOT_ORDER_WRITES_ENABLED": "true",
                "EVORYN_LIVE_TRADING_ACK": ACK,
                "NOBITEX_SPOT_API_KEY": "dummy",
                "NOBITEX_SPOT_PRIVATE_KEY": "dummy",
                "CI": "true",
            }
        )
    with pytest.raises(TradingDisabledError):
        LiveSpotExecutionSettings.from_mapping(
            {
                "EVORYN_SPOT_ORDER_WRITES_ENABLED": "true",
                "NOBITEX_SPOT_API_KEY": "dummy",
                "NOBITEX_SPOT_PRIVATE_KEY": "dummy",
            }
        )
    disabled = LiveSpotExecutionSettings.from_mapping(
        {
            "EVORYN_SPOT_ORDER_WRITES_ENABLED": "false",
            "NOBITEX_SPOT_API_KEY": "secret-placeholder",
            "NOBITEX_SPOT_PRIVATE_KEY": "secret-placeholder-private",
        }
    )
    assert disabled.api_key is None
    assert disabled.private_key is None
    assert "secret-placeholder" not in repr(disabled)


def test_gated_service_submits_once_after_risk_and_instrument_validation() -> None:
    gateway = FakeTradingGateway()
    journal = SQLiteExecutionJournal(":memory:")
    runner, rules, risk = service(gateway, journal)
    request = order_request(order_type=OrderType.MARKET, price=None)

    result = runner.submit_order(request, portfolio(), snapshot(), now=NOW)

    assert result.disposition is ExecutionDisposition.SUBMITTED
    assert result.order is not None and result.order.order_id == "9001"
    assert len(gateway.submissions) == 1
    assert gateway.submissions[0][0].quantity == Decimal("1")
    assert gateway.submissions[0][1] == Decimal("100.50")
    assert rules.calls == 1
    record = journal.get(request.client_order_id)
    assert record is not None and record.state == OrderStatus.OPEN.value
    assert risk.is_halted is False


def test_active_order_blocks_a_second_submission_until_lifecycle_is_resolved() -> None:
    gateway = FakeTradingGateway()
    journal = SQLiteExecutionJournal(":memory:")
    runner, _rules, _risk = service(gateway, journal)
    first = runner.submit_order(
        order_request(order_type=OrderType.MARKET, price=None),
        portfolio(),
        snapshot(),
        now=NOW,
    )
    assert first.disposition is ExecutionDisposition.SUBMITTED

    second = runner.submit_order(
        order_request(
            client_order_id="evoryn-test-002",
            order_type=OrderType.MARKET,
            price=None,
        ),
        portfolio(),
        snapshot(),
        now=NOW,
    )

    assert second.disposition is ExecutionDisposition.BLOCKED
    assert second.reason_code == "prior_order_still_active"
    assert len(gateway.submissions) == 1


def test_unknown_submit_is_journaled_and_latches_risk_halt() -> None:
    gateway = FakeTradingGateway()
    gateway.submit_error = OrderOutcomeUnknown("synthetic network timeout")
    journal = SQLiteExecutionJournal(":memory:")
    runner, _rules, risk = service(gateway, journal)
    request = order_request(order_type=OrderType.MARKET, price=None)

    result = runner.submit_order(request, portfolio(), snapshot(), now=NOW)

    record = journal.get(request.client_order_id)
    assert result.disposition is ExecutionDisposition.UNKNOWN
    assert result.reconciliation_required
    assert record is not None and record.state == "unknown"
    assert risk.is_halted
    assert len(gateway.submissions) == 1


def test_not_found_reconciliation_stays_unknown_and_does_not_resubmit() -> None:
    gateway = FakeTradingGateway()
    gateway.submit_error = OrderOutcomeUnknown("synthetic timeout")
    journal = SQLiteExecutionJournal(":memory:")
    runner, _rules, risk = service(gateway, journal)
    request = order_request(order_type=OrderType.MARKET, price=None)
    first = runner.submit_order(request, portfolio(), snapshot(), now=NOW)
    assert first.disposition is ExecutionDisposition.UNKNOWN

    reconciliation = runner.reconcile(request.client_order_id)

    assert reconciliation.disposition is ExecutionDisposition.UNKNOWN
    assert reconciliation.reconciliation_required
    assert len(gateway.submissions) == 1
    assert journal.get(request.client_order_id) is not None
    assert risk.is_halted


def test_known_order_can_be_cancelled_without_exposing_arbitrary_order_ids() -> None:
    gateway = FakeTradingGateway()
    journal = SQLiteExecutionJournal(":memory:")
    runner, _rules, _risk = service(gateway, journal)
    request = order_request(order_type=OrderType.MARKET, price=None)
    submitted = runner.submit_order(request, portfolio(), snapshot(), now=NOW)
    assert submitted.disposition is ExecutionDisposition.SUBMITTED

    cancelled = runner.cancel_order(request.client_order_id)

    assert cancelled.disposition is ExecutionDisposition.CANCELLED
    assert cancelled.order is not None
    assert cancelled.order.status is OrderStatus.CANCELLED
    record = journal.get(request.client_order_id)
    assert record is not None and record.state == OrderStatus.CANCELLED.value
    assert len(gateway.cancellations) == 1
