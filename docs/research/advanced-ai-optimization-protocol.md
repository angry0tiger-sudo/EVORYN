# Advanced AI Optimization — Research Protocol

**Status:** implementation candidate on feat/ai-advanced-optimization; empirical result pending.  
**Runtime disposition:** selected_model_for_runtime = null; no Champion or Runtime promotion.

## Latest exact-head execution checkpoint — 2026-10-21

- Exact code SHA: `292c1544467e1d434b5f39977e8b444ca63d5418`. All seven required CI jobs passed in [CI Run #39541721607](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39541721607).
- Research workflow [Run #39541721758](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39541721758) succeeded end-to-end: dependencies, Ruff, mypy, Bandit, offline tests (`422 passed, 2 skipped`), pip-audit, public-history download, walk-forward meta-label evaluation and artifact upload. Transport defaults are a 20-second timeout, up to 2 retries (3 attempts total), 0.5-second exponential backoff and fail-closed immediate handling for TLS certificate-verification errors. Failure diagnostics classify DNS, timeout, TLS/certificate, connection and other network failures, with sanitized detail. No transport warning appeared in this successful run, so the root cause of the earlier `URLError` incident remains unconfirmed.
- Artifact #12320006168; ZIP SHA-256 `dfda0241534d3b55df5bdeae1143b573baccfc536c4834ebb6040306a0b4c8ef`. The artifact includes the immutable data CSV and manifest, both reports and sidecar digests, diagnostics, and all report-referenced model files. Recomputed ZIP, report, sidecar, CSV, manifest and all 22 model-artifact checksums matched.
- Dataset: `nobitex-btc-usdt-60m`, version `c9f86ce7df23ea3e3c09eaa8e6e8a874b3c3943c52b68b8be0f3ba00f609dfe8`; 11,438 H1 candles, 23 pages, zero gaps, `2025-07-01T13:30:00Z` through `2026-10-21T02:30:00Z` (about 477 days despite 730 days requested). CSV SHA-256 `58bf4c5b63333f72ec3d20197a4f9258a5aa24dd55b36503f570cf3afba2f011`; manifest SHA-256 `f2552331d1763d0cb8be5cd69c825cc33c3a1629defbfe675b8c90bb32b7e9da`; training report SHA-256 `bb29b3b377df1b4969af28d0e202c96148c6c85a2f4754997dc4d5435c0caaef`; advanced report SHA-256 `4ae340f2f2ce9e55533666b661ca6a00cab4419f7eda7d996fd2849a22358a53`.
- Experiment ID `770544672acc9035de7a24e69832aa4b847a25c0726db9e1dcbbe87a70756114`. Expected folds 13; evaluated folds 11 for each model. Folds 8 and 9 were not evaluated because post-purge train/validation event class support was insufficient. Pooled test: 42 events, positive prevalence 0.095238. Pooled AP was `0.329165` for XGBoost and `0.121343` for LightGBM. Both are above prevalence, but the classification gate still fails because full 13/13 fold coverage is mandatory. Both models abstained with threshold `1.01` in all 11 evaluated folds, made 0 closed trades and achieved mean net return 0.0.
- Mean net-return differences vs Buy-and-Hold, SMA state, and SMA entry-event baselines were `-0.4414%`, `-0.1278%`, and `-0.2354%`, respectively. Paired-fold bootstrap 95% lower bounds were all negative: XGBoost `-0.018651`, `-0.012116`, `-0.012850`; LightGBM `-0.019432`, `-0.012288`, `-0.012965`. Both classification and economic gates failed. Final disposition: **reject both candidates; no Champion; `selected_model_for_runtime = null`; no Runtime promotion; no live trading**.
- Volume semantics remain `unknown`; volume is excluded from features and capacity limits remain disabled. No verified historical order book is available, and spread/slippage are assumptions. The public provider terms remain unreviewed.

### Historical transport incident (prior SHA)

- On SHA `4343d637f2bfd6536566a8b2d23a395f37bca433`, [Run #39493546367](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39493546367) failed in two attempts before model evaluation with `Historical UDF GET failed with URLError`. The newer transport diagnostics/retry code allowed a later fetch to complete, but no error category was emitted during that successful fetch, so it does not prove whether the earlier fault was DNS, TLS, timeout, or another transient condition.

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
