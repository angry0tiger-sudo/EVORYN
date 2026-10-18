# Paper Trading and Risk Operations

## Constructing the paper environment

Construct a RiskEngine with explicit RiskLimits, then construct a PaperBroker bound to one canonical Spot Market. Provide a Phase 5 MarketDataPort implementation, a starting quote-currency capital amount, and an aware clock when deterministic replay is required. CI and unit tests should use only synthetic snapshots and the in-memory failure injector.

Fresh MarketSnapshot values must contain Decimal keys:

- bid, ask, and last: finite, positive prices
- bid_size and ask_size: finite, non-negative visible base-asset quantities

Timestamps must be timezone-aware, not in the future, and within the configured age limit. A non-empty source and empty quality_flags are required. Bid greater than ask is treated as a data fault.

## Order behavior

Market orders execute against visible top-of-book size at ask for buys or bid for sells. A quantity larger than available displayed liquidity can remain partial and be revisited on later ticks. Limit orders remain open until their limit is marketable. Canceling an open/partial order records canceled and releases its risk reservation. Repeating a client order ID returns its existing order and never submits a second simulation request.

For disconnect/time-out injection at submit, the order becomes unknown and the risk engine halts. Do not treat unknown as rejected and do not retry it. A market-data read failure or invalid market input rejects the not-yet-executed request and trips Auto-Halt. A disconnect/time-out on market-update processing marks active orders unknown. Recovery means stopping, investigating the ledger and injected/real fault, reconciling state, and obtaining explicit operator acknowledgement before clearing the halt.

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
