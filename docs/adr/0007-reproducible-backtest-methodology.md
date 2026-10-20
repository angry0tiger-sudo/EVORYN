# ADR 0007: Reproducible OHLCV Research and Causal Baseline Backtesting

- Status: Proposed for review
- Date: 2026-10-18
- Scope: Roadmap Phase 13
- Dependencies: MarketCandle domain model, versioned source/license metadata, deterministic baseline
  strategies, and the approved Freqtrade/CCXT boundaries in ADR 0006

## Context

Future strategy and Intelligence improvements need a stable comparator. A backtest must not
be able to make unsupported performance claims from mutable data, undocumented cost settings,
same-candle fills, or test-period reuse. A public data URL is not proof of permission to
store or evaluate its content.

## Decision

1. Add a local, immutable, content-addressed historical dataset store. Every dataset requires
   a market identity, interval, candle timestamp semantics, explicit volume unit (base, quote, or unknown),
   source name/reference/revision, collection time, explicit license identifier/reference, and
   validated OHLCV candles. Volume participation caps require confirmed base-asset units.
2. Canonicalize time to UTC and decimal values to strings; calculate SHA-256 addresses over
   canonical metadata and candle CSV bytes. Store immutable files under
   `<root>/<dataset_id>/<dataset_version>/manifest.json` and `candles.csv`. Re-saving the
   same version is idempotent; a conflicting or altered version is rejected, never repaired
   silently.
3. Add two stateless baselines, `SmaCrossoverBaseline` and `SmaTrendBaseline`. Both are
   long/flat, deterministic, and independent of ML/AI. Strategy APIs receive only completed
   history supplied by the backtest engine.
4. Establish signal time at the close of the last completed candle. An order becomes eligible
   at the next candle open. The engine never passes the current fill candle to the strategy.
5. To avoid look-ahead in liquidity estimates, fill capacity at the current open may be based
   on the **last completed candle's** reported volume only when the provider confirms that the
   value is base-asset volume. Unknown-unit history explicitly disables the capacity cap, records
   zero as the unknown liquidity reference, and marks the run unconstrained. Partial orders
   persist across later bars; a changed target cancels the remaining order. Even confirmed
   volume is a coarse proxy, not historical order-book depth.
6. Require explicit initial capital, commission basis points, spread basis points, slippage
   basis points, volume-cap enablement, bar-participation fraction, allocation fraction,
   quantity step, and gap policy. Volume-cap enablement is forbidden unless base-volume units
   are confirmed. Buys and sells are filled at adverse prices around the next open using the
   configured half-spread and slippage assumptions; commissions are charged on executed notional.
7. Calculate net marked equity, maximum drawdown, closed-round-trip expectancy, win rate,
   turnover, order/fill counts, time-in-market and each friction component. Open positions
   are marked to the final close and are not mislabeled as closed trades.
8. Provide chronological train/validation/test partitions and rolling walk-forward folds.
   Every fold's test bars are later than its training and validation bars, and test windows do
   not overlap. The fixed baselines are not fitted ML models; the split protocol establishes
   time-separated evaluation and provides the interface future learned models must obey.
9. Persist full run reports immutably by a deterministic run ID covering the dataset version,
   strategy ID/parameters, simulator assumptions, evaluation range, and warm-up range.
10. No historical data is fetched in this phase. Dataset creation requires the caller to supply
    explicit source/license provenance; a strict canonical-CSV import helper is the input boundary.
    Every report includes dataset provenance and a checksum sidecar, while noting that a checksum
    is not a digital signature. Synthetic unit fixtures are not reported as market performance.
    No Freqtrade, CCXT, pandas, NumPy or other new runtime dependency is added.

## Definitions

- **Dataset version:** SHA-256 of schema version, canonical provenance/configuration metadata,
  and the SHA-256 digest of canonical candle bytes.
- **Signal:** desired exposure computed from candles whose close times are no later than the
  decision time.
- **Fill:** simulated next-open execution. A last-completed-bar volume cap is applied only
  when base-volume units are confirmed; with unknown units the simulation is explicitly
  unconstrained. OHLCV does not establish exact spread or order-book depth.
- **Maximum drawdown:** maximum peak-to-subsequent-equity decline divided by that running peak,
  using initial equity and the bar-close marked equity curve.
- **Expectancy:** arithmetic mean net PnL in quote currency per completed round trip after
  commissions. If no position round trip is complete, expectancy is null.
- **Turnover:** sum of executed notional across all simulated fills. Turnover multiple is
  turnover divided by starting equity.
- **Net return:** final marked equity minus initial equity, divided by initial equity.
- **Execution friction:** commission is explicit; spread and slippage costs are estimates from
  configured basis-point assumptions around candle open. These assumptions must not be
  presented as measured historical spread/slippage unless separately supported by tick/book data.

## Consequences

### Positive

- A result points back to exact source/license metadata, candle bytes, strategy parameters,
  cost assumptions, and time ranges.
- Future strategies can be compared against the same transparent baselines.
- Temporal splits and next-open execution reduce common look-ahead errors.
- The simulator makes its OHLCV approximation and unresolved realism limits explicit.

### Limitations

- No licensed historical dataset is currently included by this phase; therefore no real-market
  return, expectancy, or profitability result is claimed.
- Unknown-unit OHLCV cannot be used as a quantitative capacity constraint. OHLCV cannot
  reproduce queue priority, true intrabar path, bid/ask history, exchange fees that vary by
  account/tier, or precise partial-fill mechanics. Static spread/slippage are sensitivity
  assumptions until proper quote/tick data and fees are registered.
- The engine is long/flat Spot-only. It does not model shorts, margin, funding, liquidation,
  exchange transfers, AI signals or live execution.
- Chronological segments reset capital independently. Their metrics are diagnostics and must not
  be blindly summed into a single equity curve.
- A benchmark's attractive metrics are not evidence of future profitability. Acceptance requires
  out-of-sample, walk-forward stability, cost sensitivity, and an independently reviewed data
  license.

## Validation gate

Phase 13 code adds synthetic unit scenarios for content-address integrity, provenance, gaps,
causal signal/fill timing, costs, volume-based partial fills, closed/open trade accounting,
metrics, chronological partitions, walk-forward windows and immutable run reports. Tests must
not be called passed unless a verified runner's output is visible. No synthetic-fixture result
is a profitability claim.

## Rollback

Remove the learning_lab dataset/backtest modules, baseline strategies, tests and this ADR.
This phase adds no exchange adapter changes, dependency changes or live execution permission.


## Update — Nobitex UDF volume ambiguity (2026-10-20)

The public UDF history response exposes `v` but its published field contract does not certify
base-asset versus quote-asset units. Research datasets therefore retain `volume_unit="unknown"`.
The regime research schema excludes volume entirely; the backtest disables volume-derived fill
caps for this dataset and emits `UNCONSTRAINED_UNKNOWN_VOLUME_UNIT`. It is not valid to claim
executable capacity or liquidity from those simulations. Re-enable volume features/caps only
after independent provider semantics evidence, a schema/version update, and relevant tests.
