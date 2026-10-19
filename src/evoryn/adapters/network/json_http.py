"""Bounded HTTPS-only JSON transport for explicitly allowlisted provider API hosts."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


def parse_retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse RFC Retry-After delta-seconds or HTTP-date without accepting invalid numbers."""
    if value is None or not value.strip():
        return None
    candidate = value.strip()
    try:
        delay = float(candidate)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(candidate)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None or retry_at.utcoffset() is None:
            return None
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None or reference.utcoffset() is None:
            raise ValueError("now must be timezone-aware.") from None
        delay = (retry_at.astimezone(UTC) - reference.astimezone(UTC)).total_seconds()
    if not math.isfinite(delay) or delay < 0:
        return None
    return delay


class JsonApiError(RuntimeError):
    """Sanitized transport/protocol failure; response bodies and secrets are never included."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "protocol",
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


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
        self,
        request: Any,
        fp: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Any:
        original = urlsplit(request.full_url)
        target = urlsplit(new_url)
        if (
            original.scheme != "https"
            or target.scheme != "https"
            or (original.hostname or "").casefold() != (target.hostname or "").casefold()
            or original.port not in (None, 443)
            or target.port not in (None, 443)
            or target.username is not None
            or target.password is not None
        ):
            raise JsonApiError(
                "Cross-origin or non-HTTPS redirect refused.",
                kind="policy",
            )
        return super().redirect_request(request, fp, code, message, headers, new_url)


class HttpsJsonTransport:
    """Standard-library transport with fixed host allowlist, timeout and response-size cap."""

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str],
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 2_000_000,
    ) -> None:
        if not allowed_hosts or any(not host.strip() for host in allowed_hosts):
            raise ValueError("A non-empty HTTPS host allowlist is required.")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive.")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer.")
        self._allowed_hosts = frozenset(host.lower().rstrip(".") for host in allowed_hosts)
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._opener = build_opener(ProxyHandler({}), _SameHostHttpsRedirects())

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
    ) -> Any:
        """Make one safe GET; this class does not retry, scrape, or log credentials."""
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise JsonApiError("Request URL is malformed.", kind="policy") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.lower().rstrip(".") not in self._allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise JsonApiError(
                "Request URL failed HTTPS host allowlist validation.",
                kind="policy",
            )
        if port not in (None, 443):
            raise JsonApiError("Non-default HTTPS ports are not permitted.", kind="policy")

        query = urlencode(dict(params), doseq=False)
        request_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
        request_headers = {"Accept": "application/json", "User-Agent": "EVORYN/0.1"}
        for name, value in headers.items():
            if not name.strip() or "\r" in name or "\n" in name:
                raise JsonApiError("Invalid HTTP header name.", kind="policy")
            if "\r" in value or "\n" in value:
                raise JsonApiError("Invalid HTTP header value.", kind="policy")
            if name.casefold() in {
                "host",
                "content-length",
                "transfer-encoding",
                "connection",
            }:
                raise JsonApiError(
                    "The transport owns framing and routing headers.",
                    kind="policy",
                )
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
                            raise JsonApiError(
                                "Provider response exceeded the configured size cap."
                            )
                    except ValueError as exc:
                        raise JsonApiError("Provider supplied an invalid Content-Length.") from exc
                raw = response.read(self._max_response_bytes + 1)
                if len(raw) > self._max_response_bytes:
                    raise JsonApiError("Provider response exceeded the configured size cap.")
        except HTTPError as exc:
            retry_after = parse_retry_after_seconds(
                exc.headers.get("Retry-After") if exc.headers is not None else None
            )
            raise JsonApiError(
                f"Provider API returned HTTP status {exc.code}.",
                kind="http_status",
                status_code=exc.code,
                retry_after_seconds=retry_after,
            ) from exc
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            kind = "timeout" if isinstance(reason, TimeoutError) else "network"
            raise JsonApiError("Provider API request failed or timed out.", kind=kind) from exc
        except OSError as exc:
            kind = "timeout" if isinstance(exc, TimeoutError) else "network"
            raise JsonApiError("Provider API request failed or timed out.", kind=kind) from exc

        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise JsonApiError("Provider returned invalid JSON.") from exc
