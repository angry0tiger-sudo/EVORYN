"""Offline tests for the isolated Nobitex USDT account reader."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from evoryn.adapters.exchanges.nobitex_account import (
    NobitexReadOnlyAccountReader,
    WalletPresence,
    WalletType,
)
from evoryn.ports.exchange_errors import AuthenticationError, MalformedExchangeResponse

NOW = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)


@dataclass
class FakeAccountTransport:
    """Mock-only GET transport with explicit wallet responses."""

    responses: dict[str, dict[str, object]]
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
        if method != "GET" or path != "/v2/wallets" or params is None:
            raise AssertionError("Unexpected request to account-reader fake.")
        if set(params) != {"currencies", "type"}:
            raise AssertionError("Wallet request must use only currencies and type.")
        if params["currencies"].casefold() != "usdt":
            raise AssertionError("Wallet reader must request USDT only.")
        if params["type"] not in self.responses:
            raise AssertionError("Unexpected wallet type.")
        return self.responses[params["type"]]


def make_reader(
    transport: FakeAccountTransport,
    *,
    token: str | None = "dummy-read-only-token",
) -> NobitexReadOnlyAccountReader:
    from evoryn.infrastructure.exchange.rate_limit import SlidingWindowRateLimiter

    return NobitexReadOnlyAccountReader(
        transport=transport,
        token=token,
        rate_limiter=SlidingWindowRateLimiter(clock=lambda: 10.0),
        clock=lambda: NOW,
    )


def test_account_snapshot_reads_only_usdt_spot_and_margin_balances() -> None:
    transport = FakeAccountTransport(
        responses={
            "spot": {
                "status": "ok",
                "wallets": {
                    "USDT": {"balance": "100.25", "blocked": "10.25"},
                    "BTC": {"balance": "3", "blocked": "1"},
                },
            },
            "margin": {
                "status": "ok",
                "wallets": {"USDT": {"balance": "20", "blocked": "5"}},
            },
        }
    )

    snapshot = make_reader(transport).read_account_snapshot()

    assert snapshot.spot.presence is WalletPresence.PRESENT
    assert snapshot.spot.available_balance == Decimal("90.00")
    assert snapshot.margin.presence is WalletPresence.PRESENT
    assert snapshot.margin.available_balance == Decimal("15")
    assert [
        (call[0], call[1], call[2])
        for call in transport.calls
    ] == [
        ("GET", "/v2/wallets", {"currencies": "usdt", "type": "spot"}),
        ("GET", "/v2/wallets", {"currencies": "usdt", "type": "margin"}),
    ]
    assert all(call[3] is None for call in transport.calls)
    assert all(call[4] == "dummy-read-only-token" for call in transport.calls)


def test_account_read_without_token_stops_before_transport() -> None:
    transport = FakeAccountTransport(responses={"spot": {}, "margin": {}})

    with pytest.raises(AuthenticationError, match="NOBITEX_READONLY_API_TOKEN"):
        make_reader(transport, token=None).read_wallet(WalletType.SPOT)

    assert transport.calls == []


def test_missing_usdt_wallet_is_not_fabricated_as_zero() -> None:
    transport = FakeAccountTransport(
        responses={"spot": {"status": "ok", "wallets": {}}}
    )

    snapshot = make_reader(transport).read_wallet(WalletType.SPOT)

    assert snapshot.presence is WalletPresence.NOT_LISTED
    assert snapshot.balance is None
    assert snapshot.blocked is None
    assert snapshot.available_balance is None


def test_malformed_or_non_ok_wallet_responses_fail_closed() -> None:
    failed = FakeAccountTransport(
        responses={"spot": {"status": "failed", "wallets": {}}}
    )
    with pytest.raises(MalformedExchangeResponse, match="status is not ok"):
        make_reader(failed).read_wallet(WalletType.SPOT)

    bad_shape = FakeAccountTransport(
        responses={"spot": {"status": "ok", "wallets": []}}
    )
    with pytest.raises(MalformedExchangeResponse, match="must be an object"):
        make_reader(bad_shape).read_wallet(WalletType.SPOT)


@pytest.mark.parametrize(
    "wallet",
    [
        {"balance": "10"},
        {"balance": "10", "blocked": "11"},
        {"balance": 1.5, "blocked": "0"},
        {"balance": "NaN", "blocked": "0"},
    ],
)
def test_missing_inexact_or_inconsistent_usdt_balances_are_rejected(
    wallet: dict[str, object],
) -> None:
    transport = FakeAccountTransport(
        responses={"spot": {"status": "ok", "wallets": {"USDT": wallet}}}
    )

    with pytest.raises((MalformedExchangeResponse, ValueError)):
        make_reader(transport).read_wallet(WalletType.SPOT)


def test_account_reader_repr_never_includes_token() -> None:
    transport = FakeAccountTransport(
        responses={"spot": {"status": "ok", "wallets": {}}}
    )
    client = make_reader(transport, token="unit-test-secret-not-real")

    assert "unit-test-secret-not-real" not in repr(client)
