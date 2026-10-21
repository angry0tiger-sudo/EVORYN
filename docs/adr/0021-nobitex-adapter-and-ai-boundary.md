# ADR-0021: EVORYN-Owned Nobitex Adapter and Research-Only AI Integration

- **Status:** Proposed
- **Date:** 2026-10-21
- **Owners:** Project owner and AI/platform engineering
- **Related work:** ADR-0017 (gated Nobitex Spot execution), ADR-0020 (AI Intelligence Core roadmap), Phase 31 Triple-Barrier, Phase 32 cross-fitted Meta-Labeling

## Context

EVORYN already has separate exchange and execution boundaries; the task is not to start another parallel exchange client. The generic `ExchangePort` plugin at `src/evoryn/adapters/exchanges/nobitex.py` is explicitly read-only. A separate candidate gateway lives in `nobitex_spot_trading.py`, with an official instrument-rule reader, signed Ed25519 transport, a SQLite idempotency journal, deterministic Risk Engine checks, and explicit local opt-in gates. The candidate is not yet a production-ready automated runtime: live account/open-order/fill reconciliation after restart, process supervision, operational recovery drills and independent risk/security review remain release gates.

The companion [Novaryn repository](https://github.com/angry0tiger-sudo/Novaryn) provides an additional reference implementation with public/private API classes and a Freqtrade integration boundary. Its current README explicitly reports `VALIDATION_STATE = VALIDATION_BLOCKED_EXTERNAL` and `OWNER_AUTHORIZATION_REQUIRED`; its code must therefore be treated as an implementation reference, not independent proof of production readiness or verified real-order performance.

EVORYN Phase 32 is research-only. The model is not a Runtime dependency and no candidate is selected. The earlier regime experiment showed why classification scores cannot be treated as trading evidence: its XGBoost score largely reproduced its label definition while its cost-adjusted strategy failed economic acceptance. Phase 32 therefore keeps a predeclared, artifact-backed evaluation protocol.

## Decision

### 1. Keep EVORYN as the policy and safety authority

- Preserve the general `ExchangePort` as read-only for Nobitex.
- Keep write-capable operations isolated in the dedicated, fixed-route Spot gateway and invoked only through `LiveSpotExecutionService`.
- Keep the supported live-candidate scope at Nobitex Spot/USDT, explicit Limit orders only. Do not enable Market, Margin, Futures, transfer or withdrawal capabilities as part of this phase.
- Preserve exact official-host HTTPS validation, redirect rejection, key-pair verification, bounded response handling, price/risk/instrument-rule checks, a durable pre-submit journal claim, and the no-blind-retry rule for ambiguous writes.
- Do not expose a generic signed `request(method, path, payload)` method or register the write gateway as an unrestricted general exchange plugin.
- Do not change a feature flag, owner acknowledgement, or any live order setting as part of Phase 32.

### 2. Use Novaryn as a comparison source, not as a reason to broaden write permissions

Relevant design patterns to compare in Novaryn's `src/novaryn/infrastructure/exchange/nobitex/` include separate public/private clients, explicit domain mapping, Ed25519 signing, client-order IDs, order lookup, open-order listing, and normalized account/order snapshots. These are useful checklist items for EVORYN's remaining account/order/fill reconciliation.

Do **not** copy Novaryn's broader Market-order surface into EVORYN without a separate API-contract review, risk review, and deterministic Paper/Mock tests. EVORYN's more restrictive Limit-only gateway is intentional. Never weaken EVORYN's host pinning, redirect policy or no-blind-retry semantics merely to match the reference client.

Before any owner-supervised live validation, close the following independently:
1. verify current Nobitex signed-route and status semantics against official documentation and sanitized real responses;
2. reconcile starting balances, open orders, closed orders and fills, including partial fills, fee currency and pagination;
3. prove restart/reconnect behavior on accepted, rejected and outcome-unknown submissions without duplicate writes;
4. verify the supported instrument's quantity step, price tick and minimum notional from fresh exchange rules;
5. run independent security/risk review and an owner-controlled, least-privilege validation. This ADR grants no live-trading authorization.

### 3. Do not embed FreqAI as a standalone runtime dependency yet

The [FreqAI documentation](https://docs.freqtrade.io/en/stable/freqai/) describes adaptive retraining, feature engineering and model lifecycle as a subsystem used through Freqtrade's bot/strategy/configuration lifecycle, with a separate FreqAI dependency set. It is not documented as a stable, framework-independent library API for arbitrary host systems.

Consequently:
- Do not add Freqtrade or FreqAI to EVORYN's core dependencies or import FreqAI internal modules from Runtime.
- Continue to use EVORYN-owned model-provider, experiment-registry, dataset/model digest and promotion contracts.
- Reuse mature underlying libraries through existing optional extras (scikit-learn, XGBoost, LightGBM, Optuna) where compatible, and borrow FreqAI's documented ideas for feature expansion, adaptive retraining and inference/training separation.
- A future isolated compatibility spike may test a narrowly scoped adapter only if a pinned FreqAI version exposes a supported import surface, its dependency/license effects are understood, and the integration can be tested without changing EVORYN's deterministic risk boundary. This is not approved by the present ADR.
- Do not copy GPL-covered Freqtrade/FreqAI implementation code into EVORYN without an explicit license and distribution review.

### 4. Preserve and harden Phase 32's anti-leakage protocol

[Jesse's Meta-Labeling documentation](https://docs.jesse.trade/docs/research/ml/meta-labeling) reinforces three relevant constraints: the primary signal and meta-model have distinct responsibilities; the meta-model uses observations captured at signal time; and a meta-model is specific to the primary signal that produced its training events. Its Triple-Barrier description treats the future path as a label outcome, never as an event-time feature.

EVORYN must continue to enforce:
- immutable event-time feature snapshots from completed candles only;
- frozen barrier geometry determined from data available at the event timestamp;
- inclusive event-label intervals and train/validation/test purging with an explicit embargo;
- chronological outer test folds that are not used for model fitting, probability calibration or threshold selection;
- probability calibration on a separate earlier validation slice, purged from the threshold-selection slice;
- train-only fitting for any future scaler, selector, resampler, class-weight estimator or hyperparameter search;
- if the primary signal ever becomes a fitted model, chronological out-of-fold primary predictions must be generated before training the meta-model. The present SMA-12/48 primary signal is deterministic and has no fitted parameters, so the current implementation does not need a learned-primary cross-fit step;
- primary-signal parameters, feature schema, labeling configuration and data identity must be included in immutable model/report provenance. Any change to the primary signal or its labeling contract invalidates old meta-label artifacts.

For clarity, the current EVORYN target is **cost-adjusted positive net event return**, not exactly the canonical Jesse convention in which a positive meta-label means that the profit barrier was hit and stop/time expiry map to zero. That is a deliberate target-definition difference and must remain explicit in reports. A future change to a profit-barrier-only target requires a new schema/version and a fresh, predeclared empirical run rather than silently relabeling old data.

### 5. Make the classification gate mandatory, without relaxing economic gates

Phase 32 may be marked eligible for independent review only when **both** gates pass:
- the pooled out-of-fold Average Precision is strictly greater than pooled positive-label prevalence (no-skill baseline); and
- all existing economic conditions pass: full expected-fold coverage, minimum closed-trade count, positive mean net return, positive lower 95% paired-fold return-difference bounds versus both the unfiltered primary strategy and Buy-and-Hold, and predeclared drawdown/tail-loss limits.

Missing/invalid AP or prevalence fails the classification gate. Equality with prevalence is not a pass. A classification pass cannot compensate for failed economics, and an economic pass cannot compensate for a failed classification gate. Runtime promotion remains explicitly false even if both gates pass; such an outcome is only eligibility for independent review.

## Consequences

- EVORYN retains authority over policy, execution, risk, reconciliation and model promotion.
- Novaryn's API separation and order-reconciliation ideas can inform the connector review without expanding current permissions.
- FreqAI remains a design reference rather than a new core dependency; EVORYN avoids coupling its runtime to Freqtrade internals.
- Phase 32 reports use schema version 2 because the acceptance report now includes the classification gate and its prevalence baseline.
- No model is promoted and no exchange writes are enabled by this ADR.

## Required evidence before phase acceptance

- Exact-head CI passing on the updated source/test commit.
- The Phase 32 empirical run on the pinned dataset, with complete eight-fold evidence and preserved logs/artifacts.
- Every classification and economic gate reported explicitly; failed candidates remain rejected with Champion set to none.
- Independent review of event labeling, fold purging, target semantics, report schema, and economic simulation limitations.
- Separate resolution of exchange account/fill reconciliation and owner-host acceptance before any live execution claim.
