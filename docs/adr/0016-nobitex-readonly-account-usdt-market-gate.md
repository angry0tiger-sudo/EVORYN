# ADR 0016: Nobitex Read-Only Account Data and USDT-Only Market Gate

- **Status:** Proposed for independent engineering/security review
- **Date:** 2026-10-19
- **Scope:** EVORYN Phase 22; Nobitex only
- **Depends on:** ADR 0002, ADR 0003, ADR 0005, ADR 0014, ADR 0015 and the owner's USDT-only direction.

## Owner directive

Only Nobitex is in scope. EVORYN's active quote-currency whitelist is USDT; Spot and the
separate documented delegated-Margin domain remain distinct. No international exchange research
is in scope. Futures remains Unknown and is not being pursued.

## Official data contract

The official Nobitex API docs describe `GET /v2/wallets` with `currencies` and `type`
parameters; wallet type supports `spot` and `margin`. The implementation requests
`currencies=usdt` and reads each wallet type separately. It does not call the user-profile,
banking, transfer, withdrawal, or order APIs as part of the new account reader.

Official reference:
https://github.com/nobitex/docs-api/blob/master/source/includes/_user_data.md

## Decision

1. Add a central `MarketPolicy` used by configuration, the Risk Engine and the Nobitex adapter.
   The default and current allowed quote set is exactly USDT. The supported-quote ceiling is
   fixed in source; environment configuration may narrow the set (including disabling all
   markets) but cannot enable IRT, BTC or ETH as quote currency.
2. Permit only venue `nobitex` and market type `spot` in the generic Spot Risk Engine and
   generic Spot market adapter. The dedicated `MarginRiskPolicy` independently rejects a
   non-USDT destination/quote currency.
3. Enforce the whitelist before adapter calls and at the HTTP transport boundary:
   market statistics require `dstCurrency=usdt`; candles and market-symbol paths require
   compact `BASEUSDT`; wallet reads require `currencies=usdt` and an explicit
   `type=spot|margin`. Remove the wider `/users/wallets/list` route from the transport.
4. `get_usdt_markets` requests market statistics with `dstCurrency=usdt` and applies a second
   local filter. The base-asset list is not hard-coded; future Nobitex-listed USDT pairs can be
   recognized without redesigning the quote-currency policy.
5. Add an isolated account reader that returns typed Spot and Margin USDT wallet snapshots.
   Available balance is `balance - blocked` only when both source values are present and valid.
   A wallet omitted from a successful response is `NOT_LISTED`, not a fabricated zero balance.
   Invalid, non-finite, negative, or inconsistent balances fail closed.
6. Load credentials only from `NOBITEX_READONLY_API_TOKEN`, injected through environment or a
   secret provider. The legacy generic `NOBITEX_API_TOKEN` name is rejected. Tokens are excluded
   from object repr and are not written to logs or source control.
7. Code-level read-only boundaries are enforced by the transport route allowlist; no order-submit,
   cancel, withdrawal, transfer, profile, or collateral-write operation is added. CI tests use
   mock transports and never make a network call.
8. The code cannot independently verify exchange-side API-key scopes. Operational use remains
   blocked until the operator verifies in Nobitex settings that the supplied token has no
   trading or withdrawal permission. The variable name alone does not prove server-side scope.
9. The account reader does not call the live API during implementation or CI. Live connectivity
   must be validated separately with a restricted token and an explicitly supervised read-only
   smoke test. No secret is requested in chat.

## Consequences and limits

- Non-USDT quotes are rejected in configuration, Risk Engine, adapter and transport.
- The base-asset list remains extensible; quote currency and venue/market type are pinned.
- Missing USDT wallet data stays not-listed/unknown and cannot be consumed as zero capital.
- The new account reader returns minimal wallet state only and avoids profile/banking information.
- Read-only snapshots do not prove Margin collateral sufficiency, production polling, durable audit
  wiring, or live-trading readiness.
- Order submission and cancellation remain hard-disabled. No live order, transfer, withdrawal,
  or collateral mutation is enabled by this ADR.

## Acceptance

- Unit tests cover config parsing, whitelist enforcement, source filtering, snapshot normalization,
  missing-wallet semantics, no-token behavior, and route allowlists.
- Full configured CI passes on the exact PR head SHA.
- Independent review confirms no secret leakage and no write-capable route.
- A live API smoke test remains unrun until a separately supervised operation can use a
  provider-confirmed restricted token.
