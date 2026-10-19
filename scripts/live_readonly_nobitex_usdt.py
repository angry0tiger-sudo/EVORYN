"""Run one supervised, wallet-only Nobitex USDT read.

Only GET /v2/wallets with currencies=usdt and an explicit wallet type is allowed.
The token is read only from the environment. No CLI token argument, order path, retry loop,
poller, profile read, withdrawal, transfer, or account mutation is included.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from evoryn.adapters.exchanges.nobitex_account import (
    NobitexAccountTransport,
    NobitexReadOnlyAccountReader,
    NobitexUSDTAccountSnapshot,
    USDTWalletSnapshot,
    WalletPresence,
)
from evoryn.adapters.exchanges.nobitex_transport import NobitexHTTPTransport
from evoryn.config import NobitexReadOnlyConfig
from evoryn.infrastructure.exchange.rate_limit import SlidingWindowRateLimiter
from evoryn.ports.exchange_errors import AuthenticationError, InvalidExchangeRequest


@dataclass(slots=True)
class WalletOnlyNobitexTransport:
    """Restrict this CLI to exactly the two USDT wallet reads it needs."""

    upstream: NobitexAccountTransport = field(repr=False)
    expected_token: str = field(repr=False)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
        token: str | None = None,
    ) -> dict[str, object]:
        """Reject every request except the exact read-only wallet contract."""
        if method.upper() != "GET" or path != "/v2/wallets":
            raise InvalidExchangeRequest("Only GET /v2/wallets is allowed by this script.")
        if payload is not None:
            raise InvalidExchangeRequest("Wallet-only reads do not accept request payloads.")
        if params is None or set(params) != {"currencies", "type"}:
            raise InvalidExchangeRequest("Wallet reads require exactly currencies and type.")
        if params.get("currencies", "").strip().casefold() != "usdt":
            raise InvalidExchangeRequest("Only USDT wallet reads are allowed.")
        wallet_type = params.get("type", "").strip().casefold()
        if wallet_type not in {"spot", "margin"}:
            raise InvalidExchangeRequest("Wallet type must be spot or margin.")
        if token is None or token != self.expected_token:
            raise AuthenticationError("Read-only environment token is missing or inconsistent.")

        return self.upstream.request(
            "GET",
            "/v2/wallets",
            params={"currencies": "usdt", "type": wallet_type},
            token=token,
        )


def build_reader(config: NobitexReadOnlyConfig) -> NobitexReadOnlyAccountReader:
    """Construct a reader without network I/O."""
    token = config.read_only_api_token
    if not token:
        raise AuthenticationError("NOBITEX_READONLY_API_TOKEN is required.")
    upstream = NobitexHTTPTransport(
        base_url=config.base_url,
        token=token,
        timeout_seconds=config.timeout_seconds,
    )
    return NobitexReadOnlyAccountReader(
        transport=WalletOnlyNobitexTransport(upstream=upstream, expected_token=token),
        token=token,
        rate_limiter=SlidingWindowRateLimiter(),
        clock=lambda: datetime.now(UTC),
    )


def _decimal_text(value: Decimal | None) -> str | None:
    """Keep Decimal precision when serializing values."""
    return None if value is None else format(value, "f")


def _wallet_record(wallet: USDTWalletSnapshot) -> dict[str, str | None]:
    """Serialize only normalized USDT fields."""
    present = wallet.presence is WalletPresence.PRESENT
    return {
        "wallet_type": wallet.wallet_type.value,
        "presence": wallet.presence.value,
        "observed_at_utc": wallet.observed_at.astimezone(UTC).isoformat(),
        "currency": "USDT",
        "balance": _decimal_text(wallet.balance) if present else None,
        "blocked": _decimal_text(wallet.blocked) if present else None,
        "available_balance": _decimal_text(wallet.available_balance) if present else None,
    }


def format_snapshot(snapshot: NobitexUSDTAccountSnapshot) -> str:
    """Format a minimal account snapshot; no response body or personal identifiers."""
    payload = {
        "status": "ok",
        "venue": "nobitex",
        "mode": "read_only",
        "currency": "USDT",
        "wallets": {
            "spot": _wallet_record(snapshot.spot),
            "margin": _wallet_record(snapshot.margin),
        },
        "note": (
            "NOT_LISTED means the API response omitted a wallet; "
            "it is not an assertion of a zero balance."
        ),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def main() -> int:
    """Execute one read-only check and never print exception text or credentials."""
    try:
        config = NobitexReadOnlyConfig.from_environment()
        if not config.read_only_api_token:
            print(
                "ERROR: NOBITEX_READONLY_API_TOKEN is missing; no request was sent.",
                file=sys.stderr,
            )
            return 2
        reader = build_reader(config)
        snapshot = reader.read_account_snapshot()
        print(format_snapshot(snapshot))
        return 0
    except Exception as exc:
        # Exception text and traceback are suppressed to avoid accidental data leakage.
        print(
            f"ERROR: read-only check failed ({type(exc).__name__}); "
            "no token or response body was printed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
