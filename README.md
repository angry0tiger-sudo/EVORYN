# EVORYN

**Autonomous Adaptive Trading Intelligence**

EVORYN is an independently governed, modular trading-intelligence platform planned to begin with Nobitex Spot and Margin/تعهدی, with future venues and derivatives added only through explicit adapter and risk reviews.

## Engineering principles

- Architecture first, implementation second, evidence always.
- Modular monolith first, with clear module contracts and replaceable adapters.
- Deterministic risk controls independent from AI recommendations.
- Reproducible research, controlled optimization, and explicit validation gates.
- No credentials in source control and no live trading in development or CI.
- Every material change must be testable, reviewable, and reversible.

## Project status

The repository contains modular Spot and delegated-Margin domain code and a read-only Nobitex market adapter. Do not assume authenticated live account reads are configured, production runtime wiring is complete, or any system is ready to place real orders. Check the exact commit and current CI evidence before relying on a capability.

## Agent foundation

See [Agent Setup](docs/agents/README.md) and the repository [Agent Operating Contract](AGENTS.md).

The current agent-foundation branch adds specialized repository-level profiles for architecture, implementation, independent risk review, and test engineering. These profiles are instructions for compatible agent hosts; they do **not** by themselves install a model runtime or enable autonomous GitHub workflows.

## Safety

- Coding agents must never receive exchange credentials or live-trading authority.
- AI output cannot directly bypass deterministic risk checks or submit live orders.
- Exchange behavior must be verified against current official documentation.
- No hosted agent workflow should be enabled before its model cost/license, data handling, permissions, and sandbox are reviewed.

## Current engineering gate

Phase 22 is limited to read-only Nobitex account data and the hard USDT quote-currency whitelist. Live order submission, cancellation, withdrawals, transfers and collateral mutation remain out of scope. Provider-side read-only token permissions, live API connectivity and production runtime wiring require independent evidence.
