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

The repository contains modular Spot/Paper execution, delegated-Margin domain rules, a read-only Nobitex adapter, and a separately isolated, feature-gated Spot execution candidate. The candidate is not enabled by default, has no active trading loop, and has not been validated against a live account. Do not treat merged code or green CI as live-trading readiness.

## Agent foundation

See [Agent Setup](docs/agents/README.md) and the repository [Agent Operating Contract](AGENTS.md).

The current agent-foundation branch adds specialized repository-level profiles for architecture, implementation, independent risk review, and test engineering. These profiles are instructions for compatible agent hosts; they do **not** by themselves install a model runtime or enable autonomous GitHub workflows.

## Safety

- Coding agents must never receive exchange credentials or live-trading authority.
- AI output cannot directly bypass deterministic risk checks or submit live orders.
- Exchange behavior must be verified against current official documentation.
- No hosted agent workflow should be enabled before its model cost/license, data handling, permissions, and sandbox are reviewed.

## Current engineering gate

Phase 22 established the read-only Nobitex USDT account/data gate. A separate local Spot execution candidate is being reviewed; it remains disabled unless a local operator supplies both an explicit feature flag and acknowledgement. It requires the deterministic Risk Engine, reconciled account state, fresh Nobitex data/rules, and a persistent SQLite idempotency journal. No credentials are committed and no live test is run in GitHub. The candidate needs review and local verification before a production-ready claim. Withdrawals, transfers, Margin writes and Futures remain unsupported.
