# Paper Trading and Risk Operations

## Constructing the paper environment

Construct a RiskEngine with explicit RiskLimits, then construct a PaperBroker bound to one canonical Spot Market. Provide a Phase 5 MarketDataPort implementation, a starting quote-currency capital amount, and an aware clock when deterministic replay is required. CI and unit tests should use only synthetic snapshots and the in-memory failure injector.

Fresh MarketSnapshot values must contain Decimal keys:

- bid, ask, and last: finite, positive prices
- bid_size and ask_size: finite, non-negative visible base-asset quantities

Timestamps must be timezone-aware, not in the future, and within the configured age limit. A non-empty source and empty quality_flags are required. Bid greater than ask is treated as a data fault.

## Order behavior

Market orders execute against visible top-of-book size at ask for buys or bid for sells. A quantity larger than available displayed liquidity can remain partial and be revisited on later ticks. Limit orders remain open until their limit is marketable. Canceling an open/partial order records canceled and releases its risk reservation. Repeating a client order ID returns its existing order and never submits a second simulation request.

For disconnect/time-out injection at submit, the order becomes unknown and the risk engine halts. Do not treat unknown as rejected and do not retry it. A market-data read failure or invalid market input rejects the not-yet-executed request and trips Auto-Halt. A disconnect/time-out on market-update processing marks active orders unknown. Recovery means stopping and investigating first. Use the explicit paper-order reconciliation hook only with operator acknowledgement and a confirmed status compatible with the fill records already in the ledger. The hook never invents fills. Clear the RiskEngine halt separately, with explicit acknowledgement, only after the underlying fault is resolved. Orders whose confirmed outcome requires fill records that are not present remain unresolved.

## Risk decision meanings

| Action | Meaning |
| --- | --- |
| ALLOW | The full requested quantity fits every checked hard limit. |
| REDUCE | A positive smaller quantity fits all checked limits; only that quantity is authorized. |
| REJECT | The request is malformed or no positive safe quantity remains. |
| HALT | A critical state is unsafe or indeterminate; the halt is latched. |

Buy capacity is constrained by free capital after reservations, per-order notional, total exposure, and market exposure. Sell capacity cannot exceed unreserved long Spot inventory. Loss at or beyond max_loss, stale/invalid data, account reconciliation failure, execution error, or the manual kill switch stops new execution. Clearing a halt requires explicit operator acknowledgement after the underlying problem has been assessed.

## Deliberate limitations

This phase models one Spot market per PaperBroker instance. It uses visible top-of-book liquidity only and does not yet model exchange fees, slippage curves, queue priority, multi-market collateral, full portfolio persistence, or live reconciliation. These are not implicitly approximated. This implementation does not enable live order submission; Phases 8 and 9 are not approval for Phase 10 or real capital.


## Phase 10: Spot execution orchestration (paper-only)

`SpotExecutionService` connects a correlation-scoped `SpotIntent` to the existing
`PaperBroker`, captures the exact risk decision used by the broker, records order/fill
state transitions and paper balances, then compares all paper orders and base/quote
balances against a separate `MockSpotExchangeAdapter` ledger. The mock adapter performs
no I/O and is not an exchange connector.

- A repeated correlation ID with a different request is rejected. A repeated identical
  intent returns the original result rather than creating another order.
- A client order ID cannot be rebound to a different correlation ID.
- Unknown order outcomes are never treated as rejected, and reconciliation marks them
  unresolved even if the mock mirror also says `unknown`.
- Missing/extra orders, changed order state, divergent fills, or balance differences
  latch the same RiskEngine Auto-Halt.
- Decision, execution, balance, and reconciliation entries are append-only in memory.
  Durable persistence and tamper-evident storage are not implemented by this phase.
- Call `SpotExecutionService.reconcile_all(correlation_id=...)` from the host's periodic
  scheduler. This module intentionally creates no background thread and picks no interval.
- Integration tests use synthetic market data, PaperBroker, and an in-memory mock venue.
  No live exchange API, credentials, or exchange order write is invoked.

## CI diagnostic access limitation (recorded 2026-10-18)

GitHub Actions runs #165 (Phase 8/9) and #177 (Phase 10) reported `failure`. For
both runs, job-step retrieval returned no steps, job-log retrieval returned `BlobNotFound`,
and no diagnostic artifact was reported. The underlying cause could not be established from
the available evidence. Record this as a **CI diagnostic access limitation in the current
environment**, not as a confirmed GitHub platform defect and not as proof that the code or
tests passed.

The Phase 8/9 tests exist in the repository, but their actual pass/fail output was not
retrievable in this work session. Phase 10 integration tests are authored on the
`feat/spot-execution-phase10` branch; they must not be described as executed or passed
unless a run's output is observed. All execution work remains paper-only.
