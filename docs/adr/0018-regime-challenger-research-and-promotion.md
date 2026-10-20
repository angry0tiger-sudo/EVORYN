# ADR 0018: Regime Challenger Research and Evidence-Gated Model Promotion

- **Status:** Proposed for independent review
- **Date:** 2026-10-20
- **Owners:** EVORYN engineering and model-risk review
- **Related phases:** Phase 25/26, Intelligence and Learning Lab
- **Related changes:** branch `research/walk-forward-labels-volume-audit-20261020`
- **Related ADRs:** ADR 0007, ADR 0008, ADR 0010, ADR 0013

## Context

The original four-feature Random Forest did not beat the train-only majority-class baseline
on its held-out historical classification test and did not establish profitable performance
after simulated transaction costs. The first Walk-forward report also had a presentation defect:
it stored duplicate fold objects and each fold's policy dictionary contained only one of the two
label policies. Candidate evaluation must use identical temporal windows, labels and costs and
preserve every fold and policy in the report.

Nobitex public UDF history exposes the `v` field without establishing whether its unit is base
asset or quote asset. The research pipeline therefore cannot treat it as a verified model input
or liquidity constraint.

## Decision

1. Use research-only schema `3.0.0-price-technical-only` with 21 causal price/technical features:
   multi-horizon returns, multiple volatility horizons, SMA gaps, RSI-14, MACD, Bollinger Bands,
   ATR-14, trend strength, range position, and volatility ratio. Indicators use trailing
   completed OHLC bars only. Unknown-unit volume is excluded entirely.
2. Compare six challenger families: Random Forest, HistGradientBoosting, XGBoost, LightGBM,
   Gaussian HMM using forward-filtered (not backward-smoothed) state probabilities, and a compact
   CPU LSTM over twelve chronological feature rows. LightGBM is pinned as `lightgbm==4.7.0`;
   all challenger-specific packages remain research-only dependencies.
3. Compare two weak-label policies: train-thresholded forward-return/volatility labels and a
   volatility-scaled first-touch OHLC Triple-Barrier label. The horizon is six candles. A same
   candle touching both barriers is labelled high volatility because intrabar order is unknown.
   Label windows crossing train/validation/test boundaries are removed.
4. Fit scalers, model parameters, HMM state-to-label maps and label thresholds within each fold's
   training partition. Fit temperature calibration on that fold's validation partition only.
   Test samples are not used for fitting or temperature calibration.
5. For every fold and label policy, persist class support, validation/test metrics, model artifact
   hashes, HMM convergence status and test-window portfolio simulations. Compare each classifier
   with the per-fold train-only majority baseline; compare net portfolio return with same-fold
   Buy-and-Hold and SMA baselines after assumed commission, spread and slippage. Both label-policy
   results are nested under each fold.
6. Use descriptive paired-fold bootstrap intervals to test whether accuracy, balanced accuracy,
   and macro-F1 differences versus majority have lower 95% bounds above zero. The economic gate
   also requires a positive lower 95% bound versus Buy-and-Hold and at least twenty closed trades.
   Adjacent folds are temporally dependent; the bootstrap intervals are descriptive, not proof
   of independent observations or future performance.
7. **No automatic model promotion.** Promotion is blocked unless a candidate passes every
   empirical gate on every fold, is evaluated on a separate untouched confirmation period, has
   a schema-compatible loader with independently pinned artifact/manifest digests, and is accepted
   through separate review. Until then the report must show `selected_model_for_runtime=null`.
8. The production model loader is a separate versioned contract and does not automatically load
   the research schema. No challenger artifact is connected to runtime by this ADR.

## Alternatives considered

1. **Promote highest raw accuracy immediately:** rejected. Class imbalance makes accuracy misleading,
   the earlier RF underperformed majority, and selection on evaluation windows creates bias.
2. **Use only Random Forest:** rejected. Other model families can be evaluated without making
   their optional dependencies part of the base runtime.
3. **Use UDF `v` without provider confirmation:** rejected because a field name does not prove units.
4. **Use backward-smoothed HMM probabilities:** rejected because past posteriors would use future data.
5. **Connect the research model directly to Paper scheduling:** deferred. The v3 schema needs an
   independently reviewed loader/runtime adapter and a quality gate first.

## Consequences

### Positive
- Six materially different model families are compared under common temporal and cost protocols.
- Technical inputs are richer without importing unverified volume semantics.
- Fold identity, labels, artifacts, calibration and benchmarks are auditable.
- “No winner” remains a valid engineering outcome.

### Negative / trade-offs
- CPU LSTM training increases workflow time and dependency surface.
- Weak labels are not ground truth; technical indicators may be redundant or non-stationary.
- Historical OHLCV cannot prove live profitability or executable capacity.

## Security and safety impact

- The research workflow uses only anonymous public historical-candle GET.
- No account endpoint, credential, order route or live-trading port is used.
- Challengers remain advisory; portfolio results come from the offline backtester.
- Research artifacts are not automatically loaded by runtime.
- Provider data rights and volume semantics remain unresolved.

## Validation and evidence

- Exact-head CI must pass all six standard gates.
- The research workflow must pass its ML-enabled tests, environment audit, historical-data validation,
  fold comparisons, and artifact publication.
- Report and dataset digests, run URL and candidate-gate outcomes are recorded in
  `TECHNICAL_DEBT.md` after an exact-head run.
- Independent model-risk and data-rights review remain open.

## Rollback / supersession

Remove the research-only feature schema, challenger adapters and workflow changes. This ADR changes
neither the production feature schema nor risk and exchange-write gates.


## Exact-head challenger evidence — 2026-10-20

- Code/test SHA `c08096a8ff8c28d78a9879977cc5c30fde2d5f77`; [six-gate CI #39397941134](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39397941134), [research run #39397941187](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39397941187), [artifact #12252633238](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39397941187/artifacts/12252633238).
- ZIP SHA-256 `5d411b9ef13a0e31b5a602e4d03cbd570c0eada8530bcbc9137a552166724d8e`; report SHA-256 `5900b1c3465ad23b90bce58fcfbe020fbc6b4b8ce295b9d505bf3b0bdb1785a7`.
- Ten fold objects preserve both label policies. See [the full report](../research/2026-10-20-regime-challenger-walk-forward.md).
- Train-only majority Accuracy averaged 63.062% for forward-threshold labels and 63.445% for Triple-Barrier labels. Triple-Barrier mean Accuracy was RF 40.67%, HistGradientBoosting 55.79%, XGBoost 59.04%, HMM 61.77%, LSTM 33.16%. Several Macro-F1 results improved, but all candidates' Accuracy difference confidence intervals were below zero.
- Buy-and-Hold mean net return was +0.5629% per test fold. Mean candidate returns ranged from -0.2406% to near zero; closed trades ranged from 0 to 18. Return-difference intervals versus Buy-and-Hold all crossed zero with negative lower bounds. All five candidates failed the combined gate.
- The report records `selected_model_for_runtime=null`. No challenger is connected to Runtime; an untouched confirmation period and independently pinned schema-compatible loader remain required.


## Six-model artifact audit — 2026-10-20

Artifact `12256082280` was retrieved and its ZIP was independently hashed as
`806cc33bcc2b80746e8fa35ae08ca3886c352166bc6287589b5fb0db20475e84`; the embedded
walk-forward report SHA-256 is
`702bcbba8fd69ddcf334bf8c8fb1b938d65428336744d96c12202adfb4ede4b8`. The ZIP digest does
not match the digest transcribed in the supplied screenshot, so the locally computed digest is
the value to retain for this downloaded byte stream.

The report contains 11 logical folds, both label policies, and all six model outputs for every
fold/policy pair. It records `selected_model_for_runtime=null`. Recomputed Triple-Barrier mean
majority accuracy is 64.3887% for this artifact, not 63.44%. All six candidate mean accuracies
are below that baseline; their cost-adjusted mean net returns are below Buy-and-Hold.

This schema-version-2 artifact is not sufficient as final exact-head evidence: its
`forward_thresholds` policy records have no model backtest entries, and the artifact's source
commit association has not yet been independently established from artifact metadata. The
current schema-version-3 source is expected to persist policy-specific backtests and
`candidate_gates_by_policy`; a fresh exact-head research run must verify that behavior before
this ADR's evidence section is considered complete. No model is promoted.
