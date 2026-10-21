# Causal Market Regime Detection — Research Protocol

**Disposition: research-only; the model is not connected to Runtime and is not approved for trading.**

## Why this is a different experiment

The earlier meta-label study attempted to filter SMA entries and failed its classification/economic acceptance gates. It executed zero closed trades for both XGBoost and LightGBM: every evaluated validation threshold abstained, and two of thirteen expected folds lacked adequate event/class support after purging. That is a genuine negative result, not a cue to weaken acceptance rules.

This experiment changes the target. It classifies the **descriptive current state** from completed price history: **BULL_TREND**, **BEAR_TREND**, or **RANGE**. It does not train on future returns and does not claim to predict the next price move.

## Data and causal feature contract

The runner loads the content-addressed snapshot produced by the bounded public Nobitex hourly-history fetch. Dataset version, candle digest, source revision, provider terms, evaluation ranges, model artifact hashes, and report digests are recorded with the results.

Ten causal price-only features are built at each closed-candle index: 1/6/24/72-bar returns, 24-bar realized volatility, gaps to SMA-12/SMA-48, position within the trailing 24-bar high-low range, 24-bar path efficiency, and a 24-bar log-price regression return. The feature builder rejects non-hourly/gapped input, short histories, and non-finite values. A prefix-invariance test ensures appending future candles does not change an earlier feature row.

Volume is deliberately excluded because the available UDF volume unit remains unverified. Historical order-book depth is not available. Simulated spread/slippage therefore remain assumptions and the backtest disables volume participation limits; results are not liquidity-realistic. Provider data-use terms remain unreviewed.

## Weak target definition

For each fold, fit a directional threshold on the absolute trailing 24-bar return in that fold's training rows only: the 55th percentile, floored at 0.2%. A row is labelled bullish when its trailing 24-bar return reaches the positive threshold, bearish when it reaches the negative threshold, and range otherwise. This threshold is frozen for the fold and then applied to its other time blocks.

The target is a transparent mechanical definition, not human-verified ground truth. Good classifier metrics mainly show that a model reproduces this definition; they do not establish predictive alpha or prove that traders would agree with the state labels.

## Model families and anti-leakage controls

- **Gaussian HMM (3 hidden states):** standardization is fit only on training features. Hidden states are mapped to the three descriptive labels using training data only. Validation/test observations use an explicit forward filter, not forward-backward smoothed posterior probabilities, so a test state's inferred probability cannot depend on later candles.
- **XGBoost (3-class classifier):** fit only on the fold training rows, with a fixed seed and bounded CPU settings. No future-return target is supplied.
- **Training-majority classifier:** evaluation reference for classification metrics.
- All test windows are chronological and non-overlapping. Each fold has a train block, a validation buffer, and a locked test block. Validation is not used to fit the scaler, model, HMM state mapping, or label threshold.

Reported classification metrics include accuracy, balanced accuracy, macro-F1, per-class support/recall/precision/F1, and the confusion matrix. A classifier passes the research classification gate only if test-fold coverage is complete and both mean balanced accuracy and mean macro-F1 exceed the training-majority baseline.

## Strategy hypothesis and economic gate

The strategy is long/flat for Spot. It allows SMA-12/48 exposure only if the current predicted state is bullish and SMA-12 exceeds SMA-48. In bearish or range states it holds flat; no shorting or margin trading is simulated. The comparison uses the existing unfiltered SMA-12/48 strategy and Buy-and-Hold, over identical test windows and with the same 10 bps commission, 10 bps spread, and 10 bps slippage assumptions. Allocation is 25% and volume capacity limits are disabled because volume units are unknown.

The economic gate requires complete fold coverage, positive average net return, at least 20 closed trades, and positive lower 95% paired-fold bootstrap bounds on return differences versus both unfiltered SMA and Buy-and-Hold. These fold bootstrap intervals are descriptive and do not prove independence of time-series samples.

## No automatic promotion

Regardless of classification results, this experiment hard-codes selected_model_for_runtime = null, runtime_promotion_allowed = false, and the release disposition RESEARCH_ONLY_NO_CHAMPION_NO_LIVE_TRADING. Successful training, model artifact creation, or green CI does not authorize Runtime integration, owner-host changes, credentials, or live orders.

## First empirical checkpoint — 2026-10-21

Research run [#39544088577](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39544088577) completed successfully on code SHA `c88eef2dd238edd5e84395c77accb30286894f91`. Exact-source standard CI [#39544088508](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39544088508) passed all seven jobs. Research covered dataset `nobitex-btc-usdt-60m`, version `36a69c6e0c3dd1a25427869fa9a1ca7b7126f0059594e69805e6b9bca6d63ef0`, 11,438 H1 candles and five non-overlapping test folds per family (4,575 test samples each).

- HMM: mean balanced accuracy 89.76%, macro-F1 82.42%, but regime-gated SMA mean net return `-1.9954%` per test fold and 111 closed trades. Lower 95% paired-fold bounds versus unfiltered SMA and Buy-and-Hold: `-2.9919%` and `-5.4534%`.
- XGBoost: mean balanced accuracy 99.73%, macro-F1 99.79%, but regime-gated SMA mean net return `-2.3676%` per test fold and 122 closed trades. Lower 95% paired-fold bounds versus unfiltered SMA and Buy-and-Hold: `-3.7861%` and `-5.2291%`.
- The unfiltered SMA averaged `-0.1999%`; Buy-and-Hold averaged `+0.9950%`. Both candidate classification gates passed, but both economic gates failed. No Champion was selected.

**Interpretation warning:** labels are produced by thresholding the trailing 24-bar return, and `return_24` is directly included among the model inputs. The high XGBoost classification score therefore largely reflects reproducing the deterministic weak-label rule; it is not evidence of future-price forecasting or tradable alpha. Independent/annotated ground-truth regimes and stronger economic validation would be needed in any next experiment.

Verified artifact: [run #39544088577 artifact ZIP](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39544088577/artifacts/12320553643), artifact ID `12320553643`, ZIP SHA-256 `6f63c0a2e3d029163a8c6c42eef82f6c46a66b4b10cc44d44a15273c0166bc11`. Report SHA-256 `1ab2024cb11d64bed6056e7031e623881ad64c2f20b040d1713f1a349f20ae17`; manifest SHA-256 `a0ffa51ac92a7c70ab0febd01343f560da0ed29fd9bd3e8efe451dd85b458fbd`. Dataset volume units remain unknown, historical order-book depth is absent, and provider data-use terms are unreviewed.

The release disposition remains `RESEARCH_ONLY_NO_CHAMPION_NO_LIVE_TRADING`; selected_model_for_runtime is null and runtime_promotion_allowed is false.
