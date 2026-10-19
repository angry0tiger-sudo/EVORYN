# Architecture Decision Records (ADR)

Use the template in [0000-adr-template.md](0000-adr-template.md) for material architecture decisions.

## Rules

- Number ADRs sequentially and use immutable filenames after acceptance.
- Record context, decision, alternatives, consequences, risks, and evidence.
- Use statuses **Proposed**, **Accepted**, **Superseded**, or **Rejected**.
- Do not describe an unverified design as implemented or production-ready.
- Changes affecting exchange connectivity, order execution, risk controls, data handling, or permissions require explicit review.
- The repository includes a read-only Nobitex market-data adapter. No live order-routing implementation is authorized by these ADRs.

## Current decision records

- [ADR 0010 — Learning Lab, Champion/Challenger and Paper Rollback](0010-learning-lab-champion-challenger.md): immutable experiment identity, trust-gated evidence, Paper-only champion pointer, and rollback boundaries.
- [ADR 0011 — Connectivity Management and Feed Freshness](0011-connectivity-management.md): bounded read-only transport retry/circuit/rate-limit handling and timestamp/gap validation. Phase 18 remains conditionally accepted with outstanding CI and runtime integration debt.
- [ADR 0012 — Shadow Mode, Observation Only](0012-shadow-mode-observation-only.md): Phase 19 read-only candle observation, registered active Paper Champion comparison, fail-closed snapshot capture and strict separation of Shadow from execution/Paper evidence. Proposed for independent review; no live poll has been executed by this development session.
- [Phase 19 Verification Status](../engineering/phase-19-verification-status.md): current source-blob identities, structural checks, runner-allocation evidence, and outstanding acceptance gates.
