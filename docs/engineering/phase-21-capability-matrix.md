# Phase 21 — Venue Capability Matrix (Evidence Snapshot)

- **As of:** 2026-10-19
- **Repository:** `angry0tiger-sudo/EVORYN`
- **Main SHA reviewed:** `0e8085c8aaa7f03ab92a9808e716d0715a34a364`
- **Status:** Initial evidence snapshot; not an authorization to connect, trade, or deploy.

## Status semantics

| Status | Meaning |
|---|---|
| **Supported** | An exact capability has official source evidence and an implementation/contract scoped to that capability. It does not by itself prove production runtime wiring. |
| **Unsupported** | Current authoritative evidence explicitly says the exact capability is unavailable. Absence of a listing is not enough. |
| **Unknown** | The available evidence is incomplete, stale, contradictory, or does not establish the exact behavior. Must not be enabled. |
| **Restricted** | The capability is disabled by EVORYN project policy or requires a separate authorization gate. |

Market types are independent. The presence of Spot or delegated-Margin support never proves Futures support.

## Current matrix

| Venue | Market | Capability | Status | Evidence / scope | Runtime or safety boundary |
|---|---|---|---|---|---|
| Nobitex | Spot | Public market-data reads (candles/order book/trades/stats) | **Supported** | Official [market-data API docs](https://github.com/nobitex/docs-api/blob/master/source/includes/_market_data.md) and current `NobitexExchangeAdapter` read mappings | Code/contract scope only; live runtime reliability not independently verified |
| Nobitex | Spot | Account/balance read contract | **Supported** | Official [user-data API docs](https://github.com/nobitex/docs-api/blob/master/source/includes/_user_data.md) and read-only adapter implementation | Credentialed production integration is not verified; no secret is needed for this phase |
| Nobitex | Spot | Real order submission/cancellation | **Restricted** | EVORYN adapter raises `TradingDisabledError`; no order-write transport route | Remains disabled |
| Nobitex | Margin | Delegated-position domain semantics and pure normalization | **Supported** | Official [position/Margin docs](https://github.com/nobitex/docs-api/blob/master/source/includes/_position.md), ADR 0005, and `src/evoryn/margin/` | Domain model only; no authenticated Margin HTTP adapter or live position mutation |
| Nobitex | Margin | Production account/position runtime reads | **Unknown** | Official docs describe endpoints, but current EVORYN Margin package documents a pure, no-I/O model | Do not treat domain normalization as runtime connectivity |
| Nobitex | Margin | Real position-opening/closing or collateral writes | **Restricted** | EVORYN development safety boundary; no live write permission | Remains disabled |
| Nobitex | Futures | Contract metadata, funding, margin mode, position mode, liquidation semantics and order lifecycle | **Unknown** | No verified Futures implementation or complete official Futures capability evidence found in the reviewed project sources | No Futures adapter or route may be enabled |
| Unselected external venues | Spot/Margin/Futures | Any runtime operation | **Unknown** | No candidate venue selected and no per-venue capability, rights, eligibility, or contract evidence pack accepted | Do not add provider dependencies yet |
| Any venue | Cross-exchange | Transfer, custody, coordinated positions, atomicity/recovery | **Restricted** | Phase 21 architecture gate; transfer exposure/custody and concurrency design not yet reviewed | Not implemented or authorized |
| Any venue | Futures | CCXT-specific method or market-mode behavior | **Unknown** | [CCXT manual](https://github.com/ccxt/ccxt/wiki/Manual) documents method flags and distinct contract market forms; that is not per-venue EVORYN conformance evidence | No CCXT runtime adapter/dependency added |

## Futures data contract requirements

Any future venue-specific specification must evidence, at minimum:

- Instrument identity: base, quote, settlement asset, exact native symbol, canonical identity, contract kind, contract size/multiplier, and expiry/perpetual semantics.
- Order constraints: price tick, quantity step, minimum quantity/notional, supported order types, trigger/close-only rules if applicable, and precision/rounding behavior.
- Position/account semantics: one-way versus hedge mode, isolated/cross/other margin modes, leverage scope, position side and quantity units.
- Risk observations: entry price, mark/index source, initial and maintenance margin, venue-reported liquidation fields, accrued funding/fees, timestamp, source, and reconciliation identity.
- Funding/settlement: applicability, rate/time units, next funding/settlement times, billing currency, historical behavior, and missing/stale-data response.
- Reliability: rate limits, timeout/error mapping, idempotency, unknown-submit reconciliation, outage recovery, and audit durability.
- Authorization: official API/terms, license/data permissions, account/market eligibility, jurisdictional review, credential scope, and an evidence-backed no-write test boundary.

These are data and verification requirements, not an assertion that every venue exposes all the fields. Missing required fields keep the exact capability **Unknown** or **Restricted**.

## Evidence references reviewed

- [Official Nobitex API documentation repository](https://github.com/nobitex/docs-api)
- [Official Nobitex delegated-Margin/positions reference](https://github.com/nobitex/docs-api/blob/master/source/includes/_position.md)
- [Official CCXT manual](https://github.com/ccxt/ccxt/wiki/Manual)
- [Binance Futures (COIN-M) official exchange-info and funding documentation](https://developers.binance.com/docs/derivatives/coin-margined-futures/market-data/rest-api/Get-Funding-Info)

The Binance page is used only as a reference example of venue-specific fields. Binance has **not** been selected as an EVORYN venue, and its documentation does not establish EVORYN account eligibility or a production-ready adapter.
