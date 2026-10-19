"""Strict runtime configuration and the active Nobitex/USDT market whitelist."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from collections.abc import Mapping
from urllib.parse import urlsplit

from evoryn.domain.models import Market

SUPPORTED_QUOTE_CURRENCIES: frozenset[str] = frozenset({"USDT"})
DEFAULT_ALLOWED_QUOTE_CURRENCIES: tuple[str, ...] = ("USDT",)
OFFICIAL_NOBITEX_BASE_URL = "https://apiv2.nobitex.ir"


@dataclass(frozen=True, slots=True)
class MarketPolicy:
    """Hard market whitelist shared by config, the Risk Engine, and Nobitex reads."""

    allowed_quote_currencies: tuple[str, ...] = DEFAULT_ALLOWED_QUOTE_CURRENCIES
    allowed_venues: tuple[str, ...] = ("nobitex",)
    allowed_market_types: tuple[str, ...] = ("spot",)

    def __post_init__(self) -> None:
        normalized = tuple(value.strip().upper() for value in self.allowed_quote_currencies)
        if any(not value or not value.isalnum() for value in normalized):
            raise ValueError("Quote-currency whitelist entries must be non-empty currency codes.")
        if len(normalized) != len(set(normalized)):
            raise ValueError("Quote-currency whitelist cannot contain duplicates.")
        unsupported = set(normalized) - SUPPORTED_QUOTE_CURRENCIES
        if unsupported:
            raise ValueError(
                "Unsupported quote currency in configuration; only reviewed currencies "
                "may be enabled."
            )
        object.__setattr__(self, "allowed_quote_currencies", normalized)

        venues = tuple(value.strip().casefold() for value in self.allowed_venues)
        market_types = tuple(value.strip().casefold() for value in self.allowed_market_types)
        if venues != ("nobitex",):
            raise ValueError("This project scope only permits the Nobitex venue.")
        if market_types != ("spot",):
            raise ValueError("The generic Risk Engine only permits the Spot market type.")
        object.__setattr__(self, "allowed_venues", venues)
        object.__setattr__(self, "allowed_market_types", market_types)

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> MarketPolicy:
        """Build policy from ordinary config without allowing runtime scope expansion."""
        raw = values.get("EVORYN_ALLOWED_QUOTE_CURRENCIES", "USDT")
        currencies = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
        return cls(allowed_quote_currencies=currencies)

    @classmethod
    def from_environment(cls) -> MarketPolicy:
        """Load market policy from process environment; no secrets are read."""
        return cls.from_mapping(os.environ)

    @staticmethod
    def quote_currency_for_symbol(symbol: object) -> str | None:
        """Resolve an explicitly delimited BASE/QUOTE pair or compact BASEUSDT symbol."""
        if not isinstance(symbol, str):
            return None
        normalized = symbol.strip().upper()
        if not normalized:
            return None

        separators = tuple(separator for separator in ("/", "-", "_") if separator in normalized)
        if separators:
            if len(separators) != 1:
                return None
            parts = normalized.split(separators[0])
            if len(parts) != 2:
                return None
            base, quote = parts
            if not base.isalnum() or not quote.isalnum():
                return None
            return quote

        compact_quotes = tuple(
            quote
            for quote in sorted(SUPPORTED_QUOTE_CURRENCIES, key=len, reverse=True)
            if normalized.endswith(quote) and len(normalized) > len(quote)
        )
        if len(compact_quotes) != 1:
            return None
        base = normalized[: -len(compact_quotes[0])]
        return compact_quotes[0] if base.isalnum() else None

    def rejection_reason(self, market: object) -> str | None:
        """Return a stable reason code if market identity is outside the hard whitelist."""
        if not isinstance(market, Market):
            return "invalid_market"
        if market.venue.strip().casefold() not in self.allowed_venues:
            return "venue_not_allowed"
        if market.market_type.strip().casefold() not in self.allowed_market_types:
            return "market_type_not_allowed"
        quote = self.quote_currency_for_symbol(market.symbol)
        if quote is None:
            return "market_quote_unrecognized"
        if quote not in self.allowed_quote_currencies:
            return "quote_currency_not_allowed"
        return None

    def allows_market(self, market: object) -> bool:
        """Return true only when venue, market type, and quote currency all pass."""
        return self.rejection_reason(market) is None


@dataclass(frozen=True, slots=True)
class NobitexReadOnlyConfig:
    """Read-only Nobitex client config; tokens are excluded from repr."""

    market_policy: MarketPolicy = field(default_factory=MarketPolicy)
    read_only_api_token: str | None = field(default=None, repr=False)
    base_url: str = OFFICIAL_NOBITEX_BASE_URL
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "apiv2.nobitex.ir"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Read-only Nobitex config requires the official HTTPS API host.")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Nobitex timeout must be finite and positive.")
        if self.read_only_api_token is not None:
            token = self.read_only_api_token.strip()
            object.__setattr__(self, "read_only_api_token", token if token else None)

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> NobitexReadOnlyConfig:
        """Load config while refusing the legacy generic API token variable."""
        if values.get("NOBITEX_API_TOKEN", "").strip():
            raise ValueError(
                "The legacy NOBITEX_API_TOKEN name is blocked; configure "
                "NOBITEX_READONLY_API_TOKEN instead."
            )
        timeout_text = values.get("NOBITEX_TIMEOUT_SECONDS", "10")
        try:
            timeout_seconds = float(timeout_text)
        except (TypeError, ValueError) as exc:
            raise ValueError("NOBITEX_TIMEOUT_SECONDS must be numeric.") from exc
        return cls(
            market_policy=MarketPolicy.from_mapping(values),
            read_only_api_token=values.get("NOBITEX_READONLY_API_TOKEN") or None,
            base_url=values.get("NOBITEX_BASE_URL", OFFICIAL_NOBITEX_BASE_URL),
            timeout_seconds=timeout_seconds,
        )

    @classmethod
    def from_environment(cls) -> NobitexReadOnlyConfig:
        """Load runtime config from process environment or secret injection."""
        return cls.from_mapping(os.environ)
