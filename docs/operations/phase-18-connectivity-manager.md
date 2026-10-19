# Phase 18: Connectivity Manager and Feed Freshness

## Safety boundary

Phase 18 is read-only and route-policy-bound. It does not submit exchange orders, change RiskEngine limits, clear Auto-Halt, or enable Live mode. Only explicitly authorized destinations/routes may be configured. Do not use proxy or tunnel routing to hide identity or bypass law/provider terms. This implementation does not ship an HTTP-proxy, SOCKS5 or VPN adapter; those require a separately reviewed, approved adapter and environment evidence.

## Integration

Construct `ConnectivityManager` around an approved implementation of `JsonApiTransport` and pass it to existing market/news connectors in place of the raw transport. The manager preserves `get_json(url, headers=..., params=...)`, with an optional `correlation_id`. Bind it to a `NetworkRouteAuthorization` record with an exact hostname allowlist, approval reference/actor, and UTC review deadline, plus a durable `ConnectionAuditSink`, retry and circuit policies. Authorization is rechecked before each request; an expired approval stops outbound calls. Do not log URLs, query strings, request headers, bodies or credential material.

Example shape (the values below are placeholders; the audit sink is an application-owned durable adapter, not a no-op sink):

```python
from datetime import UTC, datetime, timedelta

route_authorization = NetworkRouteAuthorization(
    route_id="provider-api-direct",
    authorization_ref="change-ticket:approved-route",
    approved_by="authorized-reviewer",
    approved_at=datetime.now(UTC),
    review_due_at=datetime.now(UTC) + timedelta(days=30),
    allowed_hosts=frozenset({"api.provider.example"}),
)
manager = ConnectivityManager(
    transport=approved_read_only_transport,
    route_authorization=route_authorization,
    audit_sink=durable_connection_audit,
)
```

The hostname, ticket, reviewer and dates are illustrative only. Replace them with current route approval evidence and an exact provider host already authorized in the source/license register. Do not copy placeholder values into production configuration.

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

The 12 explicit tests in `tests/unit/test_phase18_connectivity.py` passed in GitHub Actions [Run #874](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39307664012) on candidate SHA `1d435a4b0df40f2d38e8e43515aded1aa1d3a3b0`. The full suite reported 346 passed; Run #873 (push) and Run #874 (pull request) both passed all six configured CI jobs on that exact SHA: pytest, Ruff, strict mypy, Bandit, runner/dependency preflight and compileall, and pip-audit. This establishes mock-only code/test evidence for that candidate. It does not validate the exact historical Phase 18 merge SHA by itself.

No live Nobitex request or order was sent from GitHub Actions. Every further source or packaging change must pass CI on the new exact PR head. These CI runs do not execute `setup.bat` on Windows and do not prove installation or network-route authorization on the owner's host.

## Remaining gate items

1. Independent review of retry/circuit/freshness semantics, redirects, host allowlisting, 429 cooldown, and audit fail-closed behavior.
2. Evidence that approved runtime connectors actually use `ConnectivityManager` and a durable, access-controlled `ConnectionAuditSink` with current route authorization and secret-provider configuration.
3. Failure-drill evidence on the target runtime for source timeout/outage, rate limit, feed gap, audit persistence loss and recovery.
4. Separate authorization and implementation evidence for any proxy, SOCKS5 or VPN/tunnel transport; no such transport is enabled.
5. Historical verification of the exact conditional-merge commit remains open in `TECHNICAL_DEBT.md`; current superset CI does not retroactively test that SHA.


## Verified code boundaries and current runtime integration status

The reusable connection manager implements bounded retries (maximum five total attempts), exponential backoff with jitter, transport timeout, Retry-After delta/date parsing, a shared HTTP 429 cooldown, a CLOSED/OPEN single-probe HALF_OPEN circuit, and feed freshness/gap validation. The "reconnect" behavior means attempting a fresh logical read after bounded delay or after the single half-open recovery probe; the code does not keep a persistent socket alive or blindly replay order writes.

The Nobitex fixed-host transports explicitly disable ambient environment proxy discovery and use direct HTTPS. `EVORYN_PROXY_URL` in `.env.example` is a reserved placeholder and is ignored. No proxy/SOCKS/VPN adapter is delivered. Alternative egress requires an independent implementation and route authorization record, not merely setting a variable.

The manager's GET retry policy and audit protocol are available and covered by mock-only tests. A complete production trading runtime and its connector composition root are not yet present, so this document does not claim every runtime connector is already wired through a manager and durable audit sink. Before a live runtime is authorized, the approved route record and durable sink must be supplied by the local composition root and failure drills must be performed locally. Signed Spot order submit/cancel and order-status POSTs remain single-attempt operations and must never be automatically retried.
