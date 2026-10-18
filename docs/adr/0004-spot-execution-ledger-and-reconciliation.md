# ADR 0004: Paper Spot Execution Ledger and Reconciliation

- Status: Proposed for review
- Scope: Phase 10 Spot execution orchestration
- Depends on: Phases 5 through 9, normalized market data, PaperBroker, and deterministic RiskEngine

## Context

The execution flow must connect an explicit intent to an order, known fills, an internal
balance snapshot, and a reconciliation result while preserving correlation across records.
Phase 10 remains isolated from live order submission. The available CI diagnostic path
did not expose the Phase 8/9 failure logs in the 2026-10-18 session, so that evidence
limitation is documented separately from claims about code correctness.

## Decision

Implement `SpotExecutionService` as a paper-only orchestration boundary that depends on
`PaperBroker`, not on the live-capable abstract `ExchangePort`. The broker remains the
final deterministic risk gate. It exposes the exact `RiskResult` that was evaluated for
each order so the service can correlate the actual decision instead of inferring it after
the fact.

An in-memory `ExecutionLedger` stores immutable intent, risk-decision, execution,
balance, and reconciliation entries. `MockSpotExchangeAdapter` keeps an independent
order/fill/balance projection and applies each newly observed fill exactly once. The
orchestrator compares local orders and fills, order states, and base/quote balances
against that projection.

## Safety and recovery

- No HTTP transport or live-exchange write interface is injected into this service.
- The service rejects reused correlation IDs with a different order payload and refuses
  to bind one client order ID to multiple correlation IDs.
- Unknown outcomes remain unresolved; no blind retry or automatic status invention is
  performed.
- Missing or unexpected orders, order/fill divergence, unknown outcomes, and balance
  mismatches latch Auto-Halt.
- Periodic reconciliation is caller-driven through `reconcile_all`; this module creates
  no scheduler thread and does not choose an operating interval.
- Ledgers are in-memory only. Durable persistence, fee modeling, multi-market collateral,
  and actual exchange reconciliation are explicitly out of scope.
- AI/strategy components receive no execution authority and cannot change hard risk limits.

## Validation and evidence

- Added end-to-end synthetic tests for approved market execution, risk rejection,
  incremental partial fills, idempotency, unknown outcomes, balance drift, and missing
  venue orders.
- At authoring time, the test suite has **not been executed in a verified runner**.
  GitHub Actions runs #165 (Phase 8/9) and #177 (Phase 10) failed; job-step retrieval was
  empty, log retrieval returned `BlobNotFound`, and no artifacts were exposed. Root cause
  is unconfirmed; this is a diagnostic access limitation, not a verified platform defect.
- No test pass, production readiness, or live trading approval is claimed.

## Rollback

Revert the Phase 10 execution service and its tests/docs. Phases 8/9 remain separate
paper-only components; no live exchange behavior is enabled by this ADR.
