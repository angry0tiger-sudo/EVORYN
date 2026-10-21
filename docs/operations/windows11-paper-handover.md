# Windows 11 Handover — EVORYN Paper Runtime and AI Research

**Date:** 2026-10-21  
**Repository:** https://github.com/angry0tiger-sudo/EVORYN  
**Regime research merge:** `201d7493615458c9b482fbbb21f98ec2936952f0`  
**Champion:** none. `selected_model_for_runtime = null`.  
**Authorized mode:** offline Paper only.  
**Owner-host status:** this work did not access the owner's Windows host, create local files there, run commands there, or register a Task Scheduler task.

## 1. Current verified state

PR #32 and PR #33 are merged into `main`. The last documented PR #33 head passed all seven standard CI gates. Research [Run #39544524629](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39544524629) completed and uploaded an immutable artifact.

- HMM: 5/5 folds, balanced accuracy 89.76%, gated-SMA mean net return -1.9954% per fold.
- XGBoost: 5/5 folds, balanced accuracy 99.73%, gated-SMA mean net return -2.3676% per fold.
- Unfiltered SMA averaged -0.1999%; Buy-and-Hold averaged +0.9950% under the same simulated costs.
- Both regime candidates failed economic acceptance. Previous Meta-Labeling evaluated only 11/13 expected folds; every evaluated threshold abstained and both candidates had zero closed trades.

**High-score trap:** the XGBoost target was generated from trailing 24-bar return, and `return_24` was also an input. The near-99% metric mainly reproduces the label rule. This is target/feature or definitional leakage, not proof of predictive alpha; the evidence alone does not establish classic future-row look-ahead bias.

Latest dataset snapshot: 11,439 hourly candles, 23 public-history pages, zero detected gaps, 2025-07-01 13:30 UTC through 2026-10-21 03:30 UTC. Volume units remain unverified; historical order-book depth is unavailable; spread/slippage are assumptions; provider terms remain unreviewed.

## 2. Windows 11 setup

Prerequisites: Windows 11, Git and Python 3.12.x. The following are instructions for a later owner-authorized local setup; **they have not been executed on the owner's computer**.

From the repository root in PowerShell or Command Prompt:

```bat
scripts\setup.bat
```

The setup script creates `.venv` if absent, preserves an existing `.env`, copies `.env.example` only if `.env` is missing, creates `data\`, installs `.[dev]`, compiles source/tests and runs offline tests. It does not contact Nobitex, start Paper or register a task.

Verify:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src tests scripts
git rev-parse HEAD
```

Record the exact SHA and summaries; never paste secrets or `.env` contents into issues/chat.

## 3. `.env` and secret boundary

For development/Paper, keep:

```dotenv
EVORYN_ALLOWED_QUOTE_CURRENCIES=USDT
ENABLE_REAL_TRADING=False
EVORYN_SPOT_ORDER_WRITES_ENABLED=False
EVORYN_LIVE_TRADING_ACK=
NOBITEX_READONLY_API_TOKEN=
NOBITEX_SPOT_API_KEY=
NOBITEX_SPOT_PRIVATE_KEY=
EVORYN_PROXY_URL=
```

Other variables include `EVORYN_ENV`, `EVORYN_LOG_LEVEL`, `EVORYN_DATA_DIR`, `EVORYN_DATABASE_URL`, `EVORYN_METRICS_ENABLED`, `NOBITEX_BASE_URL`, `NOBITEX_TIMEOUT_SECONDS`, `EVORYN_EXECUTION_DB_PATH` and `EVORYN_MAX_MARKET_SLIPPAGE_BPS`. Several are reserved/future-facing; a variable in the template is not proof a full service consumes it. `EVORYN_PROXY_URL` is ignored; proxy/SOCKS/VPN is not enabled.

The Paper runner scrubs credential-bearing EVORYN/NOBITEX/Binance variables and does not load `.env`. Its separate JSON configuration is authoritative. Paper needs no exchange token or key. The optional read-only wallet request is a separate, supervised operation and is not part of this runbook.

## 4. What Paper Runtime actually does

Entry point: `scripts/windows_paper_task.py`. It consumes a local CSV, writes a local SQLite Paper journal and rotating log, and runs the deterministic Risk Engine/Paper components. It does not connect to an exchange, read credentials or load HMM/XGBoost. Its classifier returns `UNKNOWN` / `NO_CHAMPION_SELECTED`, so strategy selection abstains. This validates supervision/recovery only, not AI performance or profitability.

Task defaults:
- Name: `EVORYN-PaperOnly`
- Trigger: current interactive user at logon
- Privilege: Limited
- Multiple instances: `IgnoreNew` plus process/journal lock
- Restart budget: 3 restarts, 5 minutes apart
- Source: local CSV only
- Journal: local SQLite, e.g. `data\paper-runtime.sqlite3`
- Log: rotating local file, e.g. `logs\windows-paper-runtime.log`

It is not a Windows Service and does not run while the user is logged out. No task was registered on the owner's host. Windows CI tests registration on an ephemeral runner only; native Windows 11 acceptance remains open.

## 5. Local CSV and configuration

The CSV header must be exactly:

```csv
timestamp,open,high,low,close,volume
```

Rows require explicit UTC timestamps (`Z` or `+00:00`), even chronological intervals, finite positive OHLC values and finite non-negative volume. Latest completed candle age must be at most two intervals. This is a local quality check, not proof of a current exchange quote.

Example: `config/windows-paper.example.json`. Later, from the repository root in PowerShell:

```powershell
New-Item -ItemType Directory -Force .\data, .\logs | Out-Null
Copy-Item .\config\windows-paper.example.json .\config\windows-paper.local.json
```

The example paths are `../data/paper-candles.csv`, `../data/paper-runtime.sqlite3` and `../logs/windows-paper-runtime.log` relative to the config file. The strict schema only accepts `runtime_mode=paper_only`, `simulation_only=true`, and `market_symbol=BTC-USDT`; unknown keys (including model path) are rejected. The bootstrap does not generate a verified CSV. Use an approved source or synthetic fixture; don't bypass stale-data preflight with an unapproved fetch. Never commit local config, CSV, SQLite or logs.

## 6. Preflight and Windows Task Scheduler

After preparing the CSV and local config:

```powershell
# Validate files/config without creating the journal or starting a loop.
.\.venv\Scripts\python.exe .\scripts\windows_paper_task.py preflight --config .\config\windows-paper.local.json

# One offline cycle.
.\.venv\Scripts\python.exe .\scripts\windows_paper_task.py run --config .\config\windows-paper.local.json --max-cycles 1

# Register the current-user logon task only after preflight passes.
.\.venv\Scripts\python.exe .\scripts\windows_paper_task.py register --config .\config\windows-paper.local.json

# Inspect state.
.\.venv\Scripts\python.exe .\scripts\windows_paper_task.py status

# Remove the EVORYN Paper task.
.\.venv\Scripts\python.exe .\scripts\windows_paper_task.py unregister
```

Registration refuses to overwrite an existing task and checks CSV freshness first. Do not elevate privileges to bypass failure. If an owner-authorized local test is later performed, inspect Task Scheduler history, `LastTaskResult`, SQLite and rotating logs; verify account ACLs, logon/logoff and shutdown behavior. Unregister if no longer needed.

## 7. Next AI phases

See [ADR-0020](../adr/0020-end-to-end-ai-intelligence-core-roadmap.md) and [AI Intelligence Core Roadmap](../research/ai-intelligence-core-roadmap.md):

1. Triple-Barrier: take-profit, stop-loss and vertical expiry labels, causal volatility scaling, event intervals, purge and embargo.
2. Cross-fitted Meta-Labeling: base SMA signal, out-of-fold meta features, separate validation calibration and abstention.
3. Dynamic Position Sizing: calibrated confidence plus stop/volatility risk, strictly capped by deterministic Risk Engine.
4. Reinforcement Learning last: offline discrete actions only after a credible simulator and stronger baselines exist.

Each stage needs separate changes, tests, exact-head CI, immutable artifacts, full planned walk-forward coverage and predeclared positive economic evidence. Don't lower gates because the classifier score looks impressive.

## 8. Hard boundaries

- No real order, live connection or authenticated exchange operation is permitted.
- No credential is needed for Paper.
- No script/task was executed or registered on the owner's Windows 11 machine during this handover.
- `selected_model_for_runtime = null`; `runtime_promotion_allowed = false`; no Champion.
- If local state differs, stop and record evidence instead of bypassing a fail-closed guard.

## References

- [ADR-0020 — End-to-End AI Intelligence Core Roadmap](../adr/0020-end-to-end-ai-intelligence-core-roadmap.md)
- [AI Intelligence Core Roadmap](../research/ai-intelligence-core-roadmap.md)
- [Windows Task Scheduler Runbook](windows-paper-task-scheduler.md)
- [Local Deployment Guide](local-deployment.md)
- [Phase 23 Mock/Paper Runtime](phase-23-paper-runtime.md)
- [Phase 24 Research and Scheduler](phase-24-regime-training-supervisor.md)
