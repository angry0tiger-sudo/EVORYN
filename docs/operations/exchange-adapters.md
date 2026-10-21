# Exchange Adapter Architecture

## Plugin loading

EVORYN defines `ExchangePort` in `src/evoryn/ports/contracts.py`. Providers implement the common protocol and declare capabilities. The registry discovers installed plugins through the Python entry-point group `evoryn.exchanges`; the built-in Nobitex plugin is registered through `pyproject.toml`.

To instantiate a provider, create an `ExchangePluginRegistry`, call `discover_installed()`, then call `create(name, config)` with an explicit configuration mapping. Do not load a plugin from an arbitrary file path or user-provided import string. Entry-point loading executes installed Python code.

## Error taxonomy

Provider-independent errors are in `evoryn.ports.exchange_errors`. Callers should branch on stable exception classes/codes rather than parse provider-specific error text:

- Authentication and authorization failures
- Invalid requests, not found, and conflicts
- Rate limits with an explicit `retry_after_seconds`
- Timeouts, transport failures, and provider unavailability
- Malformed responses and unsupported capabilities
- Trading-disabled and unknown-order-outcome cases

Retry policy is not implicit. In particular, an unknown order outcome must not trigger an automatic second submission.

## Rate limits

`RateLimitPolicy` defines a maximum number of requests per sliding window. `SlidingWindowRateLimiter` is thread-safe, supports deterministic clock injection in tests, and raises `RateLimitExceeded` instead of sleeping or retrying silently. Keys should include the exchange and the endpoint category.

Nobitex local client budgets are currently:

| Endpoint category | Local budget |
|---|---:|
| Order book | 300 requests / 60 seconds |
| Recent trades | 60 requests / 60 seconds |
| Market statistics | 20 requests / 60 seconds |
| UDF candles | 60 requests / 60 seconds, conservative local budget |
| Wallet balances | 15 requests / 60 seconds |
| Order status | 60 requests / 60 seconds |

Official endpoint notes are available in the [Nobitex market API documentation](https://github.com/nobitex/docs-api/blob/master/source/includes/_market_data.md) and [wallet/user API documentation](https://github.com/nobitex/docs-api/blob/master/source/includes/_user_data.md). Provider limits may change; verify official docs before modifying budgets. A local limiter does not coordinate across processes or hosts.

## Nobitex adapter scope in Phases 6/7

Implemented normalized reads:

- OHLCV candles through `GET /market/udf/history`, with aligned-array validation and the documented maximum of 500 candles per request.
- Order-book levels through `GET /v3/orderbook/{symbol}`.
- Recent trades through `GET /v2/trades/{symbol}`.
- Market statistics through `GET /market/stats`.
- Spot wallet balances through `GET /v2/wallets`, requiring an explicitly supplied read token.
- Order status through `POST /market/orders/status`, using the HTTP POST method solely for the documented read query.

The HTTP transport pins the official HTTPS host, disallows redirects, and allowlists exact read routes. It rejects order creation, cancellation, transfer, withdrawal, and all other unlisted routes.

### Write boundary

The common `ExchangePort` declares `submit_order` and `cancel_order` for architectural completeness. The current Nobitex adapter always raises `TradingDisabledError` for both methods and never calls the transport. `ENABLE_REAL_TRADING=True` is rejected rather than treated as authorization. The example flag is not a runtime safety system and does not enable writes.

Order-writing implementation requires the later execution and risk phases, verified API/order-lifecycle contracts, reconciliation and idempotency tests, independent review, and explicit release authorization. This adapter must not be described as production-ready for real-money trading.


## Authenticated order/trade snapshots for reconciliation research (Phase 32)

The dedicated `NobitexReconciliationReader` uses two documented GET-only user endpoints:

- `GET /market/orders/list` for filtered Spot/USDT user-order snapshots, including matched/unmatched amounts and order identity where the provider returns those fields.
- `GET /market/trades/list` for user-trade records with the originating order ID, fee and provider pagination marker.

The low-level transport allowlists these exact paths only. It requires a read-only API token, requires a bounded BASE/USDT query, rejects unknown query keys, and performs no order mutation. The provider returns one page at a time; it does **not** claim the account ledger is complete. Missing `hasNext`, absent `tradeType`, unknown status, or quantity inconsistencies leave a page ineligible for authoritative reconciliation. Nobitex documents the user-trade history window as three days, so this endpoint alone cannot rebuild an arbitrarily old ledger.

Official contract reference: [Nobitex Spot API documentation](https://apidocs.nobitex.ir/), sections “فهرست سفارش‌های کاربر” and “فهرست معاملات کاربر”.

These read primitives are not wired into Live Runtime and do not enable writes. Before using them to clear a blocked submission, EVORYN still needs a complete pagination policy, exact-head contract tests against sanitized exchange responses, and a tested reconciliation state machine that compares the journal, balances, orders and fills after restart. Unknown state must continue to block new submissions.

## Phase 21A capability evidence boundary

The legacy `ExchangeCapabilities` booleans continue to describe the existing common adapter contract; they do
not prove support for every market type. See `docs/engineering/phase-21-capability-matrix.md` and ADR 0014
for the evidence-backed per-venue/per-market/per-feature model. An omitted capability is `unknown`. This
initial model is not yet wired as a production runtime permission service. In particular, current Nobitex
Spot/Margin contracts do not prove Futures support. No new order-writing route or Futures adapter is
introduced by the initial Phase 21A foundation.
