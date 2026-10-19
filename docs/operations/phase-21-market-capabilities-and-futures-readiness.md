# Phase 21A — Market Capability and Futures Readiness Contract

## Purpose and boundary

This document describes the initial Phase 21 contract foundation. It does not implement a Futures venue,
activate any network route, or grant live order permission. All new work must preserve the existing
`ExchangePort`, read-only Nobitex adapter, deterministic RiskEngine, and Paper/Shadow/Live separation.

## Capability evaluation

Every decision is keyed by `(venue, market_type, feature)`. A capability for Spot cannot authorize Margin
or Futures. An omitted record is `unknown`, not a permissive default. A record marked `supported` or
`unsupported` must carry an HTTPS evidence reference and an aware observation timestamp. `restricted`
requires a specific policy rationale. Evidence scope must match the exact market and operation.

`CapabilityMatrix.require_supported` is a pure fail-closed domain check. It is not yet a production runtime
permission service and must not be mistaken for one until it is explicitly wired into reviewed adapters.

## Instrument and rules

`InstrumentIdentity` keeps canonical identity separate from a venue's exact symbol. Futures identities
must explicitly declare settlement asset, contract kind, settlement lifecycle, positive contract size, and
expiry when expiring. No Futures field may be inferred from a Spot/Margin symbol.

`InstrumentTradingRules` stores decimal price tick, quantity step, minimum quantity, and minimum notional
with evidence. `minimum_notional=None` means unknown. It must not be treated as an unrestricted market.
No float-based rounding is allowed in domain contracts.

## Funding, margin and liquidation inputs

`FundingObservation`, `FuturesAccountModeSnapshot`, and `FuturesRiskSnapshot` preserve source and event
timestamps. Signed funding rates are valid. Position mode, margin mode, contract size, mark price, margin
inputs, and the venue-reported liquidation field must not be fabricated. These models deliberately do not
implement generic funding or liquidation arithmetic: a future formula requires a selected venue, pinned
semantics, documented units, and its own risk tests.

An incomplete Futures risk snapshot cannot be treated as safe. A complete input record is still not a risk
approval. Any future FuturesRiskEngine must return an explicit decision and remain separate from AI, strategy,
exchange adapter, and order transport.

## Plugin isolation and conformance

For each venue adapter, the future conformance suite must validate only claimed capabilities and must include:

- every supported market/feature pair has source-backed evidence and valid normalized types;
- unsupported, unknown, and restricted operations fail explicitly, without fallback to another venue;
- malformed/stale data, precision violations, rate limits, timeouts, and unknown order outcomes are fail-closed;
- adapter registration cannot shadow another plugin or import arbitrary modules;
- failure of one adapter does not mutate another adapter's registry, state, rate budget, or ledger;
- submit/cancel operations remain disabled during this phase, and no test requires exchange credentials.

This initial PR checks the typed capability rules and the existing Nobitex write-disabled boundary. It is not
a claim that all listed future failure drills are complete.

## CCXT policy

ADR 0006 remains authoritative: CCXT is adapter-only. A later PoC must pin the exact version, identify the
exact venue and market mode, verify method semantics and capability flags, record license/SBOM evidence,
compare precision and error mapping, and pass offline contract/failure tests. No dependency is added until
that evidence demonstrates concrete value.

## Cross-exchange scope

Transfer, custody, atomic multi-venue execution, and automatic hedging remain restricted. These require
separate analysis of partial completion, asynchronous settlement, duplicate transfers, counterparty and
custody assumptions, audit reconstruction, and recovery invariants. A common plugin interface alone does
not solve these problems.

## Acceptance status for this foundation

- typed capability and Futures data contracts exist;
- missing/incomplete evidence is explicitly unknown or incomplete;
- unit and contract tests pass on the exact PR SHA;
- existing read-only/write-disabled boundaries remain intact;
- no new exchange dependency, credential path, network call, order route, transfer, or hard-risk mutation is added.

The later selection and implementation of a concrete venue is a separate gate.
