# EVORYN Technical Debt Ledger

Updated: 2026-10-19  
Canonical repository: `angry0tiger-sudo/EVORYN`

This ledger records unclosed verification and integration obligations. An item can be closed only with a recorded command/runner, exact commit SHA, exit status, and reviewable output or artifact. Do not mark an item complete merely because a test exists or because a conditional merge was accepted.

## Phase 18 — Connectivity Manager

- [ ] **Run the complete GitHub Actions quality workflow** on the merged Phase 18 commit. Current runs `39212965648` and `39212970490` failed before any step ran: six jobs each reported `runner_id=0`, empty runner name and `steps=[]`. Investigate repository/account Actions availability and billing/minute allowance; quota exhaustion is suspected but not proven.
- [ ] **Run the complete canonical pytest suite** from a complete checkout on Python 3.12. The nine local Phase 18 smoke cases are supplemental evidence only; they are not the full suite.
- [ ] **Run Ruff**: `ruff check .` on the same commit.
- [ ] **Run strict mypy**: `mypy src tests` on the same commit.
- [ ] **Run Bandit**: `bandit -q -r src` on the same commit.
- [ ] **Run pip-audit** after installing the canonical dependencies from `pyproject.toml`.
- [ ] **Independent Phase 18 review**: check retry/circuit semantics, 429 shared cooldown, HTTP redirect behavior, hostname allowlist and route-authorization evidence, audit durability/fail-closed behavior, timestamp freshness and recovery behavior.
- [ ] **Runtime integration review**: verify that actual permitted data connectors use `ConnectivityManager` and a durable, access-controlled `ConnectionAuditSink`; confirm route approvals and secrets-provider integration in the intended runtime. Unit-level abstractions alone do not prove runtime wiring.

## Phase 19 — Shadow Mode

- [ ] Validate the read-only source/feed provider and exact permission scope before enabling any external data flow.
- [ ] Verify that SHADOW event schemas preserve mode, feed provenance, model/strategy identity, timestamps, correlation ID and risk-gate outcomes.
- [ ] Verify that SHADOW intent logging has no route to exchange order submission and that credentials capable of exchange writes are absent.
- [ ] Run mock tests for disconnect/stale feed, repeated or out-of-order events, model identity mismatch, timeout, rejection/abstention, Auto-Halt, and duplicate event handling.
- [ ] Run full lint/type/security/pytest gates for the Phase 19 candidate when a runner is available.
- [ ] Obtain independent risk and architecture review before declaring Phase 19 accepted.

## Evidence classification

- **Observed pass:** exact stated command passed in a recorded environment.
- **Not run:** no command execution evidence exists.
- **Blocked:** execution was prevented by an external prerequisite.
- **Unconfirmed:** available data does not establish a root cause or conclusion.

No outstanding item may be closed by inference. This ledger does not authorize live trading, provider-policy circumvention, or changes to deterministic risk limits.
