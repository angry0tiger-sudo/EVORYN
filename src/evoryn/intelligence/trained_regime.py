"""Inference adapter for a versioned, calibrated historical regime model.

The optional scikit-learn/joblib dependencies are loaded lazily. This module has no exchange
transport, risk authority, or order capability. Never load a joblib artifact from an untrusted
source; validate its SHA-256 against a separately trusted manifest before deserialization.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from evoryn.domain.models import MarketCandle
from evoryn.intelligence.regime import (
    REGIME_FEATURE_SCHEMA_VERSION,
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
        """Predict from completed history and reject malformed estimator output."""
        features = self._features.extract(completed_history)
        numpy = importlib.import_module("numpy")
        matrix = numpy.asarray([features.as_float_tuple()], dtype="float64")
        try:
            raw_output = self._estimator.predict_proba(matrix)
            raw_values = tuple(float(value) for value in raw_output[0])
        except Exception as exc:
            raise TrainedRegimeArtifactError(
                f"Loaded estimator prediction failed: {type(exc).__name__}."
            ) from None
        if len(raw_values) != len(self._class_order):
            raise TrainedRegimeArtifactError(
                "Loaded estimator class count does not match its verified class order."
            )
        if any(
            not math.isfinite(value) or value < 0.0 or value > 1.0
            for value in raw_values
        ):
            raise TrainedRegimeArtifactError(
                "Model returned a non-finite or out-of-range class probability."
            )
        raw_total = sum(raw_values)
        if not math.isclose(raw_total, 1.0, rel_tol=1e-6, abs_tol=1e-8):
            raise TrainedRegimeArtifactError(
                "Model returned class probabilities that do not sum to one."
            )
        logged = [math.log(max(value, 1e-15)) / self._temperature for value in raw_values]
        peak = max(logged)
        exp_values = [math.exp(value - peak) for value in logged]
        denominator = sum(exp_values)
        if not math.isfinite(denominator) or denominator <= 0:
            raise TrainedRegimeArtifactError("Model probability normalization failed.")
        probabilities = {
            label: Decimal(str(value / denominator))
            for label, value in zip(self._class_order, exp_values, strict=True)
        }
        total = sum(probabilities.values(), Decimal("0"))
        if not total.is_finite() or total <= 0:
            raise TrainedRegimeArtifactError("Model returned an invalid probability distribution.")
        return {
            label: probability / total
            for label, probability in probabilities.items()
        }



def _is_sha256(value: object) -> bool:
    """Return whether value is a canonical lowercase SHA-256 hex digest."""
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def load_trained_regime_model(
    artifact_path: str | Path,
    manifest_path: str | Path,
    *,
    expected_artifact_sha256: str,
    expected_manifest_sha256: str,
) -> TrainedRegimeProbabilityModel:
    """Load only when both files match independently trusted SHA-256 pins.

    The expected digests must come from a trusted review/release record, not be copied from
    the adjacent manifest being checked. Hash verification occurs before joblib deserialization.
    """
    if not _is_sha256(expected_artifact_sha256):
        raise TrainedRegimeArtifactError(
            "An independently pinned artifact SHA-256 is required."
        )
    if not _is_sha256(expected_manifest_sha256):
        raise TrainedRegimeArtifactError(
            "An independently pinned manifest SHA-256 is required."
        )
    artifact = Path(artifact_path).expanduser().resolve()
    manifest_file = Path(manifest_path).expanduser().resolve()
    try:
        artifact_bytes = artifact.read_bytes()
        manifest_bytes = manifest_file.read_bytes()
    except OSError as exc:
        raise TrainedRegimeArtifactError("Model artifact or manifest cannot be read.") from exc
    actual_sha = hashlib.sha256(artifact_bytes).hexdigest()
    if actual_sha != expected_artifact_sha256:
        raise TrainedRegimeArtifactError(
            "Model artifact SHA-256 does not match the independently pinned digest."
        )
    actual_manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_manifest_sha != expected_manifest_sha256:
        raise TrainedRegimeArtifactError(
            "Model manifest SHA-256 does not match the independently pinned digest."
        )
    try:
        manifest_value = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrainedRegimeArtifactError("Model manifest is not valid UTF-8 JSON.") from exc
    if not isinstance(manifest_value, dict) or manifest_value.get("schema_version") != 1:
        raise TrainedRegimeArtifactError("Unsupported model artifact manifest schema.")
    if manifest_value.get("artifact_sha256") != actual_sha:
        raise TrainedRegimeArtifactError("Model artifact SHA-256 does not match its manifest.")
    if manifest_value.get("model_id") != "evoryn.trained-regime-random-forest":
        raise TrainedRegimeArtifactError("Model artifact identifier is not recognized.")
    if manifest_value.get("model_version") != actual_sha[:16]:
        raise TrainedRegimeArtifactError("Model version does not match the pinned artifact.")
    training_manifest = manifest_value.get("training")
    if not isinstance(training_manifest, dict):
        raise TrainedRegimeArtifactError("Model training manifest is missing.")
    if training_manifest.get("feature_schema_version") != REGIME_FEATURE_SCHEMA_VERSION:
        raise TrainedRegimeArtifactError("Model feature schema is unsupported.")
    manifest_lookback = training_manifest.get("feature_lookback")
    if type(manifest_lookback) is not int or manifest_lookback < 2:
        raise TrainedRegimeArtifactError("Manifest feature lookback is invalid.")
    classifier = training_manifest.get("classifier")
    if not isinstance(classifier, dict):
        raise TrainedRegimeArtifactError("Manifest classifier metadata is missing.")
    manifest_temperature = classifier.get("calibration_temperature")
    if (
        isinstance(manifest_temperature, bool)
        or not isinstance(manifest_temperature, (int, float))
        or not math.isfinite(float(manifest_temperature))
        or float(manifest_temperature) <= 0
    ):
        raise TrainedRegimeArtifactError("Manifest calibration temperature is invalid.")
    try:
        joblib = importlib.import_module("joblib")
        # Deserialize the exact byte sequence whose digest was checked, avoiding path TOCTOU.
        payload = joblib.load(io.BytesIO(artifact_bytes))
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
        if lookback != manifest_lookback:
            raise ValueError("feature lookback mismatch")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise ValueError("temperature")
        if not math.isfinite(float(temperature)) or float(temperature) <= 0:
            raise ValueError("temperature")
        if not math.isclose(
            float(temperature), float(manifest_temperature), rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("temperature mismatch")
        if len(class_order) != 4 or len(set(class_order)) != 4:
            raise ValueError("unique class order")
        estimator = payload.get("estimator")
        estimator_classes = getattr(estimator, "classes_", None)
        estimator_feature_count = getattr(estimator, "n_features_in_", None)
        if not callable(getattr(estimator, "predict_proba", None)):
            raise ValueError("estimator probability interface")
        if estimator_classes is None or estimator_feature_count is None:
            raise ValueError("estimator fitted-state metadata")
        if tuple(str(value) for value in estimator_classes) != tuple(labels_raw):
            raise ValueError("estimator class order mismatch")
        if isinstance(estimator_feature_count, bool) or int(estimator_feature_count) != 4:
            raise ValueError("estimator feature dimension mismatch")
        return TrainedRegimeProbabilityModel(
            estimator=estimator,
            class_order=class_order,
            temperature=float(temperature),
            model_fingerprint=actual_sha,
            model_version=actual_sha[:16],
            feature_extractor=RegimeFeatureExtractor(lookback=lookback),
            training_manifest=training_manifest,
        )
    except (ValueError, TypeError, AttributeError, OverflowError) as exc:
        raise TrainedRegimeArtifactError(
            f"Model payload fields or estimator schema are invalid: {exc}."
        ) from exc


def load_trained_regime_detector(
    artifact_path: str | Path,
    manifest_path: str | Path,
    *,
    expected_artifact_sha256: str,
    expected_manifest_sha256: str,
    min_confidence: Decimal = Decimal("0.65"),
    max_data_age_intervals: int = 2,
) -> AbstainingRegimeDetector:
    """Build the production RegimeClassifier contract from a verified local research artifact.

    Confidence/freshness gates remain explicit and fail closed. The returned detector has no
    execution authority; pass it into a PaperStrategyRuntime only with an approved feed/registry.
    """
    model = load_trained_regime_model(
        artifact_path,
        manifest_path,
        expected_artifact_sha256=expected_artifact_sha256,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    return AbstainingRegimeDetector(
        model,
        feature_extractor=model.feature_extractor,
        thresholds=RegimeThresholds(
            min_confidence=min_confidence,
            max_data_age_intervals=max_data_age_intervals,
        ),
    )
