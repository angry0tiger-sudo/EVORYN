"""Read-only, USDT-only Nobitex account snapshots.

Only GET /v2/wallets is called. No profile, order, transfer, withdrawal, or mutation API
is part of this reader. Transport and clock are injectable for offline tests.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Callable, Protocol

from evoryn.adapters.exchanges.nobitex_transport import NobitexHTTPTransport
from evoryn.config import NobitexReadOnlyConfig
from evoryn.infrastructure.exchange.rate_limit import SlidingWindowRateLimiter
from evoryn.ports.contracts import RateLimiterPort
from evoryn.ports.exchange_errors import AuthenticationError, MalformedExchangeResponse
from evoryn.ports.rate_limits import RateLimitPolicy


class WalletType(StrEnum):
    """Wallet type supported by the documented /v2/wallets read endpoint."""

    SPOT = "spot"
    MARGIN = "margin"


class WalletPresence(StrEnum):
    """A missing USDT wallet is not silently converted to a zero balance."""

    PRESENT = "present"
    NOT_LISTED = "not_listed"


@dataclass(frozen=True, slots=True)
class USDTWalletSnapshot:
    """Point-in-time USDT wallet values; missing balances stay missing."""

    wallet_type: WalletType
    presence: WalletPresence
    observed_at: datetime
    balance: Decimal | None = None
    blocked: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.wallet_type, WalletType):
            raise ValueError("Wallet type must be an explicit WalletType.")
        if not isinstance(self.presence, WalletPresence):
            raise ValueError("Wallet presence must be an explicit WalletPresence.")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("Wallet observation time must include a timezone.")
        if self.presence is WalletPresence.PRESENT:
            if self.balance is None or self.blocked is None:
                raise ValueError("A present USDT wallet requires balance and blocked values.")
            for name, value in (("balance", self.balance), ("blocked", self.blocked)):
                if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                    raise ValueError(f"USDT wallet {name} must be finite and non-negative.")
            if self.blocked > self.balance:
                raise ValueError("USDT blocked balance exceeds the reported wallet balance.")
        elif self.balance is not None or self.blocked is not None:
            raise ValueError("A not-listed USDT wallet must not fabricate numeric balances.")

    @property
    def available_balance(self) -> Decimal | None:
        """Compute balance minus blocked only when both values are present and valid."""
        if self.presence is not WalletPresence.PRESENT:
            return None
        if self.balance is None or self.blocked is None:
            return None
        return self.balance - self.blocked


@dataclass(frozen=True, slots=True)
class NobitexUSDTAccountSnapshot:
    """Minimal USDT account state without profile or banking personal information."""

    spot: USDTWalletSnapshot
    margin: USDTWalletSnapshot

    def __post_init__(self) -> None:
        if self.spot.wallet_type is not WalletType.SPOT:
            raise ValueError("Spot snapshot must contain the spot wallet type.")
        if self.margin.wallet_type is not WalletType.MARGIN:
            raise ValueError("Margin snapshot must contain the margin wallet type.")


class NobitexAccountTransport(Protocol):
    """Narrow request contract; this reader only invokes GET /v2/wallets."""

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
        token: str | None = None,
    ) -> dict[str, object]:
        """Perform one request against the allowlisted official Nobitex host."""
        ...


_USDT_WALLET_READ_LIMIT = RateLimitPolicy(max_requests=10, window_seconds=60.0)


@dataclass(slots=True)
class NobitexReadOnlyAccountReader:
    """Read USDT Spot/Margin balances through a dedicated no-write reader."""

    transport: NobitexAccountTransport
    token: str | None = field(default=None, repr=False)
    rate_limiter: RateLimiterPort = field(
        default_factory=SlidingWindowRateLimiter,
        repr=False,
    )
    clock: Callable[[], datetime] = field(
        default_factory=lambda: lambda: datetime.now(UTC),
        repr=False,
    )

    @classmethod
    def from_config(
        cls,
        config: NobitexReadOnlyConfig,
        *,
        rate_limiter: RateLimiterPort | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> NobitexReadOnlyAccountReader:
        """Build the reader without making a network request."""
        transport = NobitexHTTPTransport(
            base_url=config.base_url,
            token=config.read_only_api_token,
            timeout_seconds=config.timeout_seconds,
        )
        return cls(
            transport=transport,
            token=config.read_only_api_token,
            rate_limiter=(
                rate_limiter if rate_limiter is not None else SlidingWindowRateLimiter()
            ),
            clock=clock if clock is not None else lambda: datetime.now(UTC),
        )

    @classmethod
    def from_environment(cls) -> NobitexReadOnlyAccountReader:
        """Build from secret-injected environment; does not start polling."""
        return cls.from_config(NobitexReadOnlyConfig.from_environment())

    def read_wallet(self, wallet_type: WalletType) -> USDTWalletSnapshot:
        """Read one explicit USDT wallet type and reject missing credential/config."""
        if not self.token or not self.token.strip():
            raise AuthenticationError(
                "NOBITEX_READONLY_API_TOKEN is required to read USDT wallet balances."
            )
        if not isinstance(wallet_type, WalletType):
            raise ValueError("Wallet type must be an explicit WalletType.")

        self.rate_limiter.acquire("nobitex:usdt-wallet-read", _USDT_WALLET_READ_LIMIT)
        observed_at = self.clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise RuntimeError("Wallet reader clock must return a timezone-aware datetime.")

        response = self.transport.request(
            "GET",
            "/v2/wallets",
            params={"currencies": "usdt", "type": wallet_type.value},
            token=self.token,
        )
        if response.get("status") != "ok":
            raise MalformedExchangeResponse("Nobitex wallet response status is not ok.")
        wallets_raw = response.get("wallets")
        if not isinstance(wallets_raw, Mapping):
            raise MalformedExchangeResponse("Nobitex USDT wallet response must be an object.")

        matches = [
            value
            for currency, value in wallets_raw.items()
            if isinstance(currency, str) and currency.upper() == "USDT"
        ]
        if len(matches) > 1:
            raise MalformedExchangeResponse("Nobitex returned duplicate USDT wallet keys.")
        if not matches:
            return USDTWalletSnapshot(
                wallet_type=wallet_type,
                presence=WalletPresence.NOT_LISTED,
                observed_at=observed_at,
            )
        wallet_raw = matches[0]
        if not isinstance(wallet_raw, Mapping):
            raise MalformedExchangeResponse("Nobitex USDT wallet entry must be an object.")
        balance = _decimal(wallet_raw.get("balance"), field_name="balance")
        blocked = _decimal(wallet_raw.get("blocked"), field_name="blocked")
        if blocked > balance:
            raise MalformedExchangeResponse("Nobitex blocked USDT balance exceeds total balance.")
        return USDTWalletSnapshot(
            wallet_type=wallet_type,
            presence=WalletPresence.PRESENT,
            observed_at=observed_at,
            balance=balance,
            blocked=blocked,
        )

    def read_account_snapshot(self) -> NobitexUSDTAccountSnapshot:
        """Read Spot and Margin USDT wallets; no other wallet/profile data is fetched."""
        return NobitexUSDTAccountSnapshot(
            spot=self.read_wallet(WalletType.SPOT),
            margin=self.read_wallet(WalletType.MARGIN),
        )


def _decimal(value: object, *, field_name: str) -> Decimal:
    """Parse numeric source values without accepting floats or booleans."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise MalformedExchangeResponse(
            f"Nobitex USDT wallet field {field_name!r} is missing or invalid."
        )
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MalformedExchangeResponse(
            f"Nobitex USDT wallet field {field_name!r} is not decimal."
        ) from exc
    if not result.is_finite() or result < 0:
        raise MalformedExchangeResponse(
            f"Nobitex USDT wallet field {field_name!r} must be finite and non-negative."
        )
    return result
