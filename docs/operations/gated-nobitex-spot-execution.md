# Gated Nobitex Spot Execution Candidate (Local Validation Required)

## Status

This is an isolated execution candidate under independent review, not a live deployment and not a claim of production readiness. The generic ExchangePort remains read-only. No live or account test is performed by GitHub Actions or this engineering session.

## Components

- src/evoryn/adapters/exchanges/nobitex_spot_trading.py: fixed private-route gateway using Ed25519-signed requests. It exposes only Spot order submission, status reconciliation, and single-order cancellation. It does not expose a generic route caller or any withdrawal/transfer/Margin/Futures write.
- src/evoryn/adapters/exchanges/nobitex_instrument_rules.py: reads the public GET /v2/options response and requires official amount step, price tick and USDT minimum notional for the exact pair.
- src/evoryn/execution/live_spot.py: local feature gate, deterministic Risk Engine precondition, account reconciliation state requirement, exact Limit price/precision validation, durable SQLite idempotency, and operator-invoked reconciliation.
- The Paper execution service remains separate.
- ConnectivityManager retry is not used for signed POSTs; submit/cancel/status operations are single-attempt operations. An ambiguous write outcome remains unknown and blocks further writes.

## Local environment variables

The committed .env.example lists variable names only and contains no credentials. The new write flag defaults to False; ENABLE_REAL_TRADING remains False. A local API key/private-key pair and the exact acknowledgement phrase are required to build the write transport only after the owner deliberately enables the separate flag. The key should have only permissions required for the intended Spot flow and must not permit withdrawals. Remote permissions must be checked in Nobitex; code cannot prove them.

- EVORYN_SPOT_ORDER_WRITES_ENABLED=False
- EVORYN_LIVE_TRADING_ACK= (must match the explicit owner acknowledgement when locally enabling)
- NOBITEX_SPOT_API_KEY=
- NOBITEX_SPOT_PRIVATE_KEY=
- EVORYN_EXECUTION_DB_PATH=./data/execution.sqlite3
Market orders are currently rejected; only limit orders with an explicit validated price are allowed.

Never put these values in GitHub, test fixtures, shell history, or chat. Tests create ephemeral keys in memory and use fake HTTP openers.

## Conservative execution behavior

- Requires account_reconciled is exactly true and execution_error is exactly false.
- Requires the Risk Engine not halted, valid/fresh market inputs, exact Nobitex Spot/USDT identity, and recent official precision/minimum-notional rules. Market orders fail closed.
- Persists the client-order ID as submitting before network I/O.
- Does not automatically retry an order submission or cancellation.
- An unknown submit/cancel outcome latches Risk Engine halt and leaves a durable blocking journal row.
- GET market-data retry remains a separate Phase 18 concern; no retry wrapper handles signed POSTs.
- The first version permits only one active/unresolved order at a time until a complete runtime portfolio/exposure assembler is integrated.
- It does not start a trading loop, generate strategies, construct a production account snapshot, or activate writes merely by importing the module.

## Validation boundary

CI runs synthetic contract/failure tests only. Local integration, private API permission review, order/response schema confirmation, process-restart/reconciliation drills, account exposure integration, protective exit policy, and release approval remain open. A green CI run is not permission to trade and does not establish live compatibility.
