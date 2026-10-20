# Regime Challenger Walk-Forward — Final Evidence

**Disposition: research-only; no model is selected for Runtime.**

- Code/test SHA: `c08096a8ff8c28d78a9879977cc5c30fde2d5f77`
- [Six-gate exact-head CI #39397941134](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39397941134): all six jobs passed.
- [Research run #39397941187](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39397941187): completed successfully on that same SHA.
- [Artifact #12252633238](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39397941187/artifacts/12252633238)
- Artifact ZIP SHA-256: `5d411b9ef13a0e31b5a602e4d03cbd570c0eada8530bcbc9137a552166724d8e`
- Walk-forward report SHA-256: `5900b1c3465ad23b90bce58fcfbe020fbc6b4b8ce295b9d505bf3b0bdb1785a7`
- Training report SHA-256: `df30334ddd2323539f67478fc852b1fe1e89ca1101fa07de69b62a851364bbe6`
- Dataset manifest SHA-256: `ce4bfbf7a78a975e1f0148215e565e781af7772e4d3c942aa1ac4c245a64ec5b`

## Data and validation

Anonymous public Nobitex Spot BTC-USDT 1h history only; 4,319 candles, nine pages, zero gaps. First candle: `2026-04-23T09:30:00Z`; last candle: `2026-10-20T07:30:00Z`; collected at `2026-10-20T09:08:01Z`.

Dataset version: `275bdf9bb7c5049e24b1a7e17944ec3a09db7e831465da56038fdf8daa766420`. Candle CSV SHA-256: `b68b69b0f5dfffbf0e373d97725923a7ba45612dd410b6da93810907ca5c958f`.

UDF volume units remain **unknown**. Volume is excluded from features and volume-based fill caps are disabled, so these simulations are liquidity-unconstrained. Provider data-use/retention rights remain unreviewed. ML-enabled suite: **391 passed in 15.26s**. Dependency audit: no known vulnerabilities in the installed environment.

## Protocol

- Research schema `3.0.0-price-technical-only`, 21 causal price/technical features, 48-bar lookback: multi-horizon returns/volatility, SMA gaps, RSI-14, MACD, Bollinger %B/bandwidth, ATR-14, trend strength, range position and volatility ratio.
- Two label policies: train-quantile forward-return/volatility labels; volatility-scaled first-touch OHLC Triple-Barrier. Six-bar horizon; same-bar double-touch is conservatively labelled high volatility.
- Five model families: Random Forest, HistGradientBoosting, XGBoost, forward-filtered Gaussian HMM, and CPU LSTM using 12 sequential feature rows.
- Ten chronological test folds; both label policies are nested inside each fold. Scalers/model fits/HMM mapping/label thresholds use training partitions only; temperature calibration uses validation only. Test blocks are not used for fitting/calibration.
- Backtest cost assumptions: 10,000 USDT starting equity, 25% allocation, commission 10 bps, spread 10 bps, slippage 10 bps. Unknown volume unit disables capacity limits.

## Out-of-sample classification means

Means over ten test folds. Targets are weak future-outcome labels, not verified economic truth. Train-only majority Accuracy averaged **63.062%** for forward-threshold labels (fold range **41.63%–88.52%**) and **63.445%** for Triple-Barrier labels (fold range **48.80%–75.12%**).

### Forward-threshold labels

| Model | Accuracy | Balanced accuracy | Macro-F1 | Log loss |
|---|---:|---:|---:|---:|
| Random Forest | 47.37% | 27.97% | 26.85% | 1.2182 |
| HistGradientBoosting | 54.50% | 26.12% | 24.87% | 1.1528 |
| XGBoost | 56.65% | 25.39% | 23.08% | 1.1300 |
| Gaussian HMM | 62.01% | 25.63% | 20.48% | 1.0871 |
| CPU LSTM | 31.53% | 22.17% | 20.60% | 1.3944 |

### Volatility-scaled Triple-Barrier labels

| Model | Accuracy | Balanced accuracy | Macro-F1 | Log loss |
|---|---:|---:|---:|---:|
| Random Forest | 40.67% | 35.54% | 30.04% | 1.2172 |
| HistGradientBoosting | 55.79% | 30.05% | 28.72% | 1.1112 |
| XGBoost | 59.04% | 29.99% | 28.37% | 1.0786 |
| Gaussian HMM | 61.77% | 25.95% | 20.65% | 1.0817 |
| CPU LSTM | 33.16% | 33.92% | 27.30% | 1.2824 |

All five candidates’ mean Triple-Barrier Accuracy was below the majority baseline. Some models improved Macro-F1/balanced accuracy, but this did not erase their Accuracy deficit or establish economic performance.

## Cost-adjusted portfolio comparison

Returns are arithmetic means across ten independently reset test windows, not a compounded equity curve.

| Strategy | Mean net return per fold | Closed trades | Fills | Mean estimated costs per fold |
|---|---:|---:|---:|---:|
| Buy-and-Hold reference | **+0.5629%** | 0 (positions remained open at test ends) | 10 | same assumptions |
| SMA crossover | -0.0454% | 31 | per-fold reports | same assumptions |
| Random Forest / Triple-Barrier | -0.2406% | 18 | 37 | 22.98 USDT |
| HistGradientBoosting / Triple-Barrier | -0.2098% | 17 | 34 | 21.03 USDT |
| XGBoost / Triple-Barrier | -0.1604% | 12 | 25 | 15.52 USDT |
| Gaussian HMM / Triple-Barrier | 0.0000% | 0 | 0 | 0.00 USDT |
| CPU LSTM / Triple-Barrier | about +0.0004% | 3 | 6 | 3.75 USDT |

The LSTM result is near zero because it barely took exposure, not because it found an edge. HMM made no trades. No candidate exceeded Buy-and-Hold’s mean return.

## Acceptance gate and confidence intervals

Promotion requires positive lower 95% paired-fold bootstrap bounds for Accuracy, balanced Accuracy and Macro-F1 gains versus the training-only majority baseline; positive lower bound for net return versus same-fold Buy-and-Hold; at least 20 closed trades; and complete fold coverage. The bootstrap is descriptive because temporal samples may remain dependent.

| Model | Accuracy delta 95% CI vs majority | Macro-F1 delta 95% CI | Net-return delta 95% CI vs B&H | Closed trades | Gate |
|---|---:|---:|---:|---:|---|
| Random Forest | [-30.29, -15.89] pp | [+5.87, +13.83] pp | [-2.1635, +0.2094] pp | 18 | **Fail** |
| HistGradientBoosting | [-12.15, -4.11] pp | [+6.84, +10.66] pp | [-2.1495, +0.3064] pp | 17 | **Fail** |
| XGBoost | [-8.61, -1.24] pp | [+5.60, +10.82] pp | [-2.1047, +0.3184] pp | 12 | **Fail** |
| Gaussian HMM | [-4.40, -0.10] pp | [-0.08, +1.64] pp | [-1.9618, +0.4576] pp | 0 | **Fail** |
| CPU LSTM | [-37.89, -22.68] pp | [+1.87, +12.54] pp | [-1.9650, +0.4550] pp | 3 | **Fail** |

Every Accuracy interval is below zero; all return-difference intervals span zero and have a negative lower bound. No model meets the trade threshold. The report records `selected_model_for_runtime=null` and `runtime_promotion_allowed=false` for every candidate.

## Runtime and Windows status

No model is connected to Runtime. Research schema v3 is not compatible with the existing production loader contract. Fresh untouched confirmation data, an independently pinned schema-compatible loader and independent model-risk review remain required.

The present `PaperRuntimeScheduler` is process-local. Windows Task Scheduler registration, an OS-level single-instance mutex, an unattended composition root, crash/recovery tests and native Windows acceptance are **not implemented or verified**. ADR 0019 is design-only. No local Windows host was accessed and no task was registered.

The workflow used no account endpoint, credentials, live order endpoint or exchange-write route.
