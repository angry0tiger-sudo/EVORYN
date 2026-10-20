# Six-model Walk-Forward Artifact Audit — 2026-10-20

**Disposition: historical challenger evidence only; no model is approved for Runtime.**

## Artifact identity and integrity

- Artifact ID: `12256082280`
- Downloaded ZIP SHA-256: `806cc33bcc2b80746e8fa35ae08ca3886c352166bc6287589b5fb0db20475e84`
- Embedded report: `artifacts/nobitex-regime/walk-forward/walk-forward-report.json`
- Embedded report SHA-256: `702bcbba8fd69ddcf334bf8c8fb1b938d65428336744d96c12202adfb4ede4b8`
- Report schema version: 2
- Dataset: Nobitex Spot BTC-USDT, 1-hour candles; 4,320 candles; zero timestamp gaps.
- Dataset version: `8aaf2e83e1aa68d8a4ed6deae9057626f3a0556467d7bd99a6f509fc6be2e384`
- Feature schema: `3.0.0-price-technical-only`, 21 trailing price/technical features; volume is excluded because UDF volume units remain unknown.
- The downloaded ZIP digest is valid SHA-256 (64 hex characters) and differs from the 66-character value transcribed in the supplied screenshot. The digest above was computed from the downloaded ZIP bytes.
- All 91 embedded SHA-256 sidecars were checked against their target files; no sidecar mismatch was found.

The artifact contains 11 unique logical folds and both label policies under each fold. All six model families have `EVALUATED` classification records for every fold/policy pair. `selected_model_for_runtime` is `null`.

## Classification results

Means recomputed from the report's per-fold metric records.

### Forward-threshold labels

| Model | Accuracy | Balanced accuracy | Macro-F1 | Log loss |
|---|---:|---:|---:|---:|
| Random Forest | 48.65% | 26.54% | 24.54% | 1.1842 |
| HistGradientBoosting | 55.99% | 26.82% | 24.60% | 1.1081 |
| XGBoost | 58.43% | 25.81% | 23.31% | 1.0788 |
| LightGBM | 58.06% | 27.56% | 25.07% | 1.0904 |
| Gaussian HMM | 64.51% | 25.55% | 20.53% | 1.0434 |
| CPU LSTM | 36.24% | 22.74% | 20.48% | 1.3264 |

The train-only majority Accuracy baseline recomputed from the 11 folds is **65.3918%**.

### Volatility-scaled Triple-Barrier labels

| Model | Accuracy | Balanced accuracy | Macro-F1 | Log loss |
|---|---:|---:|---:|---:|
| Random Forest | 40.06% | 36.20% | 28.66% | 1.2133 |
| HistGradientBoosting | 56.24% | 30.02% | 28.66% | 1.1029 |
| XGBoost | 58.87% | 29.58% | 27.15% | 1.0573 |
| LightGBM | 58.06% | 29.82% | 27.94% | 1.0591 |
| Gaussian HMM | 62.70% | 26.31% | 20.57% | 1.0586 |
| CPU LSTM | 33.29% | 32.64% | 25.08% | 1.2597 |

The train-only majority Accuracy baseline recomputed from the 11 folds is **64.3887%**, not 63.44%. Every model's mean Accuracy is below this baseline. Higher raw Accuracy for HMM does not indicate useful class discrimination: its balanced accuracy and Macro-F1 remain weak.

## Cost-adjusted economic results

Arithmetic mean of the 11 fold-level net-return fractions; each fold resets its own starting equity. Costs are simulated assumptions: 10 bps commission, 10 bps spread, and 10 bps slippage.

| Strategy | Mean net return per fold | Total closed trades |
|---|---:|---:|
| Buy-and-Hold reference | +0.5551% | 0 |
| SMA crossover | +0.1249% | 25 |
| Random Forest | -0.2463% | 22 |
| HistGradientBoosting | -0.1732% | 16 |
| XGBoost | -0.1513% | 12 |
| LightGBM | -0.1343% | 10 |
| Gaussian HMM | 0.0000% | 0 |
| CPU LSTM | -0.0022% | 1 |

The open Buy-and-Hold position remains open at each fold end, hence zero closed trades. HMM never entered the market; LSTM's near-zero return reflects only one closed trade and negligible exposure, not demonstrated edge. All challengers failed to outperform Buy-and-Hold after the stated cost assumptions.

## Reporting defect and limitation

This is a **schema-version-2 historical artifact**, not proof that the newer source revision has been validated. Although both policies contain all six models' classification records, the `forward_thresholds` policy has **no model backtest entries**. Therefore economic comparisons for that policy are absent and economic acceptance cannot be claimed across both policies from this artifact.

The current schema-version-3 source records policy-specific backtests and `candidate_gates_by_policy`. A fresh research run on the exact PR HEAD must confirm that the JSON and ZIP actually contain those fields, all expected model/policy/fold records, reloadable model artifacts, and matching checksums before the reporting defect can be closed.

## Release decision

- `selected_model_for_runtime = null`
- No model is promoted.
- Research-only artifacts are not connected to the production/Paper runtime.
- The 432-candle terminal period in this artifact is experiment-scoped and marked reserved, not evaluated. Its independence from prior experiments has not been established, so it is not yet a project-level untouched confirmation set.
- Unknown volume semantics, unreviewed data-use rights, assumed costs, and temporal dependence remain limitations.
- Windows Task Scheduler registration, a verified dedicated Windows Paper composition root, and owner-host Windows 11 acceptance remain open. No local machine was accessed, no task was registered, and no live connectivity or order was used.
