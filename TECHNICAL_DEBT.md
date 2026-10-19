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

## Phase 21A — Market Capability Contract Foundation

- [x] **Merged and verified.** PR #23 squash-merged as `1a7871dad08a12565bf63edcd8c21af8794da816`; exact merge-SHA CI [Run #39256040848](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39256040848) passed all six jobs.
- [ ] **Runtime capability authorization is still not wired end-to-end.** `CapabilityMatrix` remains a domain contract; this does not grant Futures support or production readiness.
- [x] **Owner scope recorded:** EVORYN trading market scope is Nobitex only; currently enabled quote currency is USDT. Nobitex Futures remains Unknown and is not being pursued.

## Phase 21B — Nobitex Margin Conformance and Pool-Aware Risk

- [x] **Pool evidence is required for an offline new-risk assessment.** PR #24 merged as `a535d14ff60e64530aa50ee441ab06dd9e7a0cb2`; pool snapshots require fresh, valid, matching source-currency evidence.
- [x] **Side-specific delegation caps are tested.** BUY uses destination-currency notional; SELL uses source-currency amount. Equality is allowed and excess is rejected.
- [x] **Position health cannot authorize new exposure.** The model rejects `new_risk_permitted=True`; a separate open-intent evaluation is required.
- [x] **Exact-head CI passed.** PR CI [Run #39258330307](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39258330307) passed all six configured jobs with 286 pytest tests.
- [ ] **Account Margin-wallet collateral sufficiency and a production authenticated Margin data runtime remain unimplemented.** The pure opening-intent evaluation does not authorize an exchange order.

## Phase 22 — Nobitex Read-Only Account Data and USDT-Only Market Gate

- [ ] **Read-only wallet reader:** implement isolated Spot/Margin USDT wallet snapshots through `GET /v2/wallets?currencies=usdt&type=spot|margin`; no profile, banking, transfer, withdrawal, or order endpoint is used by the new account reader.
- [ ] **Provider-side token permission remains an external deployment prerequisite.** Code accepts only `NOBITEX_READONLY_API_TOKEN`; it cannot independently inspect or prove the account's server-side permissions. Do not deploy until the supplied token is verified in Nobitex account/API-key settings to have no trade or withdrawal permissions.
- [ ] **Hard whitelist:** configuration and the Risk Engine reject markets outside Nobitex Spot/USDT. The supported quote-currency ceiling is fixed in source to USDT; unsupported currencies in environment config fail at startup. Margin's separate policy also rejects non-USDT quote currency.
- [ ] **Transport-level defense:** constrain market-statistics, candle, order-book, trade-data, and wallet reads to USDT quote/wallet requests, so adapter bypass does not silently broaden the read scope.
- [ ] **Exact-SHA evidence required:** unit tests, Ruff, strict mypy, Bandit, preflight/compileall and pip-audit. Tests use mock transports; no live account call is issued in CI.
- [ ] **Live API smoke test is intentionally unrun.** No read-only token is available inside this engineering session. Do not claim live-data connectivity until a separately supervised, read-only smoke test runs with a provider-confirmed restricted token; never ask for that token in chat or put it in source control.
- [ ] **No trade/write route is enabled.** Live order submission/cancel, withdrawal, transfer and collateral mutation remain unavailable.

## Evidence classification

- **Observed pass:** exact command on an identified SHA ran and passed, with inspectable runner/log evidence.
- **Not run:** no valid command execution evidence exists.
- **Blocked:** execution was prevented by an external prerequisite.
- **Unconfirmed:** the available evidence does not establish the claimed cause or conclusion.

No open item may be closed by inference. This ledger does not authorize live trading, provider-policy circumvention, or changes to deterministic risk limits.
