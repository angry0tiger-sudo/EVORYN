"""Offline Phase 20 contract and adapter tests; no network or model weights are used."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from evoryn.intelligence.huggingface_provider import HuggingFaceSequenceClassificationProvider
from evoryn.intelligence.model_provider import (
    ConfidenceSemantics,
    DataClassification,
    InMemoryModelAuditSink,
    IntelligenceCore,
    ModelAuditEvent,
    ModelAuditEventType,
    ModelIdentity,
    ModelPolicy,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelTask,
    OpenAICompatibleChatAdapter,
    StructuredJsonSchema,
    TokenUsage,
    canonical_json_bytes,
)

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
INPUT_SCHEMA = StructuredJsonSchema(
    schema_id="test.input.text.v1",
    required_fields=("text",),
    allowed_fields=("text",),
    field_types=(("text", "string"),),
    forbidden_keys=frozenset(),
)
OUTPUT_SCHEMA = StructuredJsonSchema(
    schema_id="test.advisory.v1",
    required_fields=("summary", "suggestion"),
    allowed_fields=("summary", "suggestion"),
    field_types=(("summary", "string"), ("suggestion", "string")),
    max_bytes=2_048,
)


def identity(
    provider_id: str = "local-test",
    model_id: str = "test-classifier",
    revision: str = "snapshot-2026-10-19",
    *,
    rights_verified: bool = True,
    commercial_approved: bool = False,
) -> ModelIdentity:
    return ModelIdentity(
        provider_id=provider_id,
        model_id=model_id,
        revision=revision,
        model_license_id="Apache-2.0",
        license_review_id="review:offline-test",
        training_dataset_id="synthetic-test-fixture",
        training_dataset_revision="fixture-v1",
        training_data_license_id="CC0-1.0",
        training_data_rights_verified=rights_verified,
        commercial_use_approved=commercial_approved,
    )


def request_for(
    provider_id: str = "local-test",
    *,
    task: ModelTask = ModelTask.LLM_ADVISORY,
    payload: Mapping[str, Any] | None = None,
    created_at: datetime = NOW,
    deadline_at: datetime = NOW + timedelta(seconds=10),
    input_schema_id: str = INPUT_SCHEMA.schema_id,
    output_schema_id: str = OUTPUT_SCHEMA.schema_id,
    data_classification: DataClassification = DataClassification.PUBLIC,
    max_output_tokens: int = 32,
) -> ModelRequest:
    return ModelRequest(
        request_id="req:test:001",
        correlation_id="corr:test:001",
        provider_id=provider_id,
        task=task,
        created_at=created_at,
        deadline_at=deadline_at,
        input_schema_id=input_schema_id,
        output_schema_id=output_schema_id,
        data_classification=data_classification,
        input_payload=payload or {"text": "Synthetic public research input."},
        max_output_tokens=max_output_tokens,
    )


class FakeProvider:
    def __init__(
        self,
        model_identity: ModelIdentity,
        *,
        output: Mapping[str, Any] | None = None,
        remote: bool = False,
        finish_reason: str = "completed",
        truncated: bool = False,
        completed_at: datetime = NOW + timedelta(milliseconds=250),
        response_identity: ModelIdentity | None = None,
        usage: TokenUsage | None = None,
        error: Exception | None = None,
    ) -> None:
        self._identity = model_identity
        self._output = output or {
            "summary": "synthetic summary",
            "suggestion": "abstain unless deterministic gates pass",
        }
        self._remote = remote
        self._finish_reason = finish_reason
        self._truncated = truncated
        self._completed_at = completed_at
        self._response_identity = response_identity or model_identity
        self._usage = usage
        self._error = error
        self.calls: list[tuple[str, float]] = []

    @property
    def identity(self) -> ModelIdentity:
        return self._identity

    @property
    def is_remote(self) -> bool:
        return self._remote

    @property
    def supported_tasks(self) -> frozenset[ModelTask]:
        return frozenset({ModelTask.LLM_ADVISORY, ModelTask.CLASSIFICATION, ModelTask.SENTIMENT})

    def invoke(self, request: ModelRequest, *, timeout_seconds: float) -> ModelResponse:
        self.calls.append((request.request_id, timeout_seconds))
        if self._error is not None:
            raise self._error
        return ModelResponse(
            request_id=request.request_id,
            identity=self._response_identity,
            task=request.task,
            output_schema_id=request.output_schema_id,
            output=self._output,
            started_at=NOW,
            completed_at=self._completed_at,
            finish_reason=self._finish_reason,
            truncated=self._truncated,
            confidence=Decimal("0.85"),
            confidence_semantics=ConfidenceSemantics.MODEL_POSTERIOR,
            usage=self._usage,
        )


def make_core(
    provider: Any,
    model_policy: ModelPolicy | None = None,
    *,
    audit_sink: Any | None = None,
    schemas: tuple[StructuredJsonSchema, ...] = (INPUT_SCHEMA, OUTPUT_SCHEMA),
) -> tuple[IntelligenceCore, InMemoryModelAuditSink]:
    sink = audit_sink or InMemoryModelAuditSink()
    policy = model_policy or ModelPolicy(
        allowed_tasks=frozenset({ModelTask.LLM_ADVISORY}),
        allowed_data_classifications=frozenset({DataClassification.PUBLIC}),
        allowed_identities=(provider.identity,),
    )
    return (
        IntelligenceCore(
            providers=(provider,),
            policy=policy,
            schemas=schemas,
            audit_sink=sink,
        ),
        sink,
    )


def test_core_returns_typed_advisory_and_only_sanitized_audit() -> None:
    provider = FakeProvider(identity())
    core, sink = make_core(provider)

    result = core.evaluate(request_for(), now=NOW)

    assert result.abstained is False
    assert result.reason_code == "ADVISORY_VALIDATED"
    assert result.recommendation == {
        "summary": "synthetic summary",
        "suggestion": "abstain unless deterministic gates pass",
    }
    assert result.identity == provider.identity
    assert result.output_fingerprint is not None
    assert [event.event_type for event in sink.events] == [
        ModelAuditEventType.REQUEST_RECEIVED,
        ModelAuditEventType.INFERENCE_STARTED,
        ModelAuditEventType.RESPONSE_VALIDATED,
    ]
    assert all(not hasattr(event, "input_payload") for event in sink.events)
    assert "Synthetic public research input." not in repr(sink.events)


def test_provider_can_be_switched_without_changing_core_contract() -> None:
    left_identity = identity("local-left", "model-left", "r1")
    right_identity = identity("local-right", "model-right", "r2")
    left = FakeProvider(left_identity, output={"summary": "left", "suggestion": "observe"})
    right = FakeProvider(right_identity, output={"summary": "right", "suggestion": "abstain"})
    policy = ModelPolicy(
        allowed_tasks=frozenset({ModelTask.LLM_ADVISORY}),
        allowed_data_classifications=frozenset({DataClassification.PUBLIC}),
        allowed_identities=(left_identity, right_identity),
    )
    core, _ = make_core(left, policy)
    # A provider registry can be replaced through construction without changing ModelRequest/response.
    switched = IntelligenceCore(
        providers=(left, right),
        policy=policy,
        schemas=(INPUT_SCHEMA, OUTPUT_SCHEMA),
        audit_sink=InMemoryModelAuditSink(),
    )

    left_result = core.evaluate(request_for("local-left"), now=NOW)
    right_result = switched.evaluate(request_for("local-right"), now=NOW)

    assert left_result.recommendation is not None
    assert left_result.recommendation["summary"] == "left"
    assert right_result.recommendation is not None
    assert right_result.recommendation["summary"] == "right"


def test_remote_provider_is_disabled_by_default_and_never_invoked() -> None:
    provider = FakeProvider(identity("remote-test"), remote=True)
    core, _ = make_core(provider)

    result = core.evaluate(request_for("remote-test"), now=NOW)

    assert result.abstained is True
    assert result.reason_code == "REMOTE_PROVIDER_DISABLED"
    assert provider.calls == []


def test_remote_provider_requires_an_explicit_cost_ceiling_and_usage() -> None:
    model_identity = identity("remote-test")
    provider = FakeProvider(model_identity, remote=True)
    policy = ModelPolicy(
        allowed_tasks=frozenset({ModelTask.LLM_ADVISORY}),
        allowed_data_classifications=frozenset({DataClassification.PUBLIC}),
        allowed_identities=(model_identity,),
        allow_remote_providers=True,
    )
    core, _ = make_core(provider, policy)

    result = core.evaluate(request_for("remote-test"), now=NOW)

    assert result.abstained is True
    assert result.reason_code == "REMOTE_COST_BUDGET_UNCONFIGURED"
    assert provider.calls == []


def test_unverified_training_data_rights_block_inference() -> None:
    model_identity = identity(rights_verified=False)
    provider = FakeProvider(model_identity)
    core, _ = make_core(provider)

    result = core.evaluate(request_for(), now=NOW)

    assert result.abstained is True
    assert result.reason_code == "TRAINING_DATA_RIGHTS_UNVERIFIED"
    assert provider.calls == []


def test_commercial_use_requires_separate_approval() -> None:
    model_identity = identity(commercial_approved=False)
    provider = FakeProvider(model_identity)
    policy = ModelPolicy(
        allowed_tasks=frozenset({ModelTask.LLM_ADVISORY}),
        allowed_data_classifications=frozenset({DataClassification.PUBLIC}),
        allowed_identities=(model_identity,),
        require_commercial_approval=True,
    )
    core, _ = make_core(provider, policy)

    result = core.evaluate(request_for(), now=NOW)

    assert result.abstained is True
    assert result.reason_code == "COMMERCIAL_USE_NOT_APPROVED"
    assert provider.calls == []


def test_confidential_input_is_rejected_before_remote_transport() -> None:
    model_identity = identity("remote-test")
    provider = FakeProvider(model_identity, remote=True)
    policy = ModelPolicy(
        allowed_tasks=frozenset({ModelTask.LLM_ADVISORY}),
        allowed_data_classifications=frozenset({DataClassification.PUBLIC}),
        allowed_identities=(model_identity,),
        allow_remote_providers=True,
        max_cost_usd=Decimal("0.10"),
    )
    core, _ = make_core(provider, policy)

    result = core.evaluate(
        request_for("remote-test", data_classification=DataClassification.CONFIDENTIAL),
        now=NOW,
    )

    assert result.abstained is True
    assert result.reason_code == "DATA_CLASSIFICATION_NOT_ALLOWED"
    assert provider.calls == []


def test_expired_deadline_abstains_without_calling_provider() -> None:
    provider = FakeProvider(identity())
    core, _ = make_core(provider)
    request = request_for(
        created_at=NOW - timedelta(seconds=10),
        deadline_at=NOW - timedelta(seconds=1),
    )

    result = core.evaluate(request, now=NOW)

    assert result.abstained is True
    assert result.reason_code == "DEADLINE_EXPIRED"
    assert provider.calls == []


def test_input_schema_and_size_gates_fail_closed() -> None:
    provider = FakeProvider(identity())
    core, _ = make_core(provider)
    bad_schema = request_for(payload={"unexpected": "value"})
    too_large = request_for(payload={"text": "x" * 20_000})

    missing_field_result = core.evaluate(bad_schema, now=NOW)
    size_result = core.evaluate(too_large, now=NOW)

    assert missing_field_result.abstained is True
    assert missing_field_result.reason_code == "INPUT_SCHEMA_INVALID"
    assert size_result.abstained is True
    assert size_result.reason_code == "INPUT_SIZE_EXCEEDED"
    assert provider.calls == []


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ({"summary": "only one field"}, "Required output fields are missing."),
        (
            {"summary": "ok", "suggestion": "ok", "submit_order": True},
            "Output contains fields outside the declared schema.",
        ),
        (
            {
                "summary": "ok",
                "suggestion": "ok",
                "metadata": {"clear_auto_halt": True},
            },
            "Output contains fields outside the declared schema.",
        ),
    ],
)
def test_output_schema_failures_abstain(
    output: Mapping[str, Any], expected: str
) -> None:
    provider = FakeProvider(identity(), output=output)
    core, _ = make_core(provider)

    result = core.evaluate(request_for(), now=NOW)

    assert result.abstained is True
    assert result.reason_code == expected
    assert provider.calls


def test_nested_forbidden_action_fields_are_rejected_by_schema() -> None:
    schema = StructuredJsonSchema(
        schema_id="nested.v1",
        required_fields=("recommendation",),
        allowed_fields=("recommendation",),
        field_types=(("recommendation", "object"),),
    )

    reason = schema.validate({"recommendation": {"submit_order": True}})

    assert reason == "Forbidden advisory field: submit_order."


def test_model_identity_drift_and_truncation_abstain() -> None:
    approved = identity()
    drifted = identity(model_id="different-model")
    provider = FakeProvider(approved, response_identity=drifted)
    core, _ = make_core(provider)
    drift_result = core.evaluate(request_for(), now=NOW)

    truncated_provider = FakeProvider(approved, truncated=True, finish_reason="length")
    truncated_core, _ = make_core(truncated_provider)
    truncated_result = truncated_core.evaluate(request_for(), now=NOW)

    assert drift_result.abstained is True
    assert drift_result.reason_code == "MODEL_IDENTITY_MISMATCH"
    assert truncated_result.abstained is True
    assert truncated_result.reason_code == "OUTPUT_TRUNCATED"


def test_provider_exception_is_sanitized_and_becomes_abstention() -> None:
    provider = FakeProvider(identity(), error=RuntimeError("secret=do-not-log"))
    core, sink = make_core(provider)

    result = core.evaluate(request_for(), now=NOW)

    assert result.abstained is True
    assert result.reason_code == "PROVIDER_ERROR"
    assert "do-not-log" not in repr(sink.events)
    assert "do-not-log" not in result.reason_code


class FailingAuditSink:
    def __init__(self, fail_at: ModelAuditEventType) -> None:
        self.fail_at = fail_at
        self.events: list[ModelAuditEvent] = []

    def record(self, event: ModelAuditEvent) -> None:
        if event.event_type is self.fail_at:
            raise OSError("private storage path")
        self.events.append(event)


def test_audit_failure_before_invocation_prevents_provider_call() -> None:
    provider = FakeProvider(identity())
    sink = FailingAuditSink(ModelAuditEventType.REQUEST_RECEIVED)
    core, _ = make_core(provider, audit_sink=sink)

    result = core.evaluate(request_for(), now=NOW)

    assert result.abstained is True
    assert result.reason_code == "AUDIT_UNAVAILABLE"
    assert provider.calls == []


def test_audit_failure_after_inference_withholds_recommendation() -> None:
    provider = FakeProvider(identity())
    sink = FailingAuditSink(ModelAuditEventType.RESPONSE_VALIDATED)
    core, _ = make_core(provider, audit_sink=sink)

    result = core.evaluate(request_for(), now=NOW)

    assert result.abstained is True
    assert result.reason_code == "AUDIT_UNAVAILABLE"
    assert provider.calls
    assert result.recommendation is None


def test_model_policy_requires_unique_identities_and_revision_not_floating_alias() -> None:
    with pytest.raises(ValueError, match="immutable"):
        identity(revision="latest")
    with pytest.raises(ValueError, match="duplicates"):
        ModelPolicy(
            allowed_tasks=frozenset({ModelTask.SENTIMENT}),
            allowed_data_classifications=frozenset({DataClassification.PUBLIC}),
            allowed_identities=(identity(), identity()),
        )


class FakeCredentials:
    def __init__(self, headers: Mapping[str, str] | None = None) -> None:
        self._headers = headers or {"Authorization": "Bearer fake-secret-token"}

    def headers_for(self, provider_id: str) -> Mapping[str, str]:
        assert provider_id == "llm-compatible"
        return self._headers


class FakeJsonTransport:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
        timeout_seconds: float,
        allow_redirects: bool,
    ) -> Mapping[str, Any]:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
                "allow_redirects": allow_redirects,
            }
        )
        return self.response


def test_openai_compatible_adapter_uses_injected_transport_and_never_exposes_credentials() -> None:
    model_identity = identity("llm-compatible", "fixture-llm", "fixture-snapshot-1")
    response_body = {
        "model": "fixture-llm",
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"summary": "synthetic response", "suggestion": "remain advisory"}
                    )
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "estimated_cost_usd": "0.001",
        },
    }
    transport = FakeJsonTransport(response_body)
    clock_values = iter((NOW, NOW + timedelta(milliseconds=300)))
    adapter = OpenAICompatibleChatAdapter(
        identity=model_identity,
        base_url="https://gateway.example.test/v1",
        transport=transport,  # type: ignore[arg-type]
        credentials=FakeCredentials(),
        schemas=(OUTPUT_SCHEMA,),
        clock=lambda: next(clock_values),
    )
    request = request_for("llm-compatible")
    response = adapter.invoke(request, timeout_seconds=2.0)

    assert response.identity == model_identity
    assert response.output["summary"] == "synthetic response"
    assert response.usage == TokenUsage(11, 7, Decimal("0.001"))
    assert transport.calls[0]["url"] == "https://gateway.example.test/v1/chat/completions"
    assert transport.calls[0]["allow_redirects"] is False
    assert transport.calls[0]["timeout_seconds"] == 2.0
    assert "fake-secret-token" not in canonical_json_bytes(transport.calls[0]["body"]).decode()
    assert "tools" not in transport.calls[0]["body"]
    assert "execution authority" in transport.calls[0]["body"]["messages"][0]["content"]


def test_openai_compatible_adapter_abstains_on_identity_drift_and_invalid_json() -> None:
    model_identity = identity("llm-compatible", "fixture-llm", "fixture-snapshot-1")
    changed_model = {
        "model": "unapproved-remote-model",
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
    }
    adapter = OpenAICompatibleChatAdapter(
        identity=model_identity,
        base_url="https://gateway.example.test/v1",
        transport=FakeJsonTransport(changed_model),  # type: ignore[arg-type]
        credentials=FakeCredentials(),
        schemas=(OUTPUT_SCHEMA,),
        clock=lambda: NOW,
    )
    with pytest.raises(ModelProviderError, match="identity changed"):
        adapter.invoke(request_for("llm-compatible"), timeout_seconds=1.0)

    invalid_json = {
        "model": "fixture-llm",
        "choices": [{"message": {"content": "not-json"}, "finish_reason": "stop"}],
    }
    bad_adapter = OpenAICompatibleChatAdapter(
        identity=model_identity,
        base_url="https://gateway.example.test/v1",
        transport=FakeJsonTransport(invalid_json),  # type: ignore[arg-type]
        credentials=FakeCredentials(),
        schemas=(OUTPUT_SCHEMA,),
        clock=lambda: NOW,
    )
    with pytest.raises(ModelProviderError, match="valid JSON"):
        bad_adapter.invoke(request_for("llm-compatible"), timeout_seconds=1.0)


def test_openai_compatible_adapter_rejects_non_https_endpoint() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        OpenAICompatibleChatAdapter(
            identity=identity("llm-compatible"),
            base_url="http://gateway.example.test/v1",
            transport=FakeJsonTransport({}),  # type: ignore[arg-type]
            credentials=FakeCredentials(),
            schemas=(OUTPUT_SCHEMA,),
        )


class FakeHuggingFaceRuntime:
    def __init__(self, scores: Mapping[str, float] | None = None) -> None:
        self.scores = scores or {"negative": 0.8, "neutral": 0.15, "positive": 0.05}
        self.calls: list[dict[str, Any]] = []

    def predict_scores(
        self,
        text: str,
        *,
        model_id: str,
        revision: str,
        max_token_count: int,
    ) -> Mapping[str, float]:
        self.calls.append(
            {
                "text": text,
                "model_id": model_id,
                "revision": revision,
                "max_token_count": max_token_count,
            }
        )
        return self.scores


def hf_identity(*, rights_verified: bool = True) -> ModelIdentity:
    return ModelIdentity(
        provider_id="huggingface-local",
        model_id="org/reviewed-classifier",
        revision="a" * 40,
        model_license_id="Apache-2.0",
        license_review_id="review:hf-offline-test",
        training_dataset_id="org/reviewed-dataset",
        training_dataset_revision="b" * 40,
        training_data_license_id="CC-BY-4.0",
        artifact_sha256="c" * 64,
        tokenizer_revision="a" * 40,
        training_data_rights_verified=rights_verified,
        commercial_use_approved=False,
    )


HF_OUTPUT_SCHEMA = StructuredJsonSchema(
    schema_id="classifier.scores.v1",
    required_fields=("predicted_label", "scores"),
    allowed_fields=("predicted_label", "scores"),
    field_types=(("predicted_label", "string"), ("scores", "object")),
)


def hf_request() -> ModelRequest:
    return ModelRequest(
        request_id="req:hf:001",
        correlation_id="corr:hf:001",
        provider_id="huggingface-local",
        task=ModelTask.SENTIMENT,
        created_at=NOW,
        deadline_at=NOW + timedelta(seconds=10),
        input_schema_id=INPUT_SCHEMA.schema_id,
        output_schema_id=HF_OUTPUT_SCHEMA.schema_id,
        data_classification=DataClassification.PUBLIC,
        input_payload={"text": "Synthetic financial news fixture."},
        max_output_tokens=32,
    )


def test_huggingface_classifier_is_offline_and_uses_pinned_revision() -> None:
    model_identity = hf_identity()
    runtime = FakeHuggingFaceRuntime()
    clock_values = iter((NOW, NOW + timedelta(milliseconds=100)))
    provider = HuggingFaceSequenceClassificationProvider(
        identity=model_identity,
        runtime=runtime,
        max_token_count=64,
        clock=lambda: next(clock_values),
    )
    policy = ModelPolicy(
        allowed_tasks=frozenset({ModelTask.SENTIMENT}),
        allowed_data_classifications=frozenset({DataClassification.PUBLIC}),
        allowed_identities=(model_identity,),
        minimum_confidence=Decimal("0.70"),
    )
    audit = InMemoryModelAuditSink()
    core = IntelligenceCore(
        providers=(provider,),
        policy=policy,
        schemas=(INPUT_SCHEMA, HF_OUTPUT_SCHEMA),
        audit_sink=audit,
    )

    result = core.evaluate(hf_request(), now=NOW)

    assert result.abstained is False
    assert result.recommendation is not None
    assert result.recommendation["predicted_label"] == "negative"
    assert result.confidence == Decimal("0.8")
    assert runtime.calls == [
        {
            "text": "Synthetic financial news fixture.",
            "model_id": model_identity.model_id,
            "revision": "a" * 40,
            "max_token_count": 64,
        }
    ]


def test_huggingface_classifier_never_runs_without_rights_review() -> None:
    model_identity = hf_identity(rights_verified=False)
    runtime = FakeHuggingFaceRuntime()
    provider = HuggingFaceSequenceClassificationProvider(
        identity=model_identity,
        runtime=runtime,
        clock=lambda: NOW,
    )
    policy = ModelPolicy(
        allowed_tasks=frozenset({ModelTask.SENTIMENT}),
        allowed_data_classifications=frozenset({DataClassification.PUBLIC}),
        allowed_identities=(model_identity,),
    )
    core = IntelligenceCore(
        providers=(provider,),
        policy=policy,
        schemas=(INPUT_SCHEMA, HF_OUTPUT_SCHEMA),
        audit_sink=InMemoryModelAuditSink(),
    )

    result = core.evaluate(hf_request(), now=NOW)

    assert result.abstained is True
    assert result.reason_code == "TRAINING_DATA_RIGHTS_UNVERIFIED"
    assert runtime.calls == []


def test_huggingface_classifier_rejects_invalid_probability_distribution() -> None:
    model_identity = hf_identity()
    runtime = FakeHuggingFaceRuntime({"negative": float("nan"), "positive": 1.0})
    provider = HuggingFaceSequenceClassificationProvider(
        identity=model_identity,
        runtime=runtime,
        clock=lambda: NOW,
    )
    policy = ModelPolicy(
        allowed_tasks=frozenset({ModelTask.SENTIMENT}),
        allowed_data_classifications=frozenset({DataClassification.PUBLIC}),
        allowed_identities=(model_identity,),
    )
    core = IntelligenceCore(
        providers=(provider,),
        policy=policy,
        schemas=(INPUT_SCHEMA, HF_OUTPUT_SCHEMA),
        audit_sink=InMemoryModelAuditSink(),
    )

    result = core.evaluate(hf_request(), now=NOW)

    assert result.abstained is True
    assert result.reason_code == "PROVIDER_ERROR"


def test_model_request_payload_is_immutable_and_non_finite_json_is_rejected() -> None:
    original = {"text": "before"}
    request = request_for(payload=original)
    original["text"] = "after"
    assert request.input_payload["text"] == "before"
    with pytest.raises(ValueError, match="finite"):
        request_for(payload={"text": "ok", "value": float("nan")})
