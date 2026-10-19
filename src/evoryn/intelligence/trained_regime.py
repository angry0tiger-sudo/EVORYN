"""Inference adapter for a versioned, calibrated historical regime model.

The optional scikit-learn/joblib dependencies are loaded lazily. This module has no exchange
transport, risk authority, or order capability. Never load a joblib artifact from an untrusted
source; validate its SHA-256 against a separately trusted manifest before deserialization.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from evoryn.domain.models import MarketCandle
from evoryn.intelligence.regime import (
    AbstainingRegimeDetector,
    RegimeFeatureExtractor,
    RegimeLabel,
    RegimeProbabilityModel,
    RegimeThresholds,
)


class TrainedRegimeArtifactError(ValueError):
    """Raised when a trained model artifact cannot be verified or used safely."""


class TrainedRegimeProbabilityModel(RegimeProbabilityModel):
    """Four-regime RF classifier whose probability temperature is fitted on validation data."""

    model_id = "evoryn.trained-regime-random-forest"
    confidence_semantics = "calibrated_probability"

    def __init__(
        self,
        *,
        estimator: Any,
        class_order: Sequence[RegimeLabel],
        temperature: float,
        model_fingerprint: str,
        model_version: str,
        feature_extractor: RegimeFeatureExtractor,
        training_manifest: Mapping[str, Any],
    ) -> None:
        if not math.isfinite(temperature) or temperature <= 0:
            raise ValueError("Calibration temperature must be positive and finite.")
        if len(class_order) != 4 or set(class_order) != {
            RegimeLabel.BULL_TREND,
            RegimeLabel.BEAR_TREND,
            RegimeLabel.RANGE_BOUND,
            RegimeLabel.HIGH_VOLATILITY,
        }:
            raise ValueError("Trained regime model must declare all four known regimes.")
        if (
            len(model_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in model_fingerprint)
        ):
            raise ValueError("Model fingerprint must be a lowercase SHA-256.")
        if not model_version.strip():
            raise ValueError("Model version must not be empty.")
        self._estimator = estimator
        self._class_order = tuple(class_order)
        self._temperature = temperature
        self._model_fingerprint = model_fingerprint
        self._model_version = model_version
        self._features = feature_extractor
        self._training_manifest = dict(training_manifest)

    @property
    def model_version(self) -> str:
        return self._model_version

    @property
    def model_fingerprint(self) -> str:
        return self._model_fingerprint

    @property
    def is_ready(self) -> bool:
        return self._estimator is not None and bool(self._model_fingerprint)

    @property
    def training_manifest(self) -> dict[str, Any]:
        """Return a copy of dataset/split/label-policy provenance for audit consumers."""
        return dict(self._training_manifest)

    @property
    def feature_extractor(self) -> RegimeFeatureExtractor:
        return self._features

    def predict_probabilities(
        self,
        completed_history: Sequence[MarketCandle],
    ) -> Mapping[RegimeLabel, Decimal]:
        """Predict from the trailing completed history; no future candle is accepted or required."""
        features = self._features.extract(completed_history)
        numpy = importlib.import_module("numpy")
        matrix = numpy.asarray([features.as_float_tuple()], dtype="float64")
        raw = self._estimator.predict_proba(matrix)[0]
        if len(raw) != len(self._class_order):
            raise TrainedRegimeArtifactError("Loaded estimator class count does not match manifest.")
        logged = [
            math.log(max(float(value), 1e-15)) / self._temperature
            for value in raw
        ]
        peak = max(logged)
        exp_values = [math.exp(value - peak) for value in logged]
        denominator = sum(exp_values)
        if not math.isfinite(denominator) or denominator <= 0:
            raise TrainedRegimeArtifactError("Model probability normalization failed.")
        probabilities = {
            label: Decimal(str(value / denominator))
            for label, value in zip(self._class_order, exp_values, strict=True)
        }
        # Keep all labels present and normalize against Decimal arithmetic at the public boundary.
        total = sum(probabilities.values(), Decimal("0"))
        if not total.is_finite() or total <= 0:
            raise TrainedRegimeArtifactError("Model returned an invalid probability distribution.")
        return {
            label: probability / total
            for label, probability in probabilities.items()
        }


def load_trained_regime_model(
    artifact_path: str | Path,
    manifest_path: str | Path,
) -> TrainedRegimeProbabilityModel:
    """Verify artifact digest/manifest before loading an explicitly trusted local joblib payload."""
    artifact = Path(artifact_path).expanduser().resolve()
    manifest_file = Path(manifest_path).expanduser().resolve()
    try:
        manifest_bytes = manifest_file.read_bytes()
        manifest_value = json.loads(manifest_bytes)
        artifact_bytes = artifact.read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrainedRegimeArtifactError("Model artifact or manifest cannot be read.") from exc
    if not isinstance(manifest_value, dict) or manifest_value.get("schema_version") != 1:
        raise TrainedRegimeArtifactError("Unsupported model artifact manifest schema.")
    actual_sha = hashlib.sha256(artifact_bytes).hexdigest()
    if manifest_value.get("artifact_sha256") != actual_sha:
        raise TrainedRegimeArtifactError("Model artifact SHA-256 does not match its manifest.")
    if manifest_value.get("model_id") != "evoryn.trained-regime-random-forest":
        raise TrainedRegimeArtifactError("Model artifact identifier is not recognized.")
    try:
        joblib = importlib.import_module("joblib")
        payload = joblib.load(artifact)
    except Exception as exc:
        raise TrainedRegimeArtifactError(
            f"Trusted model artifact deserialization failed: {type(exc).__name__}."
        ) from None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise TrainedRegimeArtifactError("Model payload schema is invalid.")
    labels_raw = payload.get("class_order")
    if not isinstance(labels_raw, list) or any(not isinstance(item, str) for item in labels_raw):
        raise TrainedRegimeArtifactError("Model payload class order is invalid.")
    try:
        class_order = tuple(RegimeLabel(item) for item in labels_raw)
        lookback = payload.get("feature_lookback")
        temperature = payload.get("temperature")
        if type(lookback) is not int or lookback < 2:
            raise ValueError("feature lookback")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise ValueError("temperature")
        if not math.isfinite(float(temperature)) or float(temperature) <= 0:
            raise ValueError("temperature")
        training_manifest = manifest_value.get("training")
        if not isinstance(training_manifest, dict):
            raise ValueError("training manifest")
        return TrainedRegimeProbabilityModel(
            estimator=payload.get("estimator"),
            class_order=class_order,
            temperature=float(temperature),
            model_fingerprint=actual_sha,
            model_version=actual_sha[:16],
            feature_extractor=RegimeFeatureExtractor(lookback=lookback),
            training_manifest=training_manifest,
        )
    except (ValueError, TypeError) as exc:
        raise TrainedRegimeArtifactError("Model payload fields are invalid.") from exc


def load_trained_regime_detector(
    artifact_path: str | Path,
    manifest_path: str | Path,
    *,
    min_confidence: Decimal = Decimal("0.65"),
    max_data_age_intervals: int = 2,
) -> AbstainingRegimeDetector:
    """Build the production RegimeClassifier contract from a verified local research artifact.

    Confidence/freshness gates remain explicit and fail closed. The returned detector has no
    execution authority; pass it into a PaperStrategyRuntime only with an approved feed/registry.
    """
    model = load_trained_regime_model(artifact_path, manifest_path)
    return AbstainingRegimeDetector(
        model,
        feature_extractor=model.feature_extractor,
        thresholds=RegimeThresholds(
            min_confidence=min_confidence,
            max_data_age_intervals=max_data_age_intervals,
        ),
    )
