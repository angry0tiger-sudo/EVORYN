# Phase 15: Strategy Plugins, Adaptive Selection, and OOS Review

## Safety first

The selector produces only `Decimal("0")` or `Decimal("1")` target exposure in the research
adapter. It is not an order planner and has no exchange API. A regime model or strategy plugin
cannot change risk limits or authorize an execution.

For paper execution, the selector may share the Phase 10 ledger:

```python
from evoryn.execution.spot import SpotExecutionService
from evoryn.strategies.plugins import build_baseline_registry
from evoryn.strategies.selector import RegimeStrategySelector

# spot_execution_service is an already constructed Phase 10 paper-only service.
registry = build_baseline_registry()
selector = RegimeStrategySelector(
    registry,
    decision_ledger=spot_execution_service.ledger,
)
```

This only records decisions. Any downstream orchestration that translates target exposure into an
order must still construct an explicit `SpotIntent` and call
`SpotExecutionService.execute(intent)`. PaperBroker's independent RiskPort/RiskEngine remains
the final authority before a simulated fill. The selector does not call or bypass that gate.

## Plugin contract and registry

A `CausalTargetStrategy` supplies `strategy_id`, `strategy_version`, deterministic string
parameters, and `target_exposure(completed_history)`. A plugin registration wraps that pure
implementation with:
- a unique matching plugin ID/version;
- a clear description and an enabled flag;
- a non-empty set of explicitly supported known regimes.

The registry refuses duplicate IDs, unknown-as-routable-regime, missing plugin IDs, and mappings
that include a plugin not registered for that regime. Its SHA-256 fingerprint covers plugin
identity, supported regimes, enabled state and strategy parameters. Registry changes clear selector
state and require fresh evidence.

Built-in route hypotheses:

| Regime | Preferred plugin | Alternative/control |
|---|---|---|
| `bull_trend` | `sma-crossover-long-flat` | only the explicitly compatible enabled plugins |
| `bear_trend` | `flat-no-position` | none by default |
| `range_bound` | `sma-trend-long-flat` | `flat-no-position` |
| `high_volatility` | `flat-no-position` | none by default |
| `unknown` | no route | abstain, no new strategy signal |

This mapping is a transparent baseline hypothesis. It is not a claim that the SMA choice has been
proven best for that regime.

## Default selector guardrails

- A regime output must not be abstained/unknown and must meet confidence threshold `0.65`.
- The score semantics must be an HMM posterior or a separately calibrated probability. Heuristic
  rule scores are rejected as unvalidated confidence.
- Candidate selection requires three distinct, increasing as-of timestamps.
- The candidate must remain stable for at least two hours.
- At least six hours must have passed since the last strategy switch.
- A duplicate/out-of-order timestamp never increments evidence.
- A gap between distinct regime observations longer than the configured maximum age resets
  candidate evidence.
- The selector requires canonical UTC prediction timestamps and abstains on future/stale data.
- One selector instance is bound to the first market it receives; a different market ID invalidates
  its active state and fails closed. Use an independent selector and hysteresis ledger stream per
  market rather than sharing evidence across symbols.
- A model ID/version/fingerprint/schema change or registry fingerprint change clears the active
  choice. Evidence must be re-established.
- While a new candidate is pending, the incumbent remains active if one exists. If there is no
  incumbent yet, the research adapter remains flat. Abstention also returns flat.

Time durations are configuration constraints and cannot be zero. The evidence count is a minimum,
not permission to skip the stability or minimum-hold timer.

## Decision ledger

`ExecutionLedger.strategy_selections` exposes a read-only snapshot of the append-only records.
Each record contains:
- correlation ID, market symbol, event ID and recorded timestamp;
- observed regime, confidence and its semantics, model ID/version/fingerprint, feature schema;
- prior active strategy, candidate/active selection and available compatible alternatives;
- reason/status codes, evidence count, candidate start and last switch time;
- configured confidence threshold, required evidence, stability and hold durations;
- selector engine version, registry and routing fingerprints, invalidation conditions and
  `allow_new_signals`;
- both the ledger-recorded timestamp and the regime prediction's `as_of` timestamp, so event
  freshness can be reconstructed independently;
- the previous, candidate, and active strategy IDs; actual evidence count even after the threshold
  is met; and the configured maximum prediction age.

The event object resides in the domain layer. The selector depends only on a decision-sink protocol,
not on PaperBroker or execution itself. Ledgers are in-memory and append-only within the process;
durable persistence and tamper-evident signatures are not claimed.

## Held-out evaluation

`evaluate_strategy_selector_out_of_sample(...)` requires:
1. A versioned dataset and `TemporalPartitions` whose digest/count match it.
2. A fitted detector with a model training manifest for the same dataset and a training range
   strictly inside `partitions.train`.
3. A fixed selector policy and registry before the test run.
4. A chosen fixed-strategy comparator, evaluated over the exact same test range and simulator costs.

It returns both complete Phase 13 backtest results plus differences in net profit, net return,
maximum drawdown and expectancy where both runs close at least one trade. Its combined report
contains the train/test index ranges, model manifest, registry fingerprint and all strategy
selection/abstention ledger rows. If the registry/model identity changes during the test run, the
evaluator refuses to return a valid comparison.

The test split must not be used to retune thresholds or route preferences. Doing so consumes the
holdout and requires a different unseen dataset range for a future performance claim.

## Architecture connection

```text
HistoricalDataset + TemporalPartitions
              |
              v
   Phase 14 Regime Detector ----> UNKNOWN / abstain
              |
              v
   Phase 15 Strategy Selector <---- StrategyPluginRegistry
       | evidence + dwell + hold
       v
   CausalTargetStrategy target exposure
       |
       +--> Phase 13 backtest (fees, spread, slippage, volume cap, quantity step)
       |
       +--> Phase 10 paper orchestrator only after explicit target-to-intent planning
                  |
                  v
       SpotExecutionService -> PaperBroker -> independent RiskEngine -> paper fill
                  |
                  v
       Same ExecutionLedger (strategy-selection, risk, execution, balance, reconcile)
```

The current Phase 15 OOS evaluator uses Phase 13's Spot simulator; it does not inject the full
RiskEngine into historical bar simulation. The RiskEngine boundary remains separately authoritative
for paper orders. Selection performance and order-risk approval are distinct pieces of evidence.

## Current evidence status

Network-free synthetic tests cover plugin validation, hysteresis, abstention, ledger rows, model/
registry invalidation and OOS train/test boundary checks. GitHub Actions execution has not produced
verifiable job-step logs/artifacts in the current environment; test authorship is not represented
as test success. No licensed live-market test dataset or real performance report is included here.
