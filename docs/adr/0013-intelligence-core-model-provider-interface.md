# ADR 0013: Intelligence Core and Provider-Neutral Model Interface

- Status: Proposed for independent architecture, security, privacy and risk review
- Date: 2026-10-19
- Scope: Phase 20 — Intelligence Core foundation and Model Provider Interface
- Depends on: Phases 13–17 research/provenance contracts, ADR 0008 (regime abstention), ADR 0009 adaptive strategy selection and licensed news ablation, ADR 0010 (Learning Lab), ADR 0011 (connectivity boundaries), and ADR 0012 (read-only Shadow Mode; currently proposed on the dependent Phase 19 branch).
- Activation status: Contract/design only. No cloud LLM provider is selected or enabled by this ADR. Phase 19 is not yet accepted or merged; Phase 20 must not be treated as permission to bypass its release gates.

## Context

EVORYN already contains research-oriented intelligence building blocks: a causal regime detector and optional HMM adapter, strategy-selection contracts, an optional local financial-news sentiment adapter, licensed external-data provenance, and the Learning Lab experiment/Champion-Challenger workflow. These components have different inputs and evidence requirements. They must not become coupled directly to a vendor SDK or to an unconstrained text-generation API.

The next layer needs a stable provider-neutral contract for local open-source ML/NLP models and, if separately reviewed, remote language models. The interface must normalize identity, request limits, output structure, timeouts, usage metadata and abstention. It must also preserve EVORYN's decisive boundary: AI may provide advice, but deterministic risk controls and the execution authorization chain remain independent and authoritative.

No canonical implementation or approval for OpenRouter, another hosted LLM service, or a general-purpose autonomous agent was found in the current `main` architecture records. This ADR therefore defines a provider-selection boundary and evaluation gates; it does not claim a cloud provider has been selected, configured, or called.

## Decision

### 1. Provider-neutral domain contract

Introduce a core-owned, dependency-light contract in a later implementation change. Exact names may be refined during interface review, but the contract must separate these concepts:

- `ModelRequest`: request/correlation ID, task/capability, UTC deadline, versioned input-schema ID, bounded normalized inputs, declared data classification and an explicit output-schema ID.
- `ModelResponse`: provider/model identity, immutable revision when available, request ID, start/completion timestamps, latency, normalized structured output, finish/status metadata and usage/cost metadata where the provider exposes it.
- `ModelProviderPort`: one normalized invocation method with no vendor request/response types in its public signature.
- `ModelIdentity`: provider ID, model ID, immutable revision or local artifact digest, tokenizer/runtime identity where relevant, model/license review identity, and prompt/template/schema fingerprints.
- `AdvisoryResult`: typed, bounded recommendations plus provenance, confidence semantics and explicit abstention reason. It must not contain an executable order, an exchange client, credentials, a RiskLimits mutation, or an Auto-Halt operation.
- `ModelPolicy`: allowed task, allowed data classes, maximum input/output size, deadline, retry policy, cost/token budget, confidence/abstention thresholds and permitted provider/model identities.
- `ModelAuditSink`: append-only recording of sanitized request metadata, model identity, policy decision, completion/timeout/invalid-output/abstention, latency and usage. Raw secrets and unapproved source text must not be copied into logs.

Core interfaces use standard Python types and EVORYN domain-owned immutable records. Provider SDK objects must be converted at the adapter boundary. Optional ML frameworks and cloud-provider SDKs must not be required for importing the core package.

### 2. Separate model capabilities; do not conflate ML and LLMs

The interface distinguishes at least:

- **Numerical/classification inference:** probabilities, labels, embeddings or other explicitly versioned numeric outputs from local/open-source ML models.
- **NLP task inference:** sentiment or extraction from licensed text under a reviewed label/schema contract.
- **LLM advisory:** structured, bounded language-model output for a specifically approved analytical task. This is not an execution agent and has no tool/function capability that can call exchange, broker, risk mutation, filesystem-write, GitHub-write or credential interfaces.

The interface must not turn LLM-generated prose into a trading signal by heuristic parsing. Any LLM advisory consumed downstream must pass a strict schema validator, provenance/identity checks, task-specific confidence policy, freshness checks and deterministic domain gates. Unsupported, malformed, partial, contradictory or uncertain outputs abstain.

### 3. Provider adapters and vendor neutrality

- Local Hugging Face/GitHub-sourced model adapters are the preferred evaluation path where the task can be met with a reviewed open-source model. Each candidate must be pinned to an immutable upstream revision and verified artifact digest where available; no model download, model loading or network inference occurs at import time.
- Remote LLM providers are optional adapters behind `ModelProviderPort`. Provider-specific model names, SDKs, authentication formats, rate-limit headers and error types must not leak into strategy, regime, RiskEngine, Paper Broker, Shadow or Learning Lab core logic.
- OpenRouter is an **evaluation candidate only**, not a selected or enabled provider. It may be compared with other providers or with self-hosted/local inference after documenting endpoint/region, retention/training terms, data handling, supported schema/structured-output guarantees, availability, rate limits, model immutability, latency, cost and contractual rights for the intended use.
- No provider fallback may silently change model identity or privacy boundary. Failover, if approved later, must be explicit, separately configured, independently allowlisted, and recorded as a new model/provider identity in the evidence.
- No cloud provider credential or remote inference call is added by this ADR. If a provider is selected later, its adapter is a separate, reviewed implementation behind the same interface.

### 4. Open-source model and training-data governance

For every Hugging Face/GitHub model and local inference dependency, record:

- canonical upstream repository/model card, immutable revision/commit, artifact/weight digest and source retrieval date;
- licenses/terms for code, weights, tokenizer, training/fine-tuning datasets and required notices/attribution;
- provenance and restrictions for source data, commercial-use permission, redistribution/hosting rights and applicable retention terms;
- Python/framework compatibility, hardware/RAM/storage footprint, inference latency, model-load cost, CPU/GPU assumptions, deterministic/reproducible settings and security advisories;
- expected input domain, language, feature schema, known limitations, evaluation dataset identity, test ranges and results including abstention/coverage.

A permissive weight license does not prove that training-data rights are permissive. The existing Phase 16 financial-news candidate's training-data-rights flag remains false by default until the explicit review gate is satisfied. Phase 20 must not weaken this policy.

### 5. Remote data, privacy and prompt-injection boundary

Before sending any input to a remote provider, the caller must establish that the specific data source and usage rights allow that transfer. Data classification must default to denied for remote inference unless an explicit policy grants the exact task and provider. Private account balances, order/position details, identities, credentials, private repository contents and raw licensed article text must not be sent remotely by default.

- Credentials are injected only through an approved secret provider and must never enter prompts, model responses, logs, exception messages or experiment artifacts.
- External news, web text, model output and retrieved content are untrusted data, never instructions that override the system policy. Ignore embedded tool requests, authorization claims or attempts to expose secrets.
- No model gets general tool use, arbitrary URL fetch, shell/SQL execution, filesystem write, repository write, secrets access or exchange-operation tools.
- Requests must be bounded by size, deadline and budget. No unbounded context growth, recursive agent loop or automatic retry storm is permitted.
- Audit telemetry stores sanitized metadata by default; retention of raw prompts/responses requires separate explicit approval, source rights, data minimization and retention/deletion controls.

### 6. Output validation, confidence and abstention

Every result must be rejected or abstained if any of the following holds:

- the provider/model/revision is not on the explicit allowlist or its approval has expired;
- task, data classification, input schema, feature schema or output schema does not match the registered policy;
- timeout, rate limit beyond the bounded policy, network error, invalid encoding/JSON, missing required field, non-finite number, out-of-range value, unexplained extra field or impossible semantic combination occurs;
- model identity, prompt fingerprint, tokenizer/artifact digest or relevant configuration changes during a run;
- data is stale, malformed, unlicensed, mismatched, future-dated or insufficient for the task;
- confidence is unavailable, below the predeclared threshold or has semantics not accepted by the task policy;
- usage/cost budget, context limit, safety policy, license permission or audit persistence cannot be verified.

For classification, the contract must distinguish calibrated probability, model posterior, heuristic score and unavailable confidence. Do not represent LLM-generated self-reported confidence as a calibrated probability. If the provider cannot supply defensible confidence for a task, use a task-specific independent validation gate or abstain.

Provider/network failure must not silently fall back to a stale result, heuristic guess, different model identity, live strategy or forced action. Any fallback is a separate explicit policy with its own provenance and validation.

### 7. Strict advisory-only and financial safety boundary

This ADR preserves the following hard boundaries:

- AI output is advisory, not authorization. It can only produce a typed recommendation or abstention.
- No model or provider adapter imports or receives an exchange execution port, `OrderRequest` submission function, live-order routing API, withdrawal/account mutation, hard-risk-limit setter or Auto-Halt clear/reset function.
- Only the existing deterministic RiskEngine and separately governed execution path may determine whether an action is allowed. The model cannot alter risk thresholds, override REJECT/HALT, resume a halted system, or convert uncertainty into an approval.
- Market intelligence, strategy selection, NLP and LLM results remain distinct from actual Paper/Shadow/Live telemetry. Model output must never be mislabeled as a fill, closed trade, trusted Paper observation or live execution.
- Model-driven source-code changes, strategy deployments, registry promotions and provider configuration changes cannot be made automatically by the model. Existing human review, immutable evidence and governed rollout gates remain mandatory.
- Phase 20 does not enable live trading and does not authorize a provider to receive live trading credentials.

### 8. Reproducibility, observability and cost controls

For each inference event, record where applicable: correlation/request ID, UTC start/end/deadline, task and schema versions, data/source provenance fingerprint, provider/model/revision, local artifact/tokenizer/runtime fingerprint, prompt/template fingerprint, relevant generation settings, validated response fingerprint, abstention/error reason, latency, usage/cost metadata, retry count and policy version.

Records must distinguish attempted, completed, validated, abstained, rejected and audit-failed requests. Audit failure is fail-closed for any downstream process that requires a trustworthy evidence record.

Local model inference and remote calls are separately metered. Default remote request budget is zero until a provider/task is explicitly approved. Each cloud adapter must support hard per-request limits and a configurable spend/usage ceiling; unknown pricing or usage measurement is a release blocker for cost-sensitive deployment.

### 9. Scope of Phase 20

Phase 20 is divided into controlled gates:

1. **G0 — Contract review:** finalize domain models/protocol and data classification without provider dependencies.
2. **G1 — Offline contract tests:** fake providers, deterministic fixtures, schema failures, timeouts, identity changes, malformed responses and audit failure. No external network calls.
3. **G2 — Local open-source adapter assessment:** select at most one suitable task-specific model with immutable revision and approved model/data rights; keep runtime optional and offline by default.
4. **G3 — Remote LLM provider evaluation:** compare OpenRouter and other candidates against privacy, contract, immutable identity, structured-output reliability, latency, cost and retention criteria. Do not enable remote data flow or pick a provider until the review is approved.
5. **G4 — Advisory integration in research/Shadow only:** integrate through typed outputs with abstention and no execution capability, only after Phase 19 is independently accepted and the input data/provider is authorized.
6. **G5 — Evidence/review:** execute pytest, Ruff, strict mypy, Bandit, dependency audit and provider-specific compliance/risk review on a trusted runner; retain artifacts and exact commit/model identities.

This ADR does not authorize completing these gates in one unreviewed change. Each model family and remote provider needs an explicit allowlist decision and task-level evaluation. The project may complete the contract and offline tests before choosing a cloud provider.

## Alternatives considered

- **Direct provider SDK calls from strategies/regime logic:** rejected because it couples domain logic to vendor APIs, encourages inconsistent error handling, complicates testing and weakens provenance boundaries.
- **Select OpenRouter as the default now:** deferred because no completed provider-specific privacy/cost/retention/latency comparison or approved remote data classification is recorded in the repository.
- **LLM as an autonomous trading agent with tools:** rejected because arbitrary tool use and vendor-generated decisions must not bypass deterministic risk or exchange permission boundaries.
- **Require every model to be remote:** rejected because optional local open-source ML/NLP supports lower data-exposure risk and reproducible research where compute permits.
- **Make all ML libraries base dependencies:** rejected because the core must remain importable/testable without heavyweight optional frameworks.
- **Implicit failover to a different model:** rejected because it changes model identity and possibly data handling without explicit approval and comparable validation.

## Consequences and known limits

- Core gains a stable contract but no provider SDK, cloud credentials or network endpoint.
- Model adapters remain replaceable, optionally installed, and separately reviewed.
- Existing Phase 14/16 model limitations remain unresolved until their own rights and evaluation gates pass.
- Provider-neutral design does not itself guarantee equal outputs, reproducibility of hosted inference, privacy, license compliance or model quality.
- Current Phase 19 branch is not yet accepted; Phase 20 advisory integration and live feed activation remain gated.
- All conclusions about accuracy/performance require authorized versioned data, predeclared holdout/walk-forward evaluation, and observed run artifacts. No profitability claim is created by an LLM interface.

## Rollback

Remove the Phase 20 contracts/adapters and this ADR/index entry without changing the Phase 14/16 model implementations, Learning Lab records, RiskEngine, Auto-Halt or exchange adapters. A provider configuration can be disabled independently; core behavior must return to the prior deterministic/research path or abstain, never to forced execution.
