"""Optional open-source Gaussian HMM adapter for research-only market-regime inference.

The core EVORYN package imports no HMM dependencies. Install the optional regime extra to fit
a model. Only the explicitly supplied train split is accepted for fitting. No model is persisted
with pickle/joblib; the adapter publishes a JSON-safe provenance manifest and fingerprint.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from evoryn.domain.models import Market, MarketCandle
from evoryn.intelligence.regime import (
    RegimeFeatureExtractor,
    RegimeLabel,
)


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
class RegimeDependencyUnavailable(RuntimeError):
    """Raised when optional HMM dependencies are not installed."""


@dataclass(frozen=True, slots=True)
class HMMTrainingManifest:
    """Reproducibility and license evidence for one fitted HMM artifact."""

    model_id: str
    model_version: str
    library_name: str
    library_version: str
    library_license: str
    training_dataset_version: str
    training_split: str
    train_start_index: int
    train_end_index_exclusive: int
    train_start_at: datetime
    train_end_at: datetime
    random_seed: int
    feature_schema_version: str
    lookback: int
    n_components: int
    max_iterations: int
    feature_mean: tuple[float, ...]
    feature_scale: tuple[float, ...]
    state_label_mapping: tuple[tuple[int, RegimeLabel], ...]
    fitted_state_counts: tuple[tuple[int, int], ...]
    model_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-safe metadata; no Python object serialization is used."""
        return {
            "schema_version": 1,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "library": {
                "name": self.library_name,
                "version": self.library_version,
                "license": self.library_license,
            },
            "training": {
                "dataset_version": self.training_dataset_version,
                "split": self.training_split,
                "start_index": self.train_start_index,
                "end_index_exclusive": self.train_end_index_exclusive,
                "start_at": _iso_utc(self.train_start_at),
                "end_at": _iso_utc(self.train_end_at),
                "random_seed": self.random_seed,
            },
            "features": {
                "schema_version": self.feature_schema_version,
                "lookback": self.lookback,
                "mean": list(self.feature_mean),
                "scale": list(self.feature_scale),
            },
            "model_config": {
                "family": "GaussianHMM",
                "n_components": self.n_components,
                "covariance_type": "diag",
                "max_iterations": self.max_iterations,
                "state_label_mapping": [
                    {"state": state, "label": label.value}
                    for state, label in self.state_label_mapping
                ],
                "fitted_state_counts": [
                    {"state": state, "count": count}
                    for state, count in self.fitted_state_counts
                ],
            },
            "model_fingerprint": self.model_fingerprint,
        }


class GaussianHMMRegimeModel:
    """Four-state hmmlearn Gaussian HMM fit on the train split and used causally.

    The semantic mapping is learned only from training-state means:
    highest realized-volatility state -> HIGH_VOLATILITY; among the remaining states,
    highest/lowest trend strength -> BULL_TREND/BEAR_TREND; the remaining state -> RANGE_BOUND.
    This mapping is heuristic and must be reviewed against validation data. It is not a
    supervised claim that an unsupervised state equals a human market regime.
    """

    model_id = "evoryn.hmmlearn-gaussian-hmm-regime"
    _model_version_prefix = "1.0.0"

    def __init__(
        self,
        *,
        feature_extractor: RegimeFeatureExtractor | None = None,
        n_components: int = 4,
        random_seed: int = 42,
        max_iterations: int = 200,
        min_state_trend_separation: Decimal = Decimal("0.0001"),
    ) -> None:
        if type(n_components) is not int or n_components != 4:
            raise ValueError("Phase 14 semantic mapping currently requires exactly 4 HMM states.")
        if type(random_seed) is not int or random_seed < 0:
            raise ValueError("random_seed must be a non-negative integer.")
        if type(max_iterations) is not int or max_iterations < 10:
            raise ValueError("max_iterations must be an integer >= 10.")
        if (
            not isinstance(min_state_trend_separation, Decimal)
            or not min_state_trend_separation.is_finite()
            or min_state_trend_separation < 0
        ):
            raise ValueError("min_state_trend_separation must be a finite non-negative Decimal.")

        self._features = feature_extractor or RegimeFeatureExtractor()
        self._n_components = n_components
        self._random_seed = random_seed
        self._max_iterations = max_iterations
        self._min_state_trend_separation = min_state_trend_separation
        self._model: Any | None = None
        self._np: Any | None = None
        self._feature_mean: Any | None = None
        self._feature_scale: Any | None = None
        self._state_labels: dict[int, RegimeLabel] = {}
        self._training_start_at: datetime | None = None
        self._training_end_at: datetime | None = None
        self._training_market: Market | None = None
        self._training_interval: timedelta | None = None
        self._manifest: HMMTrainingManifest | None = None

    @property
    def model_version(self) -> str:
        if self._manifest is not None:
            return self._manifest.model_version
        return self._model_version_prefix

    @property
    def is_ready(self) -> bool:
        return (
            self._model is not None
            and self._np is not None
            and self._feature_mean is not None
            and self._feature_scale is not None
            and self._training_start_at is not None
            and self._training_end_at is not None
            and self._training_market is not None
            and self._training_interval is not None
            and self._manifest is not None
            and len(self._state_labels) == self._n_components
        )

    @property
    def training_manifest(self) -> HMMTrainingManifest | None:
        return self._manifest

    def fit(
        self,
        training_candles: Sequence[MarketCandle],
        *,
        dataset_version: str,
        split_name: str,
        train_start_index: int,
        train_end_index_exclusive: int,
    ) -> HMMTrainingManifest:
        """Fit only on an explicit train range; validation/test splits are rejected."""
        self._model = None
        self._np = None
        self._feature_mean = None
        self._feature_scale = None
        self._state_labels = {}
        self._training_start_at = None
        self._training_end_at = None
        self._training_market = None
        self._training_interval = None
        self._manifest = None
        if split_name != "train":
            raise ValueError("HMM fitting accepts only split_name='train'; validation/test are holdouts.")
        if not _SHA256_PATTERN.fullmatch(dataset_version):
            raise ValueError("dataset_version must be a lowercase SHA-256 digest.")
        if (
            type(train_start_index) is not int
            or type(train_end_index_exclusive) is not int
            or train_start_index < 0
            or train_end_index_exclusive <= train_start_index
        ):
            raise ValueError("Training index range must be a valid non-empty half-open interval.")
        if len(training_candles) != train_end_index_exclusive - train_start_index:
            raise ValueError("Training candle count must match the declared train index range.")

        feature_rows = self._features.extract_series(training_candles)
        minimum_rows = max(50, self._n_components * 10)
        if len(feature_rows) < minimum_rows:
            raise ValueError(
                f"Need at least {minimum_rows} train feature rows before fitting a four-state HMM."
            )

        try:
            import numpy as np
            from hmmlearn.hmm import GaussianHMM
        except ImportError as exc:
            raise RegimeDependencyUnavailable(
                "Install EVORYN's optional open-source regime extra before HMM fitting."
            ) from exc

        matrix = np.asarray(
            [features.as_float_tuple() for features in feature_rows],
            dtype=np.float64,
        )
        if matrix.ndim != 2 or matrix.shape[1] != 4 or not np.isfinite(matrix).all():
            raise ValueError("Training features must be a finite N x 4 matrix.")

        feature_mean = np.mean(matrix, axis=0)
        feature_scale = np.std(matrix, axis=0)
        feature_scale = np.where(feature_scale > 1e-12, feature_scale, 1.0)
        scaled = (matrix - feature_mean) / feature_scale
        candidate = GaussianHMM(
            n_components=self._n_components,
            covariance_type="diag",
            n_iter=self._max_iterations,
            random_state=self._random_seed,
            min_covar=1e-3,
            tol=1e-3,
            implementation="log",
        )
        candidate.fit(scaled)
        if not bool(candidate.monitor_.converged):
            raise ValueError("HMM did not converge within max_iterations; model remains unusable.")

        states = candidate.predict(scaled)
        counts_array = np.bincount(states, minlength=self._n_components)
        minimum_state_count = max(2, int(len(states) * 0.01))
        if any(int(count) < minimum_state_count for count in counts_array):
            raise ValueError("At least one HMM state has insufficient train occupancy.")

        raw_means = candidate.means_ * feature_scale + feature_mean
        volatility_column = 2
        trend_column = 1
        high_volatility_state = max(
            range(self._n_components),
            key=lambda state: (float(raw_means[state, volatility_column]), -state),
        )
        other_states = [
            state for state in range(self._n_components) if state != high_volatility_state
        ]
        bull_state = max(
            other_states,
            key=lambda state: (float(raw_means[state, trend_column]), -state),
        )
        bear_state = min(
            other_states,
            key=lambda state: (float(raw_means[state, trend_column]), state),
        )
        if bull_state == bear_state:
            raise ValueError("HMM states are not separable by trend strength; refusing label mapping.")
        separation = Decimal(
            str(float(raw_means[bull_state, trend_column] - raw_means[bear_state, trend_column]))
        )
        if separation < self._min_state_trend_separation:
            raise ValueError("HMM trend-state separation is too small; refusing label mapping.")
        range_state = next(
            state for state in other_states if state not in (bull_state, bear_state)
        )
        state_labels = {
            bull_state: RegimeLabel.BULL_TREND,
            bear_state: RegimeLabel.BEAR_TREND,
            range_state: RegimeLabel.RANGE_BOUND,
            high_volatility_state: RegimeLabel.HIGH_VOLATILITY,
        }

        library_version = importlib.metadata.version("hmmlearn")
        train_start_at = training_candles[0].timestamp
        train_end_at = training_candles[-1].timestamp + training_candles[-1].interval
        training_market = training_candles[0].market
        training_interval = training_candles[0].interval
        state_label_pairs = tuple(sorted(state_labels.items(), key=lambda item: item[0]))
        state_count_pairs = tuple(
            (state, int(count)) for state, count in enumerate(counts_array.tolist())
        )
        fingerprint_material = {
            "model_id": self.model_id,
            "model_version_prefix": self._model_version_prefix,
            "library": {"name": "hmmlearn", "version": library_version, "license": "BSD-3-Clause"},
            "training": {
                "dataset_version": dataset_version,
                "split": split_name,
                "start_index": train_start_index,
                "end_index_exclusive": train_end_index_exclusive,
                "start_at": _iso_utc(train_start_at),
                "end_at": _iso_utc(train_end_at),
                "random_seed": self._random_seed,
            },
            "features": {
                "schema_version": self._features.feature_schema_version,
                "lookback": self._features.lookback,
                "mean": feature_mean.tolist(),
                "scale": feature_scale.tolist(),
            },
            "model_config": {
                "n_components": self._n_components,
                "covariance_type": "diag",
                "max_iterations": self._max_iterations,
                "tol": 1e-3,
                "min_covar": 1e-3,
                "implementation": "log",
                "min_state_trend_separation": str(self._min_state_trend_separation),
                "means": candidate.means_.tolist(),
                "covars": candidate.covars_.tolist(),
                "startprob": candidate.startprob_.tolist(),
                "transmat": candidate.transmat_.tolist(),
                "state_labels": [
                    {"state": state, "label": label.value}
                    for state, label in state_label_pairs
                ],
                "state_counts": [
                    {"state": state, "count": count}
                    for state, count in state_count_pairs
                ],
            },
        }
        fingerprint = hashlib.sha256(_canonical_json_bytes(fingerprint_material)).hexdigest()
        manifest = HMMTrainingManifest(
            model_id=self.model_id,
            model_version=f"{self._model_version_prefix}+hmmlearn-{library_version}",
            library_name="hmmlearn",
            library_version=library_version,
            library_license="BSD-3-Clause",
            training_dataset_version=dataset_version,
            training_split=split_name,
            train_start_index=train_start_index,
            train_end_index_exclusive=train_end_index_exclusive,
            train_start_at=train_start_at,
            train_end_at=train_end_at,
            random_seed=self._random_seed,
            feature_schema_version=self._features.feature_schema_version,
            lookback=self._features.lookback,
            n_components=self._n_components,
            max_iterations=self._max_iterations,
            feature_mean=tuple(float(value) for value in feature_mean.tolist()),
            feature_scale=tuple(float(value) for value in feature_scale.tolist()),
            state_label_mapping=state_label_pairs,
            fitted_state_counts=state_count_pairs,
            model_fingerprint=fingerprint,
        )
        self._np = np
        self._model = candidate
        self._feature_mean = feature_mean
        self._feature_scale = feature_scale
        self._state_labels = state_labels
        self._training_start_at = train_start_at
        self._training_end_at = train_end_at
        self._training_market = training_market
        self._training_interval = training_interval
        self._manifest = manifest
        return manifest

    def predict_probabilities(
        self, completed_history: Sequence[MarketCandle]
    ) -> Mapping[RegimeLabel, Decimal]:
        """Return filtered posterior probabilities for the latest known prefix only."""
        if not self.is_ready:
            raise RuntimeError("HMM is not fitted with a usable, versioned training split.")
        if not completed_history:
            raise ValueError("No completed history supplied for HMM inference.")
        self._features.validate_history(completed_history)
        if completed_history[0].timestamp != self._training_start_at:
            raise ValueError("Inference history must start at the fitted train range's first candle.")
        if completed_history[0].market != self._training_market:
            raise ValueError("Inference market differs from the model's training market.")
        if completed_history[0].interval != self._training_interval:
            raise ValueError("Inference interval differs from the model's training interval.")
        latest_close_time = completed_history[-1].timestamp + completed_history[-1].interval
        if latest_close_time < self._training_end_at:
            raise ValueError("Inference time precedes the end of the HMM training range.")
        feature_rows = self._features.extract_series(completed_history)
        matrix = self._np.asarray(
            [features.as_float_tuple() for features in feature_rows],
            dtype=self._np.float64,
        )
        scaled = (matrix - self._feature_mean) / self._feature_scale
        # Only the supplied prefix is passed to predict_proba. The final row has no later
        # observations inside this call, so it cannot be smoothed using future bars.
        posterior = self._model.predict_proba(scaled)
        last_posterior = posterior[-1]
        return {
            self._state_labels[state]: Decimal(str(float(last_posterior[state])))
            for state in range(self._n_components)
        }


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware.")
    from datetime import UTC

    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
