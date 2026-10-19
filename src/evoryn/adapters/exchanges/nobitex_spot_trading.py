"""Isolated, explicitly allowlisted Nobitex Spot order gateway.

This module is not an ExchangePort and is never registered as a general plugin. All writes
are routed through LiveSpotExecutionService, which applies the deterministic Risk Engine,
instrument rules, durable idempotency journal, and the disabled-by-default owner gate.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from evoryn.config import (
    MarketPolicy,
    NobitexReadOnlyConfig,
    OFFICIAL_NOBITEX_BASE_URL,
)
from evoryn.domain.models import Market, Order, OrderRequest, OrderStatus, OrderType, Side
from evoryn.ports.exchange_errors import (
    AuthenticationError,
    AuthorizationError,
    ExchangeTransportError,
    ExchangeUnavailable,
    InvalidExchangeRequest,
    MalformedExchangeResponse,
    OrderOutcomeUnknown,
    RateLimitExceeded,
    TradingDisabledError,
)


class _Response(Protocol):
    """Minimal response protocol so tests can inject a socket-free fake."""

    def __enter__(self) -> _Response:
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool | None:
        ...

    def read(self, size: int = -1) -> bytes:
        ...


class _Opener(Protocol):
    """Narrow urllib opener protocol used by the real and fake transports."""

    def open(self, request: Request, *, timeout: float) -> _Response:
        ...


class _NoRedirectHandler(HTTPRedirectHandler):
    """Never follow a redirect from an order endpoint."""

    def redirect_request(
        self,
        request: Request,
        file_pointer: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


def _env_true(value: str | None) -> bool:
    """Parse one explicit boolean environment value without truthiness shortcuts."""
    return (value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _decode_urlsafe_base64(value: str, *, label: str) -> bytes:
    """Decode URL-safe Base64 without leaking supplied key material in errors."""
    if not isinstance(value, str) or not value.strip():
        raise AuthenticationError(f"Nobitex {label} is missing.")
    try:
        raw = value.strip().encode("ascii")
        padded = raw + b"=" * ((-len(raw)) % 4)
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise AuthenticationError(f"Nobitex {label} is not valid URL-safe Base64.") from exc


def _decimal(value: object, *, label: str) -> Decimal:
    """Require an exact finite Decimal representation from provider data."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise MalformedExchangeResponse(f"Nobitex order field {label} is missing or invalid.")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MalformedExchangeResponse(f"Nobitex order field {label} is not decimal.") from exc
    if not parsed.is_finite():
        raise MalformedExchangeResponse(f"Nobitex order field {label} is not finite.")
    return parsed


def split_usdt_symbol(market: Market, policy: MarketPolicy) -> tuple[str, str]:
    """Normalize an explicitly delimited or compact symbol under the current USDT whitelist."""
    reason = policy.rejection_reason(market)
    if reason is not None:
        raise InvalidExchangeRequest(
            "Nobitex write requires an allowed Nobitex Spot/USDT market."
        )
    normalized = market.symbol.strip().upper()
    for separator in ("/", "-", "_"):
        if separator in normalized:
            parts = normalized.split(separator)
            if len(parts) != 2 or not all(part.isalnum() for part in parts):
                raise InvalidExchangeRequest("Market symbol must be one BASE/USDT pair.")
            base, quote = parts
            if quote != "USDT":
                raise InvalidExchangeRequest("Only USDT-quoted Spot orders are allowed.")
            return base.lower(), quote.lower()
    if normalized.endswith("USDT") and len(normalized) > 4:
        base = normalized[:-4]
        if base.isalnum():
            return base.lower(), "usdt"
    raise InvalidExchangeRequest("Market symbol is not a recognized BASE/USDT pair.")


_STATUS_MAP: dict[str, OrderStatus] = {
    "new": OrderStatus.PENDING,
    "inactive": OrderStatus.PENDING,
    "pending": OrderStatus.PENDING,
    "active": OrderStatus.OPEN,
    "open": OrderStatus.OPEN,
    "partiallymatched": OrderStatus.PARTIALLY_FILLED,
    "partiallyfilled": OrderStatus.PARTIALLY_FILLED,
    "done": OrderStatus.FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "cancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
}


def normalize_nobitex_order(
    raw: Mapping[str, object],
    *,
    requested_market: Market,
    expected_client_order_id: str | None = None,
) -> Order:
    """Normalize a single documented Spot order response without trusting raw fields."""
    order_id_raw = raw.get("id")
    if isinstance(order_id_raw, bool) or not str(order_id_raw or "").isdigit():
        raise MalformedExchangeResponse("Nobitex order response lacks a numeric order id.")

    side_raw = raw.get("type")
    if not isinstance(side_raw, str):
        raise MalformedExchangeResponse("Nobitex order response lacks an order side.")
    try:
        side = Side(side_raw.strip().lower())
    except ValueError as exc:
        raise MalformedExchangeResponse("Nobitex order side is unsupported.") from exc

    status_raw = raw.get("status")
    if not isinstance(status_raw, str):
        raise MalformedExchangeResponse("Nobitex order response lacks a status.")
    normalized_status = status_raw.strip().lower().replace("_", "").replace(" ", "")
    status = _STATUS_MAP.get(normalized_status, OrderStatus.UNKNOWN)
    if status is OrderStatus.UNKNOWN:
        raise OrderOutcomeUnknown("Nobitex returned an unmapped order status.")

    remote_client_id = raw.get("clientOrderId")
    if (
        expected_client_order_id is not None
        and remote_client_id is not None
        and remote_client_id != expected_client_order_id
    ):
        raise OrderOutcomeUnknown(
            "Nobitex response clientOrderId does not match the submitted intent."
        )

    remote_market = raw.get("market")
    if isinstance(remote_market, str) and remote_market.strip():
        normalized_remote = (
            remote_market.upper().replace("/", "").replace("-", "").replace("_", "")
        )
        base, quote = split_usdt_symbol(requested_market, MarketPolicy())
        if normalized_remote != f"{base}{quote}".upper():
            raise OrderOutcomeUnknown(
                "Nobitex response market does not match the requested market."
            )

    quantity = _decimal(raw.get("amount"), label="amount")
    if quantity <= 0:
        raise MalformedExchangeResponse("Nobitex order amount must be positive.")
    price_raw = raw.get("price")
    price: Decimal | None = None
    if price_raw is not None and str(price_raw).strip().lower() != "market":
        price = _decimal(price_raw, label="price")
        if price <= 0:
            raise MalformedExchangeResponse("Nobitex order price must be positive.")

    return Order(
        order_id=str(order_id_raw),
        market_symbol=requested_market.symbol,
        side=side,
        quantity=quantity,
        status=status,
        price=price,
        client_order_id=(
            remote_client_id if isinstance(remote_client_id, str) else expected_client_order_id
        ),
    )


class NobitexSpotTradingTransport:
    """Private signed transport with three fixed Spot-order routes and no generic request API.

    The object supports order creation, status lookup and single-order cancellation only.
    It has no routes for margin, withdrawal, transfer, conversion, profile changes, or bulk cancel.
    Write methods are called only by the risk-gated application service.
    """

    _ORDER_ADD = "/market/orders/add"
    _ORDER_STATUS = "/market/orders/status"
    _ORDER_CANCEL = "/market/orders/update-status"
    _MAX_RESPONSE_BYTES = 2_000_000

    def __init__(
        self,
        *,
        api_key: str,
        private_key: str,
        base_url: str = OFFICIAL_NOBITEX_BASE_URL,
        timeout_seconds: float = 10.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        opener: _Opener | None = None,
        market_policy: MarketPolicy | None = None,
    ) -> None:
        """Construct a signed client. No network request occurs during construction."""
        parsed = urlsplit(base_url)
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
            raise ValueError("Nobitex trading transport requires the official HTTPS API host.")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Nobitex timeout must be finite and positive.")

        public_bytes = _decode_urlsafe_base64(api_key, label="API public key")
        private_bytes = _decode_urlsafe_base64(private_key, label="API private key")
        if len(public_bytes) != 32 or len(private_bytes) != 32:
            raise AuthenticationError("Nobitex Ed25519 API keys must each decode to 32 bytes.")
        signer = Ed25519PrivateKey.from_private_bytes(private_bytes)
        derived_public = signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        if derived_public != public_bytes:
            raise AuthenticationError("Nobitex public and private API keys do not match.")

        running_in_ci = _env_true(os.environ.get("CI")) or _env_true(
            os.environ.get("GITHUB_ACTIONS")
        )
        if running_in_ci and opener is None:
            raise TradingDisabledError(
                "A real Nobitex write transport cannot be constructed in CI or test automation."
            )
        self._api_key = api_key.strip()
        self._signer = signer
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._clock = clock
        self._opener = opener if opener is not None else build_opener(_NoRedirectHandler())
        self._market_policy = market_policy or MarketPolicy()

    def submit_order(self, request: OrderRequest, *, price_guard: Decimal) -> Order:
        """Submit exactly one validated Spot order; network outcomes are never retried."""
        if not isinstance(request, OrderRequest):
            raise InvalidExchangeRequest(
                "Nobitex Spot submission requires a validated OrderRequest."
            )
        base, quote = split_usdt_symbol(request.market, self._market_policy)
        if len(request.client_order_id) > 32 or not request.client_order_id.strip():
            raise InvalidExchangeRequest("Nobitex clientOrderId must contain 1 to 32 characters.")
        if not all(char.isalnum() or char in "._-" for char in request.client_order_id):
            raise InvalidExchangeRequest("Nobitex clientOrderId contains unsupported characters.")
        if request.order_type is not OrderType.LIMIT:
            raise InvalidExchangeRequest(
                "Only Limit orders are enabled until Market-order semantics are verified."
            )
        if request.price is None or request.price != price_guard:
            raise InvalidExchangeRequest("Limit order price and approved price must match.")
        if not price_guard.is_finite() or price_guard <= 0:
            raise InvalidExchangeRequest("An explicit positive limit price is required.")

        payload: dict[str, object] = {
            "type": request.side.value,
            "execution": "limit",
            "tradeType": "spot",
            "srcCurrency": base,
            "dstCurrency": quote,
            "amount": format(request.quantity, "f"),
            "clientOrderId": request.client_order_id,
            "price": format(price_guard, "f"),
        }

        response = self._post_json(self._ORDER_ADD, payload)
        if response.get("status") == "failed":
            code = response.get("code")
            safe_code = (
                "".join(char for char in code if char.isalnum() or char in "_-")[:64]
                if isinstance(code, str)
                else "provider_rejected"
            )
            raise InvalidExchangeRequest(f"Nobitex rejected the Spot order ({safe_code}).")
        raw_order = response.get("order")
        if not isinstance(raw_order, Mapping):
            raise OrderOutcomeUnknown("Nobitex submission response has no usable order object.")
        try:
            return normalize_nobitex_order(
                raw_order,
                requested_market=request.market,
                expected_client_order_id=request.client_order_id,
            )
        except (MalformedExchangeResponse, InvalidExchangeRequest) as exc:
            raise OrderOutcomeUnknown(
                "Nobitex may have accepted the order, but its acknowledgement was unusable."
            ) from exc

    def get_order_status(
        self,
        *,
        requested_market: Market,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> Order | None:
        """Perform a read-only status query by exactly one numeric or client order identity."""
        split_usdt_symbol(requested_market, self._market_policy)
        if (order_id is None) == (client_order_id is None):
            raise InvalidExchangeRequest("Provide exactly one of order_id or client_order_id.")
        payload: dict[str, object]
        if order_id is not None:
            normalized_id = order_id.strip()
            if not normalized_id.isdigit():
                raise InvalidExchangeRequest("Nobitex order_id must be numeric.")
            payload = {"id": int(normalized_id)}
        else:
            if client_order_id is None or not client_order_id or len(client_order_id) > 32:
                raise InvalidExchangeRequest(
                    "Nobitex clientOrderId must contain 1 to 32 characters."
                )
            payload = {"clientOrderId": client_order_id}

        response = self._post_json(self._ORDER_STATUS, payload)
        if response.get("status") == "failed":
            return None
        raw_order = response.get("order")
        if not isinstance(raw_order, Mapping):
            raise MalformedExchangeResponse("Nobitex order-status response lacks an order object.")
        return normalize_nobitex_order(
            raw_order,
            requested_market=requested_market,
            expected_client_order_id=client_order_id,
        )

    def cancel_order(self, *, requested_market: Market, known_order: Order) -> Order:
        """Cancel one explicitly known, nonterminal order and verify the provider response."""
        split_usdt_symbol(requested_market, self._market_policy)
        if not isinstance(known_order, Order):
            raise InvalidExchangeRequest("Cancellation requires a journaled normalized Order.")
        if known_order.market_symbol != requested_market.symbol:
            raise InvalidExchangeRequest("Cancellation market does not match the journaled order.")
        if known_order.status not in {
            OrderStatus.PENDING,
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
        }:
            raise InvalidExchangeRequest("Only a known nonterminal Spot order may be canceled.")
        normalized_id = known_order.order_id.strip()
        if not normalized_id.isdigit():
            raise InvalidExchangeRequest("Nobitex cancellation requires a numeric order id.")
        response = self._post_json(
            self._ORDER_CANCEL,
            {"order": int(normalized_id), "status": "canceled"},
        )
        if response.get("status") == "failed":
            raise InvalidExchangeRequest("Nobitex rejected the requested order cancellation.")
        updated_status = response.get("updatedStatus")
        if not isinstance(updated_status, str) or updated_status.strip().lower() not in {
            "canceled",
            "cancelled",
        }:
            raise OrderOutcomeUnknown(
                "Nobitex cancellation acknowledgement is not conclusive; reconcile before retry."
            )
        return replace(known_order, status=OrderStatus.CANCELLED)

    def _post_json(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
        """Sign and send one fixed POST request; never retry a state-changing operation."""
        self._validate_payload(path, payload)
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Nobitex signing clock must return a timezone-aware datetime.")
        timestamp = str(int(now.astimezone(UTC).timestamp()))
        body_text = json.dumps(dict(payload), separators=(",", ":"), ensure_ascii=False)
        signature_input = f"{timestamp}POST{path}{body_text}".encode("utf-8")
        signature = base64.urlsafe_b64encode(self._signer.sign(signature_input)).decode("ascii")
        url = f"{self._base_url}{path}"
        request = Request(
            url,
            data=body_text.encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "EVORYN/0.1",
                "Nobitex-Key": self._api_key,
                "Nobitex-Signature": signature,
                "Nobitex-Timestamp": timestamp,
            },
            method="POST",
        )
        is_write = path in {self._ORDER_ADD, self._ORDER_CANCEL}
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(self._MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if exc.code == 401:
                raise AuthenticationError("Nobitex rejected API-key authentication.") from exc
            if exc.code == 403:
                raise AuthorizationError(
                    "Nobitex denied the requested API-key permission."
                ) from exc
            if is_write and (exc.code in {408, 425} or exc.code >= 500 or 300 <= exc.code < 400):
                raise OrderOutcomeUnknown(
                    "Nobitex order-write response is uncertain; reconcile before any further write."
                ) from exc
            if exc.code == 429:
                retry_after = _retry_after(exc.headers.get("Retry-After"))
                raise RateLimitExceeded(
                    "Nobitex rate limit rejected the request.",
                    retry_after_seconds=retry_after,
                ) from exc
            if exc.code >= 500:
                raise ExchangeUnavailable("Nobitex service returned a server error.") from exc
            raise InvalidExchangeRequest(
                f"Nobitex rejected the API request with HTTP {exc.code}."
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            if is_write:
                raise OrderOutcomeUnknown(
                    "Nobitex order-write outcome is unknown; reconcile before any further write."
                ) from exc
            raise ExchangeTransportError(
                "Nobitex status request failed at the transport boundary."
            ) from exc

        if len(raw) > self._MAX_RESPONSE_BYTES:
            if is_write:
                raise OrderOutcomeUnknown(
                    "Nobitex order-write response exceeded the bounded size limit; reconcile first."
                )
            raise MalformedExchangeResponse("Nobitex response exceeded the bounded size limit.")
        try:
            decoded = json.loads(raw.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            if is_write:
                raise OrderOutcomeUnknown(
                    "Nobitex order-write response could not be parsed; "
                    "reconcile before another write."
                ) from exc
            raise MalformedExchangeResponse(
                "Nobitex returned invalid JSON for order status."
            ) from exc
        if not isinstance(decoded, dict):
            if is_write:
                raise OrderOutcomeUnknown(
                    "Nobitex order-write response was not an object; "
                    "reconcile before another write."
                )
            raise MalformedExchangeResponse("Nobitex order-status response must be an object.")
        return decoded

    @classmethod
    def _validate_payload(cls, path: str, payload: Mapping[str, object]) -> None:
        """Enforce exact endpoint schemas so this transport cannot become a generic API tunnel."""
        keys = set(payload)
        if path == cls._ORDER_ADD:
            required = {
                "type",
                "execution",
                "tradeType",
                "srcCurrency",
                "dstCurrency",
                "amount",
                "clientOrderId",
                "price",
            }
            if keys != required:
                raise InvalidExchangeRequest("Spot order payload has an unexpected field set.")
            if payload["type"] not in {"buy", "sell"}:
                raise InvalidExchangeRequest("Spot order side is unsupported.")
            if payload["execution"] != "limit" or payload["tradeType"] != "spot":
                raise InvalidExchangeRequest("Only Spot Limit orders are allowlisted.")
            if str(payload["dstCurrency"]).lower() != "usdt":
                raise InvalidExchangeRequest("Only USDT-quoted Spot orders are allowed.")
            amount = _decimal(payload["amount"], label="amount")
            price = _decimal(payload["price"], label="price")
            if amount <= 0 or price <= 0:
                raise InvalidExchangeRequest(
                "Spot order amount and price must be positive."
            )
            client_id = payload["clientOrderId"]
            if not isinstance(client_id, str) or not client_id or len(client_id) > 32:
                raise InvalidExchangeRequest("Spot clientOrderId must contain 1 to 32 characters.")
            if (
                not isinstance(payload["srcCurrency"], str)
                or not payload["srcCurrency"].isalnum()
            ):
                raise InvalidExchangeRequest("Spot base currency is invalid.")
        elif path == cls._ORDER_STATUS:
            if keys == {"id"}:
                if isinstance(payload["id"], bool) or not str(payload["id"]).isdigit():
                    raise InvalidExchangeRequest("Order-status id must be numeric.")
            elif keys == {"clientOrderId"}:
                value = payload["clientOrderId"]
                if not isinstance(value, str) or not value or len(value) > 32:
                    raise InvalidExchangeRequest("Order-status clientOrderId is invalid.")
            else:
                raise InvalidExchangeRequest("Order status needs exactly id or clientOrderId.")
        elif path == cls._ORDER_CANCEL:
            if keys != {"order", "status"}:
                raise InvalidExchangeRequest("Cancellation payload has an unexpected field set.")
            if isinstance(payload["order"], bool) or not str(payload["order"]).isdigit():
                raise InvalidExchangeRequest("Cancellation order id must be numeric.")
            if payload["status"] != "canceled":
                raise InvalidExchangeRequest("Only single-order cancellation is allowed.")
        else:
            raise TradingDisabledError("Nobitex trading transport route is not allowlisted.")


def _retry_after(value: str | None) -> float:
    """Parse Retry-After as a bounded delay for operator information; do not retry writes."""
    if value is None:
        return 0.0
    try:
        result = float(value.strip())
    except (TypeError, ValueError):
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                return 0.0
            result = (when.astimezone(UTC) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0.0
    if not math.isfinite(result):
        return 0.0
    return max(0.0, result)
