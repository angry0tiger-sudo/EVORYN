"""Causal market-regime features, research baseline, and fail-closed abstention policy.

This module is research-only. It has no broker, risk-engine, exchange or order permissions.
Callers must pass completed candles only; all outputs are advisory metadata.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from evoryn.domain.models import MarketCandle


REGIME_FEATURE_SCHEMA_VERSION = "1.0.0"
REGIME_MODEL_OUTPUTS = (
    "bull_trend",
    "bear_trend",
    "range_bound",
    "high_volatility",
)


class RegimeLabel(StrEnum):
    """Canonical regime labels; UNKNOWN is an abstention, not a learned class."""

    BULL_TREND = "bull_trend"
    BEAR_TREND = "bear_trend"
    RANGE_BOUND = "range_bound"
    HIGH_VOLATILITY = "high_volatility"
    UNKNOWN = "unknown"


class InsufficientRegimeHistory(ValueError):
    """Raised when the supplied candle prefix is too short for the configured features."""


class InvalidRegimeHistory(ValueError):
    """Raised when candle ordering, identity, intervals, or numerical data are not usable."""


@dataclass(frozen=True, slots=True)
class RegimeFeatureVector:
    """Four dimensionless, causal features calculated at a completed candle close."""

    as_of: datetime
    sample_count: int
    mean_return: Decimal
    trend_strength: Decimal
    realized_volatility: Decimal
    volume_ratio: Decimal

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("Feature as_of must be timezone-aware.")
        if self.sample_count <= 0:
            raise ValueError("Feature sample_count must be positive.")
        for name in (
            "mean_return",
            "trend_strength",
            "realized_volatility",
            "volume_ratio",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal.")
        if self.realized_volatility < 0:
            raise ValueError("realized_volatility must be non-negative.")

    def as_decimal_tuple(self) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """Stable feature ordering, included in every model's feature-schema contract."""
        return (
            self.mean_return,
            self.trend_strength,
            self.realized_volatility,
            self.volume_ratio,
        )

    def as_float_tuple(self) -> tuple[float, float, float, float]:
        """Convert only at the numerical-model boundary; domain/accounting stays Decimal."""
        return (
            float(self.mean_return),
            float(self.trend_strength),
            float(self.realized_volatility),
            float(self.volume_ratio),
        )


@dataclass(frozen=True, slots=True)
class RegimeFeatureExtractor:
    """Trailing return/trend/volatility/volume features with strict time-series validation."""

    lookback: int = 20
    feature_schema_version: str = REGIME_FEATURE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.lookback) is not int or self.lookback < 2:
            raise ValueError("lookback must be an integer >= 2.")
        if not self.feature_schema_version.strip():
            raise ValueError("feature_schema_version must not be empty.")

    @property
    def minimum_candles(self) -> int:
        """Minimum candles needed for lookback adjacent returns."""
        return self.lookback + 1

    def extract(self, history: Sequence[MarketCandle]) -> RegimeFeatureVector:
        """Use only the supplied completed prefix, whose last close is the decision time."""
        self.validate_history(history)
        if len(history) < self.minimum_candles:
            raise InsufficientRegimeHistory(
                f"Need at least {self.minimum_candles} completed candles."
            )
        return self._extract_window(history[-self.minimum_candles :])

    def extract_series(
        self, candles: Sequence[MarketCandle]
    ) -> tuple[RegimeFeatureVector, ...]:
        """Build causal features row by row; each row sees only candles ending at its as_of."""
        self.validate_history(candles)
        if len(candles) < self.minimum_candles:
            raise InsufficientRegimeHistory(
                f"Need at least {self.minimum_candles} completed candles."
            )
        vectors: list[RegimeFeatureVector] = []
        for end in range(self.minimum_candles, len(candles) + 1):
            start = end - self.minimum_candles
            vectors.append(self._extract_window(candles[start:end]))
        return tuple(vectors)

    def validate_history(self, candles: Sequence[MarketCandle]) -> None:
        """Reject mixed markets, mixed intervals, duplicates, and any missing interval."""
        if not candles:
            raise InsufficientRegimeHistory("No completed candle history supplied.")
        first = candles[0]
        previous: MarketCandle | None = None
        for candle in candles:
            if candle.market != first.market:
                raise InvalidRegimeHistory("History mixes canonical market identities.")
            if candle.interval != first.interval:
                raise InvalidRegimeHistory("History mixes candle intervals.")
            if candle.source != first.source:
                raise InvalidRegimeHistory("History mixes candle data sources.")
            if candle.timestamp.tzinfo is None or candle.timestamp.utcoffset() is None:
                raise InvalidRegimeHistory("Every candle timestamp must be timezone-aware.")
            if candle.timestamp.utcoffset() != timedelta(0):
                raise InvalidRegimeHistory("Regime features require canonical UTC timestamps.")
            if previous is not None:
                delta = candle.timestamp - previous.timestamp
                if delta <= timedelta(0):
                    raise InvalidRegimeHistory("History must be strictly chronological.")
                if delta != first.interval:
                    raise InvalidRegimeHistory("History contains an interval gap.")
            previous = candle

    def _extract_window(self, window: Sequence[MarketCandle]) -> RegimeFeatureVector:
        if len(window) != self.minimum_candles:
            raise ValueError("Internal feature window has an unexpected length.")
        returns = tuple(
            (current.close / previous.close) - Decimal("1")
            for previous, current in zip(window, window[1:])
        )
        mean_return = sum(returns, Decimal("0")) / Decimal(len(returns))
        variance = sum(
            ((value - mean_return) ** 2 for value in returns),
            Decimal("0"),
        ) / Decimal(len(returns))
        realized_volatility = variance.sqrt()
        trend_strength = (window[-1].close / window[0].close) - Decimal("1")
        volumes = tuple(candle.volume for candle in window[1:])
        mean_volume = sum(volumes, Decimal("0")) / Decimal(len(volumes))
        volume_ratio = (
            (volumes[-1] / mean_volume) - Decimal("1")
            if mean_volume > 0
            else Decimal("0")
        )
        last = window[-1]
        return RegimeFeatureVector(
            as_of=last.timestamp + last.interval,
            sample_count=len(returns),
            mean_return=mean_return,
            trend_strength=trend_strength,
            realized_volatility=realized_volatility,
            volume_ratio=volume_ratio,
        )


@dataclass(frozen=True, slots=True)
class RegimeThresholds:
    """Fail-closed confidence and freshness thresholds; none are inferred at runtime."""

    min_confidence: Decimal = Decimal("0.65")
    max_data_age_intervals: int = 2

    def __post_init__(self) -> None:
        if (
            not isinstance(self.min_confidence, Decimal)
            or not self.min_confidence.is_finite()
            or self.min_confidence <= 0
            or self.min_confidence > 1
        ):
            raise ValueError("min_confidence must be a finite Decimal in (0, 1].")
        if type(self.max_data_age_intervals) is not int or self.max_data_age_intervals <= 0:
            raise ValueError("max_data_age_intervals must be a positive integer.")


@dataclass(frozen=True, slots=True)
class RegimePrediction:
    """Auditable advisory output; UNKNOWN means the detector deliberately abstained."""

    label: RegimeLabel
    confidence: Decimal
    confidence_semantics: str
    probabilities: tuple[tuple[RegimeLabel, Decimal], ...]
    as_of: datetime
    model_id: str
    model_version: str
    feature_schema_version: str
    model_fingerprint: str
    min_confidence_threshold: Decimal
    max_data_age_intervals: int
    abstained: bool
    reason_code: str

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("Prediction timestamp must be timezone-aware.")
        if (
            not isinstance(self.confidence, Decimal)
            or not self.confidence.is_finite()
            or self.confidence < 0
            or self.confidence > 1
        ):
            raise ValueError("Prediction confidence must be in [0, 1].")
        if self.label is RegimeLabel.UNKNOWN and not self.abstained:
            raise ValueError("UNKNOWN must represent an explicit abstention.")
        if self.label is not RegimeLabel.UNKNOWN and self.abstained:
            raise ValueError("A classified regime must not be marked as abstained.")
        if not self.model_id.strip() or not self.model_version.strip():
            raise ValueError("Model identity and version are required.")
        if not self.feature_schema_version.strip() or not self.reason_code.strip():
            raise ValueError("Feature schema version and reason code are required.")
        if not self.model_fingerprint.strip():
            raise ValueError("Model fingerprint or an explicit unfit sentinel is required.")
        if not self.confidence_semantics.strip():
            raise ValueError("confidence_semantics must state how the score should be interpreted.")
        if (
            not isinstance(self.min_confidence_threshold, Decimal)
            or not self.min_confidence_threshold.is_finite()
            or self.min_confidence_threshold <= 0
            or self.min_confidence_threshold > 1
        ):
            raise ValueError("Recorded min_confidence_threshold must be in (0, 1].")
        if type(self.max_data_age_intervals) is not int or self.max_data_age_intervals <= 0:
            raise ValueError("Recorded max_data_age_intervals must be positive.")
        if self.probabilities:
            labels = tuple(label for label, _ in self.probabilities)
            if set(labels) != {
                RegimeLabel.BULL_TREND,
                RegimeLabel.BEAR_TREND,
                RegimeLabel.RANGE_BOUND,
                RegimeLabel.HIGH_VOLATILITY,
            } or len(labels) != 4:
                raise ValueError("Probability output must include each known regime exactly once.")
            if any(
                not probability.is_finite() or probability < 0 or probability > 1
                for _, probability in self.probabilities
            ):
                raise ValueError("Each regime probability must be finite and in [0, 1].")
            if abs(
                sum((probability for _, probability in self.probabilities), Decimal("0"))
                - Decimal("1")
            ) > Decimal("0.000001"):
                raise ValueError("Regime probabilities must sum to one.")


class RegimeProbabilityModel(Protocol):
    """Research model port. A model predicts regimes; it cannot authorize trading actions."""

    model_id: str
    model_version: str
    model_fingerprint: str
    confidence_semantics: str

    @property
    def is_ready(self) -> bool:
        """False until fitted/loaded with a valid, versioned training artifact."""
        ...

    def predict_probabilities(
        self, completed_history: Sequence[MarketCandle]
    ) -> Mapping[RegimeLabel, Decimal | float]:
        """Predict a four-label distribution using only the supplied past/current prefix."""
        ...


class RuleBasedRegimeBaseline:
    """Transparent rule-only comparator; its scores are heuristic, not calibrated probabilities."""

    model_id = "evoryn.rule-regime-baseline"
    model_version = "1.0.0"
    confidence_semantics = "heuristic_score"

    @property
    def model_fingerprint(self) -> str:
        return _sha256_text(
            f"{self.model_id}|{self.model_version}|{self._features.lookback}|"
            f"{self._trend_threshold}|{self._high_volatility_threshold}"
        )

    def __init__(
        self,
        feature_extractor: RegimeFeatureExtractor,
        *,
        trend_threshold: Decimal = Decimal("0.02"),
        high_volatility_threshold: Decimal = Decimal("0.025"),
    ) -> None:
        self._features = feature_extractor
        for name, value in (
            ("trend_threshold", trend_threshold),
            ("high_volatility_threshold", high_volatility_threshold),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a finite positive Decimal.")
        self._trend_threshold = trend_threshold
        self._high_volatility_threshold = high_volatility_threshold

    @property
    def is_ready(self) -> bool:
        return True

    def predict_probabilities(
        self, completed_history: Sequence[MarketCandle]
    ) -> Mapping[RegimeLabel, Decimal]:
        """Return normalized rule scores only as a non-ML benchmark, never as calibrated odds."""
        features = self._features.extract(completed_history)
        normalized_trend = features.trend_strength / self._trend_threshold
        volatility_ratio = features.realized_volatility / self._high_volatility_threshold
        scores = {
            RegimeLabel.BULL_TREND: max(Decimal("0"), normalized_trend),
            RegimeLabel.BEAR_TREND: max(Decimal("0"), -normalized_trend),
            RegimeLabel.RANGE_BOUND: max(
                Decimal("0"), Decimal("1") - abs(normalized_trend)
            ),
            RegimeLabel.HIGH_VOLATILITY: max(
                Decimal("0"), (volatility_ratio - Decimal("0.5")) * Decimal("2")
            ),
        }
        return _normalize_weights(scores)


class AbstainingRegimeDetector:
    """Apply data-quality, freshness, model-output, and confidence gates to regime predictions."""

    def __init__(
        self,
        model: RegimeProbabilityModel,
        *,
        feature_extractor: RegimeFeatureExtractor | None = None,
        thresholds: RegimeThresholds | None = None,
    ) -> None:
        self._model = model
        self._features = feature_extractor or RegimeFeatureExtractor()
        self._thresholds = thresholds or RegimeThresholds()

    def classify(
        self, completed_history: Sequence[MarketCandle], *, now: datetime
    ) -> RegimePrediction:
        """Classify the latest completed bar or return UNKNOWN for any invalid/uncertain state."""
        _require_aware(now, "now")
        if not completed_history:
            return self._unknown(now, "NO_HISTORY")

        last = completed_history[-1]
        as_of = last.timestamp + last.interval
        if as_of > now:
            return self._unknown(as_of, "FUTURE_CANDLE")
        if now - as_of > last.interval * self._thresholds.max_data_age_intervals:
            return self._unknown(as_of, "STALE_DATA")
        if len(completed_history) < self._features.minimum_candles:
            return self._unknown(as_of, "INSUFFICIENT_HISTORY")
        try:
            self._features.extract(completed_history)
        except (InsufficientRegimeHistory, InvalidRegimeHistory, ValueError):
            return self._unknown(as_of, "INVALID_HISTORY")
        if not self._model.is_ready:
            return self._unknown(as_of, "MODEL_NOT_READY")
        if self._model.confidence_semantics != "hmm_posterior":
            return self._unknown(as_of, "UNVALIDATED_CONFIDENCE")

        try:
            raw = self._model.predict_probabilities(completed_history)
            probabilities = _normalize_model_probabilities(raw)
        except (ArithmeticError, TypeError, ValueError, RuntimeError):
            return self._unknown(as_of, "INVALID_MODEL_OUTPUT")

        label, confidence = max(
            probabilities.items(),
            key=lambda pair: (pair[1], _label_tie_break_order(pair[0])),
        )
        ordered = tuple(
            (known_label, probabilities[known_label])
            for known_label in (
                RegimeLabel.BULL_TREND,
                RegimeLabel.BEAR_TREND,
                RegimeLabel.RANGE_BOUND,
                RegimeLabel.HIGH_VOLATILITY,
            )
        )
        if confidence < self._thresholds.min_confidence:
            return RegimePrediction(
                label=RegimeLabel.UNKNOWN,
                confidence=confidence,
                confidence_semantics=self._model.confidence_semantics,
                probabilities=ordered,
                as_of=as_of,
                model_id=self._model.model_id,
                model_version=self._model.model_version,
                feature_schema_version=self._features.feature_schema_version,
                model_fingerprint=self._model.model_fingerprint,
                min_confidence_threshold=self._thresholds.min_confidence,
                max_data_age_intervals=self._thresholds.max_data_age_intervals,
                abstained=True,
                reason_code="LOW_CONFIDENCE",
            )
        return RegimePrediction(
            label=label,
            confidence=confidence,
            confidence_semantics=self._model.confidence_semantics,
            probabilities=ordered,
            as_of=as_of,
            model_id=self._model.model_id,
            model_version=self._model.model_version,
            feature_schema_version=self._features.feature_schema_version,
            model_fingerprint=self._model.model_fingerprint,
            min_confidence_threshold=self._thresholds.min_confidence,
            max_data_age_intervals=self._thresholds.max_data_age_intervals,
            abstained=False,
            reason_code="CLASSIFIED",
        )

    def _unknown(self, as_of: datetime, reason: str) -> RegimePrediction:
        return RegimePrediction(
            label=RegimeLabel.UNKNOWN,
            confidence=Decimal("0"),
            confidence_semantics=self._model.confidence_semantics,
            probabilities=(),
            as_of=as_of,
            model_id=self._model.model_id,
            model_version=self._model.model_version,
            feature_schema_version=self._features.feature_schema_version,
            model_fingerprint=self._model.model_fingerprint,
            min_confidence_threshold=self._thresholds.min_confidence,
            max_data_age_intervals=self._thresholds.max_data_age_intervals,
            abstained=True,
            reason_code=reason,
        )


def _normalize_weights(
    weights: Mapping[RegimeLabel, Decimal],
) -> dict[RegimeLabel, Decimal]:
    total = sum(weights.values(), Decimal("0"))
    if total <= 0 or not total.is_finite():
        raise ValueError("Regime weights must have a finite positive sum.")
    labels = (
        RegimeLabel.BULL_TREND,
        RegimeLabel.BEAR_TREND,
        RegimeLabel.RANGE_BOUND,
        RegimeLabel.HIGH_VOLATILITY,
    )
    normalized = {label: weights[label] / total for label in labels}
    normalized[labels[-1]] = Decimal("1") - sum(
        (normalized[label] for label in labels[:-1]), Decimal("0")
    )
    return normalized


def _normalize_model_probabilities(
    raw: Mapping[RegimeLabel, Decimal | float],
) -> dict[RegimeLabel, Decimal]:
    labels = (
        RegimeLabel.BULL_TREND,
        RegimeLabel.BEAR_TREND,
        RegimeLabel.RANGE_BOUND,
        RegimeLabel.HIGH_VOLATILITY,
    )
    if set(raw) != set(labels):
        raise ValueError("Model must return exactly the four known regime labels.")
    converted: dict[RegimeLabel, Decimal] = {}
    for label in labels:
        try:
            value = Decimal(str(raw[label]))
        except (ArithmeticError, ValueError) as exc:
            raise ValueError("Model probability is not a valid decimal.") from exc
        if not value.is_finite() or value < 0 or value > 1:
            raise ValueError("Model probabilities must be finite values in [0, 1].")
        converted[label] = value
    total = sum(converted.values(), Decimal("0"))
    if total <= 0 or abs(total - Decimal("1")) > Decimal("0.02"):
        raise ValueError("Model probability distribution must sum to one within tolerance.")
    return _normalize_weights(converted)


def _label_tie_break_order(label: RegimeLabel) -> int:
    """Stable tie-breaking only; it must never imply confidence."""
    order = {
        RegimeLabel.BULL_TREND: 0,
        RegimeLabel.BEAR_TREND: 1,
        RegimeLabel.RANGE_BOUND: 2,
        RegimeLabel.HIGH_VOLATILITY: 3,
    }
    return -order[label]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")
