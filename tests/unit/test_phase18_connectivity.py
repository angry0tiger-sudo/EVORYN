"""Mock-only resilience tests; these tests never access a live network or exchange."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evoryn.adapters.network.connectivity import (
    CircuitBreakerPolicy,
    CircuitOpenError,
    CircuitState,
    ConnectionEvent,
    ConnectionEventType,
    ConnectivityAuditError,
    ConnectivityManager,
    FeedFreshnessError,
    FeedFreshnessGuard,
    FeedFreshnessPolicy,
    NetworkRouteAuthorization,
    RateLimitDeferredError,
    RetryPolicy,
)
from evoryn.adapters.network.json_http import (
    HttpsJsonTransport,
    JsonApiError,
    parse_retry_after_seconds,
)


class MemoryAuditSink:
    def __init__(self) -> None:
        self.events: list[ConnectionEvent] = []

    def record(self, event: ConnectionEvent) -> None:
        self.events.append(event)


class FakeTime:
    def __init__(self) -> None:
        self.monotonic_now = 0.0
        self.wall_now = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.monotonic_now

    def utcnow(self) -> datetime:
        return self.wall_now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.monotonic_now += seconds
        self.wall_now += timedelta(seconds=seconds)


class FakeTransport:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []

    def get_json(
        self,
        url: str,
        *,
        headers: Any,
        params: Any,
    ) -> Any:
        self.calls.append((url, dict(headers), dict(params)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make_route_authorization(clock: FakeTime) -> NetworkRouteAuthorization:
    return NetworkRouteAuthorization(
        route_id="approved-direct",
        authorization_ref="test:phase18-policy",
        approved_by="unit-test",
        approved_at=clock.wall_now - timedelta(minutes=1),
        review_due_at=clock.wall_now + timedelta(days=30),
        allowed_hosts=frozenset({"api.example.test"}),
    )


def make_manager(
    transport: FakeTransport,
    sink: MemoryAuditSink,
    clock: FakeTime,
    *,
    retry: RetryPolicy | None = None,
    circuit: CircuitBreakerPolicy | None = None,
) -> ConnectivityManager:
    return ConnectivityManager(
        transport=transport,
        route_authorization=make_route_authorization(clock),
        audit_sink=sink,
        retry_policy=retry or RetryPolicy(base_delay_seconds=1, max_delay_seconds=4),
        circuit_policy=circuit or CircuitBreakerPolicy(failure_threshold=2, cooldown_seconds=3),
        monotonic=clock.monotonic,
        utcnow=clock.utcnow,
        sleeper=clock.sleep,
        random_value=lambda: 0.5,
    )



def test_https_json_transport_passes_timeout_and_does_not_retry_socket_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimeoutOpener:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, float]] = []

        def open(self, request: Any, *, timeout: float) -> Any:
            self.calls.append((request, timeout))
            raise URLError(TimeoutError("synthetic socket timeout"))

    opener = TimeoutOpener()
    monkeypatch.setattr(
        "evoryn.adapters.network.json_http.build_opener",
        lambda *handlers: opener,
    )
    transport = HttpsJsonTransport(
        allowed_hosts=frozenset({"api.example.test"}),
        timeout_seconds=2.5,
    )

    with pytest.raises(JsonApiError) as error:
        transport.get_json(
            "https://api.example.test/v1/quote",
            headers={},
            params={},
        )

    assert error.value.kind == "timeout"
    assert "synthetic socket timeout" not in str(error.value)
    assert len(opener.calls) == 1
    assert opener.calls[0][1] == 2.5

def test_success_records_latency_and_never_logs_query_or_headers() -> None:
    sink = MemoryAuditSink()
    clock = FakeTime()
    transport = FakeTransport([{"data": "ok"}])
    manager = make_manager(transport, sink, clock)

    result = manager.get_json(
        "https://api.example.test/v1/quote?api_key=must-not-be-logged",
        headers={"Authorization": "secret-value"},
        params={"symbol": "BTC"},
        correlation_id="feed:001",
    )

    assert result == {"data": "ok"}
    assert [event.event_type for event in sink.events] == [
        ConnectionEventType.REQUEST_STARTED,
        ConnectionEventType.REQUEST_SUCCEEDED,
    ]
    rendered_events = repr(sink.events)
    assert "api_key" not in rendered_events
    assert "must-not-be-logged" not in rendered_events
    assert "Authorization" not in rendered_events
    assert "secret-value" not in rendered_events
    assert all(event.correlation_id == "feed:001" for event in sink.events)


def test_destination_not_allowlisted_fails_before_transport() -> None:
    sink = MemoryAuditSink()
    clock = FakeTime()
    transport = FakeTransport([{"unexpected": True}])
    manager = make_manager(transport, sink, clock)

    with pytest.raises(JsonApiError, match="destination"):
        manager.get_json(
            "https://evil.example.test/collect?secret=value",
            headers={},
            params={},
        )

    assert transport.calls == []
    assert sink.events[-1].event_type is ConnectionEventType.DESTINATION_REJECTED
    assert "evil.example.test" not in repr(sink.events)
    assert "secret" not in repr(sink.events)


def test_retries_network_failure_with_bounded_backoff_then_succeeds() -> None:
    sink = MemoryAuditSink()
    clock = FakeTime()
    transport = FakeTransport(
        [JsonApiError("sanitized timeout", kind="timeout"), {"price": "1.0"}]
    )
    manager = make_manager(transport, sink, clock)

    assert manager.get_json(
        "https://api.example.test/v1/quote",
        headers={},
        params={},
    ) == {"price": "1.0"}
    assert len(transport.calls) == 2
    assert clock.sleeps == [1.0]
    assert ConnectionEventType.RETRY_SCHEDULED in [event.event_type for event in sink.events]
    recovered_state = manager.circuit_state
    assert recovered_state is CircuitState.CLOSED


def test_rate_limit_retry_after_is_respected_and_logged() -> None:
    sink = MemoryAuditSink()
    clock = FakeTime()
    transport = FakeTransport(
        [
            JsonApiError(
                "HTTP 429",
                kind="http_status",
                status_code=429,
                retry_after_seconds=2.0,
            ),
            {"items": []},
        ]
    )
    manager = make_manager(transport, sink, clock)

    assert manager.get_json(
        "https://api.example.test/v1/items",
        headers={},
        params={},
    ) == {"items": []}
    assert len(transport.calls) == 2
    assert clock.sleeps == [2.0]
    assert ConnectionEventType.RATE_LIMIT_WAIT in [event.event_type for event in sink.events]


def test_authentication_error_is_not_retried() -> None:
    sink = MemoryAuditSink()
    clock = FakeTime()
    transport = FakeTransport(
        [JsonApiError("HTTP 401", kind="http_status", status_code=401), {"should": "not-run"}]
    )
    manager = make_manager(transport, sink, clock)

    with pytest.raises(JsonApiError):
        manager.get_json("https://api.example.test/v1/private", headers={}, params={})

    assert len(transport.calls) == 1
    assert clock.sleeps == []


def test_open_circuit_blocks_calls_and_half_open_probe_can_recover() -> None:
    sink = MemoryAuditSink()
    clock = FakeTime()
    transport = FakeTransport(
        [
            JsonApiError("timeout", kind="timeout"),
            JsonApiError("timeout", kind="timeout"),
            {"ok": True},
        ]
    )
    manager = make_manager(
        transport,
        sink,
        clock,
        retry=RetryPolicy(max_attempts=1),
        circuit=CircuitBreakerPolicy(failure_threshold=2, cooldown_seconds=3),
    )
    for _ in range(2):
        with pytest.raises(JsonApiError):
            manager.get_json("https://api.example.test/v1/ping", headers={}, params={})
    opened_state = manager.circuit_state
    assert opened_state is CircuitState.OPEN

    with pytest.raises(CircuitOpenError):
        manager.get_json("https://api.example.test/v1/ping", headers={}, params={})
    assert len(transport.calls) == 2

    clock.monotonic_now += 3
    assert manager.get_json(
        "https://api.example.test/v1/ping",
        headers={},
        params={},
    ) == {"ok": True}
    assert manager.circuit_state is CircuitState.CLOSED
    assert len(transport.calls) == 3


def test_excessive_retry_after_defers_without_early_retry() -> None:
    sink = MemoryAuditSink()
    clock = FakeTime()
    transport = FakeTransport(
        [
            JsonApiError(
                "HTTP 429",
                kind="http_status",
                status_code=429,
                retry_after_seconds=90,
            ),
            {"must": "not-run"},
        ]
    )
    manager = make_manager(
        transport,
        sink,
        clock,
        retry=RetryPolicy(max_attempts=3, max_retry_after_seconds=10),
    )

    with pytest.raises(JsonApiError):
        manager.get_json("https://api.example.test/v1/items", headers={}, params={})
    assert len(transport.calls) == 1
    with pytest.raises(RateLimitDeferredError):
        manager.get_json("https://api.example.test/v1/items", headers={}, params={})
    assert len(transport.calls) == 1


def test_audit_failure_blocks_transport() -> None:
    class BrokenSink:
        def record(self, event: ConnectionEvent) -> None:
            raise OSError("disk unavailable")

    clock = FakeTime()
    transport = FakeTransport([{"should": "not-run"}])
    manager = ConnectivityManager(
        transport=transport,
        route_authorization=make_route_authorization(clock),
        audit_sink=BrokenSink(),
        monotonic=clock.monotonic,
        utcnow=clock.utcnow,
        sleeper=clock.sleep,
        random_value=lambda: 0.5,
    )

    with pytest.raises(ConnectivityAuditError):
        manager.get_json("https://api.example.test/v1/ping", headers={}, params={})
    assert transport.calls == []


def test_retry_after_parser_supports_delta_and_http_date() -> None:
    now = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    assert parse_retry_after_seconds("3", now=now) == 3.0
    assert parse_retry_after_seconds("invalid", now=now) is None
    assert parse_retry_after_seconds("NaN", now=now) is None
    assert parse_retry_after_seconds("Mon, 19 Oct 2026 09:00:05 GMT", now=now) == 5.0


def test_freshness_gate_rejects_stale_future_duplicate_and_gap_observations() -> None:
    sink = MemoryAuditSink()
    now = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    guard = FeedFreshnessGuard(
        source_id="market-feed",
        route_id="approved-direct",
        policy=FeedFreshnessPolicy(
            max_age_seconds=10,
            max_future_skew_seconds=1,
            max_gap_seconds=5,
        ),
        audit_sink=sink,
        utcnow=lambda: now,
    )

    assert guard.check(observed_at=now, received_at=now) == 0.0
    with pytest.raises(FeedFreshnessError, match="duplicate"):
        guard.check(observed_at=now, received_at=now)
    with pytest.raises(FeedFreshnessError, match="stale"):
        guard.check(observed_at=now - timedelta(seconds=11), received_at=now)
    with pytest.raises(FeedFreshnessError, match="future"):
        guard.check(observed_at=now + timedelta(seconds=3), received_at=now)
    with pytest.raises(FeedFreshnessError, match="gap"):
        guard.check(
            observed_at=now + timedelta(seconds=8),
            received_at=now + timedelta(seconds=8),
        )
    assert guard.last_observed_at == now
    assert sum(
        event.event_type is ConnectionEventType.FEED_FRESHNESS_REJECTED for event in sink.events
    ) == 4


def test_freshness_gap_requires_explicit_reconciliation_to_rebaseline() -> None:
    sink = MemoryAuditSink()
    now = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    guard = FeedFreshnessGuard(
        source_id="market-feed",
        route_id="approved-direct",
        policy=FeedFreshnessPolicy(max_age_seconds=20, max_gap_seconds=5),
        audit_sink=sink,
        utcnow=lambda: now,
    )
    guard.check(observed_at=now, received_at=now)
    with pytest.raises(FeedFreshnessError):
        guard.check(
            observed_at=now + timedelta(seconds=10),
            received_at=now + timedelta(seconds=10),
        )

    guard.reset_after_reconciliation(evidence_ref="reconcile:issue-18")
    assert guard.last_observed_at is None
    assert sink.events[-1].event_type is ConnectionEventType.FEED_FRESHNESS_RESET
    assert sink.events[-1].evidence_ref == "reconcile:issue-18"

def test_expired_route_authorization_is_denied_before_transport() -> None:
    sink = MemoryAuditSink()
    clock = FakeTime()
    transport = FakeTransport([{"must": "not-run"}])
    manager = ConnectivityManager(
        transport=transport,
        route_authorization=NetworkRouteAuthorization(
            route_id="approved-direct",
            authorization_ref="test:short-review",
            approved_by="unit-test",
            approved_at=clock.wall_now - timedelta(minutes=1),
            review_due_at=clock.wall_now + timedelta(seconds=1),
            allowed_hosts=frozenset({"api.example.test"}),
        ),
        audit_sink=sink,
        utcnow=clock.utcnow,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        random_value=lambda: 0.5,
    )
    clock.wall_now += timedelta(seconds=1)

    with pytest.raises(JsonApiError, match="authorization"):
        manager.get_json("https://api.example.test/v1/ping", headers={}, params={})

    assert transport.calls == []
    assert sink.events[-1].event_type is ConnectionEventType.DESTINATION_REJECTED
    assert sink.events[-1].reason == "route_authorization_inactive_or_expired"
