"""Offline security tests for the wallet-only Nobitex live-read tool."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from evoryn.adapters.exchanges.nobitex_account import (
    NobitexReadOnlyAccountReader,
    NobitexUSDTAccountSnapshot,
    USDTWalletSnapshot,
    WalletPresence,
    WalletType,
)
from evoryn.infrastructure.exchange.rate_limit import SlidingWindowRateLimiter
from evoryn.ports.exchange_errors import AuthenticationError, InvalidExchangeRequest
from scripts.live_readonly_nobitex_usdt import (
    WalletOnlyNobitexTransport,
    format_snapshot,
)

NOW = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)
TOKEN = "dummy-not-a-real-token"


@dataclass
class FakeTransport:
    """Record calls and return fixtures without creating any network connection."""

    response: dict[str, object]
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
        self.calls.append((method, path, params, payload, token))
        return self.response


def test_only_exact_usdt_spot_wallet_read_reaches_upstream() -> None:
    upstream = FakeTransport(
        response={"status": "ok", "wallets": {"USDT": {"balance": "25", "blocked": "5"}}}
    )
    transport = WalletOnlyNobitexTransport(upstream=upstream, expected_token=TOKEN)

    response = transport.request(
        "GET",
        "/v2/wallets",
        params={"currencies": "usdt", "type": "spot"},
        token=TOKEN,
    )

    assert response["status"] == "ok"
    assert upstream.calls == [
        ("GET", "/v2/wallets", {"currencies": "usdt", "type": "spot"}, None, TOKEN)
    ]


@pytest.mark.parametrize(
    ("method", "path", "params", "payload"),
    [
        ("POST", "/market/orders/add", None, {"type": "buy", "amount": "1"}),
        ("POST", "/market/orders/status", None, {"id": 123}),
        ("GET", "/users/profile", None, None),
        ("GET", "/v2/wallets", {"currencies": "btc", "type": "spot"}, None),
        ("GET", "/v2/wallets", {"currencies": "usdt", "type": "all"}, None),
        (
            "GET",
            "/v2/wallets",
            {"currencies": "usdt", "type": "spot", "extra": "1"},
            None,
        ),
        ("GET", "/v2/wallets", {"currencies": "usdt", "type": "spot"}, {"x": 1}),
    ],
)
def test_other_requests_are_rejected_before_upstream_io(
    method: str,
    path: str,
    params: Mapping[str, str] | None,
    payload: Mapping[str, object] | None,
) -> None:
    upstream = FakeTransport(response={"status": "ok", "wallets": {}})
    transport = WalletOnlyNobitexTransport(upstream=upstream, expected_token=TOKEN)

    with pytest.raises(InvalidExchangeRequest):
        transport.request(method, path, params=params, payload=payload, token=TOKEN)

    assert upstream.calls == []


def test_missing_or_wrong_token_is_rejected_before_upstream_io() -> None:
    upstream = FakeTransport(response={"status": "ok", "wallets": {}})
    transport = WalletOnlyNobitexTransport(upstream=upstream, expected_token=TOKEN)
    params = {"currencies": "usdt", "type": "spot"}

    with pytest.raises(AuthenticationError):
        transport.request("GET", "/v2/wallets", params=params, token=None)
    with pytest.raises(AuthenticationError):
        transport.request("GET", "/v2/wallets", params=params, token="wrong-token")

    assert upstream.calls == []


def test_output_contains_minimal_usdt_wallet_data_only() -> None:
    snapshot = NobitexUSDTAccountSnapshot(
        spot=USDTWalletSnapshot(
            wallet_type=WalletType.SPOT,
            presence=WalletPresence.PRESENT,
            observed_at=NOW,
            balance=Decimal("25.00"),
            blocked=Decimal("5.00"),
        ),
        margin=USDTWalletSnapshot(
            wallet_type=WalletType.MARGIN,
            presence=WalletPresence.NOT_LISTED,
            observed_at=NOW,
        ),
    )

    result = format_snapshot(snapshot)

    assert '"currency": "USDT"' in result
    assert '"available_balance": "20.00"' in result
    assert '"presence": "not_listed"' in result
    assert '"balance": null' in result
    assert TOKEN not in result
    assert "BTC" not in result


def test_read_account_snapshot_makes_two_wallet_gets_and_nothing_else() -> None:
    upstream = FakeTransport(
        response={"status": "ok", "wallets": {"USDT": {"balance": "25", "blocked": "5"}}}
    )
    guard = WalletOnlyNobitexTransport(upstream=upstream, expected_token=TOKEN)
    reader = NobitexReadOnlyAccountReader(
        transport=guard,
        token=TOKEN,
        rate_limiter=SlidingWindowRateLimiter(clock=lambda: 10.0),
        clock=lambda: NOW,
    )

    snapshot = reader.read_account_snapshot()

    assert snapshot.spot.available_balance == Decimal("20")
    assert snapshot.margin.available_balance == Decimal("20")
    assert len(upstream.calls) == 2
    assert all(call[0] == "GET" and call[1] == "/v2/wallets" for call in upstream.calls)
    assert all(
        call[2] is not None and call[2]["currencies"] == "usdt"
        for call in upstream.calls
    )
