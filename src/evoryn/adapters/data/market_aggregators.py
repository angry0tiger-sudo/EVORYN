"""Official CoinMarketCap/CoinGecko JSON API adapters with explicit license checks."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin

from evoryn.adapters.data.license_registry import SourceLicenseRegistry
from evoryn.adapters.network.json_http import JsonApiTransport
from evoryn.domain.external_data import DataUse, ExternalMarketQuote, require_aware_utc

_CMC_BASE_URL = "https://pro-api.coinmarketcap.com"
_COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z0-9]{2,12}$")


class ExternalProviderDataError(ValueError):
    """A successful HTTP response that violates the provider JSON contract."""


class CoinMarketCapConnector:
    """Read one asset by stable CoinMarketCap numeric ID; never submits exchange orders."""

    source_id = "coinmarketcap"

    def __init__(
        self,
        *,
        transport: JsonApiTransport,
        license_registry: SourceLicenseRegistry,
        api_key: str,
    ) -> None:
        if not api_key.strip():
            raise ValueError("CoinMarketCap API key must be injected from a secret store.")
        self._transport = transport
        self._licenses = license_registry
        self._api_key = api_key

    def get_quote(
        self,
        asset_id: int,
        *,
        convert: str,
        now: datetime,
    ) -> ExternalMarketQuote:
        """Fetch one authenticated quote and preserve provider/receipt/terms provenance."""
        require_aware_utc(now, "now")
        if type(asset_id) is not int or asset_id <= 0:
            raise ValueError("CoinMarketCap asset_id must be a positive numeric ID.")
        currency = convert.upper()
        if not _CURRENCY_PATTERN.fullmatch(currency):
            raise ValueError("convert must be an explicit fiat or crypto currency code.")
        authorization = self._licenses.require(
            self.source_id, DataUse.MARKET_REFERENCE, now=now
        )
        if authorization.base_url.rstrip("/") != _CMC_BASE_URL:
            raise PermissionError(
                "CoinMarketCap authorization must reference its official API host."
            )
        url = urljoin(_CMC_BASE_URL + "/", "v1/cryptocurrency/quotes/latest")
        payload = self._transport.get_json(
            url,
            headers={"X-CMC_PRO_API_KEY": self._api_key},
            params={"id": str(asset_id), "convert": currency},
        )
        item = _mapping(payload, "root").get("data")
        if not isinstance(item, dict):
            raise ExternalProviderDataError("CoinMarketCap response omitted data mapping.")
        asset = item.get(str(asset_id))
        if not isinstance(asset, dict):
            raise ExternalProviderDataError(
                "CoinMarketCap did not return the requested numeric ID."
            )
        quote_map = asset.get("quote")
        if not isinstance(quote_map, dict) or not isinstance(quote_map.get(currency), dict):
            raise ExternalProviderDataError(
                "CoinMarketCap response omitted requested quote currency."
            )
        quote = quote_map[currency]
        observed = _timestamp(quote.get("last_updated") or asset.get("last_updated"))
        digest = _payload_digest(payload)
        return ExternalMarketQuote(
            source_id=self.source_id,
            provider_asset_id=str(asset_id),
            asset_name=_required_string(asset, "name"),
            symbol=_required_string(asset, "symbol").upper(),
            quote_currency=currency,
            price=_decimal(quote.get("price"), "price"),
            market_cap=_optional_decimal(quote.get("market_cap"), "market_cap"),
            volume_24h=_optional_decimal(quote.get("volume_24h"), "volume_24h"),
            observed_at=observed,
            retrieved_at=now.astimezone(UTC),
            source_license_id=authorization.license_id,
            source_terms_revision=authorization.terms_revision,
            attribution_text=authorization.attribution_text,
            payload_sha256=digest,
        )


class CoinGeckoConnector:
    """Read one CoinGecko slug using the documented API v3 endpoint."""

    source_id = "coingecko"

    def __init__(
        self,
        *,
        transport: JsonApiTransport,
        license_registry: SourceLicenseRegistry,
        api_key: str | None = None,
        api_key_header: str = "x-cg-demo-api-key",
    ) -> None:
        if api_key is not None and not api_key.strip():
            raise ValueError("api_key must be None or a non-empty secret value.")
        if api_key_header not in {"x-cg-demo-api-key", "x-cg-pro-api-key"}:
            raise ValueError("Only documented CoinGecko key header names are accepted.")
        self._transport = transport
        self._licenses = license_registry
        self._api_key = api_key
        self._api_key_header = api_key_header

    def get_quote(
        self,
        asset_id: str,
        *,
        vs_currency: str,
        now: datetime,
    ) -> ExternalMarketQuote:
        """Fetch one exact CoinGecko ID, without resolving ambiguous ticker symbols."""
        require_aware_utc(now, "now")
        if not _SLUG_PATTERN.fullmatch(asset_id):
            raise ValueError("CoinGecko asset_id must be a normalized provider slug.")
        currency = vs_currency.lower()
        if not re.fullmatch(r"[a-z0-9]{2,12}", currency):
            raise ValueError("vs_currency must be an explicit lower-case currency ID.")
        authorization = self._licenses.require(
            self.source_id, DataUse.MARKET_REFERENCE, now=now
        )
        if authorization.base_url.rstrip("/") != _COINGECKO_BASE_URL:
            raise PermissionError("CoinGecko authorization must reference its official API base.")
        headers = {} if self._api_key is None else {self._api_key_header: self._api_key}
        url = urljoin(_COINGECKO_BASE_URL + "/", "coins/markets")
        payload = self._transport.get_json(
            url,
            headers=headers,
            params={"vs_currency": currency, "ids": asset_id},
        )
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise ExternalProviderDataError(
                "CoinGecko response must contain exactly one market row."
            )
        asset = payload[0]
        if asset.get("id") != asset_id:
            raise ExternalProviderDataError("CoinGecko returned an asset ID other than requested.")
        observed = _timestamp(asset.get("last_updated"))
        return ExternalMarketQuote(
            source_id=self.source_id,
            provider_asset_id=asset_id,
            asset_name=_required_string(asset, "name"),
            symbol=_required_string(asset, "symbol").upper(),
            quote_currency=currency.upper(),
            price=_decimal(asset.get("current_price"), "current_price"),
            market_cap=_optional_decimal(asset.get("market_cap"), "market_cap"),
            volume_24h=_optional_decimal(asset.get("total_volume"), "total_volume"),
            observed_at=observed,
            retrieved_at=now.astimezone(UTC),
            source_license_id=authorization.license_id,
            source_terms_revision=authorization.terms_revision,
            attribution_text=authorization.attribution_text,
            payload_sha256=_payload_digest(payload),
        )


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExternalProviderDataError(f"{name} must be a JSON object.")
    return value


def _required_string(mapping: dict[str, Any], name: str) -> str:
    value = mapping.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ExternalProviderDataError(f"Provider field {name!r} must be a non-empty string.")
    return value


def _decimal(value: Any, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ExternalProviderDataError(f"Provider field {name!r} is not numeric.") from exc
    if not parsed.is_finite():
        raise ExternalProviderDataError(f"Provider field {name!r} is not finite.")
    return parsed


def _optional_decimal(value: Any, name: str) -> Decimal | None:
    if value is None:
        return None
    result = _decimal(value, name)
    if result < 0:
        raise ExternalProviderDataError(f"Provider field {name!r} cannot be negative.")
    return result


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ExternalProviderDataError("Provider timestamp is missing.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExternalProviderDataError("Provider timestamp is invalid ISO-8601.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExternalProviderDataError("Provider timestamp must include a timezone.")
    return parsed.astimezone(UTC)


def _payload_digest(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
