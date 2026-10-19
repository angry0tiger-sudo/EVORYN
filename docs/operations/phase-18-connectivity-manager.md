# Phase 18: Connectivity Manager and Feed Freshness

## Safety boundary

Phase 18 is read-only and route-policy-bound. It does not submit exchange orders, change RiskEngine limits, clear Auto-Halt, or enable Live mode. Only explicitly authorized destinations/routes may be configured. Do not use proxy or tunnel routing to hide identity or bypass law/provider terms. This implementation does not ship an HTTP-proxy, SOCKS5 or VPN adapter; those require a separately reviewed, approved adapter and environment evidence.

## Integration

Construct `ConnectivityManager` around an approved implementation of `JsonApiTransport` and pass it to existing market/news connectors in place of the raw transport. The manager preserves `get_json(url, headers=..., params=...)`, with an optional `correlation_id`. Configure an exact hostname allowlist, a unique `route_id`, `route_authorized=True` only after independent approval, a durable `ConnectionAuditSink`, retry and circuit policies. Do not log URLs, query strings, request headers, bodies or credential material.

Example shape (the sink is an application-owned durable audit adapter, not a no-op sink):

```python
manager = ConnectivityManager(
    transport=approved_read_only_transport,
    allowed_hosts=frozenset({"api.provider.example"}),
    route_id="provider-api-direct",
    route_authorized=True,
    audit_sink=durable_connection_audit,
)
```

The hostname is illustrative only. Replace it with an exact provider host already authorized in the source/license register. Do not copy this example hostname into production configuration.

## Retry, circuit and rate-limit behavior

- `max_attempts` includes the first attempt and cannot exceed five.
- Only selected transient GET failures are retried. Authentication, allowlist, invalid payload and protocol errors are not retried.
- Backoff is exponential, bounded and jittered. Test code injects clock/sleep/random sources so recovery can be deterministic without network access.
- HTTP 429 honors parsed `Retry-After` seconds or HTTP date. A shared cooldown prevents subsequent calls from immediately violating the same rate limit. If the delay exceeds the configured synchronous wait bound, the manager fails fast instead of retrying early.
- Circuit states are CLOSED, OPEN and HALF_OPEN. OPEN denies before transport access. HALF_OPEN allows one recovery probe. A deterministic authorization response is not counted as a network outage.
- The HTTP adapter reports status metadata only. Response bodies and secret-bearing header values are never used as error text.

## Feed freshness and gaps

Use `FeedFreshnessGuard` only where the caller has explicit provider observation and receipt timestamps. Values must be timezone-aware UTC. The guard rejects stale, implausibly future-dated, duplicate/out-of-order and configured-gap observations. Rejected observations must not be passed to strategies as fresh market data. A gap does not advance the baseline; after an upstream reconciliation, a caller supplies an evidence reference to `reset_after_reconciliation`. That operation records the reset. Keep the existing phase-5 market-data quality gate enabled; this guard is not a replacement for full candle completeness, instrument identity or cross-source reconciliation.

## Operational review

Inspect and alert on event type, correlation ID, route ID, allowlisted target hostname, latency, attempt count, breaker state, HTTP status and failure class. Sink storage must be access-controlled and durable. Do not attach raw request/response data to incidents.

## Test evidence status

`tests/unit/test_phase18_connectivity.py` contains mock-only scenarios. A test being present is not a passing run. Record lint, type-check, security scan, pytest and CI results from an identifiable runner. If reliable CI job logs remain unavailable, Phase 18 acceptance is blocked rather than inferred from a PR merge or synthetic fixtures.

## Remaining gate items

1. Independent review of the new state/retry/freshness contracts and use by actual allowed connectors.
2. Verified runner output for unit tests, lint, strict type checking and security checks.
3. Evidence that route authorization and the audit sink are configured in the intended runtime.
4. Separate authorization/evidence for any proxy, SOCKS5 or VPN/tunnel transport; no such transport is implicitly enabled by this PR.
5. Failure-drill evidence for source timeout/outage, rate limit, feed gap, audit persistence loss and recovery.
