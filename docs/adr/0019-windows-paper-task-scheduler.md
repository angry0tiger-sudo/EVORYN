# ADR 0019: Windows Task Scheduler Boundary for Paper-Only Supervision

- **Status:** Proposed for independent review; installation is not authorized
- **Date:** 2026-10-20
- **Owners:** EVORYN engineering and operations
- **Related phases:** Phase 25/26 runtime supervision
- **Related changes:** `scripts/windows_paper_task.py`, `scripts/windows_paper_task.ps1`, and `docs/operations/windows-paper-task-scheduler.md`
- **Related ADRs:** ADR 0007, ADR 0008, ADR 0010, ADR 0017, ADR 0018

## Context

The `PaperRuntimeScheduler` remains a process-local cooperative cycle loop. The repository now
contains `src/evoryn/runtime/single_instance.py` plus `scripts/windows_paper_task.py` and
`scripts/windows_paper_task.ps1`. The runner accepts only a strict offline CSV config, has an explicit
no-Champion abstaining classifier, uses the Mock/Paper runtime, and never opens an exchange
connection. The PowerShell helper registers, reports, and unregisters a limited-privilege,
current-user logon task with `IgnoreNew` instance handling and bounded restarts. The Windows CI
integration test uses a disposable `EVORYN-CI-PaperOnly-*` task and cleans it up after validation.

No challenger in the current Walk-forward comparison passed the empirical classification and
economic gates. Therefore Windows scheduling must not load a new research artifact as a runtime
model.

## Decision

1. The OS task launches `scripts/windows_paper_task.py run --config <local-config>` under the
   current interactive user with Limited privilege. It accepts only local CSV input and a fixed
   BTC-USDT Paper market; it must not enable Spot writes, load exchange credentials, or load a model.
2. Before opening the journal, the entrypoint must acquire a system-wide single-instance lock.
   A duplicate process exits without touching the database. Task Scheduler's “Do not start a new
   instance” option is a second defence, not a replacement for an application lock.
3. Startup must stop with a non-zero status on missing configuration, untrusted model pins,
   invalid/stale/gapped data, journal/hash-chain errors, unresolved order state, Risk Engine HALT,
   failed reconciliation, disk/access/logging failures, or unknown runtime mode. It must not
   clear HALT or resubmit an uncertain order after restart.
4. Restarts must be bounded and backed off. Repeated failures produce an operator-visible fault
   instead of an endless restart loop.
5. Registration must use a least-privilege user context without administrator rights or embedded
   passwords. Logs may record revision, schema, model fingerprint, dataset identity, configuration
   hash and status codes, never secrets.
6. **Current implementation status:** code for user-level Task Scheduler registration/status/
   unregistration, local CSV validation, no-Champion runtime composition, rotating logs, credential-
   environment scrubbing, graceful stop signalling, bounded restarts, and process/journal locks is
   implemented on the PR branch. Checkpoint [CI Run #1018](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39486110714) passed all seven jobs on SHA `7eaaaacd2f2c38d91e0bd2cb87352410e08cb648`; the Windows job parsed the PowerShell helper and passed the disposable task lifecycle tests (12 passed, 1 POSIX-only skip). Later documentation/workflow commits require another exact-final-HEAD CI run before merge.
   The owner Windows 11 host has not been accessed and no user-machine task was registered. Native
   account ACL, restart/fault, and owner-host acceptance remain open.
7. Model promotion and Task Scheduler registration are independent release gates. A scheduled
   Paper task does not authorize a model, exchange connection or live order route.

## Alternatives considered

1. **Treat the in-process scheduler as a Windows Service:** rejected. It has no OS installation,
   service lifecycle or system-wide locking.
2. **Install a task pointing at the current research artifact:** rejected. Research artifacts are
   not approved runtime packages and no candidate passed the gate.
3. **Allow restart to clear HALT:** rejected. Restart cannot resolve data, order, account or
   journal uncertainty.

## Consequences

### Positive
- The boundary between in-process scheduling and OS supervision is explicit.
- Single-instance, failure, recovery and privilege requirements are testable.
- The default remains Paper/Mock with no live order authority.

### Negative / trade-offs
- Unattended Windows runtime remains blocked until exact-HEAD Windows CI and owner-host acceptance
  are recorded. The implemented profile is offline Paper supervision only, not a live feed or bot.
- Native Task Scheduler behavior cannot be certified from Linux-hosted CI alone.

## Security and safety impact

- No credentials are added to task definitions.
- An unattended Paper task must use a simulation-only/offline feed until separate authorization.
- Only Mock/Paper brokers are allowed during qualification.
- HALT remains terminal until an operator-reviewed reconciliation record permits recovery.

## Validation and evidence

- Windows-hosted tests for lock contention, PowerShell parser validity, and disposable task
  register/status/unregister. Unit tests cover strict schema, fresh/stale/gapped CSV, UNC paths, and
  the no-Champion/disabled-live report. Exact final-head results remain to be verified.
- Crash injection at every durable journal transaction boundary.
- Native Windows 11 start/stop/restart/uninstall acceptance by the owner.
- Evidence that the launched process cannot enable or invoke an order-writing path.
- Independent risk/security review.

## Rollback / supersession

Remove only the fixed `EVORYN-PaperOnly` task using the provided unregistration action, then remove
its config/data/logs after retaining any evidence that must be preserved. This task uses only the
Mock/Paper runtime and does not change production trading permissions.
