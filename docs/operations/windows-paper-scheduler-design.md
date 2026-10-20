# Windows Paper Runtime Supervision Design

**Status:** The offline Paper-only composition root, CSV preflight, single-instance lock and Task Scheduler registration/status/unregistration code are implemented on the PR branch. Exact-HEAD Windows CI is pending verification. No task was registered on the owner Windows 11 host.

## Decision

Use Windows Task Scheduler for the initial owner-hosted Paper-only deployment, not a claimed Windows Service. The current PaperRuntimeScheduler remains the in-process one-cycle-at-a-time loop. A reviewed launcher/composition root is a prerequisite; no safe unattended command is available until the runtime source and dependency graph are accepted.

## Required composition root

Before implementation is enabled, provide and review:

- Implemented: local CSV-only completed-candle source with UTC timestamp, staleness, duplicate, future-candle, OHLC and gap checks. No exchange connection or network-backed feed exists in this task.
- A Paper-only dependency graph: model loader pinned to independently trusted artifact and manifest digests, regime detector, selector, intent builder, deterministic Risk Engine, Mock/Paper broker, reconciler and durable SQLite journal.
- Implemented: `evoryn-offline-csv-paper-only-v1` source identity and explicit initial Paper capital in strict config.
- Implemented: `src/evoryn/runtime/single_instance.py` process-wide Windows named mutex and exclusive journal lock; duplicate instances must exit without opening the ledger. Exact-HEAD Windows CI evidence is required.
- Sanitized rotating logs, a health state, shutdown signal handling and nonzero exit codes for data integrity, journal, reconciliation or HALT failures.
- No Live exchange connector or write-capable credential in the Paper task environment. Feed/model faults must never fall back to Live.

## Task Scheduler profile after acceptance

1. Run in the current interactive user's logon session with Limited privilege. The task registration does not request administrator rights or store a password. Restrict writes to Paper data, journal and log directories through owner-host ACL review.
2. Trigger at user logon after strict read-only preflight validates the local CSV and paths. The task does not run when the user is logged out.
3. Set MultipleInstances to `IgnoreNew`, backed by the Windows named mutex and exclusive journal lock.
4. The registered task uses three restarts with five-minute intervals. The runtime remains fail-closed; restarts do not clear a latched HALT or resubmit an uncertain order.
5. Request graceful shutdown first. Flush or durably record interrupted cycles before releasing the lock. Forced termination is not reconciliation.
6. Store model artifacts and trusted SHA-256 pins in an ACL-restricted location. Validate both digests before joblib deserialization at every process start.
7. Record code revision, model fingerprint, dataset version, feature schema, configuration hash and journal identity in sanitized logs.
8. The Paper task must not load Live credentials or modify EVORYN_SPOT_ORDER_WRITES_ENABLED or the Live acknowledgement.

## Process state machine

- STARTING: validate config, artifact pins, source/freshness, journal integrity and exclusive lock.
- READY_PAPER: only after full preflight and reconciliation pass.
- HALTED: terminal for the process; no automatic clear or order resubmit.
- STOPPING: prevent new cycles, flush audit/journal state, release lock and exit.
- FAILED: integrity, permission, stale-feed, unexplained balance or journal uncertainty; write sanitized diagnostics and exit nonzero.

A restart from HALTED or unknown operation state requires an operator-reviewed reconciliation record. Startup code must not delete or repair the journal to make preflight green.

## Acceptance tests before registering the task

- Single-instance race and locked-journal tests; implemented Windows CI round trip for task register/status/unregister using a disposable CI task name.
- Clean startup and restore from a valid persisted Paper state.
- Restart after clean shutdown and crash injection at every journal transaction boundary.
- Stale, duplicate, gapped, future and malformed candle tests.
- Artifact/manifest digest mismatch and feature-schema mismatch drills.
- Disk-full, access-denied, journal corruption, logging failure and restart-budget exhaustion.
- Stop, terminal HALT, unresolved intent and no-resubmit tests.
- Evidence that the task cannot load Live credentials, call write endpoints or enable Spot writes.
- Owner-host Windows 11 validation of permissions, startup, logs, recovery, registration and uninstallation.

**Current gates:** green exact-HEAD seven-job CI, fresh research artifact with schema-v3 policy-specific backtests, Windows crash/disk/permission/restart-budget failure drills, production audit integration, independent risk/security review, and owner-host Windows 11 ACL/start/stop/recovery/unregistration acceptance. This remains an offline Paper-only supervisor and is not permission to install a live or autonomous trading bot.
