# EVORYN Technical Debt Ledger

Updated: 2026-10-19  
Canonical repository: `angry0tiger-sudo/EVORYN`

This ledger records open verification and runtime-integration obligations. Close an item only with a command/runner or review record, exact commit/model/data identity, result, and inspectable output. A file or test definition is not execution evidence. A green CI run does not establish production readiness.

## Phase 18 — Connectivity Manager

- [ ] **Verify the original conditional-merge commit directly.** Phase 18 was merged to `main` under the owner's documented conditional waiver while hosted-runner allocation was unavailable. Later superseding-tree CI evidence does not retroactively prove that the exact Phase 18 merge SHA `bf111eb06fa13f28279154c2c4b30077a67999ca` was tested on its own.
- [x] **Latest code/test candidate CI evidence, including the direct transport timeout regression.** GitHub Actions [Run #879](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39312972444) and [PR Run #880](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39312976737) passed all six configured jobs on code/test candidate SHA `787bc5186628389f23c43809ba6eb00cd25e3091`; pytest reported 347 passed. All 13 explicit tests in `tests/unit/test_phase18_connectivity.py` passed, including bounded GET retry, HTTP 429/Retry-After, no retry on auth errors, circuit recovery, audit failure, host allowlisting, freshness/reconciliation, and a fake-opener test proving the HTTPS transport passes its configured timeout, sanitizes a socket timeout, and makes one underlying attempt. This is mock-only CI evidence, not live-network evidence, and not a retroactive pass for the original Phase 18 merge SHA.
- [x] **Initial candidate evidence retained as history.** Runs #873/#874 passed 346 tests on `1d435a4b0df40f2d38e8e43515aded1aa1d3a3b0`; the later candidate adds the direct transport-timeout regression test.
- [ ] **Independent Phase 18 review:** retry/circuit semantics, HTTP redirect behavior, 429 shared cooldown and `Retry-After`, exact host allowlist, time-bounded route authorization, timestamp freshness/reconciliation, and audit durability/fail-closed behavior.
- [ ] **Production wiring review:** confirm the real approved connectors use `ConnectivityManager`, a durable access-controlled `ConnectionAuditSink`, and current route authorization/secret-provider configuration. The reusable implementation and passing tests do not prove the intended runtime connectors are wired.
- [ ] **Local failure drills:** timeout/outage, rate limit, feed gap, audit persistence loss and recovery have not been demonstrated on the owner's actual host.
- [x] **Historical later-candidate quality evidence:** GitHub Actions [Run #787](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39228082016) passed the earlier Phase 19 superset candidate. It remains historical evidence and does not replace the exact-head runs linked above.

### Historical runner issue

- [x] **Hosted runner assignment restored for current workflow runs.** Before the repository became public, several jobs ended in about three seconds with `runner_id=0`, empty `runner_name`, and `steps=[]`; no commands executed. Run #787 and the current candidate Runs #879/#880 confirm all six jobs execute and pass on hosted runners.
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

- [x] **Read-only wallet reader merged in PR #25.** The account reader returns isolated Spot/Margin USDT wallet snapshots through `GET /v2/wallets?currencies=usdt&type=spot|margin`; the reader does not call profile, banking, transfer, withdrawal or order endpoints.
- [ ] **Provider-side token permission remains an external deployment prerequisite.** Code accepts only `NOBITEX_READONLY_API_TOKEN`; it cannot independently inspect or prove the account's server-side permissions. Do not deploy until the supplied token is verified in Nobitex account/API-key settings to have no trade or withdrawal permissions.
- [x] **Hard whitelist merged in PR #25.** Configuration and Risk Engine reject markets outside Nobitex Spot/USDT. The supported quote-currency ceiling is fixed in source to USDT; unsupported environment values cannot widen it. The Margin policy also rejects non-USDT quote currency.
- [x] **Transport-level defense merged in PR #25.** Market-statistics, candle, order-book, trade-data and wallet reads are constrained to USDT quote/wallet requests.
- [x] **Offline verification passed on exact PR #25 head.** SHA `9ea98dbb1d67cdc7c1f9b290557aa17dbdfbb4b0` passed 318 pytest tests and all six configured gates: [CI Run #39263527045](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39263527045). Tests use mock transports; no live account call was issued.
- [ ] **Live API smoke test remains unrun.** PR #26 prepares `scripts/live_readonly_nobitex_usdt.py`, whose dedicated transport permits only the exact USDT wallet GET requests. No read-only token is available in this session. Do not claim live connectivity until a supervised test uses a provider-confirmed restricted token; never ask for that token in chat or source-control it.
- [ ] **No trade/write route is enabled.** Live order submission/cancel, withdrawal, transfer and collateral mutation remain unavailable.

## Local deployment bundle

- [x] **Bootstrap behavior reviewed and hardened on PR #27 candidate.** The scripts require Python 3.12, preserve an existing `.env`, use the project-local interpreter for dependency install/compile/tests, and fail on setup/test errors. Linux setup strictly applies permissions to a newly created `.env` and the `data` directory. The exact code/test candidate `787bc5186628389f23c43809ba6eb00cd25e3091` passed all six CI gates in Runs #879/#880, including shell syntax validation; no setup script performs an exchange request or starts a trading process.
- [x] **Secret template verified.** `.env.example` contains no real credentials and keeps read-only and future Spot-write credentials blank. The Git tree contains `.env.example` but no `.env`; `.gitignore` explicitly ignores `.env`.
- [ ] **Native setup execution remains unverified.** GitHub CI runs the application tests and a shell syntax check; it does not execute `setup.bat` on Windows or prove successful installation on the owner's host. Native Windows/Linux setup and its exit output remain local acceptance items.
- [ ] **Autonomous live runtime remains unavailable.** The repository still lacks a production main/strategy loop, account/portfolio reconciliation builder, fill/open-order synchronization, protective-exit policy and process supervision. The bootstrap is for setup and offline verification; it is not a claim that a full trading robot can be launched today.
- [ ] **Local-only acceptance remains pending.** Run the setup script on the owner's target host and review output. Any separately authorized read-only API validation must occur locally after verifying the venue token permissions. No live API test is part of GitHub CI.

## Gated Nobitex Spot Execution — local validation required

- [x] **Latest code/test candidate passed exact-head CI.** PR [#27](https://github.com/angry0tiger-sudo/EVORYN/pull/27) is open and unmerged. Code/test SHA `787bc5186628389f23c43809ba6eb00cd25e3091` passed Runs #879 and #880 (all six gates; 347 pytest tests). The PR head may advance with documentation-only evidence updates, so recheck the final PR-head checks before merge.
- [ ] **Independent review and release remain open.** The separate signed Spot gateway and application service are not an operational release. The first scope is Limit-only; Market orders remain rejected until request and price-protection semantics are verified.
- [ ] **Default-off invariant.** Writes require `EVORYN_SPOT_ORDER_WRITES_ENABLED=True`, the explicit acknowledgement `EVORYN_LIVE_TRADING_ACK=I_UNDERSTAND_ORDERS_ARE_REAL`, and locally injected Nobitex Ed25519 API/private keys. The legacy `ENABLE_REAL_TRADING` remains false.
- [ ] **Provider permission is external.** The selected Nobitex API key must have only the permissions required for the Spot flow, including READ/TRADE as applicable, and no withdrawal permission. The code cannot prove remote account permissions.
- [ ] **No live validation during development.** CI tests use fake transports and keys generated in memory. User-directed account/API validation is deferred until the full source is transferred to the local system.
- [ ] **Durable unknown-result stop.** SQLite claims the client order ID before network I/O. Timeout or ambiguous submit/cancel result latches Risk Engine halt and leaves a blocking unknown state. No write is retried automatically.
- [ ] **Single-active-order baseline.** New submissions are blocked while a previous order is submitting, unknown, pending, open, or partially filled. This remains until the runtime portfolio/exposure builder incorporates journal state and venue reconciliation.
- [ ] **Phase 18 separation.** Only idempotent reads may be wrapped by bounded retry. Signed POST submit/cancel/status operations are not passed through automatic retry orchestration.
- [ ] **Local integration and release gate remain open.** Windows/Linux packaging, secret-store integration, process supervision, backup/recovery, account-state builder, order/fill reconciliation, protective exit policy, and local verification must precede a production-ready claim. This candidate supports Limit orders only; Market orders stay rejected.

## Evidence classification

- **Observed pass:** exact command on an identified SHA ran and passed, with inspectable runner/log evidence.
- **Not run:** no valid command execution evidence exists.
- **Blocked:** execution was prevented by an external prerequisite.
- **Unconfirmed:** the available evidence does not establish the claimed cause or conclusion.

No open item may be closed by inference. This ledger does not authorize live trading, provider-policy circumvention, or changes to deterministic risk limits.
