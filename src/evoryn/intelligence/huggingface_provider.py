"""Optional offline Hugging Face text-classification adapter.

No model weights or third-party ML runtime are loaded at import time. Production inference is
local-files-only; tests inject a deterministic runtime and never contact a model hub.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from evoryn.intelligence.model_provider import (
    ConfidenceSemantics,
    ModelIdentity,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelTask,
)

_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


class SequenceClassificationRuntime(Protocol):
    """Injectable runtime interface for local, pinned sequence classifiers."""

    def predict_scores(
        self,
        text: str,
        *,
        model_id: str,
        revision: str,
        max_token_count: int,
    ) -> Mapping[str, float]:
        """Return label-to-probability scores from the locally cached model."""
        ...


class TransformersSequenceClassificationRuntime:
    """Lazy Hugging Face Transformers runtime with network model downloads disabled."""

    def __init__(self, *, cache_dir: Path | None = None) -> None:
        self._cache_dir = cache_dir
        self._lock = RLock()
        self._loaded: dict[tuple[str, str], tuple[Any, Any, Any]] = {}

    def predict_scores(
        self,
        text: str,
        *,
        model_id: str,
        revision: str,
        max_token_count: int,
    ) -> Mapping[str, float]:
        if not _COMMIT_SHA.fullmatch(revision):
            raise ModelProviderError("Hugging Face model revision must be an immutable commit SHA.")
        key = (model_id, revision)
        try:
            runtime = self._load(key)
            tokenizer, model, torch = runtime
            tokens = tokenizer(
                text,
                return_tensors="pt",
                truncation=True,
                max_length=max_token_count,
            )
            with torch.inference_mode():
                logits = model(**tokens).logits
                values = torch.softmax(logits, dim=-1)[0].detach().cpu().tolist()
            label_map = getattr(model.config, "id2label", {})
            result: dict[str, float] = {}
            for index, value in enumerate(values):
                label = label_map.get(index, f"label_{index}")
                result[str(label)] = float(value)
            return result
        except ModelProviderError:
            raise
        except Exception:
            raise ModelProviderError("Local Hugging Face inference failed.") from None

    def _load(self, key: tuple[str, str]) -> tuple[Any, Any, Any]:
        with self._lock:
            cached = self._loaded.get(key)
            if cached is not None:
                return cached
            model_id, revision = key
            try:
                import torch  # type: ignore[import-not-found]
                from transformers import (  # type: ignore[import-not-found]
                    AutoModelForSequenceClassification,
                    AutoTokenizer,
                )
            except ImportError:
                raise ModelProviderError(
                    "Install EVORYN's optional news-nlp dependencies for local inference."
                ) from None
            kwargs: dict[str, object] = {
                "revision": revision,
                "local_files_only": True,
                "trust_remote_code": False,
                "cache_dir": str(self._cache_dir) if self._cache_dir else None,
            }
            try:
                tokenizer = AutoTokenizer.from_pretrained(
                    model_id,
                    use_fast=True,
                    **kwargs,
                )
                model = AutoModelForSequenceClassification.from_pretrained(
                    model_id,
                    use_safetensors=True,
                    **kwargs,
                )
                model.eval()
            except Exception:
                raise ModelProviderError(
                    "Pinned Hugging Face artifacts are not available locally or failed safe loading."
                ) from None
            loaded = (tokenizer, model, torch)
            self._loaded[key] = loaded
            return loaded


class HuggingFaceSequenceClassificationProvider:
    """Provider-port implementation for an explicitly reviewed local Hugging Face classifier.

    A caller must independently approve model and training-data rights in ModelIdentity/ModelPolicy.
    The adapter never downloads weights, executes remote code, provides tools, or sees trading ports.
    """

    def __init__(
        self,
        *,
        identity: ModelIdentity,
        runtime: SequenceClassificationRuntime | None = None,
        cache_dir: Path | None = None,
        max_input_chars: int = 8_000,
        max_token_count: int = 512,
        clock: Any | None = None,
    ) -> None:
        if identity.provider_id != "huggingface-local":
            raise ValueError("Local Hugging Face identities must use provider_id='huggingface-local'.")
        if not _COMMIT_SHA.fullmatch(identity.revision):
            raise ValueError("Hugging Face model revision must be a 40-character immutable commit SHA.")
        if type(max_input_chars) is not int or max_input_chars <= 0:
            raise ValueError("max_input_chars must be a positive integer.")
        if type(max_token_count) is not int or max_token_count <= 0:
            raise ValueError("max_token_count must be a positive integer.")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable.")
        self._identity = identity
        self._runtime = runtime or TransformersSequenceClassificationRuntime(cache_dir=cache_dir)
        self._max_input_chars = max_input_chars
        self._max_token_count = max_token_count
        self._clock = clock

    @property
    def identity(self) -> ModelIdentity:
        return self._identity

    @property
    def is_remote(self) -> bool:
        return False

    @property
    def supported_tasks(self) -> frozenset[ModelTask]:
        return frozenset({ModelTask.CLASSIFICATION, ModelTask.SENTIMENT})

    def invoke(self, request: ModelRequest, *, timeout_seconds: float) -> ModelResponse:
        if request.provider_id != self.identity.provider_id:
            raise ModelProviderError("Request provider does not match the local model identity.")
        if request.task not in self.supported_tasks:
            raise ModelProviderError("Task is not supported by the local classifier.")
        if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
            raise ModelProviderError("Inference timeout is invalid.")
        text_value = request.input_payload.get("text")
        if not isinstance(text_value, str) or not text_value.strip():
            raise ModelProviderError("Classifier input must contain non-empty text.")
        if len(text_value) > self._max_input_chars:
            raise ModelProviderError("Classifier input exceeds its configured character limit.")
        started_at = self._now()
        try:
            scores = self._runtime.predict_scores(
                text_value,
                model_id=self.identity.model_id,
                revision=self.identity.revision,
                max_token_count=self._max_token_count,
            )
        except Exception:
            raise ModelProviderError("Local classifier inference failed.") from None
        completed_at = self._now()
        if not scores:
            raise ModelProviderError("Local classifier returned no scores.")
        normalized: dict[str, float] = {}
        for label, score in scores.items():
            if (
                not isinstance(label, str)
                or not label.strip()
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
                or not 0 <= float(score) <= 1
            ):
                raise ModelProviderError("Local classifier returned invalid scores.")
            normalized[label] = float(score)
        total = sum(normalized.values())
        if not math.isfinite(total) or total <= 0 or abs(total - 1.0) > 0.02:
            raise ModelProviderError("Local classifier returned an invalid probability distribution.")
        normalized = {label: score / total for label, score in normalized.items()}
        best_label, best_score = max(normalized.items(), key=lambda item: item[1])
        return ModelResponse(
            request_id=request.request_id,
            identity=self.identity,
            task=request.task,
            output_schema_id=request.output_schema_id,
            output={
                "predicted_label": best_label,
                "scores": normalized,
            },
            started_at=started_at,
            completed_at=completed_at,
            finish_reason="completed",
            confidence=Decimal(str(best_score)),
            confidence_semantics=ConfidenceSemantics.MODEL_POSTERIOR,
        )

    def _now(self) -> datetime:
        value = self._clock() if self._clock is not None else datetime.now(UTC)
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ModelProviderError("Local model clock must return an aware UTC timestamp.")
        return value
