"""Offline tests for the public Nobitex historical UDF schema and transport policy."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from evoryn.domain.models import Market
from evoryn.learning_lab.nobitex_history import (
    NOBITEX_HISTORY_PATH,
    NOBITEX_PUBLIC_HOST,
    HistoricalFetchError,
    NobitexHistoricalClient,
    NobitexHistoryConfig,
    parse_udf_history_payload,
)

MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
START = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())


def payload() -> dict[str, object]:
    return {
        "s": "ok",
        "t": [START, START + 3600],
        "o": ["100", "101"],
        "h": ["102", "103"],
        "l": ["99", "100"],
        "c": ["101", "102"],
        "v": ["10.5", "11.25"],
    }


def response_for_rows(rows: list[tuple[int, str, str, str, str, str]]) -> dict[str, object]:
    return {
        "s": "ok",
        "t": [item[0] for item in rows],
        "o": [item[1] for item in rows],
        "h": [item[2] for item in rows],
        "l": [item[3] for item in rows],
        "c": [item[4] for item in rows],
        "v": [item[5] for item in rows],
    }


class FakeResponse:
    def __init__(self, url: str, body: bytes) -> None:
        self._url = url
        self._body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        return self._body[:size]


class FakeOpener:
    def __init__(self, bodies: list[dict[str, object]]) -> None:
        self.bodies = bodies
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def open(self, request: object, timeout: float) -> FakeResponse:
        from urllib.request import Request

        assert isinstance(request, Request)
        assert 0 < timeout <= 30
        self.requests.append((request.get_method(), request.full_url, dict(request.header_items())))
        body = self.bodies[len(self.requests) - 1]
        return FakeResponse(request.full_url, json.dumps(body).encode("utf-8"))


def test_parse_udf_history_response_into_canonical_complete_candles() -> None:
    candles = parse_udf_history_payload(
        payload(), market=MARKET, interval=timedelta(hours=1)
    )
    assert len(candles) == 2
    assert candles[0].timestamp == datetime.fromtimestamp(START, tz=UTC)
    assert candles[0].open == 100
    assert candles[1].close == 102
    assert candles[0].source == "nobitex-public-ohlcv"


def test_no_data_status_is_an_empty_page() -> None:
    assert parse_udf_history_payload(
        {"s": "no_data"}, market=MARKET, interval=timedelta(hours=1)
    ) == ()


@pytest.mark.parametrize(
    "change",
    [
        {"s": "error", "errmsg": "private/raw provider text"},
        {"t": [START]},
        {"v": ["1"]},
        {"t": [True, START + 3600]},
        {"c": ["NaN", "102"]},
        {"h": ["98", "103"]},
        {"v": ["-1", "11"]},
    ],
)
def test_malformed_or_invalid_provider_payload_fails_closed(
    change: dict[str, object],
) -> None:
    data = payload()
    data.update(change)
    with pytest.raises(HistoricalFetchError):
        parse_udf_history_payload(
            data, market=MARKET, interval=timedelta(hours=1)
        )


def test_config_builds_only_the_fixed_public_history_get_url() -> None:
    config = NobitexHistoryConfig(
        symbol="BTCUSDT", resolution="60", from_unix_seconds=START,
        to_unix_seconds=START + 100_000, max_pages=3,
    )
    url = urlsplit(config.request_url(2))
    assert url.scheme == "https"
    assert url.hostname == NOBITEX_PUBLIC_HOST
    assert url.path == NOBITEX_HISTORY_PATH
    assert parse_qs(url.query) == {
        "symbol": ["BTCUSDT"], "resolution": ["60"], "from": [str(START)],
        "to": [str(START + 100_000)], "page": ["2"],
    }
    assert "authorization" not in url.query.casefold()
    assert "token" not in url.query.casefold()


def test_history_config_and_page_numbers_are_bounded() -> None:
    config = NobitexHistoryConfig(
        symbol="BTCUSDT", resolution="60", from_unix_seconds=START,
        to_unix_seconds=START + 100_000, max_pages=3,
    )
    with pytest.raises(ValueError):
        NobitexHistoryConfig(
            symbol="BTC/USDT?host=evil", resolution="60",
            from_unix_seconds=START, to_unix_seconds=START + 100_000,
        )
    with pytest.raises(ValueError, match="page number"):
        config.request_url(4)


def test_public_fetch_paginates_only_fixed_route_and_sends_no_credentials() -> None:
    first_start = START + 100 * 3600
    first = [
        (first_start + idx * 3600, "100", "102", "99", "101", "10")
        for idx in range(500)
    ]
    second = [
        (START + idx * 3600, "100", "102", "99", "101", "10")
        for idx in range(100)
    ]
    opener = FakeOpener([response_for_rows(first), response_for_rows(second)])
    now = datetime.fromtimestamp(START + 1000 * 3600, tz=UTC)
    sleeps: list[float] = []
    config = NobitexHistoryConfig(
        symbol="BTCUSDT", resolution="60", from_unix_seconds=START,
        to_unix_seconds=START + 1000 * 3600, max_pages=3,
        minimum_candles=500, throttle_seconds=1.05,
    )
    client = NobitexHistoricalClient(
        config, opener=opener, sleep=sleeps.append, clock=lambda: now
    )
    candles = client.fetch(MARKET)
    assert len(candles) == 600
    assert client.pages_fetched == 2
    assert [request[0] for request in opener.requests] == ["GET", "GET"]
    for _, url, headers in opener.requests:
        parsed = urlsplit(url)
        assert parsed.scheme == "https"
        assert parsed.hostname == NOBITEX_PUBLIC_HOST
        assert parsed.path == NOBITEX_HISTORY_PATH
        assert "Authorization" not in headers
        assert "token" not in parsed.query.casefold()
    assert len(sleeps) == 1 and sleeps[0] == pytest.approx(1.05)
