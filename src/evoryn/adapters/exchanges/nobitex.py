"""Nobitex exchange plugin with normalized reads and a hard-disabled write boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

from evoryn.adapters.exchanges.nobitex_transport import NobitexHTTPTransport
from evoryn.config import MarketPolicy, NobitexReadOnlyConfig
from evoryn.domain.models import (
    ExchangeCapabilities,
    Market,
    MarketCandle,
    MarketStatistics,
    MarketTrade,
    Order,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderRequest,
    OrderStatus,
    Side,
    WalletBalance,
)
from evoryn.infrastructure.data.normalization import normalize_candle
from evoryn.infrastructure.exchange.rate_limit import SlidingWindowRateLimiter
from evoryn.ports.contracts import ExchangePort, RateLimiterPort
from evoryn.ports.exchange_errors import (
    AuthenticationError,
    ExchangeResourceNotFound,
    InvalidExchangeRequest,
    MalformedExchangeResponse,
    TradingDisabledError,
    UnsupportedExchangeCapability,
)
from evoryn.ports.rate_limits import RateLimitPolicy


class NobitexTransportPort(Protocol):
    """Testable transport boundary implemented by the allowlisted HTTP transport."""

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
        token: str | None = None,
    ) -> dict[str, object]:
        """Perform one reviewed request and return the decoded object response."""
        ...


_RATE_LIMITS: dict[str, RateLimitPolicy] = {
    "order_book": RateLimitPolicy(max_requests=300, window_seconds=60.0),
    "recent_trades": RateLimitPolicy(max_requests=60, window_seconds=60.0),
    "market_stats": RateLimitPolicy(max_requests=20, window_seconds=60.0),
    "candles": RateLimitPolicy(max_requests=60, window_seconds=60.0),
    "balances": RateLimitPolicy(max_requests=15, window_seconds=60.0),
    "order_status": RateLimitPolicy(max_requests=60, window_seconds=60.0),
}

_RESOLUTIONS: dict[timedelta, str] = {
    timedelta(minutes=1): "1",
    timedelta(minutes=5): "5",
    timedelta(minutes=15): "15",
    timedelta(minutes=30): "30",
    timedelta(hours=1): "60",
    timedelta(hours=3): "180",
    timedelta(hours=4): "240",
    timedelta(hours=6): "360",
    timedelta(hours=12): "720",
    timedelta(days=1): "D",
    timedelta(days=2): "2D",
    timedelta(days=3): "3D",
}

_QUOTE_CURRENCIES = ("USDT", "USDC", "RLS", "IRT", "BTC", "ETH", "DAI")


def _decimal(value: object, *, field_name: str) -> Decimal:
    """Parse a provider scalar into a finite Decimal or raise a typed response error."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise MalformedExchangeResponse(f"Nobitex field '{field_name}' is not numeric.")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MalformedExchangeResponse(
            f"Nobitex field '{field_name}' is not numeric."
        ) from exc
    if not result.is_finite():
        raise MalformedExchangeResponse(f"Nobitex field '{field_name}' is not finite.")
    return result


def _required_text(value: object, *, field_name: str) -> str:
    """Require a non-empty string-like response field without echoing provider payloads."""
    if not isinstance(value, str) or not value.strip():
        raise MalformedExchangeResponse(f"Nobitex field '{field_name}' is missing.")
    return value.strip()


@dataclass(slots=True)
class NobitexExchangeAdapter:
    """Normalize selected Nobitex reads behind the common ExchangePort.

    Order submit and cancel methods are intentionally hard-disabled in Phases 6/7.
    They do not consult an environment toggle and never reach an HTTP transport.
    A future write implementation requires the Phase 9/10 execution and release gates.
    """

    transport: NobitexTransportPort
    token: str | None = None
    rate_limiter: RateLimiterPort = field(default_factory=SlidingWindowRateLimiter)
    clock: Callable[[], datetime] = field(
        default_factory=lambda: lambda: datetime.now(UTC),
        repr=False,
    )
    market_policy: MarketPolicy = field(
        default_factory=MarketPolicy.from_environment,
        repr=False,
    )

    def get_capabilities(self) -> ExchangeCapabilities:
        """Declare currently implemented capabilities; no order-writing is available."""
        return ExchangeCapabilities(
            venue="nobitex",
            market_data=True,
            account_read=True,
            order_read=True,
            order_write=False,
            candles=True,
            order_book=True,
            recent_trades=True,
            market_stats=True,
            order_submit=False,
            order_cancel=False,
        )

    @staticmethod
    def _api_symbol(market: Market) -> str:
        """Convert accepted canonical pair forms to an exchange symbol without guessing."""
        symbol = market.symbol.strip().upper()
        for separator in ("/", "-", "_"):
            symbol = symbol.replace(separator, "")
        if not symbol.isalnum():
            raise InvalidExchangeRequest("Market symbol contains unsupported characters.")
        return symbol

    @staticmethod
    def _currency_pair(market: Market) -> tuple[str, str]:
        """Split an explicit BASE/QUOTE symbol for the Nobitex market-statistics query."""
        symbol = market.symbol.strip().upper()
        for separator in ("/", "-", "_"):
            if separator in symbol:
                parts = symbol.split(separator)
                if len(parts) == 2 and all(part.isalnum() for part in parts):
                    return parts[0].lower(), parts[1].lower()
                raise InvalidExchangeRequest("Market symbol must contain one BASE/QUOTE pair.")
        for quote in _QUOTE_CURRENCIES:
            if symbol.endswith(quote) and len(symbol) > len(quote):
                base = symbol[: -len(quote)]
                if base.isalnum():
                    return base.lower(), quote.lower()
        raise InvalidExchangeRequest(
            "Market stats require an explicit BASE/QUOTE market symbol."
        )

    def _require_spot(self, market: Market) -> None:
        """Reject markets outside the current Nobitex Spot/USDT hard whitelist."""
        if market.market_type.strip().lower() != "spot":
            raise UnsupportedExchangeCapability(
                "This Nobitex adapter currently supports Spot market data only."
            )
        reason = self.market_policy.rejection_reason(market)
        if reason is not None:
            raise UnsupportedExchangeCapability(
                "Nobitex market is outside the current USDT-only whitelist."
            )

    def _request(
        self,
        key: str,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Apply the shared rate policy before delegating to the allowlisted transport."""
        self.rate_limiter.acquire(f"nobitex:{key}", _RATE_LIMITS[key])
        return self.transport.request(
            method,
            path,
            params=params,
            payload=payload,
            token=self.token,
        )

    def get_market_candles(
        self,
        market: Market,
        *,
        interval: timedelta,
        start: datetime,
        end: datetime,
    ) -> Sequence[MarketCandle]:
        """Fetch and normalize one bounded UDF OHLCV range without trading access."""
        self._require_spot(market)
        if start.tzinfo is None or start.utcoffset() is None:
            raise InvalidExchangeRequest("Candle start must include a timezone.")
        if end.tzinfo is None or end.utcoffset() is None:
            raise InvalidExchangeRequest("Candle end must include a timezone.")
        start_utc = start.astimezone(UTC)
        end_utc = end.astimezone(UTC)
        if start_utc >= end_utc:
            raise InvalidExchangeRequest("Candle start must be before candle end.")
        resolution = _RESOLUTIONS.get(interval)
        if resolution is None:
            raise InvalidExchangeRequest("Requested candle interval is not documented.")
        expected_count = (end_utc - start_utc).total_seconds() / interval.total_seconds()
        if expected_count > 500:
            raise InvalidExchangeRequest(
                "A single Nobitex UDF request is limited to 500 candles; use bounded pages."
            )

        response = self._request(
            "candles",
            "GET",
            "/market/udf/history",
            params={
                "symbol": self._api_symbol(market),
                "resolution": resolution,
                "from": str(int(start_utc.timestamp())),
                "to": str(int(end_utc.timestamp())),
            },
        )
        status = response.get("s")
        if status == "no_data":
            return ()
        fields = tuple(response.get(key) for key in ("t", "o", "h", "l", "c", "v"))
        if not all(isinstance(field_value, list) for field_value in fields):
            raise MalformedExchangeResponse("Nobitex OHLCV fields must all be arrays.")
        timestamps, opens, highs, lows, closes, volumes = (
            field_value for field_value in fields if isinstance(field_value, list)
        )
        lengths = {len(values) for values in (timestamps, opens, highs, lows, closes, volumes)}
        if len(lengths) != 1:
            raise MalformedExchangeResponse("Nobitex OHLCV arrays have inconsistent lengths.")

        rows: list[MarketCandle] = []
        for index in range(len(timestamps)):
            timestamp_value = timestamps[index]
            if isinstance(timestamp_value, bool) or not isinstance(
                timestamp_value, (int, float, Decimal, str)
            ):
                raise MalformedExchangeResponse("Nobitex candle timestamp is malformed.")
            try:
                timestamp = datetime.fromtimestamp(float(timestamp_value), tz=UTC)
                rows.append(
                    normalize_candle(
                        {
                            "timestamp": timestamp,
                            "open": opens[index],
                            "high": highs[index],
                            "low": lows[index],
                            "close": closes[index],
                            "volume": volumes[index],
                        },
                        market=market,
                        interval=interval,
                        source="nobitex",
                    )
                )
            except (OverflowError, OSError, ValueError) as exc:
                raise MalformedExchangeResponse(
                    "Nobitex returned a malformed OHLCV candle."
                ) from exc
        return tuple(rows)

    def get_order_book(self, market: Market) -> OrderBookSnapshot:
        """Read the v3 order book and reject malformed levels instead of dropping them."""
        self._require_spot(market)
        response = self._request(
            "order_book", "GET", f"/v3/orderbook/{self._api_symbol(market)}"
        )
        bids_raw = response.get("bids")
        asks_raw = response.get("asks")
        if not isinstance(bids_raw, list) or not isinstance(asks_raw, list):
            raise MalformedExchangeResponse("Nobitex order-book sides must be arrays.")

        def parse_levels(rows: list[object], *, side_name: str) -> tuple[OrderBookLevel, ...]:
            """Parse one order-book side and reject any malformed row."""
            levels: list[OrderBookLevel] = []
            for row in rows:
                if not isinstance(row, list) or len(row) != 2:
                    raise MalformedExchangeResponse(
                        f"Nobitex {side_name} level must contain exactly price and quantity."
                    )
                price = _decimal(row[0], field_name=f"{side_name}.price")
                quantity = _decimal(row[1], field_name=f"{side_name}.quantity")
                try:
                    levels.append(OrderBookLevel(price=price, quantity=quantity))
                except ValueError as exc:
                    raise MalformedExchangeResponse(
                        f"Nobitex {side_name} level contains invalid values."
                    ) from exc
            return tuple(levels)

        update_raw = response.get("lastUpdate")
        if isinstance(update_raw, bool) or not isinstance(
            update_raw, (str, int, float, Decimal)
        ):
            raise MalformedExchangeResponse("Nobitex order book lacks a valid lastUpdate.")
        try:
            observed_at = datetime.fromtimestamp(float(update_raw) / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise MalformedExchangeResponse("Nobitex lastUpdate is invalid.") from exc
        last_trade_raw = response.get("lastTradePrice")
        last_trade = (
            _decimal(last_trade_raw, field_name="lastTradePrice")
            if last_trade_raw is not None
            else None
        )
        try:
            return OrderBookSnapshot(
                market=market,
                bids=parse_levels(bids_raw, side_name="bid"),
                asks=parse_levels(asks_raw, side_name="ask"),
                observed_at=observed_at,
                source="nobitex",
                last_trade_price=last_trade,
            )
        except ValueError as exc:
            raise MalformedExchangeResponse("Nobitex order book snapshot is invalid.") from exc

    def get_recent_trades(self, market: Market, *, limit: int) -> Sequence[MarketTrade]:
        """Read recent trades and return a chronological normalized sequence."""
        self._require_spot(market)
        if limit < 1 or limit > 500:
            raise InvalidExchangeRequest("Trade limit must be between 1 and 500.")
        response = self._request(
            "recent_trades", "GET", f"/v2/trades/{self._api_symbol(market)}"
        )
        trades_raw = response.get("trades")
        if not isinstance(trades_raw, list):
            raise MalformedExchangeResponse("Nobitex trades field must be an array.")

        trades: list[MarketTrade] = []
        for index, raw_trade in enumerate(trades_raw[:limit]):
            if not isinstance(raw_trade, dict):
                raise MalformedExchangeResponse("Nobitex trade row must be an object.")
            side_value = raw_trade.get("type")
            try:
                side = Side(_required_text(side_value, field_name="type").lower())
            except ValueError as exc:
                raise MalformedExchangeResponse("Nobitex trade side is unsupported.") from exc
            time_raw = raw_trade.get("time")
            if isinstance(time_raw, bool) or not isinstance(
                time_raw, (str, int, float, Decimal)
            ):
                raise MalformedExchangeResponse("Nobitex trade timestamp is malformed.")
            try:
                occurred_at = datetime.fromtimestamp(float(time_raw) / 1000, tz=UTC)
                trades.append(
                    MarketTrade(
                        trade_id=str(raw_trade.get("id") or f"{int(float(time_raw))}-{index}"),
                        market=market,
                        side=side,
                        price=_decimal(raw_trade.get("price"), field_name="price"),
                        quantity=_decimal(raw_trade.get("volume"), field_name="volume"),
                        occurred_at=occurred_at,
                    )
                )
            except (OverflowError, OSError, ValueError) as exc:
                raise MalformedExchangeResponse("Nobitex trade row is invalid.") from exc
        return tuple(sorted(trades, key=lambda trade: trade.occurred_at))

    def get_usdt_markets(self) -> Sequence[Market]:
        """Discover markets from the official statistics endpoint, filtering to USDT quotes."""
        response = self._request(
            "market_stats",
            "GET",
            "/market/stats",
            params={"dstCurrency": "usdt"},
        )
        stats_raw = response.get("stats")
        if not isinstance(stats_raw, dict):
            raise MalformedExchangeResponse("Nobitex stats field must be an object.")

        markets: dict[str, Market] = {}
        for symbol, row in stats_raw.items():
            if not isinstance(symbol, str) or not isinstance(row, dict):
                continue
            parts = symbol.upper().split("-")
            if len(parts) != 2 or not parts[0].isalnum() or parts[1] != "USDT":
                continue
            market = Market(
                symbol=f"{parts[0]}-USDT",
                venue="nobitex",
                market_type="spot",
            )
            if self.market_policy.allows_market(market):
                markets[market.symbol] = market
        return tuple(markets[key] for key in sorted(markets))

    def get_market_stats(self, market: Market) -> MarketStatistics:
        """Read and normalize the selected market from Nobitex aggregate market stats."""
        self._require_spot(market)
        base, quote = self._currency_pair(market)
        response = self._request(
            "market_stats",
            "GET",
            "/market/stats",
            params={"srcCurrency": base, "dstCurrency": quote},
        )
        stats_raw = response.get("stats")
        if not isinstance(stats_raw, dict):
            raise MalformedExchangeResponse("Nobitex stats field must be an object.")
        normalized_key = f"{base}-{quote}"
        raw_stats = stats_raw.get(normalized_key)
        if not isinstance(raw_stats, dict):
            raise ExchangeResourceNotFound("Nobitex did not return stats for the requested market.")

        def optional_decimal(field_name: str) -> Decimal | None:
            """Parse an optional statistic without fabricating a value."""
            raw_value = raw_stats.get(field_name)
            if raw_value is None:
                return None
            return _decimal(raw_value, field_name=field_name)

        closed_raw = raw_stats.get("isClosed")
        if closed_raw is not None and not isinstance(closed_raw, bool):
            raise MalformedExchangeResponse("Nobitex isClosed field must be boolean.")
        observed_at = self.clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise RuntimeError("Nobitex adapter clock must return an aware datetime.")
        return MarketStatistics(
            market=market,
            observed_at=observed_at.astimezone(UTC),
            latest=optional_decimal("latest"),
            best_bid=optional_decimal("bestBuy"),
            best_ask=optional_decimal("bestSell"),
            base_volume=optional_decimal("volumeSrc"),
            quote_volume=optional_decimal("volumeDst"),
            day_low=optional_decimal("dayLow"),
            day_high=optional_decimal("dayHigh"),
            day_open=optional_decimal("dayOpen"),
            day_close=optional_decimal("dayClose"),
            day_change_percent=optional_decimal("dayChange"),
            is_closed=closed_raw,
        )

    def get_balances(self) -> Sequence[WalletBalance]:
        """Read only the Spot USDT wallet; absent USDT is not fabricated as zero."""
        if not self.token:
            raise AuthenticationError(
                "A read-only NOBITEX_READONLY_API_TOKEN is required to read USDT balances."
            )
        response = self._request(
            "balances",
            "GET",
            "/v2/wallets",
            params={"currencies": "usdt", "type": "spot"},
        )
        wallets_raw = response.get("wallets")
        if not isinstance(wallets_raw, dict):
            raise MalformedExchangeResponse("Nobitex wallets field must be an object.")
        matching = [
            (currency, wallet_raw)
            for currency, wallet_raw in wallets_raw.items()
            if isinstance(currency, str) and currency.upper() == "USDT"
        ]
        if len(matching) > 1:
            raise MalformedExchangeResponse("Nobitex returned duplicate USDT wallet keys.")
        if not matching:
            return ()
        currency, wallet_raw = matching[0]
        if not isinstance(wallet_raw, dict):
            raise MalformedExchangeResponse("Nobitex USDT wallet entry must be an object.")
        try:
            return (
                WalletBalance(
                    currency=currency.upper(),
                    balance=_decimal(wallet_raw.get("balance"), field_name="balance"),
                    blocked=_decimal(wallet_raw.get("blocked"), field_name="blocked"),
                ),
            )
        except ValueError as exc:
            raise MalformedExchangeResponse("Nobitex USDT wallet entry is invalid.") from exc

    def get_order(self, order_id: str, market: Market) -> Order | None:
        """Query a numeric USDT Spot order ID via the read-only order-status endpoint."""
        self._require_spot(market)
        if not self.token:
            raise AuthenticationError(
                "A read-only NOBITEX_READONLY_API_TOKEN is required to query order status."
            )
        normalized_id = order_id.strip()
        if not normalized_id.isdigit():
            raise InvalidExchangeRequest("Nobitex order status requires a numeric order ID.")
        response = self._request(
            "order_status",
            "POST",
            "/market/orders/status",
            payload={"id": int(normalized_id)},
        )
        if response.get("status") == "failed" and not isinstance(response.get("order"), dict):
            return None
        order_raw = response.get("order")
        if not isinstance(order_raw, dict):
            raise MalformedExchangeResponse("Nobitex order-status response lacks an order object.")

        response_market = order_raw.get("market")
        if isinstance(response_market, str):
            normalized_remote = response_market.upper().replace("/", "").replace("-", "")
            if normalized_remote != self._api_symbol(market):
                raise MalformedExchangeResponse(
                    "Nobitex order market mismatches the requested market."
                )
        side_raw = _required_text(order_raw.get("type"), field_name="order.type").lower()
        try:
            side = Side(side_raw)
        except ValueError as exc:
            raise MalformedExchangeResponse("Nobitex order side is unsupported.") from exc
        raw_status = _required_text(order_raw.get("status"), field_name="order.status")
        status_key = raw_status.lower().replace("_", "").replace(" ", "")
        status_map = {
            "active": OrderStatus.OPEN,
            "open": OrderStatus.OPEN,
            "inactive": OrderStatus.PENDING,
            "pending": OrderStatus.PENDING,
            "partiallymatched": OrderStatus.PARTIALLY_FILLED,
            "partially_filled": OrderStatus.PARTIALLY_FILLED,
            "done": OrderStatus.FILLED,
            "filled": OrderStatus.FILLED,
            "canceled": OrderStatus.CANCELLED,
            "cancelled": OrderStatus.CANCELLED,
            "rejected": OrderStatus.REJECTED,
        }
        price_raw = order_raw.get("price")
        return Order(
            order_id=str(order_raw.get("id", normalized_id)),
            market_symbol=market.symbol,
            side=side,
            quantity=_decimal(order_raw.get("amount"), field_name="order.amount"),
            status=status_map.get(status_key, OrderStatus.UNKNOWN),
            price=_decimal(price_raw, field_name="order.price") if price_raw is not None else None,
            client_order_id=(
                str(order_raw["clientOrderId"])
                if order_raw.get("clientOrderId") is not None
                else None
            ),
        )

    def submit_order(self, request: OrderRequest) -> Order:
        """Fail closed; no submit endpoint is present in the Phase 6/7 transport."""
        del request
        raise TradingDisabledError(
            "Order submission is unavailable in EVORYN Phases 6/7; "
            "the adapter has no order-writing transport path."
        )

    def cancel_order(self, order_id: str) -> Order:
        """Fail closed; no cancel endpoint is present in the Phase 6/7 transport."""
        del order_id
        raise TradingDisabledError(
            "Order cancellation is unavailable in EVORYN Phases 6/7; "
            "the adapter has no order-writing transport path."
        )


@dataclass(frozen=True, slots=True)
class NobitexExchangePlugin:
    """Installable plugin descriptor for the read-only Nobitex adapter."""

    name: str = "nobitex"

    def build_adapter(self, config: Mapping[str, str]) -> ExchangePort:
        """Build a no-write adapter from explicit configuration values."""
        live_flag = config.get("ENABLE_REAL_TRADING", "False").strip().lower()
        if live_flag in {"1", "true", "yes", "on"}:
            raise TradingDisabledError(
                "ENABLE_REAL_TRADING cannot enable writes in Phases 6/7. "
                "Order writes remain hard-disabled until the Phase 9/10 release gates."
            )

        settings = NobitexReadOnlyConfig.from_mapping(config)
        transport = NobitexHTTPTransport(
            base_url=settings.base_url,
            token=settings.read_only_api_token,
            timeout_seconds=settings.timeout_seconds,
        )
        return NobitexExchangeAdapter(
            transport=transport,
            token=settings.read_only_api_token,
            rate_limiter=SlidingWindowRateLimiter(),
            market_policy=settings.market_policy,
        )
