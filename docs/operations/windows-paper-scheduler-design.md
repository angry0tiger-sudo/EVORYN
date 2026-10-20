# Windows Paper Runtime Supervision Design

**Status:** Task Scheduler provisioning and the unattended composition root remain design-only. The repository now includes a process-wide single-instance locking primitive with a Windows CI test job. No installer was run, no Task Scheduler entry was registered, and no owner Windows host was accessed.

## Decision

Use Windows Task Scheduler for the initial owner-hosted Paper-only deployment, not a claimed Windows Service. The current PaperRuntimeScheduler remains the in-process one-cycle-at-a-time loop. A reviewed launcher/composition root is a prerequisite; no safe unattended command is available until the runtime source and dependency graph are accepted.

## Required composition root

Before implementation is enabled, provide and review:

- A completed-candle source with timestamp, staleness, duplicate, future-candle and gap rules.
- A Paper-only dependency graph: model loader pinned to independently trusted artifact and manifest digests, regime detector, selector, intent builder, deterministic Risk Engine, Mock/Paper broker, reconciler and durable SQLite journal.
- An explicit paper data-source ID and deterministic Paper account seed.
- The existing `src/evoryn/runtime/single_instance.py` process-wide named mutex and exclusive journal lock; duplicate instances must exit without mutating the ledger. Exact-PR Windows CI evidence is still required.
- Sanitized rotating logs, a health state, shutdown signal handling and nonzero exit codes for data integrity, journal, reconciliation or HALT failures.
- No Live exchange connector or write-capable credential in the Paper task environment. Feed/model faults must never fall back to Live.

## Task Scheduler profile after acceptance

1. Run as a dedicated least-privilege Windows account. Do not grant administrator rights; permit writes only to Paper data, journal and log directories.
2. Trigger at system startup with bounded delay and preflight checks for storage availability.
3. Set “If the task is already running” to “Do not start a new instance”, backed by an application mutex and exclusive SQLite journal lock.
4. Use bounded restart attempts and backoff. After repeated failures, stop retrying and raise a health alert; never clear a latched HALT by restart.
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

- Single-instance race and locked-journal tests.
- Clean startup and restore from a valid persisted Paper state.
- Restart after clean shutdown and crash injection at every journal transaction boundary.
- Stale, duplicate, gapped, future and malformed candle tests.
- Artifact/manifest digest mismatch and feature-schema mismatch drills.
- Disk-full, access-denied, journal corruption, logging failure and restart-budget exhaustion.
- Stop, terminal HALT, unresolved intent and no-resubmit tests.
- Evidence that the task cannot load Live credentials, call write endpoints or enable Spot writes.
- Owner-host Windows 11 validation of permissions, startup, logs, recovery, registration and uninstallation.

**Current gate:** dedicated Paper-only composition root/launcher, safe offline or explicitly approved Paper feed contract, Task Scheduler registration/unregistration scripts, launcher failure-path tests, production account/fill reconciliation, durable audit wiring, independent risk/security review and owner-host acceptance remain open. This is a design document, not permission to install or start an autonomous bot.
