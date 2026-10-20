"""Public, unauthenticated, read-only Nobitex UDF historical-candle transport.

This client is deliberately restricted to one fixed HTTPS hostname and the public
/market/udf/history GET route. It has no credentials, live account methods, order routes,
or configurable host. Responses are treated as untrusted input and validated before use.
"""

from __future__ import annotations

import json
import re
import ssl
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import IO, Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from evoryn.domain.models import Market, MarketCandle

NOBITEX_PUBLIC_HOST = "apiv2.nobitex.ir"
NOBITEX_HISTORY_PATH = "/market/udf/history"
NOBITEX_HISTORY_URI = f"https://{NOBITEX_PUBLIC_HOST}{NOBITEX_HISTORY_PATH}"
_SUPPORTED_RESOLUTIONS_MINUTES = {
    "1": 1,
    "5": 5,
    "15": 15,
    "30": 30,
    "60": 60,
    "180": 180,
    "240": 240,
    "360": 360,
    "720": 720,
    "D": 1440,
    "2D": 2880,
    "3D": 4320,
}
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{3,24}$")
_MAX_ROWS_PER_PAGE = 500
_MAX_RESPONSE_BYTES = 2_000_000


class HistoricalFetchError(RuntimeError):
    """Sanitized failure fetching public historical market data."""


@dataclass(frozen=True, slots=True)
class NobitexHistoryConfig:
    """Explicit bounded historical GET request parameters; no credentials are supported."""

    symbol: str
    resolution: str
    from_unix_seconds: int
    to_unix_seconds: int
    max_pages: int = 16
    timeout_seconds: float = 10.0
    throttle_seconds: float = 1.05
    minimum_candles: int = 500

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not _SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("symbol must be an uppercase compact market ticker.")
        if self.resolution not in _SUPPORTED_RESOLUTIONS_MINUTES:
            raise ValueError("resolution is not in the documented Nobitex UDF resolution set.")
        if (
            type(self.from_unix_seconds) is not int
            or type(self.to_unix_seconds) is not int
            or self.from_unix_seconds <= 0
            or self.to_unix_seconds <= self.from_unix_seconds
        ):
            raise ValueError("Historical time bounds must be ordered positive Unix seconds.")
        if type(self.max_pages) is not int or not 1 <= self.max_pages <= 40:
            raise ValueError("max_pages must be an integer in [1, 40].")
        if not 0 < self.timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be in (0, 30].")
        if not 0 <= self.throttle_seconds <= 5:
            raise ValueError("throttle_seconds must be in [0, 5].")
        if type(self.minimum_candles) is not int or self.minimum_candles < 100:
            raise ValueError("minimum_candles must be an integer >= 100.")

    @property
    def interval(self) -> timedelta:
        return timedelta(minutes=_SUPPORTED_RESOLUTIONS_MINUTES[self.resolution])

    def request_url(self, page: int) -> str:
        """Build a URL only for the fixed approved host/path, with bounded page numbering."""
        if type(page) is not int or not 1 <= page <= self.max_pages:
            raise ValueError("page number exceeds configured bounds.")
        query = urlencode(
            {
                "symbol": self.symbol,
                "resolution": self.resolution,
                "from": self.from_unix_seconds,
                "to": self.to_unix_seconds,
                "page": page,
            }
        )
        return f"{NOBITEX_HISTORY_URI}?{query}"


class _RejectRedirects(HTTPRedirectHandler):
    """Historical fetches do not follow redirects to other destinations."""

    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def parse_udf_history_payload(
    payload: object,
    *,
    market: Market,
    interval: timedelta,
    source: str = "nobitex-public-ohlcv",
) -> tuple[MarketCandle, ...]:
    """Validate one documented UDF history response without performing network I/O."""
    if not isinstance(payload, dict):
        raise HistoricalFetchError("UDF response must be a JSON object.")
    status = payload.get("s")
    if status == "no_data":
        return ()
    if status != "ok":
        raise HistoricalFetchError("Nobitex UDF did not return status=ok.")
    raw_columns: dict[str, object] = {
        "t": payload.get("t"),
        "o": payload.get("o"),
        "h": payload.get("h"),
        "l": payload.get("l"),
        "c": payload.get("c"),
        "v": payload.get("v"),
    }
    if any(not isinstance(column, list) for column in raw_columns.values()):
        raise HistoricalFetchError("UDF response is missing one or more OHLCV arrays.")
    columns = cast(dict[str, list[object]], raw_columns)
    sizes = {len(column) for column in columns.values()}
    if len(sizes) != 1:
        raise HistoricalFetchError("UDF OHLCV arrays have inconsistent lengths.")
    row_count = sizes.pop()
    if row_count > _MAX_ROWS_PER_PAGE:
        raise HistoricalFetchError("UDF response exceeds the documented per-page row limit.")
    result: list[MarketCandle] = []
    for index in range(row_count):
        raw_timestamp = columns["t"][index]
        if type(raw_timestamp) is not int or raw_timestamp <= 0:
            raise HistoricalFetchError("UDF candle timestamp is not a positive integer.")
        try:
            timestamp = datetime.fromtimestamp(raw_timestamp, tz=UTC)
        except (OverflowError, OSError, ValueError):
            raise HistoricalFetchError(
                "UDF candle timestamp is outside the supported datetime range."
            ) from None
        opening = _decimal(columns["o"][index], "open")
        high = _decimal(columns["h"][index], "high")
        low = _decimal(columns["l"][index], "low")
        close = _decimal(columns["c"][index], "close")
        volume = _decimal(columns["v"][index], "volume")
        if (
            opening <= 0
            or high <= 0
            or low <= 0
            or close <= 0
            or volume < 0
            or high < max(opening, close, low)
            or low > min(opening, close, high)
        ):
            raise HistoricalFetchError("UDF OHLCV candle violates price/volume invariants.")
        result.append(
            MarketCandle(
                market=market,
                timestamp=timestamp,
                interval=interval,
                open=opening,
                high=high,
                low=low,
                close=close,
                volume=volume,
                source=source,
            )
        )
    return tuple(result)


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise HistoricalFetchError(f"UDF {name} value is not numeric.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise HistoricalFetchError(f"UDF {name} value is invalid.") from exc
    if not result.is_finite():
        raise HistoricalFetchError(f"UDF {name} value is non-finite.")
    return result


class NobitexHistoricalClient:
    """Bounded unauthenticated history downloader; all network activity is GET-only."""

    def __init__(
        self,
        config: NobitexHistoryConfig,
        *,
        opener: Any | None = None,
        sleep: Any = time.sleep,
        clock: Any = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._opener = opener or build_opener(
            ProxyHandler({}),
            HTTPSHandler(context=ssl.create_default_context()),
            _RejectRedirects(),
        )
        self._sleep = sleep
        self._clock = clock
        self._pages_fetched = 0

    @property
    def pages_fetched(self) -> int:
        """Return the number of successful public history pages fetched so far."""
        return self._pages_fetched

    def fetch(self, market: Market) -> tuple[MarketCandle, ...]:
        """Fetch all requested pages, de-duplicate identical overlap, and reject truncation."""
        self._pages_fetched = 0
        if market.venue.casefold() != "nobitex":
            raise ValueError("Public historical training is restricted to Nobitex.")
        if market.market_type.casefold() != "spot":
            raise ValueError("Public historical training is restricted to Spot.")
        if market.symbol.replace("-", "").upper() != self._config.symbol:
            raise ValueError("Configured history symbol does not match the canonical market.")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("History-client clock must return a timezone-aware timestamp.")
        collected: dict[datetime, MarketCandle] = {}
        pages_fetched = 0
        terminated = False
        for page in range(1, self._config.max_pages + 1):
            prior_count = len(collected)
            payload = self._read_page(self._config.request_url(page))
            if isinstance(payload, dict) and payload.get("s") == "no_data":
                terminated = True
                break
            candles = parse_udf_history_payload(
                payload,
                market=market,
                interval=self._config.interval,
            )
            pages_fetched += 1
            self._pages_fetched = pages_fetched
            for candle in candles:
                candle_unix = int(candle.timestamp.timestamp())
                if not (
                    self._config.from_unix_seconds
                    <= candle_unix
                    <= self._config.to_unix_seconds
                ):
                    continue
                if candle.timestamp + candle.interval > now.astimezone(UTC):
                    continue
                existing = collected.get(candle.timestamp)
                if existing is not None and existing != candle:
                    raise HistoricalFetchError(
                        "Paginated UDF results conflict at a repeated timestamp."
                    )
                collected[candle.timestamp] = candle
            if len(candles) == _MAX_ROWS_PER_PAGE and len(collected) == prior_count:
                raise HistoricalFetchError("Pagination repeated a full page without advancing.")
            if len(candles) < _MAX_ROWS_PER_PAGE:
                terminated = True
                break
            if page < self._config.max_pages and self._config.throttle_seconds:
                self._sleep(self._config.throttle_seconds)
        if not terminated:
            raise HistoricalFetchError("Configured page limit reached; history may be truncated.")
        ordered = tuple(collected[timestamp] for timestamp in sorted(collected))
        if len(ordered) < self._config.minimum_candles:
            raise HistoricalFetchError(
                f"Insufficient historical candles: received {len(ordered)}, "
                f"required at least {self._config.minimum_candles}."
            )
        for left, right in zip(ordered, ordered[1:], strict=False):
            if right.timestamp - left.timestamp != self._config.interval:
                raise HistoricalFetchError("Historical OHLCV contains a gap or duplicate interval.")
        if pages_fetched == 0:
            raise HistoricalFetchError("Nobitex returned no historical candles.")
        return ordered

    def _read_page(self, url: str) -> object:
        """Issue one GET only; requests never carry Authorization or other credentials."""
        try:
            request = Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "EVORYN-research/1.0",
                },
                method="GET",
            )
            with self._opener.open(request, timeout=self._config.timeout_seconds) as response:
                final_url = response.geturl()
                from urllib.parse import urlsplit

                parsed = urlsplit(final_url)
                if (
                    parsed.scheme != "https"
                    or parsed.hostname != NOBITEX_PUBLIC_HOST
                    or parsed.path != NOBITEX_HISTORY_PATH
                    or parsed.username is not None
                    or parsed.password is not None
                ):
                    raise HistoricalFetchError("Historical response escaped the fixed HTTPS route.")
                body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise HistoricalFetchError("Historical UDF response exceeded the body size limit.")
            try:
                return json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HistoricalFetchError(
                    "Historical UDF response is not valid UTF-8 JSON."
                ) from exc
        except HTTPError as exc:
            # Do not propagate the raw server body, URL query, or headers to logs.
            raise HistoricalFetchError(f"Historical UDF GET returned HTTP {exc.code}.") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise HistoricalFetchError(
                f"Historical UDF GET failed with {type(exc).__name__}."
            ) from None
