# ADR-0020: End-to-End AI Intelligence Core Roadmap

- **Status:** Proposed
- **Date:** 2026-10-21
- **Owners:** Project owner and AI/platform engineering
- **Related phases:** Phase 31 Triple-Barrier labels; Phase 32 cross-fitted Meta-Labeling; Phase 33 calibrated dynamic risk sizing; Phase 34 offline Reinforcement Learning research
- **Related changes:** PR #32 merged; PR #33 merged as research-only at merge commit \`201d7493615458c9b482fbbb21f98ec2936952f0\`

## Context

EVORYN now has a bounded public-history pipeline, content-addressed datasets, chronological evaluation/backtesting, a Mock/Paper runtime and a user-scoped Windows Task Scheduler boundary. The latest candidate experiments do **not** justify a trading-model Champion.

Evidence that must remain attached to this decision:

- The prior XGBoost/LightGBM Meta-Labeling experiment evaluated only 11/13 expected folds. Both candidates abstained in all evaluated folds, placed zero closed trades and failed classification coverage and economic gates. See [Advanced AI Optimization Protocol](../research/advanced-ai-optimization-protocol.md) and the Phase 29 ledger entries in \`TECHNICAL_DEBT.md\`.
- The three-state regime experiment completed five of five test folds per model on 11,439 H1 candles. HMM balanced accuracy was about 89.76%; XGBoost balanced accuracy was about 99.73%. Yet the regime-gated SMA mean net returns were about -1.995% and -2.368% per test fold respectively, versus about -0.200% for unfiltered SMA and +0.995% for Buy-and-Hold under the same assumed costs. Both economic gates failed.
- The XGBoost target was mechanically defined by a thresholded trailing 24-bar return, and \`return_24\` was also a model input. The high classification score therefore largely measures reproduction of the label rule. This is **target/feature leakage (definitional leakage)**. The feature was trailing and the temporal inference path was causal; the evidence does not, by itself, establish classic future-row look-ahead leakage in feature generation. Do not describe the 99% score as predictive alpha.
- Historical UDF volume units are unverified, historical order-book depth is unavailable, spread/slippage are assumptions, and provider data-use terms remain unreviewed.

The next approach is a research system that learns from trade outcomes and uncertainty, composes signals with independent risk controls, and is evaluated end-to-end after costs. No methodology can guarantee sustainable profit.

## Decision

Adopt a phased, evidence-gated roadmap. Each phase is implemented and reviewed in a separate change, first in offline research. No new model is selected as Champion or loaded by Runtime under this ADR.

### Phase 31 — Triple-Barrier event labeling

1. Define a base event from a causal, completed-bar rule, initially SMA-12/48 crossover or a precisely specified SMA trend event.
2. At event time \(t\), calculate the volatility scale using only completed bars at or before \(t\). Freeze the upper profit-taking barrier, lower stop-loss barrier, and vertical time barrier for that event. Use adverse/ambiguous intrabar ordering conservatively when OHLC candles cannot establish which horizontal barrier was hit first.
3. Label the event according to the first barrier touched: profit-taking, stop-loss, or vertical expiry; preserve event start, end, reason, return before costs, and return after the cost model.
4. Build chronological train/validation/test splits from event intervals. Purge training observations whose label intervals overlap validation/test intervals and apply an embargo around split boundaries. Fit any volatility thresholds, barrier multiples, class weights, feature selection, or hyperparameters on training data only.
5. Report label prevalence, barrier-touch distribution, expiry ratio, holding period, overlap/purge counts, and sensitivity to barrier widths. A future price may define the **label** by design; it must never enter the feature row or training information set available at event time.

### Phase 32 — Cross-fitted Meta-Labeling

1. A base strategy emits a timestamped, side/exposure proposal using only information available at that time. Start with the existing long/flat SMA family and do not tune it on the locked test interval.
2. Generate out-of-fold base proposals by chronological cross-fitting. The meta model receives features fixed at the event time and learns whether the base event clears its predeclared net-outcome criterion under Triple-Barrier labels. Training the meta model on in-sample base predictions is not acceptable.
3. Start with a compact, regularized XGBoost binary classifier only after class/event support is adequate. Compare with a train-only majority/constant-probability baseline and the unfiltered base strategy. Use probability calibration fitted on a separate chronological validation block, not the test block.
4. Report PR-AUC/average precision, precision at explicit coverage, selected-event count, calibration/Brier/log loss, abstention rate, and per-class/event support. Classification is secondary to net, cost-adjusted out-of-sample outcomes.
5. Require complete fold coverage; positive net economic improvement versus both unfiltered SMA and Buy-and-Hold; minimum predeclared number of closed trades; drawdown/tail-risk limits; and positive lower confidence bounds for paired, same-window return differences. Publish negative results.

### Phase 33 — Calibrated Dynamic Position Sizing

1. Size risk from a conservative per-trade equity budget and the frozen stop distance/volatility estimate. An initial candidate formula is:
   \[
   q_{\mathrm{risk}}=\frac{E\,r_{\mathrm{trade}}}{d_{\mathrm{stop}}\,v_{\mathrm{unit}}}
   \]
   where \(E\) is marked equity, \(r_{\mathrm{trade}}\) is an independently approved small risk fraction, \(d_{\mathrm{stop}}\) is adverse price distance and \(v_{\mathrm{unit}}\) converts price movement to quote-currency loss. If instrument semantics are missing, reject sizing instead of guessing.
2. Only use confidence from a separately calibrated, out-of-fold probability. A bounded confidence multiplier can reduce or cap the risk budget; it may not expand exposure beyond deterministic hard limits. Low calibration quality, stale/gapped inputs, uncertain model state, missing stop distance or a risk HALT means abstain/zero new exposure.
3. Apply volatility targeting and concentration caps after per-trade risk sizing, then pass the result through the existing deterministic Risk Engine. The Risk Engine is the final authority; a model cannot override max loss, exposure, notional, freshness, unresolved-order or halt controls.
4. Compare dynamic sizing with fixed-risk and volatility-only baselines using identical out-of-sample events, costs, and risk constraints. Report turnover, time in market, drawdown, expected shortfall/tail losses, capital utilization and sensitivity to costs. Do not start with unconstrained Kelly sizing; noisy probability/edge estimates make it fragile.

### Phase 34 — Reinforcement Learning, last and offline only

1. Defer RL until the Triple-Barrier/meta-label baseline and dynamic sizing are reproducible and a realistic, audited simulator exists. Include explicit next-open/next-fill timing, fees, spread, slippage, partial fills, stale/gapped data, unfilled intents, limits, restart and halt states. Document simulation-to-market gaps.
2. Begin with a bounded discrete action set such as \`{flat, enter-small, hold, reduce, exit}\`; do not allow the agent to change hard Risk Engine controls, bypass stops, increase leverage, or issue unrestricted orders. The simulator has no exchange-writing client.
3. Use a net-of-cost reward with explicit drawdown/tail-risk and turnover penalties and a penalty for invalid actions. Never reward mark-to-market gains that could not be realized at an admissible fill. Fix all random seeds for reproducibility and log complete experiment/config/data identities.
4. Compare RL with simple deterministic policies, unfiltered SMA, the accepted Meta-Labeling candidate (if one ever passes), and Buy-and-Hold on identical walk-forward windows. Reserve a final untouched chronological test set and control repeated-search/multiple-comparison risk. Report performance across seeds and scenarios, not the best run only.
5. A high reward in the simulator is not evidence of live profitability. RL is not permitted to train online against a live exchange, access keys/accounts, or operate a Live order route.

## Global gates applying to every phase

- Immutable dataset version, candle/report/model checksums, source/data rights, explicit feature/label timestamps, deterministic seeds and reproducible configuration.
- Prefix-invariance and event-boundary leakage tests; chronological split manifests; purging/embargo wherever label intervals overlap; fit transforms/calibrators only on training/validation windows.
- Complete planned fold coverage with no silently omitted folds. Any non-evaluable fold counts against the coverage gate, not as a pass.
- Cost sensitivity using conservative assumptions, and explicit volume/book limitations. Until provider terms and data semantics are confirmed, results remain research-grade only.
- Comparison with naive/majority, unfiltered base, Buy-and-Hold and appropriate risk baselines on paired time windows. Acceptance criteria are defined before reading the final test set.
- The model's confidence never substitutes for Risk Engine approval. Unknown/stale input or a persisted HALT must abstain and remain latched.
- Every phase reports \`selected_model_for_runtime = null\` and \`runtime_promotion_allowed = false\` by default. Any later promotion requires a separate accepted ADR, independent model-risk/security review, explicit release approval and a compatibility/rollback plan.

## Alternatives considered

1. **Keep optimizing the current price/regime classifier.** Rejected as the primary direction because high label-reproduction scores did not translate into economic improvement, and the current target is too coupled to an input feature.
2. **Immediately train a reinforcement-learning agent.** Rejected for now because reward hacking, unrealistic fills, data scarcity and simulator mismatch would make results hard to trust before a strong benchmark and execution model exist.
3. **Triple-Barrier + cross-fitted Meta-Labeling, then risk sizing, then RL.** Proposed. It makes event outcomes explicit, separates base signal from event acceptance, introduces risk sizing only after probability calibration, and delays RL until the simulator has credible economics.
4. **Stop AI development entirely.** Keep available as a fallback if no phase beats simple baselines after costs; no phase is required to pass.

## Consequences

### Positive
- A single end-to-end evaluation contract connects event definition, signal generation, acceptance, sizing and simulator outcomes.
- Target/feature leakage and event-overlap leakage become explicit, testable failure modes.
- Negative results remain publishable and do not trigger a weakening of gates.

### Negative / trade-offs
- Triple-Barrier labeling, cross-fitting and purged walk-forward evaluation add implementation and compute cost.
- Missing order-book depth, unknown volume units and unreviewed provider terms limit fill realism and possibly permitted data use.
- Dynamic sizing and RL will remain blocked if probability calibration or simulator fidelity is insufficient.

## Security and safety impact

- **Secrets and data exposure:** No secret is needed for research; use public GET history only under a reviewed source policy. Never embed credentials in dataset/report artifacts.
- **Permissions and trust boundaries:** Research CI has read-only repository contents, no account or order routes and no live secrets. Models/artifacts are untrusted proposals, not executable trading authorization.
- **Trading / execution impact:** No changes to Live or Spot-write gates. Paper simulation stays isolated. No automated model promotion, live order, withdrawal, margin or transfer operation is authorized.
- **Failure and recovery behavior:** Unknown state, integrity mismatch, incomplete data, failed reconciliation or risk HALT produces abstention/failure closed. Order-write paths are not wrapped in retry loops.

## Validation and evidence

- PR #32 merged and preserves failed Meta-Label candidate evidence in \`TECHNICAL_DEBT.md\`.
- PR #33 merged at \`201d7493615458c9b482fbbb21f98ec2936952f0\`. Standard CI and market-regime research runs are linked from the ledger; the HMM and XGBoost regime-gated strategies failed their economic gate.
- Latest regime research run [#39544524629](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39544524629), artifact [#12320394962](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39544524629/artifacts/12320394962).
- No independent review of this proposed roadmap has been recorded. All future phases require separate CI and empirical artifacts.

## Rollback / supersession

This ADR adds a research roadmap only. It can be withdrawn by superseding it with a later ADR and reverting research-only documentation/code changes. It does not migrate data, modify runtime configuration, enable any model, or change an execution boundary.
