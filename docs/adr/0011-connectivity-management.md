# ADR 0011: Connectivity Management, Bounded Recovery, and Feed Freshness

- Status: Proposed for review
- Date: 2026-10-19
- Scope: Roadmap Phase 18
- Depends on: Phases 5-7 market-data quality gates, the existing read-only JSON transport, audit/correlation contracts, and approved provider licensing.

## Context

A successful TCP/HTTPS request does not prove that the returned market observation is current, complete, authorized, or safe to use. Repeated timeouts, provider throttling, malformed data and interrupted feeds must not be converted into an unbounded retry loop. Destination and credential controls must remain independent of strategy and model behavior.

## Decision

1. Keep `HttpsJsonTransport` as one bounded HTTPS JSON GET implementation. It performs one request and never retries on its own. HTTP errors expose only sanitized status metadata and parsed `Retry-After` delay; response bodies and headers are not copied into exception messages or telemetry.
2. Wrap an injected read-only `JsonApiTransport` with `ConnectivityManager`. The manager validates exact HTTPS host allowlists, applies bounded attempts with exponential backoff and jitter, records correlation/latency/status evidence, honors `Retry-After`, and maintains a shared cooldown for HTTP 429 backpressure.
3. Retry only the transport's idempotent GET contract and selected transient statuses. Authentication/authorization failures, policy failures, malformed JSON and arbitrary protocol errors are not retried. The API contains no POST/order method, no exchange-write path and no reconciliation-bypassing order retry.
4. Use a thread-safe circuit breaker with CLOSED, OPEN and single-probe HALF_OPEN states. Repeated transient failures open the circuit; calls are refused while open; successful recovery closes it. HTTP 429 is handled as rate limiting, not as proof of endpoint outage.
5. Emit sanitized audit events with event ID, correlation ID, route ID, hostname, attempt, latency, breaker state, failure class and status. URL paths/query strings, headers, response bodies and secrets are deliberately excluded. If audit persistence fails, the manager fails closed before starting or continuing network work.
6. Apply `FeedFreshnessGuard` at the feed boundary where the provider's observation timestamp and receipt time are explicitly known. It rejects malformed/non-UTC, too-old, implausibly future-dated, duplicate/out-of-order and unexpectedly gapped observations. A detected gap does not silently rebaseline; an upstream reconciler must supply an evidence reference for explicit reset.
7. Every route requires explicit authorization in configuration. This phase does not implement proxy rotation, identity masking or circumvention of legal/provider restrictions. The manager accepts an injected transport implementation; it does not claim an HTTP proxy, SOCKS5, or VPN/tunnel adapter exists without an independently approved implementation and environment-specific evidence. The current implementation is route-neutral over an explicitly injected transport, with the existing direct HTTPS transport available.
8. Secrets remain injected by an approved caller/secret provider and are never emitted to connectivity telemetry. Route metadata is not a secret provider and does not confer permission to use a venue API.

## Interfaces and policy

- `RetryPolicy`: maximum five total attempts; finite bounded backoff/jitter; bounded synchronous wait; allowlisted transient HTTP status codes.
- `CircuitBreakerPolicy`: explicit failure threshold and cooldown.
- `ConnectionAuditSink`: required sink; inability to record a lifecycle event blocks the request.
- `FeedFreshnessPolicy`: maximum observation age, permitted future skew and optional maximum inter-observation gap.
- `FeedFreshnessGuard.reset_after_reconciliation`: requires a non-empty evidence reference and records the reset.
- `ConnectivityManager.get_json`: preserves the existing JSON GET interface and accepts an optional correlation ID. It must not be repurposed as an order/execution API.

## Failure behavior

- Destination not allowlisted, unauthorized route, invalid correlation metadata, or audit sink failure: reject before external network work.
- Network timeout and selected transient server statuses: bounded retry only; on exhaustion, record failure and update breaker state.
- HTTP 401/403 and other non-retryable HTTP statuses: no retry.
- HTTP 429: observe valid `Retry-After`, apply shared cooldown/backpressure, and never retry earlier than instructed. If the cooldown exceeds the configured synchronous bound, fail fast and keep future calls deferred until allowed.
- Open circuit: reject without calling the transport.
- Stale/future/out-of-order/gapped data: reject that observation. Do not turn it into a strategy signal; unresolved gaps require explicit upstream reconciliation.
- The manager and freshness guard do not set or relax risk limits, clear Auto-Halt, submit orders, or convert SHADOW events to PAPER evidence.

## Validation and limitations

Mock tests cover destination allowlisting, secret-free telemetry, bounded retry, rate-limit handling, authentication failure, circuit recovery, audit failure, retry-after parsing and freshness rejection/reset. Test definitions are not evidence of execution; runner results must be captured separately.

No proxy/SOCKS/VPN implementation is enabled by this ADR. The availability and legality of any such route remain provider-, environment-, and jurisdiction-specific and require a separate evidence-backed review before use.

## Rollback

Remove the Phase 18 wrapper, freshness guard, tests and this ADR, preserving the prior `HttpsJsonTransport` behavior. Revert the transport metadata enhancement if consumers cannot preserve backwards compatibility. No market write or live-order path is introduced.
