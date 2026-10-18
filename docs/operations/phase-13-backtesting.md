# Phase 13: Historical Datasets, Baselines and Backtesting

## Current scope and safety

Phase 13 consists of three independent parts:

- `evoryn.learning_lab.historical_data`: validates provenance-rich candle datasets and stores
  immutable, SHA-256-addressed manifests and CSV files.
- `evoryn.strategies.baseline`: provides deterministic SMA crossover and SMA trend rules.
- `evoryn.learning_lab.backtest`: simulates long/flat Spot exposure with explicit transaction
  costs, next-open execution, prior-completed-bar liquidity limits, time splits and walk-forward.

There is no network client, data downloader, exchange credential, margin logic, AI model or live
order path in these modules. The caller must supply candles and truthful source/license metadata.
No licensed real market dataset is embedded in the repository.

## Registering historical data

Construct a `HistoricalDataset` with:

- `dataset_id`: a safe logical identifier, not a filesystem path.
- `Market`, fixed candle interval, and explicit `volume_unit` (`base_asset` or `quote_asset`).
- `source_name`, `source_uri`, `source_revision`, and a timezone-aware `collected_at`.
- `license_id` and `license_uri` describing the actual right to store and evaluate the data.
- A tuple of validated `MarketCandle` records whose market, interval, source and timestamps
  match the manifest. Candle timestamps mean the start/open time of the candle.

The store canonicalizes times to UTC and serializes OHLCV prices/volume as Decimal strings.
Dataset version is SHA-256 over canonical metadata and the candle-file digest. Its layout is:

```text
<root>/<dataset_id>/<dataset_version>/manifest.json
<root>/<dataset_id>/<dataset_version>/candles.csv
```

The manifest holds the source/license references, source revision, collection time, market,
interval, timestamp semantics, gap policy, candle count and first/last candle. A data change,
license change, source-revision change or provenance change creates a different version.
Tampering, incomplete existing versions and conflicting bytes are rejected. The store never
fetches data and never assumes that a public data URL permits machine use or persistence.
An authorized canonical CSV export can be imported with
`dataset_from_canonical_csv(...)`, which requires explicit market, interval, source/revision,
license and collection-time parameters and rejects any candle that was not closed by that
collection time.

Gaps are rejected by default. They can only be recorded when `allow_gaps=True` is explicit in
the dataset and `BacktestConfig.allow_gaps=True` is separately enabled for a run.

## Baseline strategy contracts

### SMA crossover

Hold long when the trailing fast simple moving average exceeds the trailing slow SMA; otherwise
remain flat. The warm-up period is flat. Both windows are fixed parameters recorded in each run.

### SMA trend

Hold long when the latest completed close exceeds its trailing SMA; otherwise remain flat. The
window is fixed and recorded.

Both strategies return only target exposure zero or one. They do not import the Intelligence
package, do not access future candles, and do not authorize orders. They are control baselines,
not optimized strategies and not claims of profitability.

## Causal timing and fill rules

1. Each candle timestamp is its opening time; its close becomes known at timestamp + interval.
2. At the next candle open, the engine passes only earlier completed candles to the strategy.
3. A changed target may create a simulated market order, eligible at that next open.
4. Execution price applies the configured half-spread plus adverse slippage to the next open.
5. The fill cap is max_bar_participation × the base-asset volume of the latest completed candle.
   That volume unit is explicit in the dataset metadata. Quote-volume datasets are rejected by
   the simulator rather than silently treated as base volume. The cap is known before the new
   open and avoids using the current candle's full future volume to authorize an open-time fill.
6. Unfilled quantity remains pending across bars. If the target flips, remaining quantity is
   canceled and any necessary opposite action is separately simulated.
7. Buy quantity is reduced if current cash at the observed execution price cannot cover notional
   plus commission. Sells cannot exceed the currently held long position.
8. At each bar close, account equity is cash plus held quantity times that close.

The prior-bar volume cap is an explicit OHLCV proxy. Real exchange queue position, exact intrabar
volume timing, spread and slippage are not inferred from high-low range. For strong validity,
register licensed quote/tick data or run sensitivity bands for these assumptions.

## Required simulator assumptions

Every `BacktestConfig` requires values for initial cash, commission_bps, spread_bps,
slippage_bps, max_bar_participation, allocation_fraction, and quantity_step. These values are
part of the run fingerprint and report. Avoid silently using a default fee or a claimed spread
derived from candle volatility.

## Time-separated evaluation

`chronological_split` creates contiguous, disjoint train/validation/test index ranges; defaults
are 60% / 20% / 20%. They are independent evaluation segments with their own initial cash. The
validation/test strategy may use only earlier bars as trailing-indicator warm-up. The SMA rules
are not trained in the train split; future learned strategies must define training strictly
inside the train window.

`make_walk_forward_folds` creates rolling train/validation/test windows. Test windows must be
non-overlapping and occur after the corresponding train and validation windows. Each test result
begins with a fresh simulated account and reports its own metrics; do not add per-fold drawdowns
or expectancy as if capital were continuously compounded.

## Report definitions

| Metric | Definition |
|---|---|
| Net return | (final marked equity - initial equity) / initial equity |
| Maximum drawdown | Largest running-peak decline divided by that peak, including initial equity |
| Expectancy | Mean net PnL per completed position round trip after commissions; null if none |
| Turnover | Sum of executed fill notional in quote currency |
| Turnover multiple | Turnover / initial equity |
| Closed trades | Completed long-to-flat episodes, not each individual fill |
| Fill count | Individual simulated fills; separate from order count |
| Partial fill count | Fill events that leave residual order quantity |
| Time in market | Fraction of evaluation bars closed with nonzero position |
| Fees / spread / slippage | Commissions and estimated friction components reported separately |
| Open position / order | Reported separately at evaluation end, not relabeled as closed trades |

Spread/slippage components are estimates derived from explicit assumptions, not observed historical
quotes. Round-trip gross PnL includes the adversely adjusted execution prices; net PnL subtracts
commission. Return/equity calculations include the final mark-to-market value of any open position.

## How to use from Python

Provide a verified dataset object and all run assumptions first:

```python
from decimal import Decimal

from evoryn.learning_lab.backtest import (
    BacktestConfig,
    BacktestResultStore,
    evaluate_train_validation_test,
    evaluate_walk_forward,
)
from evoryn.learning_lab.historical_data import HistoricalDatasetStore
from evoryn.strategies.baseline import SmaCrossoverBaseline

dataset = HistoricalDatasetStore("data/historical").load(
    "your-authorized-dataset-id", "the-64-character-dataset-version"
)
config = BacktestConfig(
    initial_cash=Decimal("10000"),
    commission_bps=Decimal("10"),
    spread_bps=Decimal("5"),
    slippage_bps=Decimal("5"),
    max_bar_participation=Decimal("0.05"),
    allocation_fraction=Decimal("0.90"),
    quantity_step=Decimal("0.00000001"),
)
strategy = SmaCrossoverBaseline(fast_window=10, slow_window=30)
evaluation = evaluate_train_validation_test(dataset, strategy, config)
report_path = BacktestResultStore("data/backtest-runs").save(evaluation.test)
walk_forward = evaluate_walk_forward(
    dataset, strategy, config, train_bars=500, validation_bars=100, test_bars=100
)
```

Those numeric assumptions are illustrative configuration inputs, not venue facts or a recommended
trading configuration. Replace them only with documented, reviewed parameters; capture the exact
dataset and run report hashes alongside all comparisons.

## Evidence and results status

The unit tests use synthetic fixtures to check mechanics. No real dataset has yet been ingested
with verified source/license metadata, so this phase has no credible real-market baseline return
or profitability figure to report. The result store makes metrics reproducible; it does not make
a backtest a guarantee of future performance.

Before any strategy-selection decision, review out-of-sample/walk-forward consistency, fee/spread/
slippage sensitivity, trade distribution, drawdown, expectancy, turnover, data gaps, and any
unresolved provenance/license field. The model open-source policy for Phase 14 remains in ADR 0006:
candidate regime models must be evaluated from Hugging Face/GitHub with explicit licenses and
cannot alter risk limits or execution authority.
