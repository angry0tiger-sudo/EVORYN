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
- [x] **Research pipeline, inference adapter and reproducible outputs verified.** A versioned Random Forest is fit with training-only forward-return/volatility weak-label thresholds; the chronology is partitioned 60/20/20, temperature calibration uses validation only, and final holdout metrics plus three backtests are persisted. Current artifacts/checksums are retained by Research Run #5 and summarized in the current research report; Runs #3 and #4 are historical only. Successful training is not model acceptance.
- [x] **Process-local Paper scheduler implemented in the review branch.** It serializes bounded cycles, supports cooperative stop/cycle limits and exits on HALT. It is not an OS-level service and is not enabled automatically.
- [x] **Latest exact-code-head CI and public-history research.** On source/test SHA `5639f9ad6a1133be814251db0d958fe4d463d06d`, standard six-gate push CI [Run #39329776104](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39329776104) and PR CI [Run #39329781455](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39329781455) both passed every gate (379 passed, 2 skipped in the dependency-light base environment). Research Run #5 [#39329776038](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39329776038) on that same code/test SHA passed the ML-enabled integration suite (381 passed in 12.74s), research pip-audit (no known vulnerabilities; the local evoryn package is not a published PyPI package and therefore was not audited as one), real public OHLCV download, training, backtesting and artifact publication. Dataset version `5a9e1d165b1a9688a726563a1c06bfa96bde3abf8bd6193fa43664227ff67728`; 4,320 gap-free hourly BTC-USDT Spot candles from 2026-04-22 21:30 to 2026-10-19 20:30 UTC over 9 pages; candle SHA-256 `0d976abd6772fc8dce0578b55d69c9686aabe7ffa0a9329cc46c600349297d84`; dataset-manifest SHA-256 `b769326d0d2b686b2eb91b92910d644e05304a4fc3d8e76d4cddcd7848ddce9b`; training-report SHA-256 `04b750c2a5c9ead108d8c7bd0c78d35babc85f896cddc56a822ec46ccd4ca341`; model SHA-256 `38799b767b2aa6c2910c07383c3e213db090d5b8d5c1a1e401ee963173c17f2c`; model-manifest SHA-256 `6491232c7d0b0f475d24887406f71bd008a32424430c2e50fa60387a01f46157`. Save/reload/checksum verification, independently pinned model/manifest negative tests, and the synthetic fitted-model-through-Mock-Paper-Runtime integration test all passed. Artifact [#12224231419](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39329776038/artifacts/12224231419), SHA-256 `9ae0c56564cf7ac6eca66929ed7d6b491e60e44638fae0070455f84639959224`, expires 2027-01-17. Latest 858-sample holdout: accuracy 33.92%, balanced accuracy 25.59%, macro-F1 24.66%, Brier 0.72879, log loss 1.34498; train-only majority baseline accuracy 58.51%. Backtests under common assumed costs: trained-regime -0.2411% / 0.2411% max drawdown / 1 closed trade; SMA -1.3562% / 3.9004% / 12 trades; buy-and-hold +1.4810% / 2.2600% / 0 closed trades with open position. The model fails the predictive-quality gate and remains research-only. The subsequent documentation-only snapshot `65150ec5937abaad5e130e6f8027d026f76a939f` passed every standard gate on push [Run #39330490769](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39330490769) and PR [Run #39330494963](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39330494963); the next docs-only record update is revalidated separately. Historical Run #3 [#39326092277](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39326092277) and Run #4 [#39328332540](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39328332540) remain as earlier evidence; their dataset identities/metrics are not current.
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


## Phase 25 — Volume Semantics, Walk-Forward Robustness and Windows Paper Supervision

### Verified starting point — 2026-10-20

- [x] **PR #30 merged.** Squash merge commit `db68a4c943a6300bf1be5718e03c99bc321a71dc`; post-merge six-gate CI passed on that exact SHA in [Run #39369939519](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39369939519).
- [x] **Volume ambiguity is represented, not guessed.** The public UDF contract does not certify whether `v` is base-asset or quote-asset volume. The dataset uses `volume_unit="unknown"`; research excludes it and disables volume participation caps unless base units are verified.
- [x] **Original Random Forest remains rejected.** Its final holdout accuracy was 33.92% versus 58.51% for the train-only majority baseline. The cost-adjusted holdout backtest did not establish a profitable edge.
- [x] **Challenger stack and design gates recorded.** ADR 0018 documents Random Forest, HistGradientBoosting, XGBoost, forward-filtered Gaussian HMM and compact CPU LSTM; ADR 0019 records Windows Task Scheduler requirements.
- [ ] **Provider semantics and data rights.** Keep Nobitex UDF volume unknown. Historical data retention/commercial-use terms remain unreviewed.
- [ ] **Windows unattended execution.** At this Phase 25 checkpoint the OS task was not implemented. The Phase 28 changes below supersede that snapshot: an offline CSV runner, Windows Task Scheduler PowerShell helper and cross-process lock implementation now exist on PR #31; exact-head Windows CI and owner-host acceptance remain separate gates.
- [ ] **Production runtime model integration.** Research schema v3 is not accepted by the current production loader. No challenger is connected or promoted.
- [ ] **Independent model-risk review.** Required even if a future challenger passes empirical gates.



## Phase 26 — Challenger exact-head evidence (2026-10-20)

- [x] **Six standard CI gates passed on exact SHA** `c08096a8ff8c28d78a9879977cc5c30fde2d5f77`: [Run #39397941134](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39397941134). ML-enabled suite: 391 passed in 15.26s; dependency audit: no known vulnerabilities in the installed environment.
- [x] **Exact-head research completed** on that same SHA: [Run #39397941187](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39397941187), [artifact #12252633238](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39397941187/artifacts/12252633238), ZIP SHA-256 `5d411b9ef13a0e31b5a602e4d03cbd570c0eada8530bcbc9137a552166724d8e`. Walk-forward report SHA-256 `5900b1c3465ad23b90bce58fcfbe020fbc6b4b8ce295b9d505bf3b0bdb1785a7`.
- [x] **Walk-forward report defect corrected and ZIP audited.** Ten folds are present, each containing both label policies, five model families and fold-level validation/test/backtest metrics.
- [x] **Dataset verified:** 4,319 gap-free hourly BTC-USDT candles, version `275bdf9bb7c5049e24b1a7e17944ec3a09db7e831465da56038fdf8daa766420`; candle SHA-256 `b68b69b0f5dfffbf0e373d97725923a7ba45612dd410b6da93810907ca5c958f`; manifest SHA-256 `ce4bfbf7a78a975e1f0148215e565e781af7772e4d3c942aa1ac4c245a64ec5b`.
- [x] **No model passed the combined gate.** Train-only majority Accuracy averaged 63.062% for forward-threshold labels and 63.445% for Triple-Barrier labels; all five candidates' mean Triple-Barrier accuracies were lower than that policy's baseline. Macro-F1 improved for several candidates, but Accuracy confidence intervals remained negative and return-difference intervals versus Buy-and-Hold spanned zero with negative lower bounds. Report fields: `selected_model_for_runtime=null`, `runtime_promotion_allowed=false`.
- [ ] **Fresh confirmation and independent model-risk review** before any possible future promotion.
- [ ] **Volume semantics and data-use rights** remain unresolved; UDF volume stays unknown and unused, and capacity caps remain disabled.
- [ ] **Windows unattended Paper runtime** remains open as a release gate. The Phase 26 checkpoint predates PR #31's offline runner, task registration helper and no-Champion composition root; see Phase 28 for the current implementation status. No user-machine task has been registered.
- [ ] **Runtime schema integration** remains open. Research schema v3 does not match the production loader contract; no challenger is connected.
- Full metric tables, confidence intervals, portfolio outcomes and digests: [`docs/research/2026-10-20-regime-challenger-walk-forward.md`](docs/research/2026-10-20-regime-challenger-walk-forward.md).


## Phase 27 — Six-model evidence reconciliation and Windows supervision

### Six-model Artifact audit — 2026-10-20

- [x] **Artifact downloaded and ZIP hash recomputed.** Artifact ID `12256082280`; actual downloaded ZIP SHA-256
  `806cc33bcc2b80746e8fa35ae08ca3886c352166bc6287589b5fb0db20475e84`. The digest transcribed
  from the supplied screenshot differs; preserve the computed digest for the downloaded bytes.
- [x] **Embedded report parsed.** Walk-forward report SHA-256
  `702bcbba8fd69ddcf334bf8c8fb1b938d65428336744d96c12202adfb4ede4b8`; schema version 2;
  4,320 hourly BTC-USDT candles, zero gaps, dataset version
  `8aaf2e83e1aa68d8a4ed6deae9057626f3a0556467d7bd99a6f509fc6be2e384`.
- [x] **Fold/model coverage checked.** Eleven unique logical folds, both label policies, and all six models
  have EVALUATED classification outputs in each fold/policy. 91 embedded `.sha256` sidecars were
  checked against their corresponding files; no sidecar mismatch was found.
- [x] **No Champion.** Report records `selected_model_for_runtime=null`. Triple-Barrier mean train-only
  majority accuracy recomputes to 64.3887%; every candidate mean accuracy is lower. Mean net return
  is +0.5551% for Buy-and-Hold, +0.1249% for SMA, and negative or effectively zero for every challenger.
- [ ] **Historical artifact reporting gap.** The schema-v2 artifact has no model backtest entries under
  `forward_thresholds`, despite classification outputs for that policy. It is not sufficient to claim
  economic acceptance across both label policies. Its exact source commit was not independently
  linked to the artifact metadata in this audit.
- [x] **Audit report published.** See [six-model artifact audit](docs/research/2026-10-20-six-model-artifact-audit.md) for recomputed classification/economic summaries, digest discrepancy, and the missing forward-policy backtests.
- [ ] **Fresh schema-v3 exact-head research run.** Validate both-policy backtest coverage, all six models,
  model artifact reloads, per-policy candidate gates, report checksum, and ZIP checksum on the new PR head.
  Do not reuse the schema-v2 artifact as evidence for the newer source.
- [ ] **Windows Task Scheduler remains a release blocker until evidence is complete.** PR #31 now adds
  a strict offline CSV-only runner with a no-Champion classifier, PowerShell register/status/unregister
  helper, current-user Limited privilege, IgnoreNew behavior, bounded restarts, rotating logs, credential-
  environment scrubbing, cooperative stop handling, and an OS-level mutex plus journal lock. Windows CI
  must pass on the exact final PR HEAD. No user-machine task was registered and no local host was accessed.
- [ ] **No live connectivity or order activity.** All work remains GitHub-side; no local Windows action,
  authenticated account request, exchange-write request, or real order was performed.


## Phase 28 — Offline Windows Paper Task implementation (PR #31)

### Implementation status — 2026-10-20

- [x] **Strict offline runner added.** `scripts/windows_paper_task.py` accepts only an exact JSON allow-list,
  `runtime_mode=paper_only`, `simulation_only=true`, BTC-USDT, local CSV, local journal and local log.
  The CSV must have explicit UTC timestamps, contiguous bars, valid OHLC values, and a completed candle
  no more than two configured intervals stale. UNC/network paths, unknown keys and model paths are rejected.
- [x] **No-Champion boundary explicit.** The runner uses `NoChampionClassifier`, whose prediction is
  `UNKNOWN` / `NO_CHAMPION_SELECTED`; no model artifact is loaded and no model is selected for Runtime.
  `selected_model_for_runtime=null` remains the release decision.
- [x] **Task Scheduler helper added.** `scripts/windows_paper_task.ps1` implements user-scoped register,
  status, and unregister operations for the reserved EVORYN Paper namespace. Registration is at current-user
  logon with Limited privilege, no stored password, `MultipleInstances=IgnoreNew`, and a bounded
  three-restart/five-minute retry policy. Existing tasks are not overwritten.
- [x] **Process / journal locking retained and integrated.** `PaperRuntimeLocks` is acquired before
  constructing the SQLite Paper runtime. Duplicate processes exit before journal access. Environment
  variables likely to contain EVORYN/Nobitex/Binance credentials or live-write acknowledgements are removed
  before Paper composition. Logs rotate at 5 MiB with five backups, and SIGINT/SIGTERM requests cooperatively
  stop the in-process scheduler.
- [x] **Configuration template and runbook added.** See `config/windows-paper.example.json` and
  `docs/operations/windows-paper-task-scheduler.md`.
- [x] **Windows CI coverage defined.** `.github/workflows/ci.yml` parses the PowerShell helper and runs lock
  tests plus a native Windows task register/status/unregister round trip using a temporary
  `EVORYN-CI-PaperOnly-*` task; the test attempts cleanup in a `finally` block. Cross-platform unit tests
  validate config, CSV freshness/gaps, UNC rejection and task-name scope.
- [x] **Seven-gate checkpoint passed.** GitHub Actions [Run #1018](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39486110714) passed all seven configured jobs on checkpoint SHA `7eaaaacd2f2c38d91e0bd2cb87352410e08cb648`: pytest, Ruff, strict mypy, Bandit, preflight/compile, pip-audit, and Windows Paper/Task Scheduler. The Windows job parsed the PowerShell helper and reported **12 passed, 1 skipped** (`O_NOFOLLOW` is POSIX-only); the disposable CI task successfully completed register/status/unregister.
- **Merge gate (always enforced):** merge only after all seven required CI jobs report `success` for the current PR HEAD; do not infer latest-head status from checkpoint Run #1018 or an earlier attempt.
- [ ] **Fresh schema-v3 research artifact.** The historical six-model schema-v2 Artifact #12256082280 has
  no model backtest entries under `forward_thresholds`. A new research run on the current source must confirm
  six models, both policy backtests, per-policy candidate gates, model artifact checksums/reloads, and report
  and ZIP digests. That is separate from the seven standard CI gates.
- [ ] **Native owner-host acceptance.** No task has been registered on the user's Windows 11 host. Owner-host
  ACL, logoff/shutdown, restart budget, disk-full/permission/journal-corruption fault drills, log rotation,
  and explicit unregistration acceptance remain open. Linux/Windows CI cannot attest to the actual owner host.
- [x] **No operator-host activity.** All changes were committed through GitHub APIs. No owner-local
  Windows host was accessed, no credential or account/write endpoint was manually used, and no real order
  was placed. The historical public-data research workflow had push triggers on this branch; those triggers
  have now been removed from this branch to prevent subsequent infrastructure pushes from initiating a
  new public-history research job. Any already-triggered automatic run must be distinguished from a manually
  dispatched live session; no new research run was manually dispatched in this phase.


## Phase 29 — Advanced AI Optimization / SMA Meta-Label Research (2026-10-20)

- [x] **Transport diagnostics/retry code implemented and exact-head-verified** in the Phase 29 checkpoint below. The earlier `URLError` root cause remains undetermined; do not claim it was specifically DNS, TLS, timeout, or a provider/network outage.

- [x] **Exact-head seven-gate CI on SHA `4343d637f2bfd6536566a8b2d23a395f37bca433`.** PR CI [Run #39493555023](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39493555023) passed mypy, pytest, Bandit, pip-audit, runner/dependency preflight, Ruff and Windows Paper process/journal locks. This verifies only the exact code SHA and is not research or profitability evidence.
- [x] **Source audit of the validation-selected threshold and pooled test AP on SHA `4343d637f2bfd6536566a8b2d23a395f37bca433`.** `select_meta_label_threshold` receives validation events and probabilities only; the workflow no longer passes `--probability-threshold`; the evaluator stores each fold's selected threshold in model metadata, aggregates predictions from evaluated test events only, and fails classification unless at least 10/10 expected folds are covered, at least 30 pooled test events have both classes, and pooled AP exceeds positive prevalence. The economic gate also requires positive mean net return, at least 20 closed trades and positive lower 95% paired-fold bounds versus all three baselines. This is a source review, not execution evidence for the new experiment.
- [ ] **Independent review/model-risk approval remains open.** The latest empirical result below explicitly failed candidate gates; neither model qualifies.
- [x] **Historical transport incident on prior SHA (not the current HEAD).** Advanced AI workflow [Run #39493546367](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39493546367), on SHA `4343d637f2bfd6536566a8b2d23a395f37bca433`, failed in both attempt 1 and attempt 2 during public history retrieval with `Historical UDF GET failed with URLError`. All research pre-network gates passed in both attempts; the fetch-only step failed and the evaluator was skipped. Artifact #12296199968 has ZIP SHA-256 `412e734e3301e6947ee43a66c6bf3bdec78f6ce9cfe7f3ff1e99739243a7d875`; retry artifact #12296999003 has ZIP SHA-256 `960cb4831ba5bd1c3029bd1cc2e8b975fe40c79a9785a5780a717cc0a9c7a751`. Both contain diagnostics only, without a dataset, manifest, training report, advanced report or models. The wrapped error intentionally omits the underlying URLError reason, so DNS/TLS/timeout cause is not established. These runs are non-evaluable, not model failures or successes; no current-code AP/economic metrics exist.
- [ ] **Extended data provenance:** the proposed 730-day / maximum-40-page request is within nominal UDF response capacity, but actual historical retention, page semantics, data-use terms and continuity must be proved from the fetched immutable dataset.
- [ ] **Feature/model validation:** confirm prefix causality on exact code; audit closed-bar 4h/daily aggregation, label horizons, event purging, feature selection scope, Optuna split discipline, artifact serialization and three paired baseline comparisons.
- [ ] **Liquidity limitations:** UDF volume semantics remain unknown and historical book depth has not been sourced. Features omit volume/order-book, and capacity limits are disabled; do not interpret the backtest as liquidity-realistic.
- [ ] **No promotion by this phase:** selected_model_for_runtime remains null in code and reports. Empirical success alone cannot bypass untouched confirmation, independent model-risk review, loader compatibility, deterministic risk protections, and separate release approval.


### Phase 29 — Latest exact-head transport and empirical checkpoint (2026-10-21)

- [x] **Transport diagnostics and bounded retry verified by successful fetch on exact SHA `292c1544467e1d434b5f39977e8b444ca63d5418`.** The client now classifies DNS, timeout, TLS/certificate, connection and other network failures; logs only sanitized category/type/errno/detail; uses a 20-second timeout, up to 2 retries (3 attempts total), exponential backoff starting at 0.5 seconds; and fails immediately on TLS certificate-verification errors. The research workflow preflight and data-fetch step completed successfully on this SHA. No transport warning was emitted on this successful run, so the root cause of the earlier `URLError` incident remains unconfirmed rather than attributed to DNS, TLS, timeout, Nobitex or GitHub networking.
- [x] **Exact-head CI and research completed on the same SHA.** All seven standard CI jobs passed in [CI Run #39541721607](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39541721607). The advanced research workflow [Run #39541721758](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39541721758) passed dependencies, Ruff, mypy, Bandit, the offline test suite (422 passed, 2 skipped), pip-audit and public history download; walk-forward evaluation and artifact upload completed successfully.
- [x] **Immutable data and report integrity verified.** Artifact #12320006168, [run artifact](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39541721758), ZIP SHA-256 `dfda0241534d3b55df5bdeae1143b573baccfc536c4834ebb6040306a0b4c8ef`. Dataset `nobitex-btc-usdt-60m`, version `c9f86ce7df23ea3e3c09eaa8e6e8a874b3c3943c52b68b8be0f3ba00f609dfe8`: 11,438 hourly candles, 23 pages, zero gaps, from `2025-07-01T13:30:00Z` to `2026-10-21T02:30:00Z` (~477 days despite requesting 730). Candle CSV SHA-256 `58bf4c5b63333f72ec3d20197a4f9258a5aa24dd55b36503f570cf3afba2f011`; manifest SHA-256 `f2552331d1763d0cb8be5cd69c825cc33c3a1629defbfe675b8c90bb32b7e9da`; training-report SHA-256 `bb29b3b377df1b4969af28d0e202c96148c6c85a2f4754997dc4d5435c0caaef`; advanced-report SHA-256 `4ae340f2f2ce9e55533666b661ca6a00cab4419f7eda7d996fd2849a22358a53`. Both report sidecars match their files; manifest/data hashes and all 22 report-referenced model-artifact hashes were recomputed and matched.
- [ ] **Both Meta-Label candidates rejected; no empirical qualification.** Experiment ID `770544672acc9035de7a24e69832aa4b847a25c0726db9e1dcbbe87a70756114`. For both XGBoost and LightGBM, 13 folds were expected but only 11 were evaluated; folds 8 and 9 were not evaluated because training/validation class support was insufficient after chronological purging. Pooled test set: 42 events, positive prevalence 0.095238 (9.52%). Pooled test AP: XGBoost 0.329165; LightGBM 0.121343. Although both pooled AP values exceed pooled prevalence, classification acceptance fails because complete 13/13 fold coverage is mandatory. Both models selected abstention threshold `1.01` in all 11 evaluated folds, completed zero trades and had mean net return 0.0. Mean paired-fold return difference against Buy-and-Hold was -0.4414 percentage points; against SMA state was -0.1278 pp; against SMA entry-event was -0.2354 pp. All three 95% paired-fold bootstrap lower bounds were negative: XGBoost `-0.018651`, `-0.012116`, `-0.012850`; LightGBM `-0.019432`, `-0.012288`, `-0.012965` respectively. Classification, economic and empirical candidate gates failed for both models.
- [x] **Promotion boundary preserved.** `selected_model_for_runtime = null`; report says `runtime_promotion_allowed=false` and `RESEARCH_ONLY_NO_CHAMPION_NO_LIVE_TRADING`. The data-fetch recovery does not qualify either model. Volume units remain unknown, historical order book is unavailable, and spread/slippage are simulated assumptions.

### Historical transport incident — prior SHA only

- [x] The two earlier attempts on SHA `4343d637f2bfd6536566a8b2d23a395f37bca433` failed before model evaluation with `Historical UDF GET failed with URLError`; artifacts #12296199968 and #12296999003 contained diagnostics only. At that revision the exception wrapper did not expose the underlying reason. That incident is retained as history; the successful current run does not retroactively identify its root cause.

### Phase 29 first empirical checkpoint — 2026-10-20

- [x] The bounded public-history request completed and produced a verified immutable dataset: 11,430 H1 candles, 23 pages, zero reported gaps, actual coverage 2025-07-01 13:30 UTC through 2026-10-20 18:30 UTC (about 476 days, despite a 730-day request).
- [x] Exact-head CI on SHA \`a80c4c200c6acfbb8169bd19de1ff206a8b8e7e2\` passed all seven jobs. Research workflow static/type/security/unit/dependency preflight and run completed successfully.
- [x] **First-run candidate failed empirical gates (historical SHA only).** On code SHA `a80c4c200c6acfbb8169bd19de1ff206a8b8e7e2`, XGBoost and LightGBM each evaluated only 3/11 folds; scored test-fold AP equaled positive prevalence (0.20), both made zero closed trades, mean net return was 0, and paired-fold lower confidence bounds versus all three baselines were negative. Neither model qualified. This remains historical evidence for the first implementation and must not be substituted for a result from the validation-threshold code.
- [x] **Artifact reproducibility correction verified on historical Run #10.** Artifact #12296426394 (run #39492253046) includes the immutable dataset CSV and manifest alongside logs and model evidence; ZIP SHA-256 `5fbea9385f107be926c0d467bab751055d07338e230d13f1be6642eb49e06292` was verified. It uses code SHA `ab30adcf59ba9cc430d820722fca9e8ba6d9d191`, before validation-selected threshold changes, so it does not validate the current implementation.
- [ ] **Second research iteration:** improve signal/threshold research using only train/validation data, retain test isolation and explicit multiple-comparison controls, and never reduce empirical gates to manufacture acceptance.
- [ ] **Promotion remains blocked:** \`selected_model_for_runtime=null\`; no Champion, no runtime change, no live connection and no order submission.
- Evidence: \`docs/research/advanced-ai-optimization-first-run-2026-10-20.md\`; workflow run ID 39491394243; artifact ID 12295054562.
