# Phase 22 — Nobitex Read-Only Account Data and USDT-Only Gate

## Scope

Only Nobitex is in scope. The current quote-currency allowlist is USDT. USDT pairs such as
`BTC-USDT`, `ETH-USDT`, `SOL-USDT`, and other exchange-listed USDT pairs can be recognized;
pairs quoted in IRT, BTC, ETH or any other currency are rejected. The base-asset list is not
hard-coded.

Official Nobitex API references:
- Market statistics: https://github.com/nobitex/docs-api/blob/master/source/includes/_market_data.md
- Wallets: https://github.com/nobitex/docs-api/blob/master/source/includes/_user_data.md

## Read-only account reader

`NobitexReadOnlyAccountReader` calls only `GET /v2/wallets` with `currencies=usdt` and one
wallet type per request (`spot`, then `margin`). It returns a `NobitexUSDTAccountSnapshot`
containing typed wallet snapshots. `available_balance` equals the source-reported
`balance - blocked`; missing values, negative/non-finite values, duplicate USDT keys, a blocked
amount larger than balance, or non-success responses fail closed.

If USDT is absent from a valid wallet map, presence is `NOT_LISTED` and amounts are `None`.
It is not changed into a zero balance. The reader never calls user profile, bank account,
deposit-address, order, transfer, withdrawal or collateral-mutation endpoints. Construction does
not initiate polling.

The existing `ExchangePort.get_balances()` is also restricted to Spot USDT. It asks for the USDT
wallet and defensively filters provider rows to USDT. `get_usdt_markets()` requests statistics
using `dstCurrency=usdt` and filters results locally.

## Configuration and risk enforcement

- `EVORYN_ALLOWED_QUOTE_CURRENCIES` defaults to `USDT`. The supported quote-currency ceiling
  is fixed in `src/evoryn/config.py`; configuration attempting to widen it fails.
- The generic `RiskEngine` rejects markets outside Nobitex Spot/USDT before normal order-risk
  calculations.
- The Nobitex Spot adapter checks the same policy before market-data reads.
- The HTTP transport rejects non-USDT market-statistics, candle, order-book and trade-data
  requests; wallet reads must specify `currencies=usdt` with `type=spot|margin`.
- The distinct `MarginRiskPolicy` rejects non-USDT quote currency.
- `NOBITEX_READONLY_API_TOKEN` is the only accepted token setting. It must be injected by the
  runtime/secret provider and configured in Nobitex without trading or withdrawal permission.
  Application code cannot cryptographically verify external token scope.

## Safety status

This is a read-only capability and normalization foundation. CI uses mock transports and does not
contact Nobitex. No live token is available in this engineering session, so live API connectivity
and real account data have not been claimed or tested. Before operational use, verify provider-side
token scope, secret injection, route policy and audit behavior. No order-write, withdrawal,
transfer or collateral-write path exists in this phase.
