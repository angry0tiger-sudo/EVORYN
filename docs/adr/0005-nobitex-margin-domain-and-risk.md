# ADR 0005: Nobitex Delegated Margin Domain and Fail-Closed Risk Model

- Status: Proposed for review
- Date: 2026-10-18
- Scope: Roadmap Phase 11
- Depends on: Phase 5 normalized data; Phase 6/7 read-only exchange capability boundaries
- Official contract checked: https://github.com/nobitex/docs-api/blob/master/source/includes/_position.md

## Context

EVORYN must model Nobitex Margin independently from Spot without importing assumptions
from generic perpetual futures. The official API describes a delegated-position model,
position-specific collateral, liability, extension fee, margin ratio, an exchange-reported
liquidation price, and a maximum position lifetime. It explicitly labels Isolated Margin
as the current mode and Cross Margin as future. The buy direction is described as beta,
and individual markets can report buyEnabled=false.

Relevant documented endpoints and semantics:

- GET /margin/markets/list reports srcCurrency, dstCurrency, positionFeeRate,
  maxLeverage, buyEnabled, and sellEnabled. The maximum leverage depends on market depth
  and account level; position renewal fees are tiered by delegated-asset value. The
  official document describes Margin BUY as beta, and some markets report buyEnabled=false.
- GET /margin/v2/delegation-limit reports remaining limits by side and leverage.
  A BUY limit is denominated in destination/quote currency (price × amount); a SELL
  limit is denominated in source/base currency (amount). A missing unique limit is not
  interpreted as unlimited capacity.
- GET /positions/list and GET /positions/:positionId:/status expose position state,
  collateral, leverage, liquidationPrice, entry/exit prices, delegatedAmount, liability,
  totalAsset, marginRatio, in-order quantities, unrealized PnL, expirationDate,
  extensionFee, and markPrice.
- Official docs identify Open, Closed, Liquidated and Expired states, Isolated Margin as
  the current margin mode, an initial commitment ratio of 2, and a minimum safe ratio of
  1.1. A position may remain in a transitional state around liquidation/expiry while
  settlement orders complete. An `Expired` status is therefore settlement-pending and
  locally classified as `UNKNOWN`, not proof that the position has settled.

## Decision

1. Add a dedicated `evoryn.margin` package. No Margin model inherits from or mutates the
   Spot portfolio models. Margin side, status, mode, market capabilities, delegation
   limits, position fields, and risk outputs have independent types.
2. Add a pure `NobitexMarginAdapter` that normalizes documented payloads without HTTP,
   credentials, order writes, alias guessing, or float-based money parsing.
3. Treat all market/position timestamps as explicitly observed. Stale, future, malformed,
   missing, or unsupported inputs are unknown or halt/reject conditions, never zero values.
4. Respect direction-specific delegation-limit units and current market capability flags.
   A market's maximum leverage alone does not authorize an order; a matching source-
   reported limit for the exact side and leverage is required.
5. Use the source-reported marginRatio, markPrice, liquidationPrice and extensionFee as
   authoritative observations. Local risk logic can classify whether the mark price has
   crossed the source-reported liquidation threshold, and whether the reported ratio is
   at/below 1.1. It does not invent an exchange liquidation-price formula.
6. Provide generic notional/leverage initial-collateral estimates and a linear extension
   fee estimate for scenario planning only. The extension-fee estimate is explicitly not
   the exact Nobitex amount because the official documentation describes tiered rates;
   the reported extensionFee remains authoritative. These estimates cannot authorize live
   trading or replace the actual venue position fields.
7. Disable Cross Margin by policy unless official support and exact semantics are verified
   later. Unknown modes, missing liquidationPrice, stale snapshots, and unknown position
   states never permit additional risk.
8. Compare normalized position snapshots without silently repairing differences.

## Calculations and deliberate limitations

Planning-only initial collateral estimate:

`estimated_initial_collateral = notional / leverage`

Planning-only extension-fee estimate:

`estimated_extension_fee = delegated_amount × mark_price × daily_position_fee_rate × elapsed_days`

The first estimate is a generic sizing heuristic, not a statement of Nobitex's order
settlement formula. The second uses the observed rate linearly and does not reconstruct
the venue's tiered fee schedule or exchange rounding.

The official API schema exposes liquidationPrice and marginRatio but does not provide a
complete, versioned arithmetic specification sufficient to reproduce the exact server
liquidation-price calculation. EVORYN therefore consumes that price from the official
position record and tests directional threshold scenarios:
- buy position: markPrice <= liquidationPrice means the reported threshold has been crossed;
- sell position: markPrice >= liquidationPrice means the reported threshold has been crossed.

This is a risk classification of the reported threshold, not a substitute for the
exchange's liquidation engine or an assertion that an account is already liquidated.
Only venue status Liquidated is represented as confirmed liquidation.

## Safety impact

- The normalizer is read-only and has no transport.
- No Margin open/close/collateral-edit endpoint is called.
- No order, transfer, collateral change, or live account state is mutated.
- Tests use only synthetic payloads.
- If Margin state is missing, stale, unsupported, or inconsistent, the safe action is to
  reject/halt new risk and reconcile; no model or strategy can override that result.
- This ADR does not authorize live Spot or Margin trading.

## Validation plan and evidence

The new unit tests cover strict payload parsing, Decimal precision, side-specific
delegation caps, initial collateral and extension-fee estimates, buy/sell liquidation
threshold scenarios, minimum margin-ratio breach, stale state, missing liquidation price,
unsupported Cross Margin, closed/liquidated/unknown states, and reconciliation differences.

Tests are authored on the Phase 11 branch. They are not declared passed unless a runner
result is observed. GitHub Actions diagnostic log access remains a documented environment
limitation; root cause is unconfirmed.

## Rollback

Remove the dedicated Margin package, tests, and this ADR. No Spot module changes or live
exchange calls are required for rollback.
