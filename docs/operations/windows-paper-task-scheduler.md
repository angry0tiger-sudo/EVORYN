# Windows Task Scheduler — Paper-only runbook

## Scope and hard boundary

This implementation is a **local CSV replay/supervision task only**. It does not contact Nobitex or another exchange, does not read exchange/account credentials, does not load a model artifact, and cannot select a Champion. The runtime classifier returns `UNKNOWN` with reason `NO_CHAMPION_SELECTED`; strategy selection therefore remains abstaining. Any protective action against a previously persisted Paper position is executed only through the in-process Mock/Paper broker and its Risk Engine.

The task is registered for the **current interactive user at logon**, with Limited privilege, multiple-instance behavior `IgnoreNew`, a bounded three-restart budget at five-minute intervals, and a rotating local log. It is not a Windows Service and does not run when the user is logged out. The app-level Windows named mutex and exclusive journal lock are required even though Task Scheduler ignores a second instance.

No user-machine task is registered by this repository change. Windows CI registers, queries, and unregisters a uniquely named disposable task on the ephemeral GitHub-hosted Windows runner. Native acceptance on the owner’s Windows 11 machine remains a separate gate.

## Input contract

Copy `config/windows-paper.example.json` to a local config file, then set the paths to a local CSV, local SQLite journal, and local log. Paths are resolved relative to the config file when they are relative. The CSV must be a regular local file, not a symbolic link or UNC/network path, with exactly this header:

```csv
timestamp,open,high,low,close,volume
```

Timestamps must be ISO-8601 with explicit UTC offset `Z` or `+00:00`; candles must be chronological, gap-free, and consistent with the configured interval. OHLC prices must be finite and positive; volume must be finite and non-negative. The latest completed candle must be no more than two intervals old. This is a data-quality freshness check for local Paper replay, not evidence of a current exchange quote or tradable liquidity.

The config schema is exact and rejects unknown keys. Its only accepted mode is `runtime_mode=paper_only` with `simulation_only=true`, and the market is fixed to `BTC-USDT`. There is no model-path field. The preflight command does not create the journal or start the runtime.

## Commands

From an activated project Python environment:

- Validate the local files without mutating the journal:

  `python scripts/windows_paper_task.py preflight --config config/windows-paper.local.json`

- Register the fixed, user-scoped Task Scheduler entry (only after preflight passes):

  `python scripts/windows_paper_task.py register --config config/windows-paper.local.json`

- Inspect registration state:

  `python scripts/windows_paper_task.py status`

- Remove only the fixed EVORYN Paper task:

  `python scripts/windows_paper_task.py unregister`

- Run a bounded one-cycle check manually, still without exchange connectivity:

  `python scripts/windows_paper_task.py run --config config/windows-paper.local.json --max-cycles 1`

The PowerShell helper rejects task names outside the reserved EVORYN Paper namespace, refuses to overwrite an existing task, validates the CSV before registration, and refuses UNC-backed input. Unregistration is restricted to the fixed production task name or the CI-only disposable namespace.

## Fail-closed behavior

Missing config, unexpected config keys, a non-Paper mode, malformed/stale/gapped candles, invalid journal state, duplicate process ownership, unresolved Paper state, a risk HALT, or runtime exceptions stop the task. It never clears a latched HALT or resubmits an uncertain operation after restart. Stale input or a failed preflight is not repaired automatically.

Credential-bearing environment variables with EVORYN/Nobitex/Binance prefixes or key/secret/token/live-write markers are removed at runner startup before the runtime is composed. The runner has no network or exchange imports and uses only the local CSV and SQLite Mock/Paper components. This environment scrub is defense in depth, not a substitute for host account ACLs.

## CI coverage and remaining gate

The Windows CI job parses the PowerShell helper and runs cross-process lock tests plus an actual register/status/unregister round trip using an ephemeral CI task name. The test always attempts to unregister in a `finally` block. Linux CI runs config, CSV schema/gap/freshness, no-Champion and task-name boundary tests.

Still required before owner-host acceptance: review effective Windows account ACLs, verify task history/log rotation after a deliberate stale-feed stop, test disk/access-denied/journal-corruption/restart-budget failure modes, verify graceful user logoff/shutdown behavior, and document the native Windows 11 registration/unregistration result. These tests are not inferred from a green CI run.
