"""Domain event schema for append-only strategy-selection audit records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class StrategySelectionLedgerEntry:
    """Append-only strategy-selection decision linked to a deterministic correlation ID."""

    event_id: str
    correlation_id: str
    recorded_at: datetime
    market_symbol: str
    regime_label: str
    regime_confidence: Decimal
    confidence_semantics: str
    model_id: str
    model_version: str
    model_fingerprint: str
    feature_schema_version: str
    candidate_strategy_id: str | None
    active_strategy_id: str | None
    alternative_strategy_ids: tuple[str, ...]
    status: str
    reason_code: str
    reason: str
    evidence_count: int
    required_evidence_count: int
    candidate_since: datetime | None
    last_switch_at: datetime | None
    required_stability_seconds: int
    minimum_hold_seconds: int
    confidence_threshold: Decimal
    allow_new_signals: bool
    invalidation_conditions: tuple[str, ...]
    registry_fingerprint: str
    routing_fingerprint: str

    def __post_init__(self) -> None:
        _require_aware(self.recorded_at, "Strategy-decision timestamp")
        for name in (
            "event_id",
            "correlation_id",
            "market_symbol",
            "regime_label",
            "confidence_semantics",
            "model_id",
            "model_version",
            "model_fingerprint",
            "feature_schema_version",
            "status",
            "reason_code",
            "reason",
            "registry_fingerprint",
            "routing_fingerprint",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty.")
        if self.candidate_since is not None:
            _require_aware(self.candidate_since, "Candidate-since timestamp")
        if self.last_switch_at is not None:
            _require_aware(self.last_switch_at, "Last-switch timestamp")
        if (
            not isinstance(self.regime_confidence, Decimal)
            or not self.regime_confidence.is_finite()
            or not Decimal("0") <= self.regime_confidence <= Decimal("1")
        ):
            raise ValueError("Regime confidence must be finite and in [0, 1].")
        if (
            not isinstance(self.confidence_threshold, Decimal)
            or not self.confidence_threshold.is_finite()
            or not Decimal("0") < self.confidence_threshold <= Decimal("1")
        ):
            raise ValueError("Confidence threshold must be finite and in (0, 1].")
        if (
            type(self.evidence_count) is not int
            or type(self.required_evidence_count) is not int
            or self.evidence_count < 0
            or self.required_evidence_count <= 0
            or self.evidence_count > self.required_evidence_count
        ):
            raise ValueError("Strategy-selection evidence counters are invalid.")
        if (
            type(self.required_stability_seconds) is not int
            or type(self.minimum_hold_seconds) is not int
            or self.required_stability_seconds <= 0
            or self.minimum_hold_seconds <= 0
        ):
            raise ValueError("Strategy-selection dwell times must be positive integers.")
        if type(self.allow_new_signals) is not bool:
            raise ValueError("allow_new_signals must be an explicit boolean.")
        if len(set(self.alternative_strategy_ids)) != len(self.alternative_strategy_ids):
            raise ValueError("Alternative strategy identifiers must be unique.")
        if any(not value.strip() for value in self.alternative_strategy_ids):
            raise ValueError("Alternative strategy identifiers must not be empty.")
        if len(set(self.invalidation_conditions)) != len(self.invalidation_conditions):
            raise ValueError("Invalidation conditions must be unique.")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")
