"""Bounded HTTPS-only JSON transport for explicitly allowlisted provider API hosts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)


class JsonApiError(RuntimeError):
    """Sanitized transport/protocol failure; response bodies and secrets are not logged."""


class JsonApiTransport(Protocol):
    """Injectable network boundary; tests use a fake and never access the Internet."""

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
    ) -> Any:
        """Perform one bounded HTTPS GET and return decoded JSON."""
        ...


class _SameHostHttpsRedirects(HTTPRedirectHandler):
    """Allow redirects only to the same HTTPS origin."""

    def redirect_request(
        self, request, fp, code, message, headers, new_url
    ):
        original = urlsplit(request.full_url)
        target = urlsplit(new_url)
        if (
            original.scheme != "https"
            or target.scheme != "https"
            or original.hostname != target.hostname
            or target.username is not None
            or target.password is not None
        ):
            raise JsonApiError("Cross-origin or non-HTTPS redirect refused.")
        return super().redirect_request(request, fp, code, message, headers, new_url)


class HttpsJsonTransport:
    """Standard-library transport with fixed host allowlist, deadline and response-size cap."""

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str],
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 2_000_000,
    ) -> None:
        if not allowed_hosts or any(not host.strip() for host in allowed_hosts):
            raise ValueError("A non-empty HTTPS host allowlist is required.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive.")
        self._allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._opener = build_opener(_SameHostHttpsRedirects())

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
    ) -> Any:
        """Make one safe GET; this class does not retry, scrape, or log credentials."""
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.lower() not in self._allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise JsonApiError("Request URL failed HTTPS host allowlist validation.")
        if parsed.port not in (None, 443):
            raise JsonApiError("Non-default HTTPS ports are not permitted.")

        query_items = dict(params)
        query = urlencode(query_items, doseq=False)
        request_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, query, "")
        )
        request_headers = {"Accept": "application/json", "User-Agent": "EVORYN/0.1"}
        for name, value in headers.items():
            if not name.strip() or "\r" in name or "\n" in name:
                raise JsonApiError("Invalid HTTP header name.")
            if "\r" in value or "\n" in value:
                raise JsonApiError("Invalid HTTP header value.")
            request_headers[name] = value

        request = Request(request_url, headers=request_headers, method="GET")
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type != "application/json" and not content_type.endswith("+json"):
                    raise JsonApiError("Provider response is not declared as JSON.")
                declared_length = response.headers.get("Content-Length")
                if declared_length:
                    try:
                        if int(declared_length) > self._max_response_bytes:
                            raise JsonApiError("Provider response exceeded the configured size cap.")
                    except ValueError as exc:
                        raise JsonApiError("Provider supplied an invalid Content-Length.") from exc
                raw = response.read(self._max_response_bytes + 1)
                if len(raw) > self._max_response_bytes:
                    raise JsonApiError("Provider response exceeded the configured size cap.")
        except HTTPError as exc:
            raise JsonApiError(f"Provider API returned HTTP status {exc.code}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise JsonApiError("Provider API request failed or timed out.") from exc

        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise JsonApiError("Provider returned invalid JSON.") from exc
