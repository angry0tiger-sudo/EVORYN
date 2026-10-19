# ADR 0014: Evidence-Backed Market Capabilities and Futures Domain Contracts

- **Status:** Proposed for independent architecture, safety, and venue-evidence review
- **Date:** 2026-10-19
- **Scope:** Roadmap Phase 21A — multi-exchange capability and derivative contract foundation
- **Depends on:** ADR 0002 (ExchangePort/plugin boundary), ADR 0005 (Nobitex delegated Margin),
  ADR 0006 (CCXT adapter-only decision), ADR 0011 (connectivity/freshness), ADR 0012 (read-only
  Shadow Mode), and ADR 0013 (advisory-only Intelligence Core).

## Context

The existing `ExchangePort` and installable `ExchangePlugin` boundary are the starting point.
However, `Market.market_type` is a string and the legacy `ExchangeCapabilities` object contains
broad booleans. Neither is sufficient evidence that one particular venue supports a particular
Spot, Margin, or Futures capability. API method presence, library support, or support for one
market type must not be treated as proof for another market type.

Roadmap Phase 21 requires independent exchange plugins, market-specific risk semantics, a current
capability matrix, contract/failure conformance tests, and no failure propagation across plugins.
Venue API semantics, licensing, account eligibility, and operational authorization must be assessed
individually. Futures contract math must not be guessed from Spot or Nobitex delegated Margin.

## Decision

1. Add immutable, typed domain records for `MarketType`, `CapabilityFeature`, `CapabilityStatus`,
   evidence-backed `VenueCapability`, and `CapabilityMatrix`.
2. Use exactly four capability states: `supported`, `unsupported`, `unknown`, and `restricted`.
   Missing records resolve to `unknown`. `CapabilityMatrix.require_supported` requires callers to supply
   an aware current timestamp and an explicit positive maximum evidence age. Stale or future-dated
   observations fail as `unknown`. A `supported` or `unsupported` claim requires an HTTPS evidence
   reference and a timezone-aware observation timestamp. `restricted` requires a policy rationale.
   Absence from a documentation page is not sufficient evidence for `unsupported`.
3. Give each instrument both a canonical symbol and the venue's exact native symbol. Include venue
   and market type in the identity key. Futures identity must declare settlement asset, contract kind,
   settlement lifecycle, and positive contract size; expiring contracts also require an aware expiry.
   Spot and Margin identities cannot carry those Futures-only fields.
4. Represent price tick, quantity step, minimum quantity, and minimum notional as Decimal-based,
   source-backed trading rules. An absent minimum-notional value is unknown, not equivalent to zero.
5. Represent funding observations, position/margin mode observations, and Futures risk inputs as
   read-only domain records. Do not embed an assumed venue liquidation formula or an automatic
   leverage/default-mode policy. An incomplete Futures risk snapshot must abstain; a complete data
   shape is not a `RiskDecision` or execution authorization.
6. Keep the current `ExchangePort` and Nobitex implementation compatible during this first subphase.
   The new capability model is groundwork and is not yet a production runtime authorization service.
   Before an adapter relies on it for any operation, its per-market evidence must be wired and reviewed.
7. Do not select or implement a real Futures venue in this subphase. Nobitex Futures remains `unknown`
   because the reviewed repository and official references do not establish the required Futures
   contract capability set. This is not a claim that the venue definitively lacks such a product.
8. Preserve ADR 0006: CCXT may be evaluated behind an EVORYN-owned adapter only, using an exact pinned
   release and per-venue/mode contract tests. No new dependency is justified by this ADR. Freqtrade
   remains an isolated research tool, not the core or an execution adapter.
9. Cross-exchange transfer, custody, settlement, and coordinated-position behavior remain restricted
   from implementation until concurrency, transfer exposure, custody assumptions, and regulatory/
   contractual eligibility are separately reviewed.
10. No order-writing transport, exchange credentials, live route, or live trading path is added.

## Alternatives considered

- **Infer derivatives support from broad ExchangeCapabilities or a CCXT method:** rejected; method
  presence cannot establish contract-specific semantics or venue permission.
- **Use `spot`, `margin`, and `future` string literals throughout the core:** rejected for new
  contracts because misspellings and silent cross-market assumptions are too easy.
- **Select a venue before capability/license review:** rejected; it would convert an unverified
  preference into an architectural dependency.
- **Add CCXT to core dependencies now:** rejected; ADR 0006 only permits adapter-level evaluation.
- **Implement generic liquidation/funding formulas before a venue is chosen:** rejected; formulas,
  contract multipliers, settlement assets, funding conditions, and position modes differ by venue.
- **Treat missing precision as unrestricted:** rejected; incomplete rules cannot pass order validation.

## Consequences

### Positive

- Every capability statement is scoped to a venue, market type, and feature.
- Missing, stale, or incomplete evidence cannot silently become permission.
- Instrument identity prevents a Spot/Margin symbol alias from being mistaken for a Futures contract.
- Futures risk data can be represented without importing provider classes into the domain core.
- The legacy read-only Nobitex boundary and deterministic RiskEngine remain intact.

### Trade-offs and known limitations

- The new data contracts are not yet wired into every exchange plugin or an operational registry.
- No venue-specific Futures adapter, order lifecycle, funding calculator, initial/maintenance margin
  engine, liquidation estimator, or position-mode mutation is implemented by this subphase.
- Venue evidence and rights/eligibility review remain open. The capability matrix is not production
  readiness evidence.
- Existing ADRs retain their recorded statuses; this ADR does not retroactively approve previous phases.

## Verification and acceptance gates

- Unit tests cover evidence requirements, exact market/feature scoping, the unknown default, duplicate
  entries, precision incompleteness, linear/inverse-type representation, expiry, funding, account modes,
  and incomplete Futures risk inputs.
- Plugin conformance tests prove that legacy write-disabled behavior remains unchanged and that broad
  legacy capabilities do not imply Futures support.
- The complete configured CI workflow must pass on the exact PR head SHA.
- An independent review must confirm no live order route, credential use, transfer, or risk-limit mutation
  was introduced.
- This ADR can be accepted only after review of the implementation diff and exact CI evidence. Its current
  proposed status is not a merge or live-release authorization.

## Rollback

Remove the new domain module and its tests/documents if compatibility or policy review fails. The current
`ExchangePort`, Nobitex adapter, Margin domain, deterministic RiskEngine, and existing capability flags
must remain usable without this new module.
