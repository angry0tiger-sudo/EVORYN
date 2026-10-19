# EVORYN Technical Debt Ledger

Updated: 2026-10-19  
Canonical repository: `angry0tiger-sudo/EVORYN`

This ledger records open verification and runtime-integration obligations. Close an item only with a command/runner or review record, exact commit/model/data identity, result, and inspectable output. A file or test definition is not execution evidence. A green CI run does not establish production readiness.

## Phase 18 — Connectivity Manager

- [ ] **Verify the original conditional-merge commit directly.** Phase 18 was merged to `main` under the owner's documented conditional waiver while hosted-runner allocation was unavailable. Run #787 subsequently passed the full configured workflow on the later Phase 19 superset candidate, but it did not run on the exact Phase 18 merge SHA `bf111eb06fa13f28279154c2c4b30077a67999ca` alone.
- [ ] **Independent Phase 18 review:** retry/circuit semantics, HTTP redirect behavior, 429 shared cooldown and `Retry-After`, exact host allowlist, time-bounded route authorization, timestamp freshness/reconciliation, and audit durability/fail-closed behavior.
- [ ] **Production wiring review:** confirm the real approved connectors use `ConnectivityManager`, a durable access-controlled `ConnectionAuditSink`, and current route authorization/secret-provider configuration. The availability of unit-level contracts does not prove all intended runtime connectors are wired.
- [x] **Quality-gate coverage on the later Phase 19 candidate:** GitHub Actions [Run #787](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39228082016) passed pytest (218 tests), Ruff, mypy, Bandit, preflight/compileall, and pip-audit on `fad5154e92b9b91319006f64390299b9492a3794`. This is superseding-tree evidence only, not a retroactive claim that the original Phase 18 SHA was run.

### Historical runner issue

- [x] **Hosted runner assignment restored for current workflow runs.** Before the repository became public, several jobs ended in about three seconds with `runner_id=0`, empty `runner_name`, and `steps=[]`; no commands executed. Run #787 now confirms all six jobs execute and pass on a hosted runner.
- [ ] **Historical cause remains unconfirmed.** Exhausted quota/billing was a hypothesis, not a fact established by the raw Jobs API. Do not record it as a definitive root cause without account-level billing/Actions evidence.

## Phase 19 — Shadow Mode

### Verification status

- [x] **Full CI for the Phase 19 code candidate:** [Run #787](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39228082016), candidate HEAD `fad5154e92b9b91319006f64390299b9492a3794`: 218 pytest tests passed; Ruff, strict mypy, Bandit, preflight/compileall, and pip-audit all passed.
- [x] **Documented verification evidence:** `docs/engineering/phase-19-verification-status.md` records exact Run #787, job IDs/results, historical runner behavior, and remaining release gates.
- [x] **Read-only API/safety boundaries represented in tests:** mock tests exercise candidate/Paper comparison, persistent ledger requirement, model/strategy identity drift, stale/duplicate/gapped inputs, reconciliation, Auto-Halt behavior, risk assessment pass/reject/unavailable, feed timeout, and malformed experiment scope. These tests passed in Run #787.
- [ ] **Independent architecture/risk review:** confirm Shadow cannot invoke order submission, mutate RiskEngine hard limits, clear Auto-Halt, or relabel SHADOW as PAPER telemetry. Inspect active Paper Champion pointer, immutable experiment/data split matching and reconciliation logic.
- [ ] **Provision real registered runtime identities:** actual SHADOW candidate and active PAPER Champion specs from the immutable registry, their matching dataset/schema/evaluation/seed/split identity, and the exact controller instance backed by the same persistent registry/ledger.
- [ ] **Implement and approve production `ShadowRiskAssessmentPort`:** current tests use a mock. Production assessment must have explicit reviewed virtual-state assumptions, remain read-only, and fail closed without altering the canonical Live RiskEngine or Auto-Halt state.
- [ ] **Provision the real read-only Auto-Halt status source** and prove missing/invalid status abstains; current tests inject a callback and do not configure a production source.
- [ ] **Review audit storage deployment:** filesystem/path access control, retention, backup/restore, monitoring, recovery and integrity checks. An fsync-backed local JSONL file/hash chain alone is not protection against a privileged attacker rewriting both data and chain.
- [ ] **Review feed authorization and runtime transport wiring:** approved provider/source, market, interval, entitlement, route policy and operational limits. The implementation wraps bounded REST reads on the existing Nobitex read-only adapter; all production uses of Phase 18 ConnectivityManager remain a separate verification item.
- [ ] **Configure a supervised external scheduler for bounded `poll_once` calls** only after the preceding production gates pass. No daemon/scheduler is bundled or active, and no live market request was issued during this development pass.
- [ ] **Keep real market/execution evidence separate:** no real exchange order was sent; no fills, profitability, Paper-trust or live trading readiness are claimed by the Shadow library/API.
- [ ] **Revalidate all gates on any subsequent source-code change.** The Run #787 pass applies to the code candidate SHA specified above; documentation-only follow-up commits do not modify the Phase 19 Python source/test blobs. The post-documentation head must still be checked against the CI workflow before merging.

## Phase 20 — Intelligence Core / Model Provider Interface

- [x] **ADR 0013 accepted into main.** PR #21 was merged at `836737d955bdcb7d8e70ff76afb58b9fa1ed6704`; its PR CI Run #797 passed all six configured jobs. This adopts the architecture, not a particular LLM vendor or production provider.
- [x] **Provider-neutral contracts implemented on the Phase 20 development branch:** typed request/response and model identity, schema-bound JSON, model/task/data allowlists, deadlines and budgets, sanitized audit events, abstention, the generic compatible-LLM adapter port, and a local-files-only Hugging Face classifier adapter. See `docs/operations/phase-20-model-provider-interface.md`.
- [x] **Run all project CI gates on the exact Phase 20 implementation SHA before merge.** GitHub Actions [Run #39232961404](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39232961404) ran on `e2d00420c2fea424042462e3f85e5e95f4cf2e1c` and all six configured jobs passed: pytest, Ruff, strict mypy, Bandit, preflight/compileall and pip-audit. After the squash merge, [main CI Run #39248339221](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39248339221) also passed on `0e8085c8aaa7f03ab92a9808e716d0715a34a364`. The exact candidate and post-merge main evidence are recorded separately; neither proves production readiness.
- [ ] **Keep OpenRouter and all other hosted LLM providers disabled until a separate review approves one.** Record data classification/transfer permission, retention/training terms, region, structured-output behavior, model immutability, costs, latency, quotas and failure tests. There is no bundled HTTP transport or configured cloud endpoint in this phase.
- [ ] **Review a concrete Hugging Face model and training-data rights independently before enabling weights.** Pin immutable revisions and artifact digests where available. The Phase 16 Financial PhraseBank candidate's training-data-rights flag remains false unless explicit review evidence is recorded.
- [ ] **Implement/validate a production durable `ModelAuditSink` and approved credential/transport integration.** The in-memory audit sink is test-only. Remote execution stays blocked on absent cost ceilings, policy approval, data permission or auditable usage.
- [ ] **Review advisory-only isolation before Shadow integration.** No model/provider may send orders, alter hard risk caps, clear Auto-Halt, mutate live configuration or interpret untrusted input as higher-priority instructions. Phase 19's independent runtime/risk acceptance remains a prerequisite for Shadow integration.

## Phase 21A — Market Capability and Futures Contract Foundation

- [ ] **Wire the typed capability evidence into each runtime adapter only after explicit review.** The initial `CapabilityMatrix` model is a domain foundation, not yet a runtime authorization service; legacy booleans must not be interpreted as per-market Futures permission.
- [ ] **Select no Futures venue until official API, terms/license, account/market eligibility, and jurisdictional/contract constraints are reviewed.** Nobitex Futures and all unselected venue capabilities remain Unknown where evidence is incomplete.
- [ ] **Implement a venue-specific Futures adapter only after ADR and contract evidence.** Required fields include contract identity/size, precision and minimums, funding/settlement applicability, margin inputs, position mode, liquidation data, order lifecycle, idempotency, reconciliation, rate limits and safe timeout handling.
- [ ] **Keep CCXT as adapter-only and dependency-free until a pinned-version PoC passes.** The PoC must prove exact venue/mode semantics, license/SBOM, precision/error mapping, offline contract/failure tests and measurable benefit.
- [ ] **Complete adapter-isolation failure drills.** Prove one plugin failure cannot mutate or block other plugin state, registry entries, per-venue rate budgets, or audit/reconciliation ledgers.
- [ ] **Keep transfer/custody/cross-exchange orchestration restricted.** Atomicity, partial completion, asynchronous settlement, duplicate transfer handling, custody assumptions, and recovery invariants need a separate reviewed architecture.
- [ ] **No live order-writing route, credentials, real transfer, or hard-risk mutation is in scope.** The new Futures data types are not a risk decision or a production authorization.

## Evidence classification

- **Observed pass:** exact command on an identified SHA ran and passed, with inspectable runner/log evidence.
- **Not run:** no valid command execution evidence exists.
- **Blocked:** execution was prevented by an external prerequisite.
- **Unconfirmed:** the available evidence does not establish the claimed cause or conclusion.

No open item may be closed by inference. This ledger does not authorize live trading, provider-policy circumvention, or changes to deterministic risk limits.
