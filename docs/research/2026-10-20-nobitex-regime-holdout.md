# Nobitex Regime Model — Historical Research Record

**Research date:** 20 October 2026 (Azerbaijan time; Actions timestamps are UTC on 19 October)  
**Disposition:** Research only. The model fails the current predictive-quality gate and is **not approved for live trading**.

## Reproducible evidence

- Repository commit under test: `5639f9ad6a1133be814251db0d958fe4d463d06d`
- Standard six-gate CI: push [Run #39329776104](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39329776104) and PR [Run #39329781455](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39329781455), all six jobs succeeded on the same code/test SHA.
- Real-history training and backtest: [Research Run #5](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39329776038), successful on the same commit. The workflow's offline ML integration suite reported **381 passed in 12.74s**, the full research dependency audit passed, and the public-history download/model/backtest/report steps succeeded.
- Retained model/data/backtest artifact (90-day retention): [nobitex-regime-research-39329776038-1](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39329776038/artifacts/12224231419).
- Artifact ZIP SHA-256: `9ae0c56564cf7ac6eca66929ed7d6b491e60e44638fae0070455f84639959224`.

The latest artifact contains the JSON training report, model manifest, fitted estimator, content-addressed candle CSV and manifest, three per-strategy backtest reports/checksums, test logs and the full research environment audit log. Its SHA-256 is `9ae0c56564cf7ac6eca66929ed7d6b491e60e44638fae0070455f84639959224` and it expires on 2027-01-17. The raw dataset and binary model are intentionally **not committed to Git**; public-data rights have not been independently cleared. Historical Run #3 (4,319 candles) remains at [its original run and artifact](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39326092277).

## Data provenance

| Field | Value |
|---|---|
| Venue / market | Nobitex Spot / BTC-USDT |
| Source | Public unauthenticated `GET /market/udf/history` |
| Resolution | 60 minutes |
| Requested horizon | 180 days |
| Candles retained | 4,320 |
| Candle opens, UTC | 2026-04-22 21:30 through 2026-10-19 20:30 |
| Detected time gaps | 0 |
| Pages retrieved | 9 |
| Dataset version | `5a9e1d165b1a9688a726563a1c06bfa96bde3abf8bd6193fa43664227ff67728` |
| Candle CSV SHA-256 | `0d976abd6772fc8dce0578b55d69c9686aabe7ffa0a9329cc46c600349297d84` |
| Dataset manifest SHA-256 | `b769326d0d2b686b2eb91b92910d644e05304a4fc3d8e76d4cddcd7848ddce9b` |
| Model artifact SHA-256 | `38799b767b2aa6c2910c07383c3e213db090d5b8d5c1a1e401ee963173c17f2c` |
| Model manifest SHA-256 | `6491232c7d0b0f475d24887406f71bd008a32424430c2e50fa60387a01f46157` |
| Weak-label horizon | 6 candles |
| Partitions | Chronological 60% train / 20% validation-calibration / 20% final holdout |
| Final holdout samples | 858 |
| Market-data rights | `UNREVIEWED_PROVIDER_TERMS` |
| Volume interpretation | Assumed base-asset units for simulation; provider semantics remain unverified |

The client used a fixed HTTPS host/path, no credentials or Authorization header, no account/order routes, no redirects, bounded timeout/body/page count and throttled page requests. All 4,320 retained bars passed the client's OHLCV and continuity checks.

## Model and classification result

The classifier is a scikit-learn Random Forest (160 trees, max depth 7, minimum leaf 4, balanced-sub-sample class weights, seed 41). The four target classes are weak labels generated from future six-bar return/volatility thresholds; these are **not verified ground truth labels**. Label thresholds are derived from the training partition only. A scalar temperature is selected on validation negative log loss. The final test block is not used for fitting parameters or calibration.

| Final-holdout metric | Model result |
|---|---:|
| Accuracy | 33.92% |
| Balanced accuracy | 25.59% |
| Macro-F1 | 24.66% |
| Multiclass Brier score | 0.72879 |
| Log loss | 1.34498 |
| Samples | 858 |

The training-only constant-majority-class reference predicts `range_bound` for every case and achieved **58.51% accuracy** and **25.00% balanced accuracy** on the same holdout. The trained model therefore **underperformed the naive reference on accuracy** and had only a negligible balanced-accuracy improvement. Its macro-F1 and probability metrics do not justify promotion. The model has not passed a predictive-quality gate and must remain research-only.

## Final holdout portfolio backtest

All three strategies ran over the same chronological test interval using the repository's causal next-open backtester, USDT 10,000 starting equity, 25% allocation, 5% maximum bar participation, 10 bps commission, 10 bps assumed spread and 10 bps assumed slippage. These are explicit simulation assumptions, not observed historical bid/ask or depth.

| Strategy | Net return | Maximum drawdown | Closed trades | Final equity |
|---|---:|---:|---:|---:|
| Trained regime long/flat | −0.2411% | 0.2411% | 1 | 9,975.89 USDT |
| SMA crossover long/flat | −1.3562% | 3.9004% | 12 | 9,864.38 USDT |
| Buy-and-hold reference | +1.4810% | 2.2600% | 0 | 10,148.10 USDT |

The trained regime strategy had only one closed trade and spent approximately 0.12% of the holdout in market. Its lower drawdown should not be interpreted as evidence of a useful predictive edge: its return was negative, exposure was extremely low, and it underperformed buy-and-hold. Buy-and-hold finished with an open position, hence zero closed trades.

## Engineering decision and remaining gates

1. **Do not promote this model into the active Paper or Live runtime by default.** The inference adapter exists, but no active model pointer was changed.
2. Improve target-label validity and class imbalance, gather more time/regime coverage, run repeated walk-forward folds and compare calibrated predictions against meaningful baselines before considering another research promotion.
3. Independently review Nobitex historical-data rights/retention and the `v` volume-unit semantics before using this dataset for commercial evaluation or making stronger fill assumptions.
4. Keep the existing live Spot gate disabled. This workflow sent no authenticated request and no real order.
5. The scheduler is a process-local Paper library component, not an installed Windows/Linux service; OS service management, a production feed composition, live account/order/fill reconciliation, risk review and owner-host acceptance remain open.

**Conclusion:** the training pipeline and artifacts are reproducible and the research workflow passed. The current model's holdout performance is insufficient; completing training is not the same as validating predictive value.


## Latest-run identity and limitations

- The current values above are from Research Run #5 on code/test SHA `5639f9ad6a1133be814251db0d958fe4d463d06d`, not copied from earlier Runs #3 or #4.
- Source revision: `udf-history:BTCUSDT:60:1776891600:1792446311`; 4,320 bars were written, reloaded and checksum-verified as `content_addressed_and_verified`.
- The optional-ML integration suite reported 380 passed. Research `pip-audit` reported no known vulnerabilities; the local `evoryn` distribution itself is not present on PyPI and is not audited as a published PyPI package.
- The latest 90-day artifact is [#12224231419](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39329776038/artifacts/12224231419), ZIP SHA-256 `9ae0c56564cf7ac6eca66929ed7d6b491e60e44638fae0070455f84639959224`; `pytest.log` SHA-256 `133ee3b4290cc8328991f188fdcd18764737178427b0d2d43b39a5f7c8e8f99a`, `research-run.log` SHA-256 `3f416fab7d1589d0cddd3379b69488e7e6a3267726cd15a3f0ce81127380400e`, `pip-audit.log` SHA-256 `d9e3864027f8eb834f89f67998363e1bc6dc66edcd9a5327c71fa616e9fb3df1`. No authenticated account request or real order was made.
