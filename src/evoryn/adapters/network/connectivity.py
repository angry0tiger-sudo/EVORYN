"""Fail-closed connection resilience for explicitly approved read-only JSON transports.

This module orchestrates an injected transport; it does not create proxies, VPN tunnels,
exchange write paths, or credentials. Every configured route must be explicitly authorized
outside this module. Only the transport's idempotent JSON GET contract can be retried.
"""

from __future__ import annotations

import math
import random
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit

from evoryn.adapters.network.json_http import JsonApiError, JsonApiTransport


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_TRANSIENT_STATUS_CODES = frozenset({408, 425, 500, 502, 503, 504})


class ConnectivityAuditError(RuntimeError):
    """The audit sink failed; the manager refuses to perform the request."""


class CircuitOpenError(RuntimeError):
    """A circuit is open and the call was denied before transport access."""


class RateLimitDeferredError(RuntimeError):
    """Retry-After cooldown is active beyond the configured bounded wait."""


class FeedFreshnessError(RuntimeError):
    """The timestamp or continuity of a feed observation failed its quality gate."""


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ConnectionEventType(StrEnum):
    REQUEST_STARTED = "request_started"
    REQUEST_SUCCEEDED = "request_succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    REQUEST_FAILED = "request_failed"
    CIRCUIT_REJECTED = "circuit_rejected"
    DESTINATION_REJECTED = "destination_rejected"
    RATE_LIMIT_WAIT = "rate_limit_wait"
    RATE_LIMIT_DEFERRED = "rate_limit_deferred"
    FEED_FRESHNESS_ACCEPTED = "feed_freshness_accepted"
    FEED_FRESHNESS_REJECTED = "feed_freshness_rejected"
    FEED_FRESHNESS_RESET = "feed_freshness_reset"


@dataclass(frozen=True, slots=True)
class ConnectionEvent:
    """Sanitized operational evidence; never stores a URL, header, response body, or secret."""

    event_id: str
    correlation_id: str
    event_type: ConnectionEventType
    timestamp_utc: datetime
    route_id: str
    target_id: str
    attempt: int = 0
    latency_ms: float | None = None
    circuit_state: CircuitState | None = None
    error_kind: str | None = None
    status_code: int | None = None
    retry_delay_seconds: float | None = None
    freshness_age_seconds: float | None = None
    reason: str | None = None
    evidence_ref: str | None = None


class ConnectionAuditSink(Protocol):
    """Structural protocol without requiring a runtime dependency or a storage backend."""

    def record(self, event: ConnectionEvent) -> None:
        """Persist or forward a sanitized connection event."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded retry policy for read-only, idempotent requests."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 5.0
    jitter_fraction: float = 0.25
    max_retry_after_seconds: float = 30.0
    retryable_status_codes: frozenset[int] = frozenset(
        {408, 425, 429, 500, 502, 503, 504}
    )

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be an integer in [1, 5].")
        for name in (
            "base_delay_seconds",
            "max_delay_seconds",
            "max_retry_after_seconds",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive.")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds.")
        if (
            not math.isfinite(self.jitter_fraction)
            or self.jitter_fraction < 0
            or self.jitter_fraction > 1
        ):
            raise ValueError("jitter_fraction must be finite and in [0, 1].")
        if any(type(code) is not int or not 400 <= code <= 599 for code in self.retryable_status_codes):
            raise ValueError("retryable_status_codes must contain HTTP error status integers.")


@dataclass(frozen=True, slots=True)
class CircuitBreakerPolicy:
    failure_threshold: int = 3
    cooldown_seconds: float = 20.0

    def __post_init__(self) -> None:
        if type(self.failure_threshold) is not int or self.failure_threshold < 1:
            raise ValueError("failure_threshold must be a positive integer.")
        if not math.isfinite(self.cooldown_seconds) or self.cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be finite and positive.")


class CircuitBreaker:
    """Thread-safe closed/open/half-open circuit with a single recovery probe."""

    def __init__(
        self,
        policy: CircuitBreakerPolicy,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = policy
        self._monotonic = monotonic
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    def admit(self) -> bool:
        now = self._monotonic()
        with self._lock:
            if self._state is CircuitState.OPEN:
                if self._opened_at is None or now - self._opened_at < self._policy.cooldown_seconds:
                    return False
                self._state = CircuitState.HALF_OPEN
                self._probe_in_flight = False
            if self._state is CircuitState.HALF_OPEN:
                if self._probe_in_flight:
                    return False
                self._probe_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False

    def record_failure(self) -> None:
        now = self._monotonic()
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._opened_at = now
                self._probe_in_flight = False
                self._failures = self._policy.failure_threshold
                return
            self._failures += 1
            if self._failures >= self._policy.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = now
                self._probe_in_flight = False


@dataclass(frozen=True, slots=True)
class FeedFreshnessPolicy:
    max_age_seconds: float
    max_future_skew_seconds: float = 1.0
    max_gap_seconds: float | None = None
    reject_equal_timestamps: bool = True

    def __post_init__(self) -> None:
        if not math.isfinite(self.max_age_seconds) or self.max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be finite and positive.")
        if not math.isfinite(self.max_future_skew_seconds) or self.max_future_skew_seconds < 0:
            raise ValueError("max_future_skew_seconds must be finite and non-negative.")
        if self.max_gap_seconds is not None and (
            not math.isfinite(self.max_gap_seconds) or self.max_gap_seconds <= 0
        ):
            raise ValueError("max_gap_seconds must be None or finite and positive.")
        if type(self.reject_equal_timestamps) is not bool:
            raise ValueError("reject_equal_timestamps must be an explicit boolean.")


class FeedFreshnessGuard:
    """Reject stale, future-dated, duplicate/out-of-order, and unexpectedly gapped observations."""

    def __init__(
        self,
        *,
        source_id: str,
        route_id: str,
        policy: FeedFreshnessPolicy,
        audit_sink: ConnectionAuditSink,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._source_id = _validate_identifier(source_id, "source_id")
        self._route_id = _validate_identifier(route_id, "route_id")
        self._policy = policy
        self._audit_sink = audit_sink
        self._utcnow = utcnow
        self._last_observed_at: datetime | None = None
        self._lock = threading.Lock()

    @property
    def last_observed_at(self) -> datetime | None:
        with self._lock:
            return self._last_observed_at

    def check(
        self,
        *,
        observed_at: datetime,
        received_at: datetime,
        correlation_id: str | None = None,
    ) -> float:
        """Validate timestamps and continuity. Return observation age in seconds on acceptance."""
        cid = _correlation_id(correlation_id)
        try:
            _require_utc(observed_at, "observed_at")
            _require_utc(received_at, "received_at")
        except ValueError as exc:
            self._emit(
                cid,
                ConnectionEventType.FEED_FRESHNESS_REJECTED,
                reason="invalid_timestamp",
            )
            raise FeedFreshnessError("Feed timestamp is not timezone-aware canonical UTC.") from exc

        age = (received_at - observed_at).total_seconds()
        reason: str | None = None
        with self._lock:
            if age < -self._policy.max_future_skew_seconds:
                reason = "future_timestamp"
            elif age > self._policy.max_age_seconds:
                reason = "stale_observation"
            elif self._last_observed_at is not None:
                delta = (observed_at - self._last_observed_at).total_seconds()
                if delta < 0 or (delta == 0 and self._policy.reject_equal_timestamps):
                    reason = "duplicate_or_out_of_order_timestamp"
                elif self._policy.max_gap_seconds is not None and delta > self._policy.max_gap_seconds:
                    reason = "observation_gap"
            if reason is None:
                self._last_observed_at = observed_at
        if reason is not None:
            self._emit(
                cid,
                ConnectionEventType.FEED_FRESHNESS_REJECTED,
                reason=reason,
                freshness_age_seconds=age,
            )
            raise FeedFreshnessError(f"Feed observation rejected by freshness gate: {reason}.")
        self._emit(
            cid,
            ConnectionEventType.FEED_FRESHNESS_ACCEPTED,
            freshness_age_seconds=max(0.0, age),
        )
        return max(0.0, age)

    def reset_after_reconciliation(self, *, evidence_ref: str) -> None:
        """Reset continuity only after an upstream caller supplies reconciliation evidence."""
        ref = _validate_identifier(evidence_ref, "evidence_ref")
        self._emit(
            _correlation_id(None),
            ConnectionEventType.FEED_FRESHNESS_RESET,
            reason="explicit_reconciliation",
            evidence_ref=ref,
        )
        with self._lock:
            self._last_observed_at = None

    def _emit(
        self,
        correlation_id: str,
        event_type: ConnectionEventType,
        *,
        reason: str | None = None,
        freshness_age_seconds: float | None = None,
        evidence_ref: str | None = None,
    ) -> None:
        _record_event(
            self._audit_sink,
            ConnectionEvent(
                event_id=uuid.uuid4().hex,
                correlation_id=correlation_id,
                event_type=event_type,
                timestamp_utc=_require_utc(self._utcnow(), "utcnow"),
                route_id=self._route_id,
                target_id=self._source_id,
                freshness_age_seconds=freshness_age_seconds,
                reason=reason,
                evidence_ref=evidence_ref,
            ),
        )


class ConnectivityManager:
    """Retry/circuit/rate-limit wrapper around a preconfigured read-only JSON GET transport."""

    def __init__(
        self,
        *,
        transport: JsonApiTransport,
        allowed_hosts: frozenset[str],
        route_id: str,
        route_authorized: bool,
        audit_sink: ConnectionAuditSink,
        retry_policy: RetryPolicy = RetryPolicy(),
        circuit_policy: CircuitBreakerPolicy = CircuitBreakerPolicy(),
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleeper: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        if type(route_authorized) is not bool:
            raise ValueError("route_authorized must be an explicit boolean.")
        if not route_authorized:
            raise PermissionError("The configured network route has not been explicitly authorized.")
        if not allowed_hosts:
            raise ValueError("A non-empty destination host allowlist is required.")
        self._allowed_hosts = frozenset(_normalize_host(host) for host in allowed_hosts)
        self._route_id = _validate_identifier(route_id, "route_id")
        self._transport = transport
        self._audit_sink = audit_sink
        self._retry_policy = retry_policy
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._sleep = sleeper
        self._random_value = random_value
        self._breaker = CircuitBreaker(circuit_policy, monotonic=monotonic)
        self._rate_limit_until = 0.0
        self._rate_limit_lock = threading.Lock()

    @property
    def circuit_state(self) -> CircuitState:
        return self._breaker.state

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        correlation_id: str | None = None,
    ) -> Any:
        """Perform one logical idempotent GET with bounded retries and sanitized evidence."""
        cid = _correlation_id(correlation_id)
        host = _allowed_request_host(url, self._allowed_hosts)
        if host is None:
            self._emit(
                cid,
                ConnectionEventType.DESTINATION_REJECTED,
                target_id="<invalid-or-unapproved>",
                reason="https_host_allowlist",
            )
            raise JsonApiError("Request destination failed connectivity policy validation.", kind="policy")

        self._wait_for_rate_limit(cid, host)
        if not self._breaker.admit():
            self._emit(
                cid,
                ConnectionEventType.CIRCUIT_REJECTED,
                target_id=host,
                circuit_state=self._breaker.state,
                reason="circuit_open",
            )
            raise CircuitOpenError("Connection circuit is open; request denied before transport access.")

        last_error: JsonApiError | None = None
        last_cause: OSError | None = None
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            if attempt > 1:
                self._wait_for_rate_limit(cid, host)
            started = self._monotonic()
            self._emit(
                cid,
                ConnectionEventType.REQUEST_STARTED,
                target_id=host,
                attempt=attempt,
                circuit_state=self._breaker.state,
            )
            try:
                result = self._transport.get_json(url, headers=headers, params=params)
            except JsonApiError as exc:
                last_error = exc
                last_cause = None
            except OSError as exc:
                last_error = JsonApiError(
                    "Transport operation failed or timed out.",
                    kind="timeout" if isinstance(exc, TimeoutError) else "network",
                )
                last_cause = exc
            else:
                self._breaker.record_success()
                self._emit(
                    cid,
                    ConnectionEventType.REQUEST_SUCCEEDED,
                    target_id=host,
                    attempt=attempt,
                    latency_ms=max(0.0, (self._monotonic() - started) * 1000.0),
                    circuit_state=self._breaker.state,
                )
                return result

            error = last_error
            if error is None:
                raise RuntimeError("Internal invariant violated: transport error was not captured.")
            delay = self._retry_delay(error, attempt)
            if error.status_code == 429:
                self._set_rate_limit_cooldown(delay)
            retryable = self._is_retryable(error)
            retry_after_too_long = (
                error.retry_after_seconds is not None
                and error.retry_after_seconds > self._retry_policy.max_retry_after_seconds
            )
            if retryable and attempt < self._retry_policy.max_attempts and not retry_after_too_long:
                self._emit(
                    cid,
                    ConnectionEventType.RETRY_SCHEDULED,
                    target_id=host,
                    attempt=attempt,
                    latency_ms=max(0.0, (self._monotonic() - started) * 1000.0),
                    circuit_state=self._breaker.state,
                    error_kind=error.kind,
                    status_code=error.status_code,
                    retry_delay_seconds=delay,
                )
                if error.status_code != 429:
                    self._sleep(delay)
                continue

            if self._counts_as_connectivity_failure(error):
                self._breaker.record_failure()
            else:
                # A deterministic HTTP/protocol response proves reachability, not data validity.
                self._breaker.record_success()
            self._emit(
                cid,
                ConnectionEventType.REQUEST_FAILED,
                target_id=host,
                attempt=attempt,
                latency_ms=max(0.0, (self._monotonic() - started) * 1000.0),
                circuit_state=self._breaker.state,
                error_kind=error.kind,
                status_code=error.status_code,
                retry_delay_seconds=delay if error.status_code == 429 else None,
            )
            if last_cause is not None:
                raise error from last_cause
            raise error

        raise RuntimeError("Internal invariant violated: retry loop exited without a result.")

    def _wait_for_rate_limit(self, correlation_id: str, host: str) -> None:
        with self._rate_limit_lock:
            remaining = self._rate_limit_until - self._monotonic()
        if remaining <= 0:
            return
        if remaining > self._retry_policy.max_retry_after_seconds:
            self._emit(
                correlation_id,
                ConnectionEventType.RATE_LIMIT_DEFERRED,
                target_id=host,
                retry_delay_seconds=remaining,
                reason="retry_after_exceeds_bounded_wait",
            )
            raise RateLimitDeferredError(
                "Provider rate-limit cooldown exceeds the configured bounded wait."
            )
        self._emit(
            correlation_id,
            ConnectionEventType.RATE_LIMIT_WAIT,
            target_id=host,
            retry_delay_seconds=remaining,
            reason="retry_after_cooldown",
        )
        self._sleep(remaining)

    def _set_rate_limit_cooldown(self, delay_seconds: float) -> None:
        with self._rate_limit_lock:
            self._rate_limit_until = max(
                self._rate_limit_until,
                self._monotonic() + max(0.0, delay_seconds),
            )

    def _retry_delay(self, error: JsonApiError, attempt: int) -> float:
        base = min(
            self._retry_policy.max_delay_seconds,
            self._retry_policy.base_delay_seconds * (2 ** (attempt - 1)),
        )
        sample = self._random_value()
        if not math.isfinite(sample) or not 0.0 <= sample <= 1.0:
            raise ValueError("random_value must return a finite number in [0, 1].")
        jitter = 1.0 + ((2.0 * sample) - 1.0) * self._retry_policy.jitter_fraction
        delay = min(self._retry_policy.max_delay_seconds, max(0.0, base * jitter))
        if error.retry_after_seconds is not None:
            delay = max(delay, error.retry_after_seconds)
        return delay

    def _is_retryable(self, error: JsonApiError) -> bool:
        if error.kind in {"network", "timeout"}:
            return True
        return error.status_code in self._retry_policy.retryable_status_codes

    @staticmethod
    def _counts_as_connectivity_failure(error: JsonApiError) -> bool:
        if error.kind in {"network", "timeout"}:
            return True
        return error.status_code in _TRANSIENT_STATUS_CODES

    def _emit(
        self,
        correlation_id: str,
        event_type: ConnectionEventType,
        *,
        target_id: str,
        attempt: int = 0,
        latency_ms: float | None = None,
        circuit_state: CircuitState | None = None,
        error_kind: str | None = None,
        status_code: int | None = None,
        retry_delay_seconds: float | None = None,
        reason: str | None = None,
    ) -> None:
        _record_event(
            self._audit_sink,
            ConnectionEvent(
                event_id=uuid.uuid4().hex,
                correlation_id=correlation_id,
                event_type=event_type,
                timestamp_utc=_require_utc(self._utcnow(), "utcnow"),
                route_id=self._route_id,
                target_id=target_id,
                attempt=attempt,
                latency_ms=latency_ms,
                circuit_state=circuit_state,
                error_kind=error_kind,
                status_code=status_code,
                retry_delay_seconds=retry_delay_seconds,
                reason=reason,
            ),
        )


def _record_event(sink: ConnectionAuditSink, event: ConnectionEvent) -> None:
    try:
        sink.record(event)
    except Exception as exc:
        raise ConnectivityAuditError("Connection audit failed; network/data flow is blocked.") from exc


def _correlation_id(value: str | None) -> str:
    if value is None:
        return uuid.uuid4().hex
    return _validate_identifier(value, "correlation_id")


def _validate_identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{name} must contain 1-128 ASCII letters, digits, dot, underscore, colon or hyphen.")
    return value


def _normalize_host(host: str) -> str:
    if not isinstance(host, str) or not host.strip():
        raise ValueError("Destination hosts must be non-empty strings.")
    candidate = host.strip().lower().rstrip(".")
    if any(char in candidate for char in "/?#@") or ":" in candidate:
        raise ValueError("Host allowlist entries must be hostnames without scheme, path or port.")
    if not candidate:
        raise ValueError("Destination host normalization produced an empty value.")
    return candidate


def _allowed_request_host(url: str, allowed_hosts: frozenset[str]) -> str | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    host = parsed.hostname.lower().rstrip(".") if parsed.hostname else None
    if (
        parsed.scheme != "https"
        or host is None
        or host not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in (None, 443)
    ):
        return None
    return host


def _require_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{name} must use canonical UTC.")
    return value.astimezone(UTC)
