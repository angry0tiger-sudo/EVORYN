"""Allowlisted Nobitex HTTP transport for read-only requests.

The transport deliberately rejects order-submit, order-cancel, withdrawal, transfer,
and every other unlisted route. Only GET reads and the provider's POST order-status
query are allowed. It does not implement API-key signing or any order-write route.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from evoryn.adapters.network.json_http import parse_retry_after_seconds
from evoryn.ports.exchange_errors import (
    AuthenticationError,
    AuthorizationError,
    ExchangeConflict,
    ExchangeResourceNotFound,
    ExchangeTimeout,
    ExchangeTransportError,
    ExchangeUnavailable,
    InvalidExchangeRequest,
    MalformedExchangeResponse,
    RateLimitExceeded,
)


class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject redirects to prevent credentials or requests escaping the allowlisted host."""

    def redirect_request(
        self,
        request: Request,
        file_pointer: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        """Do not follow any redirect from the fixed exchange host."""
        del request, file_pointer, code, message, headers, new_url
        return None


@dataclass(frozen=True, slots=True)
class NobitexHTTPTransport:
    """Perform allowlisted HTTPS reads against the official Nobitex API host."""

    base_url: str = "https://apiv2.nobitex.ir"
    token: str | None = field(default=None, repr=False)
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        """Validate the host and timeout before a request can be attempted."""
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
            raise ValueError("Nobitex transport requires the official HTTPS API host.")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("HTTP timeout must be finite and positive.")

    @staticmethod
    def _is_allowed_route(method: str, path: str) -> bool:
        """Return true only for explicitly reviewed read endpoints."""
        if method == "GET":
            if path in {
                "/market/stats",
                "/market/udf/history",
                "/market/orders/list",
                "/market/trades/list",
                "/v2/wallets",
                "/v2/options",
            }:
                return True
            return (
                path.startswith("/v3/orderbook/")
                and path.removeprefix("/v3/orderbook/").isalnum()
            ) or (
                path.startswith("/v2/trades/")
                and path.removeprefix("/v2/trades/").isalnum()
            )
        return method == "POST" and path == "/market/orders/status"

    @staticmethod
    def _is_ascii_alphanumeric(value: str) -> bool:
        return bool(value) and all(
            "0" <= character <= "9"
            or "A" <= character <= "Z"
            or "a" <= character <= "z"
            for character in value
        )

    @staticmethod
    def _is_ascii_digits(value: str) -> bool:
        return bool(value) and all("0" <= character <= "9" for character in value)

    @staticmethod
    def _is_usdt_symbol(symbol: str) -> bool:
        """Accept only compact BASEUSDT symbols at the lowest HTTP boundary."""
        normalized = symbol.strip().upper()
        return (
            normalized.isalnum()
            and normalized.endswith("USDT")
            and len(normalized) > len("USDT")
            and normalized[: -len("USDT")].isalnum()
        )

    @staticmethod
    def _raise_http_error(error: HTTPError) -> NoReturn:
        """Translate HTTP status into the shared provider-independent taxonomy."""
        if error.code == 401:
            raise AuthenticationError("Nobitex rejected authentication.")
        if error.code == 403:
            raise AuthorizationError("Nobitex denied access to the requested resource.")
        if error.code == 404:
            raise ExchangeResourceNotFound("Nobitex resource was not found.")
        if error.code == 409:
            raise ExchangeConflict("Nobitex reported a conflicting resource state.")
        if error.code == 429:
            retry_after = parse_retry_after_seconds(error.headers.get("Retry-After")) or 0.0
            raise RateLimitExceeded(
                "Nobitex request rate limit was exceeded.",
                retry_after_seconds=retry_after,
            )
        if error.code >= 500:
            raise ExchangeUnavailable(f"Nobitex service returned HTTP {error.code}.")
        if 300 <= error.code < 400:
            raise ExchangeTransportError("Nobitex redirect was rejected by transport policy.")
        raise InvalidExchangeRequest(f"Nobitex rejected the request with HTTP {error.code}.")

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
        token: str | None = None,
    ) -> dict[str, object]:
        """Send one allowlisted request and decode its JSON object response."""
        normalized_method = method.upper()
        if not self._is_allowed_route(normalized_method, path):
            raise InvalidExchangeRequest("Nobitex transport blocked a non-allowlisted route.")
        if normalized_method == "GET" and path == "/market/orders/list":
            allowed_order_params = {
                "srcCurrency", "dstCurrency", "details", "status",
                "fromId", "page", "pageSize",
            }
            if (
                params is None
                or set(params) - allowed_order_params
                or not self._is_ascii_alphanumeric(params.get("srcCurrency", "").strip())
                or params.get("dstCurrency", "").strip().casefold() != "usdt"
                or params.get("details", "2") not in {"1", "2"}
                or params.get("status", "open").strip().casefold()
                not in {"all", "open", "done", "close"}
                or ("page" in params and "fromId" in params)
                or (
                    "fromId" in params
                    and (
                        not self._is_ascii_digits(params["fromId"])
                        or len(params["fromId"]) > 19
                        or int(params["fromId"]) < 1
                    )
                )
                or (
                    "page" in params
                    and (
                        not self._is_ascii_digits(params["page"])
                        or len(params["page"]) > 9
                        or int(params["page"]) < 1
                    )
                )
                or (
                    "pageSize" in params
                    and (
                        not self._is_ascii_digits(params["pageSize"])
                        or len(params["pageSize"]) > 4
                        or not 1 <= int(params["pageSize"]) <= 1000
                    )
                )
            ):
                raise InvalidExchangeRequest(
                    "Nobitex order-list reads require a bounded Spot/USDT query."
                )
        if normalized_method == "GET" and path == "/market/trades/list":
            allowed_trade_params = {
                "srcCurrency", "dstCurrency", "fromId", "page", "pageSize"
            }
            if (
            normalized_method == "GET"
            and path == "/v2/wallets"
            and (
                params is None
                or params.get("currencies", "").strip().casefold() != "usdt"
                or params.get("type", "").strip().casefold() not in {"spot", "margin"}
                or set(params) != {"currencies", "type"}
            )
        ):
                raise InvalidExchangeRequest(
                    "Nobitex wallet reads are restricted to USDT Spot or Margin balances."
                )
        if (
            normalized_method == "GET"
            and path == "/market/stats"
            and (
                params is None
                or params.get("dstCurrency", "").strip().casefold() != "usdt"
                or set(params) - {"srcCurrency", "dstCurrency"}
                or (
                    "srcCurrency" in params
                    and not params["srcCurrency"].strip().isalnum()
                )
            )
        ):
                raise InvalidExchangeRequest(
                    "Nobitex market-statistics reads are restricted to USDT-quoted markets."
                )
        if normalized_method == "GET" and path == "/market/udf/history":
            symbol = "" if params is None else params.get("symbol", "")
            if not self._is_usdt_symbol(symbol):
                raise InvalidExchangeRequest(
                    "Nobitex candle reads are restricted to USDT-quoted markets."
                )
        if normalized_method == "GET" and path.startswith("/v3/orderbook/"):
            symbol = path.removeprefix("/v3/orderbook/")
            if not self._is_usdt_symbol(symbol):
                raise InvalidExchangeRequest(
                    "Nobitex order-book reads are restricted to USDT-quoted markets."
                )
        if normalized_method == "GET" and path.startswith("/v2/trades/"):
            symbol = path.removeprefix("/v2/trades/")
            if not self._is_usdt_symbol(symbol):
                raise InvalidExchangeRequest(
                    "Nobitex trade-data reads are restricted to USDT-quoted markets."
                )

        if (
            normalized_method == "GET"
            and path == "/v2/options"
            and (params is not None or payload is not None)
        ):
            raise InvalidExchangeRequest(
                "Nobitex options reads do not accept query parameters or a payload."
            )

        if normalized_method == "GET" and payload is not None:
            raise InvalidExchangeRequest("GET requests must not contain a JSON payload.")
        if normalized_method == "POST" and payload is None:
            raise InvalidExchangeRequest("The order-status query requires a JSON payload.")

        query = f"?{urlencode(params)}" if params else ""
        url = f"{self.base_url.rstrip('/')}{path}{query}"
        headers = {"Accept": "application/json", "User-Agent": "EVORYN/0.1"}
        selected_token = token or self.token
        private_read_routes = {
            "/v2/wallets",
            "/market/orders/status",
            "/market/orders/list",
            "/market/trades/list",
        }
        if path in private_read_routes and not (selected_token or "").strip():
            raise AuthenticationError(
                "A read-only Nobitex API token is required for private account/order reads."
            )
        if selected_token and selected_token.strip():
            headers["Authorization"] = f"Token {selected_token.strip()}"

        body: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        request = Request(url, data=body, headers=headers, method=normalized_method)
        opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                raw_response = response.read()
        except HTTPError as exc:
            self._raise_http_error(exc)
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError,)):
                raise ExchangeTimeout("Nobitex request timed out.") from exc
            raise ExchangeTransportError("Nobitex network transport failed.") from exc
        except TimeoutError as exc:
            raise ExchangeTimeout("Nobitex request timed out.") from exc
        except OSError as exc:
            raise ExchangeTransportError("Nobitex network transport failed.") from exc

        try:
            decoded = json.loads(raw_response.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise MalformedExchangeResponse("Nobitex returned invalid JSON.") from exc
        if not isinstance(decoded, dict):
            raise MalformedExchangeResponse("Nobitex response must be a JSON object.")

        status = decoded.get("status") or decoded.get("s")
        if status == "no_data" and path == "/market/udf/history":
            return decoded
        if status == "failed" and path == "/market/orders/status":
            return decoded
        if status not in {"ok", "Ok", None}:
            if status in {"unauthorized", "authentication_failed"}:
                raise AuthenticationError("Nobitex rejected authentication.")
            raise InvalidExchangeRequest(f"Nobitex reported request status '{status}'.")
        return decoded
