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

## Research limits and release status

- Nobitex market-data use/commercial rights have not been independently reviewed. The report
  deliberately records the license state as UNREVIEWED_PROVIDER_TERMS.
- Backtest liquidity uses the OHLCV v field as base-asset volume; provider volume semantics must be
  confirmed before interpreting simulated fills.
- Spread and slippage are assumptions because OHLCV does not reconstruct historical bid/ask/depth.
- No performance metric on a single historical holdout establishes future profitability. Data-derived
  labels can be noisy, and a holdout may contain a different regime mix from future markets.
- Model artifacts use joblib/pickle-compatible serialization. Only load artifacts fetched from a
  trusted, verified workflow artifact; an adjacent manifest hash alone is not an authenticity
  signature against an attacker who can replace both files.
- The workflow makes no authenticated account request and sends no real orders. Model output remains
  advisory and the live order-write gate remains disabled.
