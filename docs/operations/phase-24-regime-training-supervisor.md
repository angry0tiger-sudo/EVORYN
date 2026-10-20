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

The latest successful pipeline is [Research Run #5](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39329776038) on source/test SHA 5639f9ad6a1133be814251db0d958fe4d463d06d. Standard six-gate CI passed on that same code/test SHA in push [Run #39329776104](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39329776104) and PR [Run #39329781455](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39329781455). The standard dependency-light environment reports 379 passed and two optional-ML skips; the research workflow installs ML extras and reports **381 passed in 12.74s**, clean dependency audit results for published dependencies, successful live public-history retrieval, real-estimator fitting, final holdout evaluation, three backtests and artifact publication.

- Dataset: 4,320 contiguous 1-hour BTC-USDT Spot bars, 9 pages, first candle open 2026-04-22 21:30 UTC, last candle open 2026-10-19 20:30 UTC; gap count 0. Source revision: udf-history:BTCUSDT:60:1776891600:1792446311.
- Dataset ID nobitex-btc-usdt-60m, version 5a9e1d165b1a9688a726563a1c06bfa96bde3abf8bd6193fa43664227ff67728; candle SHA-256 0d976abd6772fc8dce0578b55d69c9686aabe7ffa0a9329cc46c600349297d84; dataset-manifest SHA-256 b769326d0d2b686b2eb91b92910d644e05304a4fc3d8e76d4cddcd7848ddce9b.
- Training report SHA-256 04b750c2a5c9ead108d8c7bd0c78d35babc85f896cddc56a822ec46ccd4ca341; model SHA-256 38799b767b2aa6c2910c07383c3e213db090d5b8d5c1a1e401ee963173c17f2c; model-manifest SHA-256 6491232c7d0b0f475d24887406f71bd008a32424430c2e50fa60387a01f46157.
- Artifact [#12224231419](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39329776038/artifacts/12224231419), SHA-256 9ae0c56564cf7ac6eca66929ed7d6b491e60e44638fae0070455f84639959224, expires 2027-01-17.
- Final holdout (858 samples): accuracy 33.92%, balanced accuracy 25.59%, macro-F1 24.66%, Brier 0.72879, log loss 1.34498. Train-only majority-class baseline: 58.51% accuracy, 25.00% balanced accuracy.
- Same-cost holdout backtests: trained regime -0.2411% return / 0.2411% max drawdown / 1 closed trade; SMA crossover -1.3562% / 3.9004% / 12 closed trades; buy-and-hold +1.4810% / 2.2600% / 0 closed trades (2 fills; position remains open). Assumptions are USDT 10,000 initial equity, 25% allocation, 5% maximum prior-bar participation, and 10 bps each for commission, estimated spread and slippage.
- The offline test also fits a synthetic model and runs it through Mock Paper runtime, Risk Engine, Paper fill, reconciliation and SQLite ledger. The actual trained historical model is not installed as the runtime default; its holdout performance fails the predictive-quality gate.

Save/reload/checksum verification of the content-addressed dataset, independently pinned model/manifest loader checks, optional-ML integration suite and artifact publication all passed. Data-use rights and UDF volume-unit semantics remain unreviewed; historical spread/depth are not reconstructible from OHLCV. Runs #3 and #4 remain historical and do not define the current dataset/model/report identity.

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


## Follow-up: Volume semantics and walk-forward validation

The UDF history contract does not establish whether v is base-asset or quote-asset volume. Research data therefore uses an explicit unknown unit until verified. Price-only regime features and disabled volume-capacity simulation are the fail-closed default; those backtests must be described as liquidity-unconstrained. The follow-up workflow compares forward-threshold labels with volatility-scaled triple-barrier weak labels and Random Forest, HistGradientBoosting and a Gaussian HMM over rolling chronological folds. HMM test probabilities are forward-filtered, not backward-smoothed; training thresholds and feature scalers are fit on train only and calibration temperature on validation only. Each fold's metrics and backtests are kept separately.

Windows Task Scheduler supervision is currently design-only. No task has been registered and no local Windows host has been touched. Production feed composition, account/reconciliation, trusted artifact delivery, independent risk/security review and owner-host acceptance remain prerequisites.
