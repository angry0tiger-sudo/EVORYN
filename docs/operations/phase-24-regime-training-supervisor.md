# Phase 24 — Historical Regime Research and Process-Local Scheduler

## Public market-history source

The research runner uses the documented Nobitex public OHLCV UDF GET route:
https://apiv2.nobitex.ir/market/udf/history. Requests are fixed to this HTTPS host/path, do not
carry Authorization or exchange credentials, disable ambient proxies, reject redirects, and are
bounded by timeout, page count, request rate, response bytes and requested time range. The API
returns at most 500 bars per page; the client combines pages, filters forming/future bars, rejects
malformed arrays/conflicting timestamps, and refuses gaps or a truncated page series.

This route fetches public candles only. The standard six-gate CI remains offline. A separate GitHub
Actions research workflow installs the optional regime extra and downloads real public history only
for the scoped research workflow. It does not call account, order, transfer, withdrawal or collateral
endpoints and has no secrets configured.

## Training and temporal evaluation

- Four weak regime labels are generated from future returns and forward realized volatility. They
  are not manually verified market-state labels and must not be presented as ground truth.
- A Random Forest trains only on the initial chronological 60% of history. Trend/volatility label
  thresholds are fitted only on training samples.
- Six-bar forward outcomes are purged at partition boundaries. The next 20% is used only for
  scalar temperature calibration and calibration metrics; the final chronological block is held
  out for classification evaluation.
- Holdout metrics include accuracy, balanced accuracy, macro-F1, multiclass Brier score and log
  loss, alongside a training-only majority-class baseline.
- The same final holdout interval is backtested against a trained-regime long/flat policy, a simple
  moving-average crossover and buy-and-hold, using the repository's causal next-open backtester,
  explicit participation cap and explicit commission/spread/slippage assumptions.
- Dataset version, candle checksum, model-artifact checksum, split boundaries, label policy,
  calibration values and immutable backtest run IDs are written to JSON. Dataset/model/report files
  are uploaded as a GitHub Actions artifact rather than committed to the repository.

## Process-local scheduler

PaperRuntimeScheduler serializes one bounded runtime step per interval, uses a cooperative stop
event, avoids catch-up bursts, terminates on RuntimeStatus.HALTED, supports an explicit cycle limit,
and reports cycle count/exit reason. A caller can connect service termination signals to
request_stop(). PaperRuntimeSupervisor reconstructs the Paper runtime from its journal after an
exception, but any persisted halt or unresolved operation remains fail-closed.

This is a library component, not an always-running daemon or Windows service. It does not autonomously
construct a market-data provider or launch an exchange-backed runtime. A production feed composition,
OS-level service installation, log/health monitoring, startup policy, independent review and recovery
drills remain separate release gates.

## Latest observed real-history run

The latest successful workflow on code/test SHA `f9f634553f8c6edb2b173b7b885ad2d9cf34330c` completed the 180-day BTC-USDT hourly pipeline. Standard six-gate CI [Run #39328332810](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39328332810) passed all six jobs (379 passed, one optional-ML skip). On the same code/test SHA, public-history research [Run #4](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39328332540) passed 380 tests in 7.78s with the research dependencies installed, passed the research dependency audit, fetched public OHLCV history, trained the model, evaluated the final holdout and published artifacts.

- Dataset: 4,320 contiguous hourly BTC-USDT bars, 9 pages, 2026-04-22 21:30 to 2026-10-19 20:30 UTC; gap count 0.
- Dataset version: `c1a13b3533f3777fa743a8fd6632d73b1760b355341fa1537b7a5ef6ab24a616`; candle SHA-256: `0d976abd6772fc8dce0578b55d69c9686aabe7ffa0a9329cc46c600349297d84`; dataset-manifest SHA-256: `6054e2276864a69497a57f7953ab66b43b9f04bb192a3466303d9fa9d5905651`.
- Model SHA-256: `38799b767b2aa6c2910c07383c3e213db090d5b8d5c1a1e401ee963173c17f2c`; retained [artifact #12223262662](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39328332540/artifacts/12223262662).
- On 858 final-holdout samples: accuracy 33.92%, balanced accuracy 25.59%, macro-F1 24.66%, Brier 0.72879, log loss 1.34498. The train-only majority-class baseline reached 58.51% accuracy and 25.00% balanced accuracy.
- Same-cost holdout backtests: trained-regime long/flat -0.2411% return / 0.2411% max drawdown / 1 closed trade; SMA crossover -1.3562% / 3.9004% / 12 closed trades; buy-and-hold +1.4810% / 2.2600% / 0 closed trades (position remains open). These are simulation estimates, not profitability evidence.

The save/reload/checksum verification of the content-addressed dataset, independently pinned model/manifest loader tests, optional-ML integration suite and artifact publication all passed on this code head. The model still fails the predictive-quality gate and is not promoted into Paper or Live defaults. Historical Run #3 on `ba4bab8e46414fd8506b167f6ef8430d2d23abbb` remains retained at [Run #3](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39326092277) and [its artifact](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39326092277/artifacts/12222590242); its older 4,319-candle dataset is historical and is not the current metric identity.

## Research limits and release status

- Nobitex market-data use/commercial rights have not been independently reviewed. The report
  deliberately records the license state as UNREVIEWED_PROVIDER_TERMS.
- Backtest liquidity uses the OHLCV v field as base-asset volume; provider volume semantics must be
  confirmed before interpreting simulated fills.
- Spread and slippage are assumptions because OHLCV does not reconstruct historical bid/ask/depth.
- No performance metric on a single historical holdout establishes future profitability. Data-derived
  labels can be noisy, and a holdout may contain a different regime mix from future markets.
- Model artifacts use joblib/pickle-compatible serialization. Only load artifacts fetched from a
  trusted, verified workflow artifact. The loader requires independently pinned SHA-256 digests for
  both model and manifest before deserialization, then checks the embedded digest, model version,
  feature schema, estimator class order and feature dimension. The pins must come from a trusted
  review/release record rather than the adjacent manifest; hashes in two replaceable files are not
  an authenticity signature against an attacker who can replace both files.
- The workflow makes no authenticated account request and sends no real orders. Model output remains
  advisory and the live order-write gate remains disabled.
