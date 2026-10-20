# Advanced AI Optimization — Research Protocol

**Status:** implementation candidate on feat/ai-advanced-optimization; empirical result pending.  
**Runtime disposition:** selected_model_for_runtime = null; no Champion or Runtime promotion.

## Latest exact-head execution checkpoint — 2026-10-21

- Code SHA: `4343d637f2bfd6536566a8b2d23a395f37bca433`. Exact-head PR CI [Run #39493555023](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39493555023) passed all seven required jobs.
- Advanced research workflow [Run #39493546367](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39493546367) was attempted twice on that exact SHA. Both attempts passed the research pre-network gates (Ruff, mypy, Bandit, pytest: 417 passed and 2 skipped, and pip-audit reporting no known vulnerabilities) but failed in the public-history fetch step with `Historical UDF GET failed with URLError`. The model evaluation step was skipped.
- Attempt 1 artifact #12296199968 has ZIP SHA-256 `412e734e3301e6947ee43a66c6bf3bdec78f6ce9cfe7f3ff1e99739243a7d875`; attempt 2 artifact #12296999003 has ZIP SHA-256 `960cb4831ba5bd1c3029bd1cc2e8b975fe40c79a9785a5780a717cc0a9c7a751`. Both were inspected and contain diagnostics only, with no dataset, manifest, training report, advanced report or model files.
- The exception wrapper does not preserve the underlying `URLError` reason. The available evidence does not distinguish DNS, TLS, timeout, or another transport cause. No current-code AP, trade, net-return or baseline comparison exists. The older first-run result remains a rejected historical result for its own SHA and is not evidence for this threshold revision.
- Disposition: **empirical qualification pending; no Champion; no Runtime promotion; no live trading.** Do not infer model quality from a run that never reached model evaluation.

## Objective

Improve prior weak Nobitex Spot BTC-USDT regime/challenger research without changing live execution boundaries. Research uses public hourly OHLC history through the fixed unauthenticated GET-only UDF route. It does not use account endpoints, API secrets, order routes, or exchange writes. A successful code check is not evidence of a profitable strategy.

## Data coverage

- The public historical-fetch config is bounded to at most 40 pages and 500 rows per page. The trainer supports a 30–730 calendar-day request, with a 730-day default in the manual advanced workflow.
- Two years at 1-hour resolution is approximately 17,520 observations and is within the nominal 20,000-row page budget. Actual provider retention, returned history span, page overlap, gaps and rate behavior must be verified from the resulting immutable manifest; old data is not assumed to exist.
- The advanced manual workflow has workflow_dispatch only. It does not start a public-data run on every push or pull request.
- Higher-timeframe context is derived from completed hourly candles so that 4-hour and daily bars are only considered once their close boundary has passed. The code uses the last completed aggregated bar; it does not independently download and merge multiple provider streams.
- The UDF v field is retained as volume_unit="unknown". Volume is excluded from the advanced feature matrix and the backtest's volume-capacity constraint is explicitly disabled. Consequently results do not establish actual execution capacity and can be optimistic.
- No historical order-book/depth source has been verified for this path, so order-book features are not fabricated or included. Spread and slippage remain explicit assumptions, not measured historical bid/ask data.
- Fibonacci features are distances from a rolling 48-hour high/low range. Swing features are causal, confirmed-pivot shape proxies. They are not a claim to have solved subjective Elliott-wave counting.

## Features and model path

The versioned 27-column feature schema combines returns, realized volatility, Bollinger %B/bandwidth, ATR-normalized range, rolling-range Fibonacci distances, confirmed-swing counts/direction/amplitude and completed 4-hour/daily trend context. Features are calculated at the hourly close and are expected to be prefix-invariant. The first empirical iteration showed insufficient event support under the original 40%/10%/3.5% split and a fixed 0.55 threshold. The current iteration uses 35% train, 15% validation and 3% non-overlapping test blocks to increase validation support while retaining at least 10 expected folds on the current snapshot.

select_features_train_only uses mutual-information ranking from the fold's training events only. Validation/test rows are not provided to feature selection. The primary strategy creates a long entry event when SMA(12) crosses above SMA(48); a binary meta-label indicates whether the next-open-to-six-bar-close return exceeds the declared 50 bps round-trip label-cost hurdle. Events whose outcome windows overlap are purged chronologically.

For each rolling fold and each of XGBoost and LightGBM:

1. Fit feature selection only on the fold's train events.
2. Run a seeded Optuna TPE search, bounded by the manual workflow input, to maximize average precision on chronological validation events.
3. Refit the fixed selected features and winning hyperparameters on train plus validation after tuning.
4. Fit the final fold model on train only. Select the probability threshold on validation predictions using a fixed threshold grid and the summed after-cost event return; require at least max(3, ceil(20% of validation events)) selected events. If no candidate cutoff has positive summed validation net return, use threshold 1.01 and deliberately abstain.
5. Evaluate only the later test window using the existing causal backtest, next-open fills, and explicit commission/spread/slippage assumptions.
6. Record exact dataset version, fold ranges, selected features, Optuna parameters/trial count, validation-selected threshold, model artifact SHA-256, per-fold metrics and paired baselines.

The primary 50 bps label threshold and threshold candidate grid are fixed before test evaluation. Both Optuna settings and the decision threshold use only the chronological validation partition; that shared development partition can still be overfit, so test blocks remain locked and the reported classification gate is pooled average precision across all out-of-sample test events versus pooled positive prevalence, with at least 30 test events and both classes. Test observations do not set features, labels' thresholds, feature rankings, tuning parameters or probability threshold. Test-fold blocks are non-overlapping. The last 10% of the data is reserved and is not used in these folds. This reserve is not automatically declared an independently untouched project-level confirmation set.

## Comparator and pass gates

Each fold reports buy-and-hold, the existing SMA(12/48) state baseline, and an unfiltered SMA entry-event baseline that is reset flat at each test boundary. Meta-label classification requires test-event average precision to exceed the positive-label prevalence in every fully covered fold. Economic acceptance requires all expected folds (at least 10), positive mean net return after simulated costs, at least 20 closed trades, and positive lower 95% paired-fold bootstrap bounds against buy-and-hold, the state-SMA baseline and the unfiltered SMA entry-event baseline.

A missing/incomplete fold, insufficient label classes/events, missing economic report, low trade count, non-positive mean net return, or confidence interval touching/crossing zero fails the empirical gate. Paired-fold bootstrap intervals are descriptive and do not eliminate possible temporal dependence.

Even if a future experiment passes the empirical gates, selected_model_for_runtime remains null until separate untouched confirmation, independent model-risk review, schema-compatible loader work, runtime integration and owner-approved release gates are completed.

## Deliberate exclusions

- No Champion selection, runtime model loading or changes to deterministic risk limits.
- No live connection, authentication, order submission or local Windows installation.
- No volume-based fills while unit semantics are unknown.
- No guessed historical order-book data.
- No manually annotated or backward-looking Elliott-wave labels.
- No automatic data fetching on ordinary feature-branch pushes.
