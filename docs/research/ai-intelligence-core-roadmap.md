# AI Intelligence Core — Next-Phase Roadmap

**State:** proposed research roadmap, not an implementation or profitability claim.  
**Current Champion:** none. \`selected_model_for_runtime = null\`.  
**Runtime promotion:** disabled. Live/order writes remain disabled.

## Evidence that changes the plan

PR #32 and PR #33 are merged. The first Meta-Labeling run rejected XGBoost and LightGBM because the required 13-fold coverage was incomplete; every evaluated validation threshold abstained, so both produced zero closed trades and failed economic acceptance.

The three-state-regime experiment in [Research Run #39544524629](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39544524629) evaluated five of five walk-forward test folds for both HMM and XGBoost. HMM balanced accuracy was 89.76%; XGBoost was 99.73%. Yet regime-gated SMA mean net return per test fold was -1.9954% and -2.3676%, respectively. Unfiltered SMA averaged -0.1999% and Buy-and-Hold +0.9950% under the same simulation assumptions. Both candidate economic gates failed.

**The 99% trap:** the XGBoost target was defined by thresholding trailing 24-bar return, and that same trailing return was a direct feature. This is target/feature leakage or definitional leakage. Because the row was trailing-only, this evidence alone does not establish classical future-row look-ahead leakage. Either way, the score mainly shows that the model learned its own label definition and did not prove tradable alpha.

## Planned phases

| Phase | Objective | Core implementation | Evidence required to advance |
|---|---|---|---|
| **31: Triple-Barrier** | Define events by profit-taking, stop-loss, or vertical time barrier | Causal base events; barrier sizes from trailing volatility available at event time; adverse first-touch resolution; event interval and net outcome stored | Label distribution, holding time, expiry and purge metrics; temporal-overlap tests; completed-bar causality tests; train-only barrier selection |
| **32: Cross-fitted Meta-Labeling** | Decide whether an out-of-sample base event merits exposure | SMA or other fixed base signal; triple-barrier outcomes; chronological out-of-fold base signals; regularized XGBoost meta classifier; validation-only probability calibration | Full folds; no base-model in-sample leakage; calibration and coverage metrics; positive net improvement vs unfiltered SMA and Buy-and-Hold |
| **33: Dynamic Position Sizing** | Translate calibrated confidence and volatility into bounded risk | Fixed equity risk budget divided by stop-distance loss per unit; conservative confidence/volatility multipliers; hard caps in deterministic Risk Engine | Compare fixed-risk, volatility-only and dynamic sizing on identical OOS windows; cost sensitivity; bounded drawdown and tail-risk; all hard caps always hold |
| **34: Reinforcement Learning (last)** | Test sequential action policies only after simulator credibility is established | Offline discrete actions such as flat/enter-small/hold/reduce/exit; fee/slippage/fill/capacity simulator; net reward with drawdown/turnover penalties | Multiple seeds; untouched chronology; comparisons against SMA, Meta-Label and Buy-and-Hold; reward-hacking/simulation-gap audit; never train online on Live |

## Phase 31 — Triple-Barrier requirements

At each permitted base event, compute a volatility scale from completed bars up through that event time and freeze the profit barrier, stop barrier and maximum holding horizon. Label the first barrier reached; if OHLC data cannot resolve intrabar ordering, use the conservative adverse outcome or mark the event ambiguous and exclude it under a documented rule. Vertical expiry must be an explicit outcome, not silently dropped.

Labels legitimately depend on the path after the event. That is not feature leakage when the feature snapshot remains frozen at event time. Store the event interval and purge any training event that overlaps validation/test label intervals; apply a documented embargo at boundaries. All scalers, thresholds, feature selection and barrier hyperparameters must be fit on the relevant training partitions only.

## Phase 32 — Meta-Labeling requirements

A base strategy creates the event and intended side/exposure. The meta model decides whether to accept or veto the event; it must not replace the target after looking at test performance. Create the base signals through chronological cross-fitting, then construct meta-training rows only from out-of-fold base predictions. Fit the probability calibrator on a separate validation period. Preserve abstention: uncertain or invalid states mean no new risk.

Metrics must include event count/support, class prevalence, precision at declared coverage, PR-AUC, probability calibration/Brier score, abstention rate, closed trades, net returns after costs, turnover, drawdown and paired-fold confidence bounds. Always report fold coverage and compare against the same-window unfiltered base strategy and Buy-and-Hold. If support is too low, record NOT_EVALUABLE and fail the coverage gate.

## Phase 33 — Dynamic risk sizing requirements

Confidence alone must never determine notional. A proposed risk quantity is bounded by equity times an approved per-trade risk fraction divided by the estimated loss per unit at the frozen stop distance. Scale down further for volatility or low calibrated confidence, then apply deterministic notional, exposure, max-loss, price precision, freshness and concentration caps. If stop distance, unit conversion, model calibration or market-data quality is unavailable, return zero new exposure. The Risk Engine remains the final authority, and model output cannot clear a HALT.

Do not begin with Kelly sizing. Before any experiment, predeclare risk limits and compare fixed-risk, volatility-only and confidence-plus-volatility sizing using the same events, windows and costs.

## Phase 34 — Reinforcement Learning requirements

RL starts only after a robust event-labelled/meta-labelled baseline and a tested simulator exist. The simulator must define information timing, next-open/fill constraints, fees, spread, slippage, partial/no fills, stale/gapped feed behavior, position accounting, stop/vertical exits, halts and restart recovery. Use a bounded discrete action space and a net-of-cost objective penalizing drawdown, tail losses and excessive turnover. The agent cannot modify safety limits or access a live exchange client.

Report the distribution across seeds and scenarios, not the single best score. Compare to simple baselines on identical chronological windows, retain an untouched final test set, and account for repeated searches. A simulator reward is never sufficient evidence for Live eligibility.

## Global hard gates

- Dataset, report, experiment, config and model outputs have immutable versions and SHA-256 identities.
- Causality tests cover each feature at event time; overlap labels are purged and embargoed; test rows are never used for model, calibration or threshold fitting.
- Every expected fold is either validly evaluated or the candidate fails the complete-coverage gate.
- Economic acceptance is predeclared and based on net-of-cost OOS results, sufficient closed trades, drawdown/tail-risk limits, and positive lower confidence bounds on paired return differences against relevant baselines.
- Historical volume units remain unknown, no historical order-book depth is available, spread/slippage are assumptions, and provider data-use terms are unreviewed. Do not claim liquidity realism or commercial data rights until separately verified.
- Runtime always starts with no Champion unless a separate release decision is accepted. No research pipeline has access to secrets, private-account endpoints or order-write routes.

## Windows 11 delivery boundary

The current Task Scheduler runner is a local **offline CSV replay**. It scrubs EVORYN/Nobitex credentials from its process environment, uses \`NoChampionClassifier\`, and cannot load the research HMM/XGBoost artifacts. It is suitable for validating Paper orchestration and SQLite recovery, not for testing the newly learned regime strategy. See [Windows 11 Paper handover](../operations/windows11-paper-handover.md) and the [Windows Task Scheduler runbook](../operations/windows-paper-task-scheduler.md).

For every phase, deliver the code, unit/integration tests, protocol/ADR updates, exact-head CI, immutable empirical artifact and updated technical-debt ledger. Do not combine an experimental classifier change with enabling any Live/Spot-write gate.


## Phase 31 implementation candidate — research evidence only

A reviewable candidate is on branch `research/phase31-triple-barrier-20261021`; the code has not been merged. It implements the phase as a labeling and split-manifest layer only, with no model fit or Runtime wiring. The runner reads only a pinned local `HistoricalDatasetStore` version and writes immutable JSON plus a SHA-256 sidecar.

- Dataset version: `cf6fbd7263c0ffac39d4bbc1ef69c0e0f71e5e7fb9cd7ff29cff329a52a38344`
- Candle CSV SHA-256: `76bb3693dc9e13b1f87de2798c6c0d9aa8d718a3ff2e47681f677e4ba504ae01`
- Dataset manifest SHA-256: `cf0e9701169019acff8a96f6f9fc059abc699829fb7c57db91ae10d6c835624c`
- Event-ledger SHA-256: `bae9ef0f8b2e7d68b81ff95419bc3f33dac66387c3e94cdbd44ba76711089f1d`
- Report SHA-256: `e23b9c5f48bcfa61e4bbdaf86110a9ea785df5b11a7e8d3c26f901d3f6b0f427`
- 142 events: 22 profit-taking, 33 stop-loss, 87 vertical expiry; one incomplete horizon is explicitly skipped.
- Fold manifest: 5/5 expected folds evaluable, with 24-bar embargo. The economic gate is intentionally `NOT_EVALUATED_LABELING_PHASE_ONLY`.

The mean per-event return after the assumed 30 bps cost is -0.2382%. It describes label outcomes, not an executable strategy return. Dataset rights and volume semantics remain unresolved, and historical order-book depth is absent. Local synthetic tests passed (7 tests). GitHub Actions Runs [#1187](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39552513188) and [#1188](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39552517407) passed all seven configured jobs on implementation-source SHA `3a54ec68e2fa6c8feb4d9fa36515dd2170f2aee8`; the documentation update requires checking CI on the final PR HEAD. Independent review remains pending. Champion stays null and runtime promotion remains forbidden.
