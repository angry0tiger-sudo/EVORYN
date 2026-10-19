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

## Phase 19 — Shadow Mode runtime and verification debt

- [ ] **Run the complete Phase 19 tests and all project quality gates** on one exact commit: full pytest, Ruff, strict mypy, Bandit, and pip-audit. Source/test files now exist, but no Phase 19 test execution or green CI result is claimed.
- [ ] **Restore a real GitHub Actions runner assignment**. Latest confirmed runs are #683 / ID `39219341686` (push) and #684 / ID `39219348790` (pull request), both on evidence-documentation snapshot HEAD `cff91e9cda982c4032c7462db35d44289ac58203`; the Phase 19 Python source/test blobs are unchanged. Earlier runs #681 / ID `39219317469` and #682 / ID `39219324682` failed identically on documentation snapshot `a3f591a02895868772746088ae607293c8efc1f3`, after the source-snapshot runs #679 / ID `39219231822` and #680 / ID `39219239566` on `2ac41e6b10942a3f78f2f1a84610dabf8a0b3669`. All six jobs in the repeated runs concluded `failure` within approximately three seconds with `runner_id=0`, empty `runner_name`, and `steps=[]`; job logs returned `BlobNotFound`. None of the configured commands ran. Root cause at the job-execution level is confirmed as runner non-allocation; exhaustion of the hosted-runner quota remains a hypothesis, not proven by this evidence. Owner/operator must review Actions availability, usage quota/billing, concurrency and policy settings.
- [ ] **Supply the actual registered active Paper Champion and a compatible SHADOW experiment** from immutable registry artifacts. The runner now requires the same registry/ledger and a live `ChampionChallengerController.active_champion_id`; no production champion ID or approved runtime spec was present in this continuation.
- [ ] **Validate the real data/experiment provenance contract**. Candidate and Paper Champion must have matching dataset version, feature schema, evaluation fingerprint, seed, and split boundaries; the live source/market/interval contract is separately recorded as a feed manifest. Obtain independent review that this split reflects the real registered experiment.
- [ ] **Wire Phase 18 ConnectivityManager into the approved runtime path or record a reviewed exception**. The Phase 19 feed wraps the existing bounded read-only Nobitex adapter; generic Phase 18 manager use by production connectors remains unverified.
- [ ] **Provision and review persistent audit storage** for the Learning Lab JSONL file: access control, backup/restore, storage integrity, retention and operational alerting. A local fsync-backed file and hash chain are not a complete defense against a privileged attacker rewriting both data and chain.
- [ ] **Provision an approved read-only Auto-Halt status source** and prove runtime failure/unknown values abstain. The test API uses injected status callbacks; no real runtime provider was configured.
- [ ] **Implement and independently approve the production `ShadowRiskAssessmentPort` adapter** using explicit virtual-state assumptions and reviewed risk policies without mutating canonical Live RiskEngine or Auto-Halt state. Phase 19 tests use only a mock assessor. A target must remain abstain/reject when risk assessment is unavailable, malformed, rejecting, halting or abstaining.
- [ ] **Ensure Paper Champion pointer freshness is single-owner and current across the runtime**. The runner binds to one controller instance and rechecks its in-memory active pointer for each candle; multi-process controller state refresh/coordination is not implemented. Use one canonical process or add reviewed ledger refresh/locking before distributed runtime deployment.
- [ ] **Configure an external scheduler and health supervision** for bounded `poll_once` calls. No continuous polling daemon, WebSocket client, live market request, or production schedule is included or was run in this PR.
- [ ] **Independent Phase 19 architecture/risk review**: inspect Paper Champion pointer drift, strategy/regime identity and parameter drift, data freshness/gap recovery, baseline comparison, no-order boundary, and event ledger failure behavior.
- [ ] **No performance or fill claims** until a separately reviewed fill/cost model and actual governed observations exist. This phase does not calculate counterfactual fills or establish profitability.

## Phase 20 — Scope discovery gate

- [ ] **Obtain and register the canonical Phase 20 scope before implementation.** Repository tree/code searches for `Phase 20`, `roadmap`, and `page 6` returned no matching roadmap/specification artifact. The current owner handover provides detailed acceptance requirements for Phases 18 and 19, but no Phase 20 objectives, interfaces, safety gates or acceptance criteria. Do not invent a trading, AI, connectivity or execution feature to fill that gap. Once the canonical scope is supplied or committed, record an ADR/acceptance plan and continue on a separate phase-specific branch.

## Evidence classification

- **Observed pass:** exact stated command passed in a recorded environment.
- **Not run:** no command execution evidence exists.
- **Blocked:** execution was prevented by an external prerequisite.
- **Unconfirmed:** available data does not establish a root cause or conclusion.

No outstanding item may be closed by inference. This ledger does not authorize live trading, provider-policy circumvention, or changes to deterministic risk limits.
