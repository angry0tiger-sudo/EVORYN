# Nobitex Regime Model — Historical Research Record

**Research date:** 20 October 2026 (Azerbaijan time; Actions timestamps are UTC on 19 October)  
**Disposition:** Research only. The model fails the current predictive-quality gate and is **not approved for live trading**.

## Reproducible evidence

- Repository commit under test: \`ba4bab8e46414fd8506b167f6ef8430d2d23abbb\`
- Standard six-gate CI: [Run #908](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39326092291), all six jobs succeeded.
- Real-history training and backtest: [Research Run #3](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39326092277), successful on the same commit. The workflow's offline ML integration suite reported **380 passed in 8.90s**, the full research dependency audit passed, and the public-history download/model/backtest/report steps succeeded.
- Retained model/data/backtest artifact (90-day retention): [nobitex-regime-research-39326092277-1](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39326092277/artifacts/12222590242).
- Artifact ZIP SHA-256: \`808c3239feb9e2fd80a074b1fa52024ed9cae0ad5eea1941f11be3f7a47f8188\`.

The downloaded artifact contains the JSON training report, model manifest, fitted estimator, content-addressed candle CSV and manifest, three per-strategy backtest reports/checksums, test logs and the full research environment audit log. The raw dataset and binary model are intentionally **not committed to Git**; public-data rights have not been independently cleared.

## Data provenance

| Field | Value |
|---|---|
| Venue / market | Nobitex Spot / BTC-USDT |
| Source | Public unauthenticated \`GET /market/udf/history\` |
| Resolution | 60 minutes |
| Requested horizon | 180 days |
| Candles retained | 4,319 |
| Candle opens, UTC | 2026-04-22 21:30 through 2026-10-19 19:30 |
| Detected time gaps | 0 |
| Pages retrieved | 9 |
| Dataset version | \`4b96e3b95339860d0ed040c9461c7caba59a5cca51f8a61c9061eec16e945896\` |
| Candle CSV SHA-256 | \`2d52bc4d0e0f9af7a983d623b8b7590dd6d670ad4e743263cbb1c11599afc8ac\` |
| Dataset manifest SHA-256 | \`c8a589987ffed6ab2cbd8d07496989c14da48e5866f542ddf9ddf1bd444b18c8\` |
| Model artifact SHA-256 | \`b61c1c911ccb368dafffb13a2d7c5e4ba0e69391670118bbabed21a8db99ee43\` |
| Weak-label horizon | 6 candles |
| Partitions | Chronological 60% train / 20% validation-calibration / 20% final holdout |
| Final holdout samples | 858 |
| Market-data rights | \`UNREVIEWED_PROVIDER_TERMS\` |
| Volume interpretation | Assumed base-asset units for simulation; provider semantics remain unverified |

The client used a fixed HTTPS host/path, no credentials or Authorization header, no account/order routes, no redirects, bounded timeout/body/page count and throttled page requests. All 4,319 retained bars passed the client's OHLCV and continuity checks.

## Model and classification result

The classifier is a scikit-learn Random Forest (160 trees, max depth 7, minimum leaf 4, balanced-sub-sample class weights, seed 41). The four target classes are weak labels generated from future six-bar return/volatility thresholds; these are **not verified ground truth labels**. Label thresholds are derived from the training partition only. A scalar temperature is selected on validation negative log loss. The final test block is not used for fitting parameters or calibration.

| Final-holdout metric | Model result |
|---|---:|
| Accuracy | 34.50% |
| Balanced accuracy | 25.40% |
| Macro-F1 | 24.80% |
| Multiclass Brier score | 0.72781 |
| Log loss | 1.34321 |
| Samples | 858 |

The training-only constant-majority-class reference predicts \`range_bound\` for every case and achieved **58.39% accuracy** and **25.00% balanced accuracy** on the same holdout. The trained model therefore **underperformed the naive reference on accuracy** and had only a negligible balanced-accuracy improvement. Its macro-F1 and probability metrics do not justify promotion. The model has not passed a predictive-quality gate and must remain research-only.

## Final holdout portfolio backtest

All three strategies ran over the same chronological test interval using the repository's causal next-open backtester, USDT 10,000 starting equity, 25% allocation, 5% maximum bar participation, 10 bps commission, 10 bps assumed spread and 10 bps assumed slippage. These are explicit simulation assumptions, not observed historical bid/ask or depth.

| Strategy | Net return | Maximum drawdown | Closed trades | Final equity |
|---|---:|---:|---:|---:|
| Trained regime long/flat | −0.2411% | 0.2411% | 1 | 9,975.89 USDT |
| SMA crossover long/flat | −1.3513% | 3.9004% | 12 | 9,864.87 USDT |
| Buy-and-hold reference | +1.4833% | 2.2628% | 0 | 10,148.33 USDT |

The trained regime strategy had only one closed trade and spent approximately 0.12% of the holdout in market. Its lower drawdown should not be interpreted as evidence of a useful predictive edge: its return was negative, exposure was extremely low, and it underperformed buy-and-hold. Buy-and-hold finished with an open position, hence zero closed trades.

## Engineering decision and remaining gates

1. **Do not promote this model into the active Paper or Live runtime by default.** The inference adapter exists, but no active model pointer was changed.
2. Improve target-label validity and class imbalance, gather more time/regime coverage, run repeated walk-forward folds and compare calibrated predictions against meaningful baselines before considering another research promotion.
3. Independently review Nobitex historical-data rights/retention and the \`v\` volume-unit semantics before using this dataset for commercial evaluation or making stronger fill assumptions.
4. Keep the existing live Spot gate disabled. This workflow sent no authenticated request and no real order.
5. The scheduler is a process-local Paper library component, not an installed Windows/Linux service; OS service management, a production feed composition, live account/order/fill reconciliation, risk review and owner-host acceptance remain open.

**Conclusion:** the training pipeline and artifacts are reproducible and the research workflow passed. The current model's holdout performance is insufficient; completing training is not the same as validating predictive value.
