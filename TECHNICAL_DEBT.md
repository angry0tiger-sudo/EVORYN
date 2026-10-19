# EVORYN Technical Debt Ledger

Updated: 2026-10-20 (Asia/Baku)  
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
- [ ] **Configure production service supervision after acceptance.** Phase 24 adds a process-local PaperRuntimeScheduler for serial bounded cycles, stop requests and terminal HALT; it is not an OS scheduler/service, does not create a feed, and is not active by default. A production poll_once source, shared durable audit/registry wiring and service-specific startup/restart policy remain unproven.
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

## Phase 23 — End-to-End Mock/Paper Runtime

- [x] **Mock-only strategy runtime wired.** A bounded cycle connects completed candle input, the actual abstention/freshness classifier interface, regime-driven hysteretic selector, long/flat strategy target, deterministic Risk Engine, Paper Broker, separate Mock Spot order/balance ledger, and order reconciliation.
- [x] **Account/portfolio and protective exit path.** Same-cycle snapshots report cash, base inventory, marked equity/exposure, reserved funds, active orders and unknown orders. Adverse-regime and drawdown exits become ordinary intents that still pass through Risk Engine.
- [x] **Durable Paper checkpoint and fail-closed recovery.** SQLite writes transactional, sequence-checked/hash-chained events. Clean restart restores the committed broker/order/fill state, rebuilds the separate mock ledger and reconciles it; interrupted intents and prior halts stop without automatic resubmission.
- [x] **Offline integration coverage added.** Synthetic fixtures cover regime detector → selector → fill, active-order reservations, protective exit, clean restart, supervisor restart into halt, interrupted-intent no-resubmit, the mock-only feed boundary and ledger-tamper detection. The fixture probability model is not evidence of a trained or profitable strategy model.
- [ ] **Independent architecture/risk/security review and restart/fault drills remain open.** The runtime has no network route and no production exchange transport, OS service manager, actual Nobitex order/fill reconciliation, production account source or production risk assessor. This phase does not authorize Live, and must not enable any Spot write flag.
- [x] **Exact-head Phase 23 evidence retained.** PR #29 was squash-merged on 2026-10-19 as a2f27a8cd1fcf6f2cee90a21d9823bbd3e80e102; post-merge main CI Run #904 passed all six jobs on that SHA, with 356 passed in 1.43s. Production/live readiness remains unproven.

## Phase 24 — Historical Regime Research and Process-Local Scheduler

- [x] **Bounded public-history transport and parser implemented in the review branch.** Fixed HTTPS host/path, GET-only requests, no Authorization header or exchange credential, no proxy inheritance, redirect rejection, bounded 500-candle pagination, time/response limits, strict OHLC validation, gap checks and sanitized errors are included. The client has offline tests for parsing, malformed payload rejection, fixed-host URL construction and mocked pagination.
- [x] **Research pipeline, inference adapter and reproducible outputs verified.** A versioned Random Forest is fit with training-only forward-return/volatility weak-label thresholds; the chronology is partitioned 60/20/20, temperature calibration uses validation only, and final holdout metrics plus three backtests are persisted. The exact artifact and its checksums are available through Research Run #3 and the report above.
- [x] **Process-local Paper scheduler implemented in the review branch.** It serializes bounded cycles, supports cooperative stop/cycle limits and exits on HALT. It is not an OS-level service and is not enabled automatically.
- [x] **Latest exact-code-head CI and public-history research.** Standard six-gate CI [Run #39328332810](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39328332810) passed all six jobs on `f9f634553f8c6edb2b173b7b885ad2d9cf34330c` (379 pytest passed, one optional-ML skip). On that same code SHA, [Research Run #4](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39328332540) passed the full optional-ML suite (380 tests), the research dependency audit and real public-history fetch/train/backtest/artifact steps. Dataset version `c1a13b3533f3777fa743a8fd6632d73b1760b355341fa1537b7a5ef6ab24a616`; 4,320 gap-free 1-hour BTC-USDT candles over 9 pages; candle SHA-256 `0d976abd6772fc8dce0578b55d69c9686aabe7ffa0a9329cc46c600349297d84`; dataset-manifest SHA-256 `6054e2276864a69497a57f7953ab66b43b9f04bb192a3466303d9fa9d5905651`; model SHA-256 `38799b767b2aa6c2910c07383c3e213db090d5b8d5c1a1e401ee963173c17f2c`. Post-save dataset reload/checks and independently pinned model/manifest loader tests passed. Artifact: [Run #4 artifact #12223262662](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39328332540/artifacts/12223262662). Historical Run #3 on `ba4bab8e46414fd8506b167f6ef8430d2d23abbb` remains available at [Run #3](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39326092277) and [artifact #12222590242](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39326092277/artifacts/12222590242); those older dataset/metrics are not the current report.
- [ ] **Rights and economic assumptions remain unreviewed.** Nobitex data use/commercial rights are not certified. UDF volume is treated as base-asset volume for fills, but provider semantics must be confirmed; OHLCV does not reveal actual historical spread/depth. Spread/slippage remain explicit estimates.
- [x] **Model-quality decision recorded, not promoted.** The latest exact-head 858-sample holdout scored 33.92% accuracy, 25.59% balanced accuracy, 24.66% macro-F1, Brier 0.72879 and log loss 1.34498. The train-only majority-class reference scored 58.51% accuracy and 25.00% balanced accuracy. Same-cost holdout backtests returned -0.2411% for trained-regime long/flat, -1.3562% for SMA crossover and +1.4810% for buy-and-hold. The candidate fails the predictive-quality gate and remains research-only; the live-write flag stays disabled.
- [ ] **Live integration and local release remain out of scope.** The trained model is not wired into a production exchange runtime by default. No OS service manager, actual live account/fill reconciliation, real orders, owner-host acceptance, independent model-risk review or profitability claim is provided.

## Local deployment bundle

- [x] **Bootstrap behavior reviewed and hardened on PR #27 candidate.** The scripts require Python 3.12, preserve an existing `.env`, use the project-local interpreter for dependency install/compile/tests, and fail on setup/test errors. Linux setup strictly applies permissions to a newly created `.env` and the `data` directory. The exact code/test candidate `787bc5186628389f23c43809ba6eb00cd25e3091` passed all six CI gates in Runs #879/#880, including shell syntax validation; no setup script performs an exchange request or starts a trading process.
- [x] **Secret template verified.** `.env.example` contains no real credentials and keeps read-only and future Spot-write credentials blank. The Git tree contains `.env.example` but no `.env`; `.gitignore` explicitly ignores `.env`.
- [ ] **Native setup execution remains unverified.** GitHub CI runs the application tests and a shell syntax check; it does not execute `setup.bat` on Windows or prove successful installation on the owner's host. Native Windows/Linux setup and its exit output remain local acceptance items.
- [ ] **Autonomous live runtime remains unavailable.** Phase 23 provides end-to-end behaviors in Mock/Paper, and Phase 24 introduces a process-local cadence scheduler. Neither establishes a production main process connected to a verified live feed, provider-sourced account/portfolio state, real fill/open-order reconciliation, OS-level supervision, or production recovery policy. The bootstrap does not launch a trading process.
- [ ] **Local-only acceptance remains pending.** Run the setup script on the owner's target host and review output. Any separately authorized read-only API validation must occur locally after verifying the venue token permissions. No live API test is part of GitHub CI.

## Gated Nobitex Spot Execution — local validation required

- [x] **Gated Spot candidate merged as code, not as a live release.** PR #27 merged to main as ccbff726200dce5c37a21eba54131dfe5cc31299. Exact-head PR Runs #881/#882 passed all six jobs on candidate SHA 8037b17dc8b53855d47484cfbd12c41348191c1d; post-merge main Run #883 passed all six jobs on the merge SHA. These checks are offline/mock-only; they do not authorize the live order-write gate.
- [ ] **Independent review and release remain open.** The separate signed Spot gateway and application service are not an operational release. The first scope is Limit-only; Market orders remain rejected until request and price-protection semantics are verified.
- [ ] **Default-off invariant.** Writes require `EVORYN_SPOT_ORDER_WRITES_ENABLED=True`, the explicit acknowledgement `EVORYN_LIVE_TRADING_ACK=I_UNDERSTAND_ORDERS_ARE_REAL`, and locally injected Nobitex Ed25519 API/private keys. The legacy `ENABLE_REAL_TRADING` remains false.
- [ ] **Provider permission is external.** The selected Nobitex API key must have only the permissions required for the Spot flow, including READ/TRADE as applicable, and no withdrawal permission. The code cannot prove remote account permissions.
- [ ] **No live validation during development.** CI tests use fake transports and keys generated in memory. User-directed account/API validation is deferred until the full source is transferred to the local system.
- [ ] **Durable unknown-result stop.** SQLite claims the client order ID before network I/O. Timeout or ambiguous submit/cancel result latches Risk Engine halt and leaves a blocking unknown state. No write is retried automatically.
- [ ] **Single-active-order baseline for the live Spot candidate.** New live submissions are blocked while a previous order is submitting, unknown, pending, open, or partially filled. Phase 23's builder/reconciliation is Paper-only; the live gate remains until a production portfolio builder reconciles durable journal state with provider-sourced account balances, open orders and actual fills.
- [ ] **Phase 18 separation.** Only idempotent reads may be wrapped by bounded retry. Signed POST submit/cancel/status operations are not passed through automatic retry orchestration.
- [ ] **Local integration and release gate remain open.** Windows/Linux packaging, secret-store integration, process supervision, backup/recovery, account-state builder, order/fill reconciliation, protective exit policy, and local verification must precede a production-ready claim. This candidate supports Limit orders only; Market orders stay rejected.

## Evidence classification

- **Observed pass:** exact command on an identified SHA ran and passed, with inspectable runner/log evidence.
- **Not run:** no valid command execution evidence exists.
- **Blocked:** execution was prevented by an external prerequisite.
- **Unconfirmed:** the available evidence does not establish the claimed cause or conclusion.

No open item may be closed by inference. This ledger does not authorize live trading, provider-policy circumvention, or changes to deterministic risk limits.
