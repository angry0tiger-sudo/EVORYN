# ADR 0015: Nobitex Margin Conformance and Pool-Aware New-Risk Gate

- **Status:** Proposed for independent engineering/risk review
- **Date:** 2026-10-19
- **Scope:** Roadmap Phase 21B — Nobitex Spot/Margin conformance only
- **Depends on:** ADR 0002 (read-only Nobitex adapter boundary), ADR 0005 (delegated Margin domain/risk),
  ADR 0014 (evidence-backed market capability contracts), and the owner's explicit Phase 21 scope directive.

## Owner-directed scope

Phase 21 work is limited to Iranian venues, specifically Nobitex Spot and its documented delegated Margin
market. No international venue investigation or implementation is in scope.
Nobitex Futures remains Unknown and is not being pursued without new owner direction and official evidence.

## Context

Nobitex Margin differs from generic perpetual futures and from Spot. Its official documentation describes
per-market direction availability and max leverage, account-specific remaining delegation caps by side and
leverage, an active liquidity-pool listing, and venue-reported position health fields. The unit conventions
are asymmetric: BUY delegation limit is destination/quote currency, while SELL delegation limit is
source/base currency. Pool capacity and the current remaining user delegation are distinct controls.

The existing `MarginRiskPolicy.evaluate_open_intent` already checked fresh intent/settings/delegation-limit
snapshots, market consistency, side enablement, the reported market leverage maximum, a unique matching
side/leverage delegation row, and the asymmetric BUY/SELL limit units. It did not require a liquidity-pool
snapshot. Its position-health assessment also reported `new_risk_permitted=True` for a healthy position
without knowing the current intent, delegation limit, active pool, or its available capacity.

## Decision

1. Add `MarginLiquidityPoolSnapshot` with explicit ACTIVE, INACTIVE, and UNKNOWN states, source-currency
   capacity and filled-capacity fields, and an aware observation timestamp.
2. Normalize the official `GET /liquidity-pools/list` response without network I/O. The documented endpoint
   is a listing of active pools. Only a well-formed `status=ok` response may establish a pool snapshot.
   A currency absent from that successful complete listing is represented as INACTIVE; a failed/malformed
   response raises and must be treated as missing evidence by the caller.
3. Require a pool snapshot when evaluating any new Margin opening intent. Its currency must match the
   market's source currency, its timestamp must be fresh and not future-dated, and its reported capacity
   must be internally consistent. UNKNOWN or incomplete/stale pool evidence HALTs opening evaluation;
   an inactive pool, exhausted capacity, or amount greater than remaining source-currency capacity rejects it.
4. Preserve the venue's side-specific delegation semantics exactly: BUY checks `amount × price` against the
   unique BUY limit for the requested leverage; SELL checks `amount` against the unique SELL limit for that
   leverage. Missing/duplicate, stale, future, or wrong-market inputs never become unlimited capacity.
5. Keep the current per-market buy/sell enablement and source-reported user/account leverage caps. Do not
   infer permission from a generic market type, another market, another side, or a different leverage row.
6. A position-health assessment classifies only the observed position. It does not authorize additional
   exposure on its own. New exposure must pass `evaluate_open_intent` using fresh source settings, delegation
   caps and pool capacity. The venue-reported liquidation price remains authoritative; no formula is invented.
7. Keep all new logic pure and offline. The Margin normalizer does not gain HTTP/authentication or order-write
   capability. The existing Nobitex `ExchangePort` remains Spot-read-only and its order submit/cancel methods
   remain hard-disabled. No Margin open/close/collateral-write route is added.
8. Do not treat the estimated initial collateral (`notional / leverage`) as an authoritative venue formula.
   The current official contract requires sufficient Margin wallet collateral, but this local foundation does
   not have an approved account-runtime balance adapter or a fully specified server calculation. Production
   account integration therefore remains unimplemented; no real order can be authorized by this library.
9. Test with synthetic payloads only. Formal conformance must verify cap equality/breach, asymmetric units,
   side disablement, leverage rows, stale/future/mismatched inputs, pool inactive/unknown/exhausted/inconsistent
   capacity, position state/threshold classification, and the unchanged Spot/no-write plugin boundary.

## Consequences

- Opening risk checks now account for both per-user delegation capacity and the source-currency pool capacity.
- The lack of pool evidence blocks new opening risk rather than becoming a permissive default.
- A healthy existing position can no longer be mistaken for authorization to increase exposure.
- The pool snapshot and risk policy remain pure domain code; this does not establish live API wiring or production
  readiness.
- The account's actual collateral sufficiency and server-side eligibility still require official runtime reads
  and exchange enforcement; the library must not claim they are locally proven.

## Verification and acceptance

- Run unit and contract tests, Ruff, strict mypy, Bandit, preflight/compileall and pip-audit on the exact PR SHA.
- No test may send a network request, require exchange credentials, create an exchange order, alter collateral,
  transfer funds, change hard risk limits, or enable Live.
- Independently review side-specific cap units and the pool normalization interpretation against the official
  Nobitex API documentation.
- This ADR remains Proposed until the exact CI results and implementation diff are independently reviewed.
