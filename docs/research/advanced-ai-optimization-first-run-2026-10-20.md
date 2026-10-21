# Advanced AI Optimization — First Empirical Run

**Disposition:** failed candidate gates; research stays open; no runtime promotion.  
**Workflow run:** [39491394243](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39491394243)  
**Tested code HEAD:** \`a80c4c200c6acfbb8169bd19de1ff206a8b8e7e2\`  
**Exact-head CI:** [run 39491392066, all seven jobs successful](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39491392066)  
**Run artifact:** \`advanced-ai-optimization-39491394243-1\`, artifact ID \`12295054562\`, ZIP SHA-256 \`a1d4e6fe5d971107e817f5b93aa9da5bf963866f6e195da39da30958bdc9ca81\`.

## 1. Actual data received

The workflow requested 730 calendar days but the public Nobitex UDF response yielded only 11,430 hourly candles across 23 pages, from **2025-07-01 13:30 UTC** through **2026-10-20 18:30 UTC**. That is approximately 476 days, not two years. The dataset reports zero gaps and a post-write content-addressed verification succeeded. This demonstrates the current endpoint’s returned coverage for this symbol, not that older history exists elsewhere.

- Dataset ID: \`nobitex-btc-usdt-60m\`
- Immutable dataset version: \`6073048db6b93acf6a6940a62efb896ce61f04926d7b42b80b0bdecfb4f2cebd\`
- Candle CSV SHA-256: \`910481d495b13962dddcf043e01c7ef77070b7dfa99ee35907ab8929fe031956\`
- Manifest SHA-256: \`1e2380087998f4fd86a6a4edbc37733641b96c67ae52b39fe4342fc91f9376a6\`
- Training metadata report SHA-256: \`035b79a657c21fa6bea1deec822b9a90a298f9d6c314f06375e29655b5b3fd1f\`
- Advanced report SHA-256: \`a9c180297fff7b8849a05bd079ef397d3d736a40b6c948508b19c945f07bd2a9\`

The UDF volume unit is still unknown. The research matrix therefore excludes volume and order-book features; the cost simulator disables volume-based participation limits and explicitly documents that results may be optimistic. Historical depth data was not fabricated. The collection timestamp, exact source query, dataset/manifest hashes and page count are recorded in the workflow artifact.

**Reproducibility limitation in this first run:** the artifact currently contains dataset/manifest hashes but did not include the actual dataset CSV/manifest. The workflow is being amended to archive the immutable dataset alongside the report. Until the dataset files are retained, hashes verify identity only and cannot reconstruct the candles from the artifact alone.

## 2. Experiment protocol actually run

- Strategy: SMA(12/48) golden-cross long-entry events, entry at the next open, six-bar forward label outcome, binary target requiring return to clear the fixed 50 bps round-trip label hurdle.
- Features: 27 causal H1 price-derived fields, including closed-bar 4h/daily context, returns, realized volatility, Bollinger measurements, ATR percentage, rolling Fibonacci distances and confirmed swing-shape proxies.
- Model challengers: XGBoost and LightGBM.
- Feature selection: mutual-information ranking using train events only.
- Hyperparameter tuning: seeded Optuna TPE, 10 trials per evaluated fold/model, maximizing average precision on chronological validation events.
- Evaluation: 11 chronological, non-overlapping test folds were planned; last 10% retained as a terminal reserve. Backtests used explicit commission, spread and slippage assumptions.
- Comparators: Buy-and-Hold, state-based SMA(12/48), and an unfiltered SMA-entry-event baseline. The report never allows Runtime promotion automatically.

## 3. Outcome and failed gates

| Measure | XGBoost | LightGBM |
|---|---:|---:|
| Expected walk-forward folds | 11 | 11 |
| Folds actually evaluated | 3 | 3 |
| Complete fold coverage | No | No |
| Mean reported test-event average precision | 0.20 | 0.20 |
| Mean positive-label prevalence on scored test events | 0.20 | 0.20 |
| Closed trades across evaluated folds | 0 | 0 |
| Mean net test return | 0.00% | 0.00% |
| Classification gate | Failed | Failed |
| Economic gate | Failed | Failed |
| Empirical candidate gate | Failed | Failed |

Eight folds did not qualify for model evaluation under the predeclared minimum event/class requirements. On the only scored test fold, average precision merely matched prevalence at 0.20; it did not demonstrate out-of-sample discrimination. The other evaluated test blocks had too few or one-class outcomes for a meaningful AP estimate. At the fixed probability threshold of 0.55, the models admitted no primary signals and therefore made zero completed trades.

Across only three paired fold returns, candidate-minus-baseline bootstrap intervals were:

| Comparison | Mean net-return difference | Paired-fold 95% interval |
|---|---:|---:|
| Buy-and-Hold | -2.0484% | [-5.1560%, +0.3568%] |
| State SMA(12/48) | -1.4112% | [-4.3720%, +0.0785%] |
| Unfiltered SMA entry events | -1.3685% | [-4.9640%, +0.7799%] |

These three-fold intervals are very underpowered and are reported as diagnostics, not population-level claims. All lower bounds are negative, no candidate earned net returns, and the expected 11-fold coverage requirement failed. The outcome is **not evidence of profitability or baseline superiority**.

Machine report: experiment ID \`b4f84195bf1d60f8a781c20132c92fd6fb7ec29b1bbffbae763e56c7825d6e97\`; report digest above. The machine report identifies both candidates as \`INCOMPLETE_FOLD_COVERAGE\`, \`empirical_candidate_gate_passed=false\`, and \`runtime_promotion_allowed=false\`.

## 4. Correct interpretation and next research gate

The main bottleneck is sample support, not a reason to relax acceptance criteria: the current SMA primary signal generated only 142 labeled events on the returned history, leaving very small train/validation/test event sets and no qualifying trade output. The next research iteration should compare a predeclared, small set of primary-signal/event-generation variants and choose decision thresholds strictly on validation outcomes, while keeping test blocks locked and accounting for multiple comparisons. Any variant must still produce complete walk-forward coverage, positive net returns after the stated costs, enough closed trades, and positive lower paired-fold confidence bounds against all required baselines.

Do not increase the score by weakening the pass gates after observing test results. The current model candidates are rejected for this experiment. \`selected_model_for_runtime = null\`; no Champion, live adapter, real orders, credentials, or runtime promotion were used.
