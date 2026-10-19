# ADR 0012: Shadow Mode — Read-Only Observation and Paper Champion Comparison

- Status: Proposed for independent review
- Date: 2026-10-19
- Scope: Roadmap Phase 19 as directed in the project handover
- Depends on: Phase 18 Connectivity Manager (conditionally merged), Phase 13 data provenance, Phase 14 regime abstention, Phase 15 strategy contracts/selection, and Phase 17 Learning Lab/Paper Champion ledger.

## Source of scope and evidence boundary

The project handover establishes Phase 19 as observational Shadow Mode after Phase 18. A roadmap "page 6" artifact was not found in the canonical `main` repository tree during this continuation, so this ADR does not claim to have read or verified that missing artifact. The scope below is grounded in the owner-provided handover and the existing phase/architecture decisions.

Phase 18 was merged under the owner's explicit conditional-acceptance instruction. That merge is not CI-green certification. GitHub Actions jobs on the prior Phase 18 candidate did not start because the jobs API reported `runner_id=0`, empty `runner_name`, and `steps=[]`; local Phase 18 smoke checks were narrow, not the full suite. The outstanding gates remain in `docs/engineering/phase-18-conditional-acceptance.md` and `TECHNICAL_DEBT.md`.

## Context

A Shadow observer is useful only if a bad feed, stale candle, model identity change, missing halt state, or corrupted ledger causes abstention rather than a plausible-looking signal. Shadow output is not an order, risk approval, Paper fill or Live telemetry. In addition, a candidate should be compared to the current registered Paper Champion using the same completed history and comparable experiment assumptions; an arbitrary PAPER experiment is not a valid Champion baseline.

## Decisions

### 1. Feed and transport

- Provide `NobitexRestCandleFeed`, a narrow adapter around the already existing `NobitexExchangeAdapter.get_market_candles` read-only interface.
- The implementation uses bounded REST candle reads. It filters the currently forming candle and rejects unexpected market, interval, source or non-canonical timestamp rows. It does not introduce a WebSocket client or a new provider endpoint.
- The service API exposes one bounded `ShadowPollingService.poll_once` invocation; a separately approved runtime scheduler may call it. This PR does not start a daemon, schedule an infinite polling loop, or perform a live feed call during construction/tests.
- Existing Nobitex HTTPS host allowlisting remains the transport boundary. No scraping, proxy rotation, identity masking, write endpoint, exchange order call, or credential logging is added.

### 2. Experiment identity and active Paper Champion

- The candidate must be a registered `ExperimentSpec` with mode `SHADOW`; its strategy ID, version and sorted parameter material must match the supplied deterministic strategy implementation.
- The baseline must be a registered `ExperimentSpec` with mode `PAPER`, and it must be the current `ChampionChallengerController.active_champion_id`. The controller and runner must refer to the same registry and the same Learning Lab ledger.
- Candidate and Paper Champion experiment specs must match on underlying dataset version, feature-schema version, evaluation-config fingerprint, random seed and train/validation/test split boundaries. The current feed-contract fingerprint is recorded separately in the Shadow run metadata, because the ongoing stream is not falsely represented as a static historical dataset.
- The candidate and baseline strategy IDs, versions, fingerprints, parameters and target projections are recorded separately. A changed active Champion pointer or candidate/baseline strategy identity latches fail-closed until a new registered run is constructed.
- A Paper Champion is never promoted, demoted, or changed by Shadow. The observer has no call path that invokes the controller's promotion or rollback operations.

### 3. Decision snapshot and comparison

Every processed completed candle creates an immutable `ShadowDecisionSnapshot` containing:
- `mode=SHADOW`, event/correlation IDs, UTC observation/receipt/record times, market, source, source version, and candle interval;
- registered candidate experiment and model identity, strategy identity/fingerprint, regime label/confidence/as-of and regime-model identity;
- active Paper Champion experiment and model identity, strategy identity/fingerprint, baseline target exposure/quantity/delta;
- candidate target exposure/quantity/delta and estimated notional delta;
- outcome, reason code, Auto-Halt state, risk-assessment status and the invariant `execution_authorized=false`.

The current causal strategy contract exposes target exposure, not a calibrated strategy-confidence field; `strategy_confidence` is therefore recorded as unavailable rather than invented. Candidate and baseline targets are analytical projections from the same explicit virtual budget, allocation fraction, quantity step, completed history and reference close. They are not order sizes approved by RiskEngine, and no fill/transaction-cost model is applied. This phase does not emit counterfactual fills or assert performance/profitability.

### 4. Mandatory abstention conditions

The observer abstains and records the reason on:
- stale, future-dated, duplicated, out-of-order, malformed, wrong-source, wrong-market or wrong-interval candles;
- a candle gap or an input continuity latch without a logged, explicit reconciliation;
- a missing/invalid/active Auto-Halt status;
- regime-model exception, unknown/abstaining regime, stale/future regime timestamp, insufficient confidence or changed regime model identity;
- candidate/baseline strategy failure, invalid exposure, strategy identity drift, baseline mismatch or active Paper Champion pointer drift;
- Learning Lab ledger failure, missing persistent ledger, or invalid registry/ledger provenance.

Rejected observations do not advance the candidate or baseline virtual position state. Learning Lab append failure raises a blocking error rather than treating the event as recorded.

### 5. Event ledger and mode separation

- The run, decisions, rejected input, feed failure, startup bootstrap and reconciliation are appended using the existing hash-chained `LearningLabLedger`.
- Shadow run initialization requires a path-backed JSONL ledger with the existing fsync-backed append and startup integrity verification. In-memory-only ledgers are not accepted.
- SHADOW, PAPER and LIVE remain distinct `ExperimentMode` values. Shadow events are not relabeled as PAPER evidence and are not automatically promotion eligible through this runner.
- No order-submit/cancel API, execution port, account mutation, RiskEngine mutation, auto-halt reset, or Live-mode switch exists in this package. `risk_assessment_status=NOT_EVALUATED` is explicit; a hypothetical target is not a risk gate ALLOW.

### 6. Operational activation

The code provides a single poll operation but does not claim an already-running service. Before activation, an operator must supply a real registered SHADOW candidate, the current registered Paper Champion and controller, reviewed deterministic strategy/regime implementations, a durable access-controlled Learning Lab directory, a read-only Auto-Halt-state provider, an approved market and interval, and an externally managed polling cadence. These production identities and schedule were not supplied in this continuation; no live market request has been issued here.

## Validation and known limits

`tests/unit/test_phase19_shadow_mode.py` includes mock cases for candidate/baseline comparison, ledger persistence, identity drift, stale/duplicate/gapped feed, reconciliation, UNKNOWN regime, Auto-Halt, feed timeout, malformed experiment scope, and absence of execution authority. Writing tests is not evidence that they passed. The full canonical suite, Ruff, strict mypy, Bandit, pip-audit and independent architecture/risk review remain unverified while hosted runner assignment is unavailable.

## Rollback

Remove the Shadow engine, package exports, Phase 19 tests, these event types, this ADR and the operations guide. Preserve the merged Phase 18 connectivity code and all Phase 17 Learning Lab artifacts. The module creates no real orders or writes to the exchange.
