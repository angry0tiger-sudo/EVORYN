# ADR 0003: Paper Broker and Deterministic Risk Gate

- Status: Proposed for review
- Scope: Implementation roadmap Phases 8 and 9
- Depends on: Phase 5 market-data contracts, Phase 6 exchange contracts, Phase 7 read-only Nobitex adapter

## Decision

Implement a deterministic paper execution environment and a separate RiskPort. The paper broker consumes normalized MarketSnapshot values through MarketDataPort and does not call exchange order APIs. Every new request passes through the risk gate before a simulated fill is allowed.

The RiskPort returns a RiskResult whose action is exactly one of ALLOW, REDUCE, REJECT, or HALT. RiskEngine has no strategy, model-runtime, or AI dependency. A HALT is latched, and clearing it requires explicit operator acknowledgement.

## Paper-market data contract

One PaperBroker instance is bound to one canonical Spot Market. Each fresh MarketSnapshot must include Decimal values named bid, ask, last, bid_size, and ask_size. The engine uses the best ask for simulated buys and best bid for simulated sells. Marketable limit orders only fill at a price no worse than their limit. Fills are capped at visible top-of-book size, making the simulator intentionally conservative about liquidity; it does not claim to model full depth, fees, queue position, or real exchange execution.

Order state labels are exposed as rejected, open, partial, filled, canceled, and unknown. Client order IDs are idempotency keys. A timeout/disconnect during an ambiguous submit produces unknown and latches the risk gate instead of assuming failure or retrying. The failure injector is deterministic, in-memory, operation-scoped, and disabled unless a test explicitly queues a fault.

## Risk and stop conditions

The risk gate checks free capital and outstanding reservations, total and market exposure, maximum order notional, available long Spot quantity, PnL loss limit, data freshness/quality, account reconciliation, and execution-fault state. Limits are explicit constructor inputs, not strategy/model suggestions. Over-limit quantities are reduced only when a positive bounded quantity remains; otherwise the request is rejected. Stale/future timestamps, malformed or crossed data, account mismatch, an execution error, or maximum loss trip a latched Auto-Halt. The manual kill switch uses the same latched stop boundary.

## Safety boundaries

- This implementation is paper-only; it contains no new exchange-write path.
- Nobitex remains read-only in Phase 7. The live order submit/cancel methods remain disabled.
- No API credential or live exchange request is needed for the tests.
- Unknown execution outcomes are not retried automatically.
- Phase 10 Spot Execution remains a separate, gated workstream.
- Paper results do not prove real-world fill quality or authorize live trading.
