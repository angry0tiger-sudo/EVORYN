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
- [ ] **Restore a real GitHub Actions runner assignment**. The previous confirmed pattern was six jobs failing in 2–3 seconds with `runner_id=0`, empty runner name, and zero steps. Account-level cause remains unconfirmed; the owner/operator must review Actions availability, usage quota/billing and policy settings.
- [ ] **Supply the actual registered active Paper Champion and a compatible SHADOW experiment** from immutable registry artifacts. The runner now requires the same registry/ledger and a live `ChampionChallengerController.active_champion_id`; no production champion ID or approved runtime spec was present in this continuation.
- [ ] **Validate the real data/experiment provenance contract**. Candidate and Paper Champion must have matching dataset version, feature schema, evaluation fingerprint, seed, and split boundaries; the live source/market/interval contract is separately recorded as a feed manifest. Obtain independent review that this split reflects the real registered experiment.
- [ ] **Wire Phase 18 ConnectivityManager into the approved runtime path or record a reviewed exception**. The Phase 19 feed wraps the existing bounded read-only Nobitex adapter; generic Phase 18 manager use by production connectors remains unverified.
- [ ] **Provision and review persistent audit storage** for the Learning Lab JSONL file: access control, backup/restore, storage integrity, retention and operational alerting. A local fsync-backed file and hash chain are not a complete defense against a privileged attacker rewriting both data and chain.
- [ ] **Provision an approved read-only Auto-Halt status source** and prove runtime failure/unknown values abstain. The test API uses injected status callbacks; no real runtime provider was configured.
- [ ] **Configure an external scheduler and health supervision** for bounded `poll_once` calls. No continuous polling daemon, WebSocket client, live market request, or production schedule is included or was run in this PR.
- [ ] **Independent Phase 19 architecture/risk review**: inspect Paper Champion pointer drift, strategy/regime identity and parameter drift, data freshness/gap recovery, baseline comparison, no-order boundary, and event ledger failure behavior.
- [ ] **No performance or fill claims** until a separately reviewed fill/cost model and actual governed observations exist. This phase does not calculate counterfactual fills or establish profitability.

## Evidence classification

- **Observed pass:** exact stated command passed in a recorded environment.
- **Not run:** no command execution evidence exists.
- **Blocked:** execution was prevented by an external prerequisite.
- **Unconfirmed:** available data does not establish a root cause or conclusion.

No outstanding item may be closed by inference. This ledger does not authorize live trading, provider-policy circumvention, or changes to deterministic risk limits.
