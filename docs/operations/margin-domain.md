# Nobitex Delegated Margin Model and Risk Operations

## Scope

The `evoryn.margin` package is intentionally separate from `evoryn.risk` Spot portfolio
accounting and `evoryn.paper` execution. It currently provides domain types, pure payload
normalization, order-limit validation, scenario calculations, position-health checks, and
snapshot comparison. It does not make HTTP requests and does not open, close, or modify
positions.

## Source contract

Official reference:
https://github.com/nobitex/docs-api/blob/master/source/includes/_position.md

The normalizer follows the documented fields from:
- GET /margin/markets/list
- GET /margin/v2/delegation-limit
- GET /liquidity-pools/list
- GET /positions/list and GET /positions/:positionId:/status

Market settings are account/depth dependent. A market's buyEnabled and sellEnabled flags
are checked at evaluation time; no direction is presumed enabled. Nobitex's official
Margin documentation labels BUY as beta; side availability must be read for each market. Leverage is accepted
only when it is within maxLeverage and exactly matches a source-reported delegation-limit
row for that side. The delegation-limit response is itself a timestamped snapshot bound to
a market; stale, future, duplicate, or different-market rows cannot authorize an opening.

Nobitex's documented delegation limits have different units by side. For BUY, the order
notional price × amount is compared to the destination-currency limit. For SELL, amount
is compared to the source-currency limit. These checks are not interchangeable.

## Position and accounting units

| Field group | Unit / interpretation |
|---|---|
| collateral, totalAsset, extensionFee, PnL | destination/quote currency |
| delegatedAmount, liability, liabilityInOrder | source/base currency quantities |
| entryPrice, exitPrice, markPrice, liquidationPrice | destination currency per source unit |
| marginRatio, leverage, PnL percent | source-reported Decimal ratios |
| status | Open, Closed, Liquidated, Expired, or explicit Unknown |

No field is coerced to floating-point. Missing optional fields remain None; invalid or
missing required market capability fields raise a normalization error.

## Pool-aware opening risk

The official `GET /liquidity-pools/list` endpoint returns the active pool listing and its `capacity` /
`filledCapacity` values. A source-currency pool can become inactive; the official documentation states that
new Margin orders cannot be placed against inactive pools even though closing existing positions remains
possible. The current normalizer is pure: a currency present in a successful active-pool listing becomes
ACTIVE with source-reported capacities; a currency omitted from that complete successful listing becomes
INACTIVE for that observation. Errors or malformed responses raise instead of inventing status.

Every call to `MarginRiskPolicy.evaluate_open_intent` requires this pool snapshot as well as fresh market
settings and the user's `/margin/v2/delegation-limit` snapshot. Timestamps must be aware, not future-dated,
and within the policy's maximum age. Pool currency must match the market source currency. UNKNOWN or stale
pool evidence halts new opening evaluation; inactive, exhausted, inconsistent, or insufficient pool capacity
rejects or halts as appropriate. Available pool capacity is checked in source-currency amount units; BUY
delegation caps remain quote-currency notionals.

A healthy `MarginRiskAssessment` is only a classification of one observed position. It cannot authorize
additional exposure because it lacks the proposed intent, current user delegation caps and pool capacity.
New risk must pass the separate opening-intent gate.

## Risk operation

- The default configured minimum safe commitment ratio is 1.1, based on official docs.
- The initial ratio reference is 2, retained as a policy constant; it is not silently used
  to manufacture a position ratio from unrelated fields.
- A stale/future snapshot, unknown status, unsupported mode, missing mark price, missing
  liquidation price, or missing margin ratio cannot be classified healthy.
- A known threshold crossing is detected with the venue-reported liquidationPrice:
  BUY: markPrice <= liquidationPrice; SELL: markPrice >= liquidationPrice.
- Only the current venue status Liquidated is represented as the venue's liquidated state.
- An Expired status remains UNKNOWN/settlement-pending until settlement is verified, because
  the official documentation describes transitional status changes around system settlement.
- Cross Margin remains disabled because official docs describe it as future.
- The official extensionFee is authoritative. The local linear estimate based on the
  current positionFeeRate is only a scenario estimate because Nobitex documents a tiered
  daily renewal fee.
- MarginReconciler reports field differences; it does not mutate either snapshot.

## Evidence boundary

Tests are written with deterministic synthetic records only. They have not been claimed
as passed without a verified run. This package has no authenticated API client, and no live
position/order/collateral operation is performed.
