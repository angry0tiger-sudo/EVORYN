# Phase 20 — Intelligence Core / Model Provider Interface

## Implemented contract

The initial Phase 20 branch introduces the standard-library-only contract in
`src/evoryn/intelligence/model_provider.py`:

- `ModelRequest`, `ModelResponse`, `ModelIdentity` and `AdvisoryResult`.
- `ModelTask`, `DataClassification` and explicit `ConfidenceSemantics`.
- `ModelPolicy`: exact model-identity allowlist, task and data-classification allowlists, bounded payload/output, deadline, and optional cost/confidence constraints. Remote providers are disabled by default.
- `StructuredJsonSchema`: required/allowed-field checks, typed top-level fields, size limit and recursively blocked action/credential keys.
- `ModelProviderPort` and `IntelligenceCore`: one provider-independent invocation boundary with identity/schema/budget checks, timeout propagation, response provenance, and explicit abstention on invalid, stale, truncated, over-budget, unauthorized or unaudited outcomes.
- `ModelAuditSink` and `ModelAuditEvent`: sanitized metadata and payload/output fingerprints; raw text, prompts, provider responses and credentials are not audit fields.
- `OpenAICompatibleChatAdapter`: JSON-only adapter using an injected `JsonPostTransport` and a `CredentialHeaderProvider`. It can target any reviewed endpoint implementing the same compatible protocol. It has no bundled HTTP client, makes no network request by itself, disables redirects in its transport contract, and never exposes tool/function calling.
- `HuggingFaceSequenceClassificationProvider`: optional local text classifier supporting the common provider contract. Its Transformers runtime loads only a pinned, locally cached revision (`local_files_only=True`, `trust_remote_code=False`, safetensors); there is no download-at-import or automatic model-hub access.

## Model and provider configuration

A deployment must construct its own `ModelIdentity`, `ModelPolicy`, schema registry, provider adapter and production `ModelAuditSink`. No provider or model is enabled by default. A remote request needs all of the following before transport runs:

1. The exact provider/model/revision is in the approved identity allowlist.
2. The task and declared data classification are explicitly permitted.
3. Training-data rights are marked reviewed, and commercial-use approval is present when the policy requires it.
4. `allow_remote_providers=True` has been explicitly reviewed.
5. A finite `max_cost_usd` ceiling is configured, and cost usage is available or independently estimated by an approved model-specific accounting layer.
6. The input/output schemas, request deadline, payload size and token cap pass validation.

Remote use with an absent cost ceiling is rejected before inference. A timeout or malformed output becomes an abstention; it does not fall back to a stale output or a different model silently.

For local Hugging Face inference, install the optional `news-nlp` extra only where needed. Model and training-data licenses, immutable revisions, artifact digest (where available), data provenance and intended use still require an explicit human review. In particular, this interface does **not** override the existing Financial PhraseBank training-data-rights gate.

## Offline test strategy

`tests/unit/test_phase20_model_provider.py` uses deterministic fake providers, fake credentials, a fake JSON transport, and a fake Hugging Face runtime. Its tests do not download models, access the network, require Torch/Transformers, or contact a cloud LLM. They cover identity changes, data policy, unverified rights, cost limits, schemas and forbidden action fields, deadlines, truncation, token accounting, credential non-disclosure, and audit failure.

`InMemoryModelAuditSink` is for tests and local development only. Production integration must inject an append-only, durable, access-controlled sink and must abstain if the required audit write fails.

## Hard authority boundary

This interface returns only structured advisory data or abstention. The provider ports do not receive exchange, order, execution, risk-limit mutation, or Auto-Halt reset capabilities. AI output is not an order request, risk approval, fill, Paper evidence or Live telemetry. Nothing in this phase authorizes production scheduling or live trading.

## Not included in this phase

- No provider-specific cloud endpoint is selected or activated; OpenRouter remains an evaluation candidate only.
- No production HTTP transport, cloud credential, real provider request or remote-data transfer is configured.
- No specific Hugging Face weight set is downloaded or made active before model/data-rights and task-level evaluation.
- No autonomous tools, agent loop, code deployment, strategy promotion or execution authority.
- No profitability or model-accuracy claim without approved evaluation data and evidence.
