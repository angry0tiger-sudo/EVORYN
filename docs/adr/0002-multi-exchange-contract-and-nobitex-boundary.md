# ADR-0002: Multi-Exchange Port and Nobitex Read Boundary

- **Status:** Proposed
- **Date:** 2026-10-18
- **Owners:** EVORYN engineering owner
- **Related phases:** Phases 6 and 7
- **Related changes:** `feat/exchange-adapter`

## Context

EVORYN needs provider-independent contracts so that future exchanges can be installed without adding exchange-specific branches to the domain or application core. The repository roadmap defines Phase 7 as a read-only Nobitex adapter; order execution remains a later-phase deliverable behind deterministic risk and release gates.

## Decision

1. Define a complete `ExchangePort` protocol for candle history, order books, recent trades, market statistics, balances, order status, order submission, and cancellation.
2. Every adapter declares its supported operations through `ExchangeCapabilities`; the Nobitex adapter declares writes unavailable.
3. Discover provider plugins through the `evoryn.exchanges` Python entry-point group. New installed plugins register themselves without modifying the registry or domain core. Entry points execute installed Python code and therefore require trusted, reviewed distributions.
4. Normalize provider data to typed domain models and `Decimal` values. Reject malformed provider responses instead of silently dropping bad fields.
5. Use one provider-independent error taxonomy and a shared sliding-window `RateLimiterPort`, with budgets scoped by provider and endpoint category.
6. Allow only reviewed read routes in the Nobitex HTTP transport. The provider's `POST /market/orders/status` is allowed solely as a read-only query; order submission, cancellation, transfers, withdrawals, and all unlisted routes are rejected by the transport.
7. `submit_order` and `cancel_order` exist on the common interface but always raise `TradingDisabledError` in this phase. They do not invoke the transport. `ENABLE_REAL_TRADING=True` is rejected during plugin construction and cannot switch writes on.

## Alternatives considered

- **Put provider-specific logic in the application core:** rejected because every new exchange would force core changes and regression risk.
- **Dynamically import arbitrary module paths from configuration:** rejected because configuration would become a code-loading boundary.
- **Implement and gate live order routes now:** deferred. A Boolean environment flag alone does not satisfy the roadmap's later execution, risk, reconciliation, owner authorization, and release evidence gates.
- **Silently coerce or omit malformed API rows:** rejected because it hides data-quality failures.

## Exchange API evidence reviewed

- Official Nobitex market API documentation: https://github.com/nobitex/docs-api/blob/master/source/includes/_market_data.md
- Official Nobitex wallet/user API documentation: https://github.com/nobitex/docs-api/blob/master/source/includes/_user_data.md
- ALF reference for current read mapping, normalized reads, and default-deny write-guard behavior: https://github.com/angry0tiger-sudo/ALF/blob/c4/safety-remediation-20261011/src/alf/infrastructure/exchange/nobitex.py

Rate-limit settings in the adapter are client-side budgets based on the documented endpoint limits when available. The UDF history endpoint uses a conservative local budget because its endpoint section does not state a specific numeric limit. These values are not claims about undocumented global limits.

## Consequences

### Positive
- Provider logic is isolated behind a typed port and installable plugin boundary.
- Error classification and rate-limit handling are consistent across providers.
- Tests use fake transports only and do not depend on network access or account credentials.
- A misconfigured live flag cannot activate order writes in this phase.

### Negative / trade-offs
- The common port contains future write methods that the current adapter intentionally refuses.
- Provider capability and market metadata still need adapter-specific contract evidence.
- Entry-point discovery must only load trusted Python packages.

## Security and safety impact

- No exchange API key or secret is committed.
- No order submission/cancellation transport route exists in the current Nobitex implementation.
- No live exchange connection is used by CI tests.
- The adapter is not production-live-ready and does not authorize any real-money operation.

## Validation and evidence

The pull request must pass unit tests, Ruff, mypy, Bandit, and pip-audit on its exact HEAD. Human review is required before merge.

## Rollback / supersession

Revert the exchange-adapter PR or supersede this ADR if the port, plugin discovery convention, rate policy, or phase safety boundary changes.
