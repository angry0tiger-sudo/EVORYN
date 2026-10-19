# ADR 0009: Regime-Gated Strategy Plugins, Hysteresis, and Out-of-Sample Selection

- Status: Proposed for review
- Date: 2026-10-19
- Scope: Roadmap Phase 15
- Depends on: Phase 10 append-only ExecutionLedger and PaperBroker/RiskEngine boundary; Phase 13
  causal backtest and TemporalPartitions; Phase 14 regime abstention and open-source model manifest

## Decision

1. Define a versioned Strategy Plugin API around `StrategyPluginRegistration` and the
   strategy-neutral `CausalTargetStrategy` contract. A plugin identifies its exact strategy ID,
   implementation version, description, enabled state, and explicitly supported regimes.
2. Make plugin registration unique by ID, reject `UNKNOWN` as a routable regime, validate that
   mapped plugin IDs exist and support their assigned regimes, and fingerprint the entire registry
   including strategy parameters. A registry change invalidates active/candidate selector state.
3. Provide transparent built-in candidates: SMA crossover for bullish trend, SMA trend for range
   as a hypothesis, and an always-flat baseline for bearish/high-volatility regimes (and as an
   explicitly permitted defensive fallback). These routes are initial research hypotheses, not
   empirical claims about the best strategy.
4. Accept only non-abstained regime outputs whose confidence semantics are
   `hmm_posterior` or `calibrated_probability`. Reject heuristic scores. The default selector
   threshold is 0.65; this threshold filters model output but does not prove calibration.
5. Version the selector implementation and include that version, the full registry fingerprint,
   and the immutable regime-to-plugin routing fingerprint in each report and ledger event.
6. Require a new plugin selection to satisfy all three guards:
   - at least 3 distinct, increasing regime timestamps for the same candidate plugin;
   - at least 2 hours of candidate stability from the first candidate observation;
   - at least 6 hours since the prior switch.
   These are configurable but must remain strictly positive for time guards. Repeated timestamps
   do not count; an observation gap exceeding the configured 2-hour maximum resets candidate
   evidence. UTC-only, stale/future, invalid, unknown, low-confidence, or identity-changing inputs
   abstain and do not issue new strategy signals.
7. During a switch candidate, retain the incumbent strategy until the new candidate satisfies all
   guards. On abstention, selection marks `allow_new_signals=false`; backtest adapter returns flat
   rather than forcing uncertain predictions into the incumbent strategy.
8. Extend Phase 10's existing in-memory append-only `ExecutionLedger` with
   `StrategySelectionLedgerEntry`, modeled in the domain layer. Every selection/hold/abstention
   records market and correlation IDs, regime/model identity and confidence semantics, selected
   and candidate plugin, options considered, reason, consecutive evidence, stability/hold limits,
   registry fingerprint, invalidation conditions, and whether new signals are allowed.
9. Keep the selector decoupled from broker and risk code. Its output is a long/flat target exposure
   through the Phase 13 `CausalTargetStrategy` contract; it cannot create an `OrderRequest`, alter
   RiskLimits, clear Auto-Halt, or access exchange adapters.
10. Add an OOS evaluation gate that requires a matching Phase 13 dataset version and a model training
   manifest fully contained inside that version's train partition. It evaluates the adaptive
   selector only on the held-out test range and compares it with one fixed strategy over the exact
   same range, cost assumptions and data. The model/registry identity may not change mid-run.
11. Report adaptive/fixed return, PnL, drawdown, expectancy where defined, selection events,
    model manifest, registry fingerprint and the exact test range. Results are diagnostics, not a
    license to retune on the test split or to claim future profitability.

## Hysteresis state machine

- **Unselected:** no plugin is active until the same candidate meets evidence and stability guards.
- **Candidate pending:** record each new distinct timestamp. Keep the incumbent when present; if
  none exists, remain flat.
- **Switched:** update the active plugin only after evidence, candidate stability and minimum-hold
  rules pass.
- **Abstained:** no new strategy signal is permitted. Candidate evidence resets.
- **Invalidated:** registry or model fingerprint changes clear the active/candidate state. Fresh
  evidence must re-establish the selection.

## Phase 10 ledger and RiskEngine boundary

The selector can append decision events into the same `ExecutionLedger` used by
`SpotExecutionService`. This is an audit integration, not an execution permission. A downstream
paper orchestrator must still transform any target exposure to an explicit `SpotIntent` and call
`SpotExecutionService.execute`; its `PaperBroker` invokes the independent `RiskPort`/`RiskEngine`
before every simulated order. A REJECT/HALT remains authoritative even if the selector prefers a
strategy. No risk limit is inferred from the regime, strategy, model, or backtest results.

The Phase 13 historical simulator is not yet a full RiskEngine simulator. This Phase 15 OOS
comparison uses the simulator's explicit cash, fee, spread, slippage, volume-participation and
quantity-step assumptions. Risk-gated paper execution must be validated separately through the
Phase 10 broker/orchestration integration tests before it can be treated as execution evidence.

## Out-of-sample protocol

- Fit the regime model using only a versioned `HistoricalDataset` and a `TemporalPartitions.train`
  range. Keep state mapping, feature scaling, selector thresholds and plugin preferences fixed
  before running the test evaluation.
- Start both adaptive and fixed baselines with the same initial capital, fee/spread/slippage
  parameters, quantity step and participation cap, on the same `TemporalPartitions.test` range.
- Use only the prefix that precedes each next-open simulated decision. No training, switching-policy
  fitting, threshold calibration or route changes are performed from test performance.
- Preserve every selection/abstention event and both full Phase 13 result reports.
- If metrics are used to choose a preferred route or alter hysteresis, that test partition has been
  consumed; use a new untouched holdout for any subsequent claim.

## Safety and validation boundary

Synthetic unit tests cover plugin validation, routing, minimum evidence/time guards, duplicate
timestamps, abstention, registry/model identity changes, ledger emission and OOS data-leak gates.
They are authored on the branch; they are not described as passed until a verified runner is
available. No real-market selection uplift or profitability claim is included.
