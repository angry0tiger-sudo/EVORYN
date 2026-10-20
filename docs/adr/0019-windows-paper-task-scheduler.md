# ADR 0019: Windows Task Scheduler Boundary for Paper-Only Supervision

- **Status:** Proposed for independent review; installation is not authorized
- **Date:** 2026-10-20
- **Owners:** EVORYN engineering and operations
- **Related phases:** Phase 25/26 runtime supervision
- **Related changes:** `docs/operations/windows-paper-scheduler-design.md`
- **Related ADRs:** ADR 0007, ADR 0008, ADR 0010, ADR 0017, ADR 0018

## Context

The existing `PaperRuntimeScheduler` is a process-local cooperative cycle loop. The repository now
also contains `src/evoryn/runtime/single_instance.py`, which provides process-wide Paper locking
(including a Windows named mutex and exclusive journal lock) with a Windows-hosted CI test path.
This does not register an OS task or prove that a complete unattended Paper composition root exists.
Linux GitHub Actions cannot establish native Windows Task Scheduler behavior.

No challenger in the current Walk-forward comparison passed the empirical classification and
economic gates. Therefore Windows scheduling must not load a new research artifact as a runtime
model.

## Decision

1. A future OS task may launch a dedicated Paper-only entrypoint under a least-privilege account.
   It must not have exchange-writing credentials or enable any Spot-write/Live acknowledgement.
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
6. **Current implementation status:** single-instance locking code and a Windows CI test job exist;
   the exact PR-head Windows CI result must be green before the lock gate is closed. Task Scheduler
   registration/unregistration, a dedicated unattended Paper composition root, launcher failure-path
   tests, crash/recovery drills and owner-host acceptance remain open. No native owner Windows host
   was accessed and no task was registered.
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
- Unattended Windows runtime remains blocked until the composition root, host-level lock and
  owner acceptance are implemented and verified.
- Native Task Scheduler behavior cannot be certified from Linux-hosted CI alone.

## Security and safety impact

- No credentials are added to task definitions.
- An unattended Paper task must use a simulation-only/offline feed until separate authorization.
- Only Mock/Paper brokers are allowed during qualification.
- HALT remains terminal until an operator-reviewed reconciliation record permits recovery.

## Validation and evidence

- Windows-hosted tests for process-wide lock contention and exclusive journal locking; additional
  launcher/task-registration and fail-closed startup tests remain required.
- Crash injection at every durable journal transaction boundary.
- Native Windows 11 start/stop/restart/uninstall acceptance by the owner.
- Evidence that the launched process cannot enable or invoke an order-writing path.
- Independent risk/security review.

## Rollback / supersession

Remove any unaccepted task definition and launcher. The process-local scheduler remains available
for supervised tests; no production trading permission changes.
