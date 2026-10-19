# Phase 23 — End-to-End Mock/Paper Runtime

## Scope and safety boundary

Phase 23 provides a bounded composition root joining completed mock candles, the regime-classifier
port, the existing hysteretic strategy selector and plugin registry, the deterministic Risk Engine,
Paper Broker, independent Mock Spot ledger, portfolio builder, protective exit policy and SQLite
runtime journal. The runtime rejects feeds unless they explicitly declare `simulation_only=True`.
It contains no live exchange transport, signed gateway or production scheduler.

## One-cycle processing

1. Bootstrap a contiguous completed-candle window without submitting an order.
2. Validate market identity, interval, timestamp, ordering, candle continuity and freshness.
3. Apply the latest synthetic top-of-book update to resting Paper orders and compare fills, status
   and balances in the broker and separate Mock Spot ledger.
4. For a new completed candle, classify the regime and pass the prediction through the current
   strategy registry and hysteresis selector. Unknown, stale or insufficient evidence abstains.
5. Build a same-cycle account snapshot with available quote balance, base position, marked equity,
   exposure, reserved capital, active orders and unresolved order IDs.
6. Apply the deterministic protective adverse-regime/drawdown policy, with every intent still
   subjected to the same Risk Engine.
7. Write the prepared intent to SQLite before the Paper call; record outcome, reconcile, and commit
   the complete restorable Paper state.

Duplicate candles do not trigger a second regime decision. Multiple unprocessed bars, gaps, unknown
orders, ledger mismatches and invalid runtime state fail closed.

## Persistence and process recovery

The SQLite event journal uses transactional appends, WAL, synchronous FULL writes, sequence checks
and a SHA-256 hash chain. A clean restart restores the latest committed Paper state, reconstructs
the independent Mock venue ledger from saved orders/fills, then reconciles both sides before
processing another candle.

A prepared-but-unresolved intent or interrupted market update found after the last checkpoint
halts the runtime and prevents automatic re-submission. A previously recorded runtime halt also
remains latched. Recovery intentionally does not guess whether an interrupted operation happened.
The hash chain detects corruption, but is not a signature against a privileged attacker who can
rewrite the database and its contents.

`PaperRuntimeSupervisor` wraps one bounded cycle and can reconstruct from the same journal after
an exception. It is not an OS service manager, and it does not automatically clear a halt.

## Integration coverage

`tests/integration/test_paper_strategy_runtime.py` uses synthetic in-memory feed and brain doubles
to test brain → selector → target intent → Risk Engine → Paper fill → separate ledger reconciliation,
portfolio reservations, protective exit, clean restart without duplicate orders, fail-closed
interrupted-intent recovery, supervisor restart into a persisted halt, enforcement of the mock-only
feed marker, and corruption detection.

All this phase's tests are offline and simulated. Green CI does not prove production exchange
compatibility, profitability, Windows-host acceptance, or authorization for Live.
