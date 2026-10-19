# ADR 0017: Gated Nobitex Spot Execution Candidate

- **Status:** Proposed for independent engineering/security review
- **Date:** 2026-10-19
- **Scope:** Nobitex Spot / USDT only
- **Depends on:** ADR 0011, ADR 0016, the deterministic Risk Engine, and the owner's decision to defer account testing to the local machine.

## Context

EVORYN's canonical ExchangePort and NobitexHTTPTransport deliberately remain read-only. Spot/Paper orchestration is a separate simulation path and must not be repurposed as a live exchange client. The owner wants an eventual local Spot execution capability with an explicit default-off feature flag, while all live-account testing is deferred until the source is moved to the owner's local machine.

NOVARYN is reference material for Nobitex Ed25519 signing and for separating authorization, durable idempotency, and unknown-outcome reconciliation. Its credentials, broad private endpoints, application composition, and runtime decisions are not copied into EVORYN.

## Decision

1. Keep the generic read-only adapter and route allowlist read-only. Add a separate NobitexSpotTradingTransport that exposes only Spot submit, single-order cancel, and order-status reconciliation routes. It has no withdraw, transfer, Margin-write, Futures, conversion, profile mutation, bulk-cancel, or generic request method.
2. Use the currently documented Nobitex Ed25519 request-authentication shape: URL-safe Base64 32-byte raw public/private key material, and signature over timestamp + HTTP method + exact path + exact serialized request body. Credentials are injected from local environment/secret management and omitted from object representations.
3. The public GET /v2/options endpoint provides amountPrecisions, pricePrecisions, and minOrders. A read-only rules provider requires an exact USDT instrument key and valid positive Decimal fields. Missing/unknown rules reject the order rather than guessing. The read transport allowlists /v2/options only without query parameters or body.
4. The application layer requires EVORYN_SPOT_ORDER_WRITES_ENABLED=True plus EVORYN_LIVE_TRADING_ACK=I_UNDERSTAND_ORDERS_ARE_REAL. Both default to disabled/empty. Enabling through process environment in CI/test environments is rejected. The legacy ENABLE_REAL_TRADING stays false and does not arm this separate service.
5. Before an order write, require a non-halted Risk Engine, Nobitex/Spot/USDT market identity, Nobitex-sourced market data accepted by deterministic freshness/quality gates, account_reconciled is exactly true, execution_error is exactly false, recent instrument rules, valid amount/price step, USDT minimum notional, and a bounded market-order price guard. Maximum market slippage is hard-limited in code to 100 basis points; local default is 50. REDUCE quantities are rounded only downward to the official amount step.
6. SQLiteExecutionJournal commits a unique client order ID and a submitting state before any network write. It persists no credentials or raw request/response bodies. An active or unknown prior order blocks subsequent submissions in this initial conservative implementation.
7. Order writes are never retried. Timeout, network error, unparsable acknowledgement, or post-submit journal uncertainty marks the order unknown and latches Risk Engine halt. Reconciliation by client ID is attempted only on an explicit application call; not-found does not clear unknown state because the provider query may omit closed orders. Manual review is required before resuming.
8. Single-order cancellation is permitted only for an acknowledged journaled active order. An ambiguous cancellation remains unknown and blocks new entry.
9. Phase 18 ConnectivityManager may wrap only idempotent reads. It must not wrap signed POST order-submit/cancel/status calls in retries. Connectivity retry, durable audit integration, local runtime wiring, and operational monitoring remain separate work.
10. GitHub Actions and unit tests use mock transports only. No live request, account token, or secret is used in development/CI. A separate local validation gate is mandatory before a real account can be authorized.

## Consequences

- Spot-write code can be assembled explicitly for local use but is not activated by import, CI, or a background worker.
- The generic read-only adapter still declares order_write=False.
- The initial service serializes to a single active/unresolved order globally. This avoids silent exposure stacking until the local runtime composes account, open-order, fill, and exposure snapshots reliably.
- Reconciliation, account reconciliation, protective-order strategy, process-restart recovery, and operator runbooks need more evidence before production readiness.
- Successful mocked CI proves only local logic and static controls, not exchange interoperability, remote permission scope, actual market precision, profitability, or production readiness.

## Required acceptance evidence

- Exact-head unit/contract/failure tests with socket-free fakes.
- All six configured CI jobs pass on the exact head: pytest, Ruff, strict mypy, Bandit, runner/preflight/compileall, and pip-audit.
- Independent review confirms route allowlisting, signed payload exactness, default-off flag, secret safety, risk gate, durable idempotency, no retry on write, and unknown-result behavior.
- Local integration and release gates remain open until owner-run validation is conducted after repository transfer. No live test is part of this PR.
