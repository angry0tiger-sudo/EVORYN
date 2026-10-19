# Local Deployment and Validation Guide

## Goal and safety status

This runbook bootstraps a local EVORYN checkout for reproducible development and offline verification. It does **not** install or start a production autonomous trading service. No live exchange request is made by either setup script.

The current repository has no integrated production strategy loop, account/portfolio snapshot builder, fill/open-order reconciliation, protective exit policy or production process supervisor. Do not set the Spot-write feature flag to true to try to bypass those missing release components.

## Windows setup

1. Install Git and Python 3.12.x from their trusted official distributions.
2. Clone the EVORYN repository and open a terminal in the repository root.
3. Run the matching command:

   **Command Prompt (`cmd.exe`):**

   ```bat
   scripts\setup.bat
   ```

   **PowerShell:**

   ```powershell
   .\scripts\setup.bat
   ```

4. The script creates `.venv\` when absent and verifies that an existing environment already uses Python 3.12.x rather than replacing it. It preserves an existing `.env`, copies `.env.example` only when no `.env` exists, creates `data\`, installs `.[dev]` using the project-local interpreter, compiles source/test modules and runs the offline test suite.

The script exits non-zero if environment validation, dependency installation, compilation or tests fail. GitHub CI does not execute this batch script; native Windows setup remains unverified until tested on a Windows host.

## Linux setup

1. Install Git and Python 3.12.x using the system's trusted package/distribution source.
2. Clone EVORYN and change into the repository root.
3. Run:

   ```bash
   bash scripts/setup.sh
   ```

4. The script creates `.venv/` when absent and verifies that an existing environment already uses Python 3.12.x rather than replacing it. It preserves an existing `.env` without changing its contents or permissions; a newly copied `.env` is set to mode 600. The script creates `data/` with mode 700, installs `.[dev]` using the project-local interpreter, compiles source/test modules and runs the offline test suite. Permission-setting failures are fatal rather than silently ignored.

## Environment file

- `.env.example` is a template and must not contain real credentials.
- `.env` is ignored by Git. The setup scripts never overwrite it.
- Leave all credential fields blank for installation, test/lint/type-check and all offline work.
- `NOBITEX_BASE_URL=` and `NOBITEX_TIMEOUT_SECONDS=` may remain blank; safe defaults are the fixed official HTTPS hostname and a 10-second per-request timeout.
- For the supervised account-read command only, provide `NOBITEX_READONLY_API_TOKEN` after independently verifying the key has no trade or withdrawal permission in Nobitex.
- `NOBITEX_SPOT_API_KEY` and `NOBITEX_SPOT_PRIVATE_KEY` are reserved for the separately reviewed Spot execution candidate. Keep them blank; this is not yet a complete trading runtime.
- `ENABLE_REAL_TRADING=False` and `EVORYN_SPOT_ORDER_WRITES_ENABLED=False` must remain unchanged.
- `EVORYN_PROXY_URL=` is intentionally unused. Fixed HTTPS transports ignore ambient proxy environment configuration; no proxy/SOCKS/VPN adapter is implemented.

Never store credentials in source, shell history, CI secrets, screenshots, diagnostics, chat messages or issue descriptions.

## Offline verification commands

Windows:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src tests scripts
```

Linux:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests scripts
```

The test suite uses fake transports and generated ephemeral keys. It is not a live API test.

## Optional supervised read-only check

After the owner deliberately chooses to validate account reads on the local system, and only after confirming the token has read-only permissions, run:

Windows:
```powershell
.\.venv\Scripts\python.exe -m scripts.live_readonly_nobitex_usdt
```

Linux:
```bash
.venv/bin/python -m scripts.live_readonly_nobitex_usdt
```

This script loads `.env` locally and only permits the USDT Spot/Margin wallet read contract. It does not place, change or cancel an order. The setup scripts never run this command. An account check must not be run from GitHub Actions.

## Network resilience

Phase 18 supplies a reusable route-authorized `ConnectivityManager` for idempotent JSON GET calls with bounded retry/backoff/jitter, parsed Retry-After delays, a shared 429 cooldown, circuit-breaker recovery probes and sanitized audit events. The read-only JSON transport uses a fixed HTTPS host allowlist, a timeout and response-size bounds. No proxy route is enabled. Signed Spot order submit/cancel are one-shot calls: if the result is ambiguous, EVORYN must reconcile instead of retrying.

The approved route authorization and durable audit-sink composition remain a production runtime integration requirement. A generic helper or a green CI run must not be treated as evidence that a production connector is wired or that the local network route is authorized.

## Current CI evidence and local acceptance boundary

On 2026-10-19, code/test candidate SHA `787bc5186628389f23c43809ba6eb00cd25e3091` passed all six configured GitHub Actions gates in [Run #879](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39312972444) and [PR Run #880](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39312976737); pytest reported 347 passed. CI checks `setup.sh` syntax but does not execute either setup script as a full installation, and cannot validate native Windows behavior or the owner's machine. Any later PR-head change must pass all gates on its exact HEAD; local setup acceptance remains open until the host run is recorded.

## Evidence to capture on the local machine

After setup, record the exact commit SHA, OS version, Python version, the setup-script exit code, pytest summary and any failure logs. Do not include tokens or `.env` contents. If/when a supervised read-only check is deliberately run, retain only the sanitized normalized output and its timestamp, never the API token.

## Release blockers before real trading

A production release still needs a reviewed runtime entry point, account/portfolio builder, exposure and fill/open-order reconciliation, persistent recovery drills, explicit protective exit policy, operational monitoring/kill-switch wiring, route authorization and durable audit storage, an independent risk/security review, and owner-run local validation. Keep all Spot writes disabled until every relevant gate is closed.
