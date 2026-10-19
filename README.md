# EVORYN

**Autonomous Adaptive Trading Intelligence**

EVORYN is an independently governed trading-intelligence platform. The currently reviewed market boundary is **Nobitex Spot / USDT**. Margin code is domain-level only and does not imply that a production Margin runtime is available.

## Current engineering status

- PR #26 added the supervised read-only Nobitex USDT wallet check. No live API test is performed in GitHub Actions.
- The generic `ExchangePort` and read-only Nobitex transport remain write-disabled.
- A separate local Spot execution candidate contains an Ed25519-signed, fixed-route gateway, official instrument-rule reader, deterministic Risk Engine preconditions, a durable SQLite idempotency journal and an explicit default-off owner gate. It is **not a production trading runtime**.
- Phase 23 adds a bounded end-to-end Mock/Paper runtime connecting regime classification, strategy selection, intents, deterministic risk, Paper fills/reconciliation, portfolio state, protective exits and SQLite recovery.
- Phase 24 adds an explicitly bounded public-historical-data research workflow, a versioned regime-model training/holdout backtest pipeline, and a cooperative process-local Paper scheduler. The latest real-history workflow [Run #4](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39328332540) completed successfully on code/test SHA `f9f634553f8c6edb2b173b7b885ad2d9cf34330c`: 4,320 gap-free hourly BTC-USDT candles, dataset version `c1a13b3533f3777fa743a8fd6632d73b1760b355341fa1537b7a5ef6ab24a616`, candle SHA-256 `0d976abd6772fc8dce0578b55d69c9686aabe7ffa0a9329cc46c600349297d84`, and model SHA-256 `38799b767b2aa6c2910c07383c3e213db090d5b8d5c1a1e401ee963173c17f2c`. Holdout accuracy was 33.92% versus a train-only majority-class baseline of 58.51%; balanced accuracy 25.59%, macro-F1 24.66%, Brier 0.72879, log loss 1.34498. The trained-regime holdout return was −0.2411%, versus −1.3562% for SMA crossover and +1.4810% for buy-and-hold, under explicit estimated costs. The candidate fails the predictive-quality gate, is **not promoted**, and remains research-only. Artifacts and limitations are recorded in [the research report](docs/research/2026-10-20-nobitex-regime-holdout.md) and [Research Run #4 artifacts](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39328332540/artifacts/12223262662). Provider-backed production account state, live venue reconciliation, OS-level service installation, data-use rights review, independent risk/security review and host acceptance remain open. There is no supported command to start an autonomous live-trading bot. Do not enable Spot writes or interpret passing CI as trading authorization.

## Engineering principles

- Architecture first, implementation second, evidence always.
- Deterministic risk controls stay independent from AI recommendations.
- Secrets stay local and never enter GitHub, test fixtures, agent prompts or logs.
- A timeout during an order write is an **unknown outcome**, not permission to submit again.
- The six-gate CI uses mocks/fixtures only. A separate research workflow reads unauthenticated public Nobitex OHLCV history through a fixed GET-only route; it does not access accounts or order endpoints. Live account validation is deferred to the owner's local machine.

## Requirements

- Python 3.12.x
- Git
- Windows PowerShell/Command Prompt or a Bash-compatible Linux shell
- Internet access to the configured Python package index for the first dependency installation
- SQLite is included with Python; no external database server or Docker daemon is required by the bootstrap

## One-command local setup

Run the command from the root of a fresh EVORYN clone.

### Windows

Open PowerShell or Command Prompt:

```bat
scripts\setup.bat
```

### Linux

```bash
bash scripts/setup.sh
```

The scripts create a project-local `.venv`, install the package and development dependencies, create `data/`, copy `.env.example` to `.env` only when `.env` does not already exist, compile source/test files and run the offline unit-test suite. They do not call Nobitex, do not create an order and do not start a trading process.

If setup fails, keep the full terminal output and the exact `git rev-parse HEAD`. Do not work around a failed test by disabling a CI gate.

## Configure local environment

1. Open the local `.env` file in the repository root.
2. Leave all credentials blank for installation and offline testing. The setup scripts never ask for keys.
3. To run the explicitly supervised **read-only account command**, put a Nobitex token in `NOBITEX_READONLY_API_TOKEN` that you have independently verified in the Nobitex account interface to have no trade or withdrawal permissions. Do not use a trading key for the wallet-only check.
4. `NOBITEX_BASE_URL` may remain blank; the application selects the fixed official HTTPS hostname. `NOBITEX_TIMEOUT_SECONDS` may remain blank; the safe default is 10 seconds. The supported quote-currency policy remains fixed to USDT and cannot be widened from `.env`.
5. Save the file. `.env` is excluded from Git by `.gitignore`. Never commit it, paste its contents into GitHub or chat, include it in screenshots or diagnostics, or store a key in the command line.

### Environment reference

| Variable | Purpose | Current status |
|---|---|---|
| `NOBITEX_READONLY_API_TOKEN` | Explicitly supervised USDT wallet-read command | Locally supplied only |
| `NOBITEX_BASE_URL` | Optional fixed API endpoint override | Blank uses official HTTPS hostname; alternate hosts are rejected |
| `NOBITEX_TIMEOUT_SECONDS` | Per-request timeout | Blank defaults to 10 seconds |
| `EVORYN_ALLOWED_QUOTE_CURRENCIES` | Quote-currency whitelist | Only USDT is accepted |
| `ENABLE_REAL_TRADING` | Legacy global safety switch | Must remain `False`; it does not arm the new isolated candidate |
| `EVORYN_SPOT_ORDER_WRITES_ENABLED` | Candidate write feature flag | Must remain `False` until separate local release approval |
| `EVORYN_LIVE_TRADING_ACK` | Explicit owner acknowledgment for the candidate | Empty by default |
| `NOBITEX_SPOT_API_KEY` / `NOBITEX_SPOT_PRIVATE_KEY` | Local Ed25519 Spot API credential pair | Empty; do not use until the runtime and release gates are accepted |
| `EVORYN_EXECUTION_DB_PATH` | SQLite order journal location | Defaults to `./data/execution.sqlite3` |
| `EVORYN_MAX_MARKET_SLIPPAGE_BPS` | Reserved local candidate limit | Candidate is Limit-only; Market orders are rejected |
| `EVORYN_PROXY_URL` | Future proxy configuration placeholder | Currently ignored; network transports force direct HTTPS |
| `EVORYN_ENV`, `EVORYN_LOG_LEVEL`, `EVORYN_DATA_DIR`, `EVORYN_DATABASE_URL`, `EVORYN_METRICS_ENABLED` | Future runtime settings | Reserved, not a claim that those runtime services are wired |

Proxy, SOCKS5 and VPN support is not enabled. An environment variable is not a reviewed transport: enabling a proxy requires a separate implementation, exact route authorization, audit configuration and security review. The application ignores the placeholder and does not use ambient HTTP proxy variables for its fixed HTTPS transports.

## Run offline checks manually

### Windows

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src tests scripts
```

### Linux

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests scripts
```

These checks are offline with respect to the exchange. Tests for signed requests use ephemeral keys and fake HTTP responses.

## Optional supervised read-only wallet check

Run this **only on the owner's local machine** after the token has been verified as read-only. It sends a real account-read request; setup and GitHub CI never run it.

Windows:

```powershell
.\.venv\Scripts\python.exe -m scripts.live_readonly_nobitex_usdt
```

Linux:

```bash
.venv/bin/python -m scripts.live_readonly_nobitex_usdt
```

The command allows only the USDT Spot and Margin wallet reads defined by the script and prints a normalized snapshot. It does not submit/cancel orders, withdraw, transfer or modify account state. A successful response proves only that this read-only command completed at that time; it does not prove write capability or production readiness.

## Phase 18 connectivity and recovery

`src/evoryn/adapters/network/connectivity.py` contains the route-authorized `ConnectivityManager`, a bounded exponential-backoff/jitter policy, `Retry-After` handling, shared rate-limit cooldown, a CLOSED/OPEN/single-probe HALF_OPEN circuit breaker, sanitized fail-closed audit events and feed freshness/gap checks. The underlying `HttpsJsonTransport` uses per-request timeouts, a fixed HTTPS host allowlist and a response-size cap.

The manager retries only an idempotent JSON GET contract. It must not be wrapped around signed Spot submission or cancellation. Recovery is a new bounded request after delay; there is no long-lived socket that should be blindly replayed. The manager requires a current `NetworkRouteAuthorization` and a durable `ConnectionAuditSink`. Integration of that approved composition into a complete production bot runtime is still a release gate; the current repository must not claim that every connector is already wired through it.

## Spot execution safety boundary

The Spot candidate is deliberately separated from the read-only `ExchangePort`. Its core safety properties are:

- disabled by default and not started automatically;
- explicit local owner acknowledgment plus a local Ed25519 API-key pair required to construct a write transport;
- only Nobitex Spot/USDT and **Limit** orders; Market orders, Margin writes, Futures, transfers and withdrawals are unsupported;
- deterministic Risk Engine approval, account-reconciliation state and current public instrument rules required before submitting;
- unique client order ID is durably journaled before a single write attempt;
- order writes are never automatically retried; unknown results require explicit reconciliation and latch a risk halt;
- initial implementation blocks new orders while another order is open or unresolved.

This is a safety-oriented implementation candidate, not a launchable automated trading system. Before any live-order use, complete the application runtime, actual portfolio/account-state builder, fill/open-order reconciliation, safe-exit policy, persistence recovery drills, independent review, and owner-supervised local validation. Keep the order-write flag disabled until those gates are closed.

## Reference documents

- [Engineering Operating Contract](AGENTS.md)
- [Architecture Decision Records](docs/adr/README.md)
- [Phase 18: Connectivity Manager](docs/operations/phase-18-connectivity-manager.md)
- [Gated Spot execution candidate and local validation](docs/operations/gated-nobitex-spot-execution.md)
- [Local deployment guide](docs/operations/local-deployment.md)
- [Read-only Nobitex USDT smoke-test runbook](docs/operations/live-readonly-nobitex-usdt-smoke-test.md)
- [Phase 23: End-to-End Mock/Paper Runtime](docs/operations/phase-23-paper-runtime.md)
- [Phase 24: Historical Regime Research and Process-Local Scheduler](docs/operations/phase-24-regime-training-supervisor.md)
- [Nobitex Regime Model Holdout Research (real public OHLCV, research-only)](docs/research/2026-10-20-nobitex-regime-holdout.md)
- [Technical debt and release gates](TECHNICAL_DEBT.md)
