"""Champion/Challenger gates, Paper/Shadow rollback and release authorization evidence.

This module evaluates research candidates; it does not modify code, model files, risk limits, or
Live configuration. A successful result is only evidence for an external, reviewed release process.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from evoryn.learning_lab.experiment_registry import (
    ExperimentIntegrityError,
    ExperimentMode,
    ExperimentRegistry,
    ExperimentRunEvidence,
    ExperimentSpec,
    LearningEventType,
    LearningLabLedger,
    _canonical_json_bytes,
    _iso_utc,
    _require_aware_utc,
    _require_finite_decimal,
    _require_sha,
)


class PromotionStatus(StrEnum):
    """Comparison result; only SHADOW/PAPER acceptance states are reachable."""

    APPROVED_FOR_SHADOW = "approved_for_shadow"
    PROMOTED_TO_PAPER = "promoted_to_paper"
    REJECTED = "rejected"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class RollbackStatus(StrEnum):
    """Outcome of a paper-champion health check."""

    HEALTHY = "healthy"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    ROLLED_BACK = "rolled_back"
    SAFE_HOLD_NO_CHAMPION = "safe_hold_no_champion"


class RollbackTrigger(StrEnum):
    """Explicit automatic rollback reasons."""

    MAX_DRAWDOWN_EXCEEDED = "max_drawdown_exceeded"
    NET_LOSS_EXCEEDED = "net_loss_exceeded"
    DATA_QUALITY_FAILED = "data_quality_failed"
    RISK_GATE_DENIAL = "risk_gate_denial"
    MODEL_OR_DATA_IDENTITY_MISMATCH = "model_or_data_identity_mismatch"
    TELEMETRY_ATTESTATION_FAILED = "telemetry_attestation_failed"


class ReleaseTarget(StrEnum):
    """Phase 17 targets. LIVE exists only so that attempted live promotion can be rejected."""

    SHADOW = "shadow"
    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    """Predeclared champion/challenger thresholds; no post-test tuning is implicit."""

    minimum_observations: int = 100
    minimum_closed_trades: int = 10
    minimum_net_return_improvement: Decimal = Decimal("0.005")
    maximum_drawdown_fraction: Decimal = Decimal("0.20")
    maximum_drawdown_worsening: Decimal = Decimal("0.01")
    maximum_net_loss_fraction: Decimal = Decimal("0.05")
    maximum_risk_gate_denials: int = 0
    maximum_comparisons_per_dataset_test_range: int = 5

    def __post_init__(self) -> None:
        for name in ("minimum_observations", "minimum_closed_trades"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        for name in (
            "minimum_net_return_improvement",
            "maximum_drawdown_fraction",
            "maximum_drawdown_worsening",
            "maximum_net_loss_fraction",
        ):
            value = getattr(self, name)
            _require_finite_decimal(value, name)
            if value < 0 or value > 1:
                raise ValueError(f"{name} must be in [0, 1].")
        if (
            type(self.maximum_risk_gate_denials) is not int
            or self.maximum_risk_gate_denials < 0
        ):
            raise ValueError("maximum_risk_gate_denials must be a non-negative integer.")
        if (
            type(self.maximum_comparisons_per_dataset_test_range) is not int
            or self.maximum_comparisons_per_dataset_test_range <= 0
        ):
            raise ValueError(
                "maximum_comparisons_per_dataset_test_range must be a positive integer."
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "minimum_observations": self.minimum_observations,
            "minimum_closed_trades": self.minimum_closed_trades,
            "minimum_net_return_improvement": str(self.minimum_net_return_improvement),
            "maximum_drawdown_fraction": str(self.maximum_drawdown_fraction),
            "maximum_drawdown_worsening": str(self.maximum_drawdown_worsening),
            "maximum_net_loss_fraction": str(self.maximum_net_loss_fraction),
            "maximum_risk_gate_denials": self.maximum_risk_gate_denials,
            "maximum_comparisons_per_dataset_test_range": (
                self.maximum_comparisons_per_dataset_test_range
            ),
        }


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """Immutable evidence-based comparison outcome."""

    decision_id: str
    champion_experiment_id: str
    challenger_experiment_id: str
    mode: ExperimentMode
    status: PromotionStatus
    reasons: tuple[str, ...]
    champion_evidence_id: str
    challenger_evidence_id: str
    dataset_version: str
    test_range_start: int
    test_range_end: int
    net_return_delta: Decimal
    max_drawdown_delta: Decimal
    policy_json: str
    evaluated_at: datetime

    def __post_init__(self) -> None:
        _require_aware_utc(self.evaluated_at, "evaluated_at")
        for name in (
            "decision_id",
            "champion_experiment_id",
            "challenger_experiment_id",
            "champion_evidence_id",
            "challenger_evidence_id",
            "dataset_version",
        ):
            _require_sha(code=getattr(self, name), name=name, length=64)
        if self.mode not in {ExperimentMode.SHADOW, ExperimentMode.PAPER}:
            raise ValueError("Promotion comparisons are restricted to Shadow/Paper.")
        if self.test_range_start < 0 or self.test_range_end <= self.test_range_start:
            raise ValueError("Promotion decision requires a non-empty test range.")
        _require_finite_decimal(self.net_return_delta, "net_return_delta")
        _require_finite_decimal(self.max_drawdown_delta, "max_drawdown_delta")
        if not self.reasons:
            raise ValueError(
                "Every promotion decision must state its acceptance/rejection reasons."
            )


class ChampionChallengerGate:
    """Compare matched Paper/Shadow evidence and reject any data/config identity mismatch."""

    def __init__(self, policy: PromotionPolicy | None = None) -> None:
        self._policy = policy or PromotionPolicy()

    @property
    def policy(self) -> PromotionPolicy:
        return self._policy

    def evaluate(
        self,
        champion_spec: ExperimentSpec,
        challenger_spec: ExperimentSpec,
        champion_evidence: ExperimentRunEvidence,
        challenger_evidence: ExperimentRunEvidence,
        *,
        mode: ExperimentMode,
        evaluated_at: datetime,
        prior_comparison_count: int = 0,
    ) -> PromotionDecision:
        """Apply a fixed gate to equal data/test windows, costs, and execution environment."""
        _require_aware_utc(evaluated_at, "evaluated_at")
        if mode not in {ExperimentMode.SHADOW, ExperimentMode.PAPER}:
            raise PermissionError("Champion/Challenger evaluation cannot target Live.")
        if champion_spec.experiment_id != champion_evidence.experiment_id:
            raise ValueError("Champion evidence does not belong to the registered champion.")
        if challenger_spec.experiment_id != challenger_evidence.experiment_id:
            raise ValueError("Challenger evidence does not belong to the registered challenger.")

        hard_mismatches: list[str] = []
        pairs = (
            ("dataset version", champion_spec.dataset_version, challenger_spec.dataset_version),
            (
                "dataset version",
                champion_evidence.dataset_version,
                challenger_evidence.dataset_version,
            ),
            (
                "feature schema",
                champion_spec.feature_schema_version,
                challenger_spec.feature_schema_version,
            ),
            (
                "evaluation config",
                champion_spec.evaluation_config_fingerprint,
                challenger_spec.evaluation_config_fingerprint,
            ),
            ("test range", champion_spec.test_range, challenger_spec.test_range),
            ("test range", champion_evidence.test_range, challenger_evidence.test_range),
            ("evaluation mode", champion_evidence.mode, challenger_evidence.mode),
        )
        for label, champion_value, challenger_value in pairs:
            if champion_value != challenger_value:
                hard_mismatches.append(f"CHANGED_{label.upper().replace(' ', '_')}")
        if champion_evidence.mode is not mode or challenger_evidence.mode is not mode:
            hard_mismatches.append("EVIDENCE_MODE_DOES_NOT_MATCH_TARGET")
        for spec, evidence, label in (
            (champion_spec, champion_evidence, "CHAMPION"),
            (challenger_spec, challenger_evidence, "CHALLENGER"),
        ):
            if (
                evidence.code_revision != spec.code_revision
                or evidence.feature_schema_version != spec.feature_schema_version
                or evidence.evaluation_config_fingerprint != spec.evaluation_config_fingerprint
                or evidence.model_id != spec.model_id
                or evidence.model_version != spec.model_version
                or evidence.model_fingerprint != spec.model_fingerprint
                or evidence.dataset_version != spec.dataset_version
                or evidence.test_range != spec.test_range
                or evidence.random_seed != spec.random_seed
            ):
                hard_mismatches.append(f"{label}_EVIDENCE_IDENTITY_MISMATCH")
            if spec.mode is not mode:
                hard_mismatches.append(f"{label}_SPEC_MODE_MISMATCH")
        if type(prior_comparison_count) is not int or prior_comparison_count < 0:
            raise ValueError("prior_comparison_count must be a non-negative integer.")
        if hard_mismatches:
            return self._decision(
                champion_spec,
                challenger_spec,
                champion_evidence,
                challenger_evidence,
                mode=mode,
                status=PromotionStatus.REJECTED,
                reasons=tuple(dict.fromkeys(hard_mismatches)),
                evaluated_at=evaluated_at,
            )

        if (
            prior_comparison_count
            >= self._policy.maximum_comparisons_per_dataset_test_range
        ):
            return self._decision(
                champion_spec,
                challenger_spec,
                champion_evidence,
                challenger_evidence,
                mode=mode,
                status=PromotionStatus.INSUFFICIENT_EVIDENCE,
                reasons=("MAX_HOLDOUT_COMPARISONS_REACHED",),
                evaluated_at=evaluated_at,
            )

        insufficient: list[str] = []
        for label, evidence in (
            ("champion", champion_evidence),
            ("challenger", challenger_evidence),
        ):
            if evidence.observations < self._policy.minimum_observations:
                insufficient.append(f"{label.upper()}_OBSERVATIONS_BELOW_MINIMUM")
            if evidence.closed_trade_count < self._policy.minimum_closed_trades:
                insufficient.append(f"{label.upper()}_TRADES_BELOW_MINIMUM")
        if insufficient:
            return self._decision(
                champion_spec,
                challenger_spec,
                champion_evidence,
                challenger_evidence,
                mode=mode,
                status=PromotionStatus.INSUFFICIENT_EVIDENCE,
                reasons=tuple(insufficient),
                evaluated_at=evaluated_at,
            )

        failures: list[str] = []
        if not champion_evidence.data_quality_pass or not challenger_evidence.data_quality_pass:
            failures.append("DATA_QUALITY_NOT_PASSED")
        if (
            champion_evidence.risk_gate_denial_count > self._policy.maximum_risk_gate_denials
            or challenger_evidence.risk_gate_denial_count > self._policy.maximum_risk_gate_denials
        ):
            failures.append("RISK_GATE_DENIAL_LIMIT_EXCEEDED")
        if challenger_evidence.max_drawdown_fraction > self._policy.maximum_drawdown_fraction:
            failures.append("CHALLENGER_MAX_DRAWDOWN_EXCEEDED")
        if challenger_evidence.net_return_fraction < -self._policy.maximum_net_loss_fraction:
            failures.append("CHALLENGER_NET_LOSS_LIMIT_EXCEEDED")

        return_delta = (
            challenger_evidence.net_return_fraction - champion_evidence.net_return_fraction
        )
        drawdown_delta = (
            challenger_evidence.max_drawdown_fraction - champion_evidence.max_drawdown_fraction
        )
        if return_delta < self._policy.minimum_net_return_improvement:
            failures.append("MINIMUM_NET_RETURN_IMPROVEMENT_NOT_MET")
        if drawdown_delta > self._policy.maximum_drawdown_worsening:
            failures.append("MAXIMUM_DRAWDOWN_WORSENING_EXCEEDED")
        status = PromotionStatus.REJECTED if failures else (
            PromotionStatus.APPROVED_FOR_SHADOW
            if mode is ExperimentMode.SHADOW
            else PromotionStatus.PROMOTED_TO_PAPER
        )
        reasons = tuple(failures) if failures else (
            "ALL_PREDECLARED_PROMOTION_GATES_PASSED",
            "NO_LIVE_DEPLOYMENT_OR_RISK_POLICY_CHANGE_AUTHORIZED",
        )
        return self._decision(
            champion_spec,
            challenger_spec,
            champion_evidence,
            challenger_evidence,
            mode=mode,
            status=status,
            reasons=reasons,
            evaluated_at=evaluated_at,
        )

    def _decision(
        self,
        champion_spec: ExperimentSpec,
        challenger_spec: ExperimentSpec,
        champion_evidence: ExperimentRunEvidence,
        challenger_evidence: ExperimentRunEvidence,
        *,
        mode: ExperimentMode,
        status: PromotionStatus,
        reasons: tuple[str, ...],
        evaluated_at: datetime,
    ) -> PromotionDecision:
        return_delta = (
            challenger_evidence.net_return_fraction - champion_evidence.net_return_fraction
        )
        drawdown_delta = (
            challenger_evidence.max_drawdown_fraction - champion_evidence.max_drawdown_fraction
        )
        policy_json = _canonical_json_bytes(self._policy.as_dict()).decode("utf-8")
        material = {
            "champion_experiment_id": champion_spec.experiment_id,
            "challenger_experiment_id": challenger_spec.experiment_id,
            "mode": mode.value,
            "status": status.value,
            "reasons": list(reasons),
            "champion_evidence_id": champion_evidence.evidence_id,
            "challenger_evidence_id": challenger_evidence.evidence_id,
            "dataset_version": champion_evidence.dataset_version,
            "test_range": {
                "start": champion_evidence.test_range.start,
                "end": champion_evidence.test_range.end,
            },
            "net_return_delta": str(return_delta),
            "max_drawdown_delta": str(drawdown_delta),
            "policy": json.loads(policy_json),
            "evaluated_at": _iso_utc(evaluated_at),
        }
        return PromotionDecision(
            decision_id=_sha256(_canonical_json_bytes(material)),
            champion_experiment_id=champion_spec.experiment_id,
            challenger_experiment_id=challenger_spec.experiment_id,
            mode=mode,
            status=status,
            reasons=reasons,
            champion_evidence_id=champion_evidence.evidence_id,
            challenger_evidence_id=challenger_evidence.evidence_id,
            dataset_version=champion_evidence.dataset_version,
            test_range_start=champion_evidence.test_range.start,
            test_range_end=champion_evidence.test_range.end,
            net_return_delta=return_delta,
            max_drawdown_delta=drawdown_delta,
            policy_json=policy_json,
            evaluated_at=evaluated_at.astimezone(UTC),
        )


@dataclass(frozen=True, slots=True)
class RollbackPolicy:
    """Predeclared health triggers for the current Paper champion."""

    minimum_observations: int = 20
    maximum_drawdown_fraction: Decimal = Decimal("0.20")
    maximum_net_loss_fraction: Decimal = Decimal("0.05")
    maximum_risk_gate_denials: int = 0

    def __post_init__(self) -> None:
        if type(self.minimum_observations) is not int or self.minimum_observations <= 0:
            raise ValueError("minimum_observations must be positive.")
        for name in ("maximum_drawdown_fraction", "maximum_net_loss_fraction"):
            value = getattr(self, name)
            _require_finite_decimal(value, name)
            if not Decimal("0") <= value <= Decimal("1"):
                raise ValueError(f"{name} must be in [0, 1].")
        if (
            type(self.maximum_risk_gate_denials) is not int
            or self.maximum_risk_gate_denials < 0
        ):
            raise ValueError("maximum_risk_gate_denials must be non-negative.")


@dataclass(frozen=True, slots=True)
class RollbackDecision:
    """One health assessment; auto-rollback affects only the internal Paper champion pointer."""

    status: RollbackStatus
    trigger: RollbackTrigger | None
    previous_champion_id: str | None
    active_champion_id: str | None
    evidence_id: str
    reason: str
    checked_at: datetime

    def __post_init__(self) -> None:
        _require_aware_utc(self.checked_at, "checked_at")
        _require_sha(code=self.evidence_id, name="evidence_id", length=64)
        if not self.reason.strip():
            raise ValueError("Rollback assessment must include a reason.")


class EvidenceTrustVerifier(Protocol):
    """Trusted adapter that authenticates Paper/Shadow telemetry provenance.

    Implementations should verify a signed collector attestation and bind it to evidence ID,
    experiment identity, telemetry mode and collection window. A self-declared origin string is
    never sufficient proof of telemetry authenticity.
    """

    def verify(
        self,
        evidence: ExperimentRunEvidence,
        spec: ExperimentSpec,
    ) -> bool:
        """Return true only if a trusted external telemetry attestation validates."""
        ...


class ChampionChallengerController:
    """Coordinate paper-champion changes and automatic rollback; never touches Live."""

    def __init__(
        self,
        *,
        registry: ExperimentRegistry,
        ledger: LearningLabLedger,
        champion_experiment_id: str,
        gate: ChampionChallengerGate | None = None,
        rollback_policy: RollbackPolicy | None = None,
        evidence_trust_verifier: EvidenceTrustVerifier | None = None,
    ) -> None:
        spec = registry.get(champion_experiment_id)
        if spec.mode is not ExperimentMode.PAPER:
            raise ValueError("The active champion pointer must refer to a PAPER experiment.")
        self._registry = registry
        self._ledger = ledger
        self._gate = gate or ChampionChallengerGate()
        self._rollback_policy = rollback_policy or RollbackPolicy()
        self._evidence_trust_verifier = evidence_trust_verifier
        self._active_champion_id: str | None = champion_experiment_id
        self._previous_champion_id: str | None = None
        self._approved_shadow_candidate_id: str | None = None
        self._restore_state_from_ledger()

    def _restore_state_from_ledger(self) -> None:
        """Replay durable Paper/Shadow transitions; prior safe-hold state must never auto-enable."""
        active_id = self._active_champion_id
        previous_id: str | None = None
        shadow_candidate_id: str | None = None
        has_paper_transition = False
        for event in self._ledger.events:
            payload = json.loads(event.payload_json)
            if event.event_type is LearningEventType.CANDIDATE_PROMOTED_TO_PAPER:
                previous_id = payload["previous_paper_champion_id"]
                active_id = payload["active_paper_champion_id"]
                has_paper_transition = True
            elif event.event_type is LearningEventType.AUTO_ROLLBACK_TRIGGERED:
                rollback_status = payload["rollback_status"]
                if rollback_status == RollbackStatus.ROLLED_BACK.value:
                    active_id = payload["restored_experiment_id"]
                    previous_id = None
                    has_paper_transition = True
                elif rollback_status == RollbackStatus.SAFE_HOLD_NO_CHAMPION.value:
                    active_id = None
                    previous_id = None
                    has_paper_transition = True
            elif event.event_type is LearningEventType.CANDIDATE_APPROVED_FOR_SHADOW:
                shadow_candidate_id = event.experiment_id
        if active_id is not None:
            active_spec = self._registry.get(active_id)
            if active_spec.mode is not ExperimentMode.PAPER:
                raise ExperimentIntegrityError(
                    "Learning Lab ledger active champion is not a PAPER experiment."
                )
        if previous_id is not None:
            previous_spec = self._registry.get(previous_id)
            if previous_spec.mode is not ExperimentMode.PAPER:
                raise ExperimentIntegrityError(
                    "Learning Lab ledger rollback target is not a PAPER experiment."
                )
        if not has_paper_transition:
            bootstrap_spec = self._registry.get(self._active_champion_id or "")
            if bootstrap_spec.mode is not ExperimentMode.PAPER:
                raise ValueError("The bootstrap Champion must be registered in PAPER mode.")
        self._active_champion_id = active_id
        self._previous_champion_id = previous_id
        self._approved_shadow_candidate_id = shadow_candidate_id

    @property
    def registry(self) -> ExperimentRegistry:
        """Expose the exact registry bound to the replayed champion pointer, read-only."""
        return self._registry

    @property
    def ledger(self) -> LearningLabLedger:
        """Expose the durable ledger backing the Paper champion pointer, read-only."""
        return self._ledger

    @property
    def active_champion_id(self) -> str | None:
        return self._active_champion_id

    @property
    def previous_champion_id(self) -> str | None:
        return self._previous_champion_id

    @property
    def approved_shadow_candidate_id(self) -> str | None:
        return self._approved_shadow_candidate_id

    def compare_and_promote(
        self,
        *,
        challenger_experiment_id: str,
        challenger_evidence_id: str,
        target_mode: ExperimentMode,
        evaluated_at: datetime,
        correlation_id: str,
        champion_evidence_id: str | None = None,
        shadow_champion_experiment_id: str | None = None,
    ) -> PromotionDecision:
        """Record a comparison; only a gate-passing PAPER candidate changes Paper champion."""
        _require_aware_utc(evaluated_at, "evaluated_at")
        if not correlation_id.strip():
            raise ValueError("correlation_id must not be empty.")
        if target_mode not in {ExperimentMode.SHADOW, ExperimentMode.PAPER}:
            raise PermissionError("Phase 17 cannot promote any candidate to Live.")
        if self._active_champion_id is None:
            raise RuntimeError(
                "No active Paper champion exists; establish a reviewed fallback first."
            )

        paper_champion_spec = self._registry.get(self._active_champion_id)
        challenger_spec = self._registry.get(challenger_experiment_id)
        if target_mode is ExperimentMode.PAPER:
            champion_spec = paper_champion_spec
            if shadow_champion_experiment_id is not None:
                raise ValueError(
                    "A Shadow champion reference is valid only for Shadow comparisons."
                )
        else:
            if not shadow_champion_experiment_id:
                raise ValueError(
                    "Shadow comparison requires a Shadow-mode copy of the current champion."
                )
            champion_spec = self._registry.get(shadow_champion_experiment_id)
            if not _same_experiment_except_mode(paper_champion_spec, champion_spec):
                raise ValueError(
                    "Shadow champion reference does not match the current Paper champion identity."
                )
        if champion_evidence_id is None:
            raise ValueError("Champion evaluation evidence is required.")
        champion_evidence = self._registry.get_evidence(champion_evidence_id)
        challenger_evidence = self._registry.get_evidence(challenger_evidence_id)
        verifier = self._evidence_trust_verifier
        if verifier is None or not verifier.verify(champion_evidence, champion_spec) or not (
            verifier.verify(challenger_evidence, challenger_spec)
        ):
            self._ledger.append(
                LearningEventType.PROMOTION_REJECTED,
                experiment_id=challenger_experiment_id,
                correlation_id=correlation_id,
                occurred_at=evaluated_at,
                payload={
                    "reason_code": (
                        "TRUSTED_TELEMETRY_VERIFIER_NOT_CONFIGURED"
                        if verifier is None
                        else "TELEMETRY_ATTESTATION_REJECTED"
                    ),
                    "champion_evidence_id": champion_evidence_id,
                    "challenger_evidence_id": challenger_evidence_id,
                    "target_mode": target_mode.value,
                    "promotion_or_release": False,
                },
            )
            raise PermissionError(
                "Champion/Challenger promotion requires a trusted Paper/Shadow evidence verifier."
            )
        prior_comparison_count = sum(
            1
            for event in self._ledger.events
            if event.event_type is LearningEventType.CANDIDATE_COMPARED
            and json.loads(event.payload_json).get("dataset_version")
            == challenger_evidence.dataset_version
            and json.loads(event.payload_json).get("test_range_start")
            == challenger_evidence.test_range.start
            and json.loads(event.payload_json).get("test_range_end")
            == challenger_evidence.test_range.end
            and json.loads(event.payload_json).get("mode") == target_mode.value
        )
        decision = self._gate.evaluate(
            champion_spec,
            challenger_spec,
            champion_evidence,
            challenger_evidence,
            mode=target_mode,
            evaluated_at=evaluated_at,
            prior_comparison_count=prior_comparison_count,
        )
        self._ledger.append(
            LearningEventType.CANDIDATE_COMPARED,
            experiment_id=challenger_experiment_id,
            correlation_id=correlation_id,
            occurred_at=evaluated_at,
            payload={
                "decision_id": decision.decision_id,
                "status": decision.status.value,
                "reasons": list(decision.reasons),
                "champion_experiment_id": decision.champion_experiment_id,
                "challenger_experiment_id": decision.challenger_experiment_id,
                "net_return_delta": str(decision.net_return_delta),
                "max_drawdown_delta": str(decision.max_drawdown_delta),
                "prior_comparison_count": prior_comparison_count,
                "dataset_version": decision.dataset_version,
                "test_range_start": decision.test_range_start,
                "test_range_end": decision.test_range_end,
                "mode": decision.mode.value,
                "policy": json.loads(decision.policy_json),
            },
        )
        if decision.status is PromotionStatus.APPROVED_FOR_SHADOW:
            self._ledger.append(
                LearningEventType.CANDIDATE_APPROVED_FOR_SHADOW,
                experiment_id=challenger_experiment_id,
                correlation_id=correlation_id,
                occurred_at=evaluated_at,
                payload={"decision_id": decision.decision_id},
            )
            self._approved_shadow_candidate_id = challenger_experiment_id
        elif decision.status is PromotionStatus.PROMOTED_TO_PAPER:
            previous = self._active_champion_id
            self._ledger.append(
                LearningEventType.CANDIDATE_PROMOTED_TO_PAPER,
                experiment_id=challenger_experiment_id,
                correlation_id=correlation_id,
                occurred_at=evaluated_at,
                payload={
                    "decision_id": decision.decision_id,
                    "previous_paper_champion_id": previous,
                    "active_paper_champion_id": challenger_experiment_id,
                },
            )
            self._previous_champion_id = previous
            self._active_champion_id = challenger_experiment_id
        else:
            self._ledger.append(
                LearningEventType.PROMOTION_REJECTED,
                experiment_id=challenger_experiment_id,
                correlation_id=correlation_id,
                occurred_at=evaluated_at,
                payload={
                    "decision_id": decision.decision_id,
                    "status": decision.status.value,
                    "reasons": list(decision.reasons),
                },
            )
        return decision

    def monitor_paper_champion(
        self,
        evidence_id: str,
        *,
        checked_at: datetime,
        correlation_id: str,
    ) -> RollbackDecision:
        """Check Paper telemetry, ledger every assessment, and fail closed on hard triggers."""
        _require_aware_utc(checked_at, "checked_at")
        if not correlation_id.strip():
            raise ValueError("correlation_id must not be empty.")
        evidence = self._registry.get_evidence(evidence_id)
        if self._active_champion_id is None:
            decision = RollbackDecision(
                status=RollbackStatus.SAFE_HOLD_NO_CHAMPION,
                trigger=None,
                previous_champion_id=self._previous_champion_id,
                active_champion_id=None,
                evidence_id=evidence_id,
                reason="No Paper champion is active; no candidate is auto-enabled.",
                checked_at=checked_at,
            )
            return self._record_health_assessment(
                decision, experiment_id=evidence.experiment_id, correlation_id=correlation_id
            )

        if (
            evidence.experiment_id != self._active_champion_id
            or evidence.mode is not ExperimentMode.PAPER
        ):
            trigger = RollbackTrigger.MODEL_OR_DATA_IDENTITY_MISMATCH
        elif not self._has_trusted_telemetry(evidence):
            trigger = RollbackTrigger.TELEMETRY_ATTESTATION_FAILED
        elif not evidence.data_quality_pass:
            trigger = RollbackTrigger.DATA_QUALITY_FAILED
        elif (
            evidence.risk_gate_denial_count
            > self._rollback_policy.maximum_risk_gate_denials
        ):
            trigger = RollbackTrigger.RISK_GATE_DENIAL
        elif evidence.observations < self._rollback_policy.minimum_observations:
            decision = RollbackDecision(
                status=RollbackStatus.INSUFFICIENT_EVIDENCE,
                trigger=None,
                previous_champion_id=self._previous_champion_id,
                active_champion_id=self._active_champion_id,
                evidence_id=evidence_id,
                reason=(
                    "Insufficient monitoring window; no promotion/rollback conclusion is inferred."
                ),
                checked_at=checked_at,
            )
            return self._record_health_assessment(
                decision, experiment_id=evidence.experiment_id, correlation_id=correlation_id
            )
        elif (
            evidence.max_drawdown_fraction
            > self._rollback_policy.maximum_drawdown_fraction
        ):
            trigger = RollbackTrigger.MAX_DRAWDOWN_EXCEEDED
        elif evidence.net_return_fraction < -self._rollback_policy.maximum_net_loss_fraction:
            trigger = RollbackTrigger.NET_LOSS_EXCEEDED
        else:
            decision = RollbackDecision(
                status=RollbackStatus.HEALTHY,
                trigger=None,
                previous_champion_id=self._previous_champion_id,
                active_champion_id=self._active_champion_id,
                evidence_id=evidence_id,
                reason="Paper champion remains inside all configured health bounds.",
                checked_at=checked_at,
            )
            return self._record_health_assessment(
                decision, experiment_id=evidence.experiment_id, correlation_id=correlation_id
            )

        failed = self._active_champion_id
        restore_id = self._previous_champion_id
        if restore_id is not None:
            status = RollbackStatus.ROLLED_BACK
            reason = (
                f"Rolled Paper champion {failed} back to last known-good {restore_id}."
            )
        else:
            status = RollbackStatus.SAFE_HOLD_NO_CHAMPION
            reason = "No known-good predecessor is available; Paper selection is disabled."
        self._ledger.append(
            LearningEventType.AUTO_ROLLBACK_TRIGGERED,
            experiment_id=failed,
            correlation_id=correlation_id,
            occurred_at=checked_at,
            payload={
                "trigger": trigger.value,
                "failed_experiment_id": failed,
                "restored_experiment_id": restore_id,
                "rollback_status": status.value,
                "evidence_id": evidence.evidence_id,
                "data_quality_pass": evidence.data_quality_pass,
                "risk_gate_denial_count": evidence.risk_gate_denial_count,
                "max_drawdown_fraction": str(evidence.max_drawdown_fraction),
                "net_return_fraction": str(evidence.net_return_fraction),
            },
        )
        self._active_champion_id = restore_id
        self._previous_champion_id = None
        decision = RollbackDecision(
            status=status,
            trigger=trigger,
            previous_champion_id=failed,
            active_champion_id=self._active_champion_id,
            evidence_id=evidence_id,
            reason=reason,
            checked_at=checked_at,
        )
        return self._record_health_assessment(
            decision, experiment_id=evidence.experiment_id, correlation_id=correlation_id
        )

    def _has_trusted_telemetry(self, evidence: ExperimentRunEvidence) -> bool:
        """Fail closed if Paper health evidence lacks an accepted external attestation."""
        verifier = self._evidence_trust_verifier
        if verifier is None:
            return False
        try:
            spec = self._registry.get(evidence.experiment_id)
            return bool(verifier.verify(evidence, spec))
        except Exception:
            # Verification infrastructure errors are not health evidence; fail closed.
            return False

    def _record_health_assessment(
        self,
        decision: RollbackDecision,
        *,
        experiment_id: str,
        correlation_id: str,
    ) -> RollbackDecision:
        """Append healthy, insufficient, rollback and safe-hold assessments to the ledger."""
        self._ledger.append(
            LearningEventType.CHAMPION_HEALTH_CHECKED,
            experiment_id=experiment_id,
            correlation_id=correlation_id,
            occurred_at=decision.checked_at,
            payload={
                "status": decision.status.value,
                "trigger": decision.trigger.value if decision.trigger is not None else None,
                "previous_champion_id": decision.previous_champion_id,
                "active_champion_id": decision.active_champion_id,
                "evidence_id": decision.evidence_id,
                "reason": decision.reason,
            },
        )
        return decision


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    """Release evidence manifest; it does not deploy or change code by itself."""

    candidate_id: str
    experiment_id: str
    target: ReleaseTarget
    promotion_decision_id: str
    test_report_sha256: str
    security_report_sha256: str
    risk_policy_sha256: str
    created_at: datetime
    approved_by: str | None = None
    approval_reference: str | None = None
    approved_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in (
            "candidate_id",
            "experiment_id",
            "promotion_decision_id",
            "test_report_sha256",
            "security_report_sha256",
            "risk_policy_sha256",
        ):
            _require_sha(code=getattr(self, name), name=name, length=64)
        _require_aware_utc(self.created_at, "created_at")
        if self.approved_at is not None:
            _require_aware_utc(self.approved_at, "approved_at")
            if self.approved_at < self.created_at:
                raise ValueError("Release approval cannot precede candidate creation.")
        approval_fields = (self.approved_by, self.approval_reference, self.approved_at)
        if any(value is not None for value in approval_fields) and not all(
            value is not None for value in approval_fields
        ):
            raise ValueError("Human approval actor, reference and time must be recorded together.")

    @property
    def is_approved(self) -> bool:
        return (
            self.approved_by is not None
            and self.approval_reference is not None
            and self.approved_at is not None
        )


class ReleasePipelineGate:
    """Creates/reviews Paper/Shadow release evidence; it has no Live deploy API."""

    def __init__(
        self,
        *,
        registry: ExperimentRegistry,
        ledger: LearningLabLedger,
        approved_risk_policy_sha256: str,
    ) -> None:
        _require_sha(
            code=approved_risk_policy_sha256,
            name="approved_risk_policy_sha256",
            length=64,
        )
        self._registry = registry
        self._ledger = ledger
        self._approved_risk_policy_sha256 = approved_risk_policy_sha256
        self._candidates: dict[str, ReleaseCandidate] = {}
        self._load_candidates_from_ledger()

    def _load_candidates_from_ledger(self) -> None:
        """Rehydrate immutable manifests and approvals from the verified ledger after restart."""
        for event in self._ledger.events:
            payload = json.loads(event.payload_json)
            if event.event_type is LearningEventType.RELEASE_CANDIDATE_CREATED:
                try:
                    material = {
                        "experiment_id": event.experiment_id,
                        "target": payload["target"],
                        "promotion_decision_id": payload["promotion_decision_id"],
                        "test_report_sha256": payload["test_report_sha256"],
                        "security_report_sha256": payload["security_report_sha256"],
                        "risk_policy_sha256": payload["risk_policy_sha256"],
                        "created_at": _iso_utc(event.occurred_at),
                    }
                    candidate = ReleaseCandidate(
                        candidate_id=payload["candidate_id"],
                        experiment_id=event.experiment_id,
                        target=ReleaseTarget(payload["target"]),
                        promotion_decision_id=payload["promotion_decision_id"],
                        test_report_sha256=payload["test_report_sha256"],
                        security_report_sha256=payload["security_report_sha256"],
                        risk_policy_sha256=payload["risk_policy_sha256"],
                        created_at=event.occurred_at,
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ExperimentIntegrityError(
                        "Release-candidate ledger event is malformed."
                    ) from exc
                if _sha256(_canonical_json_bytes(material)) != candidate.candidate_id:
                    raise ExperimentIntegrityError(
                        "Release-candidate ID does not match its recorded manifest."
                    )
                previous = self._candidates.get(candidate.candidate_id)
                if previous is not None and previous != candidate:
                    raise ExperimentIntegrityError(
                        "Release-candidate ID has conflicting ledger manifests."
                    )
                self._candidates[candidate.candidate_id] = candidate
            elif event.event_type is LearningEventType.RELEASE_APPROVED:
                try:
                    candidate_id = payload["candidate_id"]
                    candidate = self._candidates[candidate_id]
                    if payload["risk_policy_sha256"] != candidate.risk_policy_sha256:
                        raise ExperimentIntegrityError(
                            "Release approval risk-policy hash differs from candidate manifest."
                        )
                    approved = replace(
                        candidate,
                        approved_by=payload["approved_by"],
                        approval_reference=payload["approval_reference"],
                        approved_at=event.occurred_at,
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    if isinstance(exc, ExperimentIntegrityError):
                        raise
                    raise ExperimentIntegrityError(
                        "Release approval event does not match an existing candidate."
                    ) from exc
                if candidate.is_approved and candidate != approved:
                    raise ExperimentIntegrityError(
                        "Release candidate has conflicting approval events."
                    )
                self._candidates[candidate_id] = approved

    def create_candidate(
        self,
        experiment_id: str,
        *,
        target: ReleaseTarget,
        promotion_decision_id: str,
        test_report_sha256: str,
        security_report_sha256: str,
        risk_policy_sha256: str,
        created_at: datetime,
        correlation_id: str,
    ) -> ReleaseCandidate:
        """Record a gated non-Live candidate; an attempted Live target is rejected and audited."""
        _require_aware_utc(created_at, "created_at")
        if not correlation_id.strip():
            raise ValueError("correlation_id must not be empty.")
        spec = self._registry.get(experiment_id)
        if target is ReleaseTarget.LIVE:
            self._ledger.append(
                LearningEventType.LIVE_RELEASE_BLOCKED,
                experiment_id=experiment_id,
                correlation_id=correlation_id,
                occurred_at=created_at,
                payload={"target": target.value, "reason": "PHASE17_HAS_NO_LIVE_PROMOTION_PATH"},
            )
            raise PermissionError("Phase 17 cannot create or authorize a Live release.")
        if target not in {ReleaseTarget.PAPER, ReleaseTarget.SHADOW}:
            self._record_rejection(
                experiment_id,
                correlation_id=correlation_id,
                occurred_at=created_at,
                reason_code="UNSUPPORTED_RELEASE_TARGET",
                payload={"target": target.value},
            )
            raise ValueError("Unsupported release target.")
        _require_sha(
            code=promotion_decision_id,
            name="promotion_decision_id",
            length=64,
        )
        expected_event = (
            LearningEventType.CANDIDATE_PROMOTED_TO_PAPER
            if target is ReleaseTarget.PAPER
            else LearningEventType.CANDIDATE_APPROVED_FOR_SHADOW
        )
        if not any(
            event.event_type is expected_event
            and event.experiment_id == experiment_id
            and json.loads(event.payload_json).get("decision_id") == promotion_decision_id
            for event in self._ledger.events
        ):
            self._record_rejection(
                experiment_id,
                correlation_id=correlation_id,
                occurred_at=created_at,
                reason_code="PASSING_PROMOTION_DECISION_MISSING",
                payload={
                    "target": target.value,
                    "promotion_decision_id": promotion_decision_id,
                },
            )
            raise PermissionError(
                "Release candidate requires a matching passing Champion/Challenger decision."
            )
        if risk_policy_sha256 != self._approved_risk_policy_sha256:
            self._record_rejection(
                experiment_id,
                correlation_id=correlation_id,
                occurred_at=created_at,
                reason_code="RISK_POLICY_FINGERPRINT_MISMATCH",
                payload={"target": target.value, "risk_policy_sha256": risk_policy_sha256},
            )
            raise PermissionError(
                "Risk policy fingerprint differs from the approved immutable policy."
            )
        expected_mode = ExperimentMode(target.value)
        if spec.mode is not expected_mode:
            self._record_rejection(
                experiment_id,
                correlation_id=correlation_id,
                occurred_at=created_at,
                reason_code="EXPERIMENT_MODE_DOES_NOT_MATCH_RELEASE_TARGET",
                payload={"target": target.value, "experiment_mode": spec.mode.value},
            )
            raise ValueError("Experiment mode must match the proposed non-Live release target.")
        for name, value in (
            ("test_report_sha256", test_report_sha256),
            ("security_report_sha256", security_report_sha256),
            ("risk_policy_sha256", risk_policy_sha256),
        ):
            _require_sha(code=value, name=name, length=64)
        material = {
            "experiment_id": experiment_id,
            "target": target.value,
            "promotion_decision_id": promotion_decision_id,
            "test_report_sha256": test_report_sha256,
            "security_report_sha256": security_report_sha256,
            "risk_policy_sha256": risk_policy_sha256,
            "created_at": _iso_utc(created_at),
        }
        candidate = ReleaseCandidate(
            candidate_id=_sha256(_canonical_json_bytes(material)),
            experiment_id=experiment_id,
            target=target,
            promotion_decision_id=promotion_decision_id,
            test_report_sha256=test_report_sha256,
            security_report_sha256=security_report_sha256,
            risk_policy_sha256=risk_policy_sha256,
            created_at=created_at,
        )
        existing = self._candidates.get(candidate.candidate_id)
        if existing is not None:
            return existing
        self._ledger.append(
            LearningEventType.RELEASE_CANDIDATE_CREATED,
            experiment_id=experiment_id,
            correlation_id=correlation_id,
            occurred_at=created_at,
            payload={
                "candidate_id": candidate.candidate_id,
                "target": target.value,
                "promotion_decision_id": promotion_decision_id,
                "test_report_sha256": test_report_sha256,
                "security_report_sha256": security_report_sha256,
                "risk_policy_sha256": risk_policy_sha256,
                "requires_external_release_pipeline": True,
                "auto_deploy": False,
            },
        )
        self._candidates[candidate.candidate_id] = candidate
        return candidate

    def _record_rejection(
        self,
        experiment_id: str,
        *,
        correlation_id: str,
        occurred_at: datetime,
        reason_code: str,
        payload: dict[str, Any],
    ) -> None:
        """Keep rejected release transitions visible in the Learning Lab Ledger."""
        self._ledger.append(
            LearningEventType.RELEASE_REJECTED,
            experiment_id=experiment_id,
            correlation_id=correlation_id,
            occurred_at=occurred_at,
            payload={"reason_code": reason_code, **payload},
        )

    def approve_candidate(
        self,
        candidate_id: str,
        *,
        approved_by: str,
        approval_reference: str,
        approved_at: datetime,
        current_risk_policy_sha256: str,
        correlation_id: str,
    ) -> ReleaseCandidate:
        """Record human review evidence; approval still does not deploy or authorize Live."""
        _require_aware_utc(approved_at, "approved_at")
        if (
            not approved_by.strip()
            or not approval_reference.strip()
            or not correlation_id.strip()
        ):
            raise ValueError("Human approver, approval reference and correlation ID are required.")
        try:
            candidate = self._candidates[candidate_id]
        except KeyError as exc:
            raise KeyError("Unknown release candidate ID.") from exc
        if (
            current_risk_policy_sha256 != self._approved_risk_policy_sha256
            or candidate.risk_policy_sha256 != self._approved_risk_policy_sha256
        ):
            self._record_rejection(
                candidate.experiment_id,
                correlation_id=correlation_id,
                occurred_at=approved_at,
                reason_code="RISK_POLICY_CHANGED_ON_APPROVAL",
                payload={
                    "candidate_id": candidate_id,
                    "candidate_risk_policy_sha256": candidate.risk_policy_sha256,
                    "current_risk_policy_sha256": current_risk_policy_sha256,
                    "approved_risk_policy_sha256": self._approved_risk_policy_sha256,
                },
            )
            raise PermissionError("Risk policy changed after release candidate creation.")
        if candidate.is_approved:
            if (
                candidate.approved_by == approved_by
                and candidate.approval_reference == approval_reference
                and candidate.approved_at == approved_at
            ):
                return candidate
            raise ValueError("Release candidate already has a different immutable approval.")
        approved = replace(
            candidate,
            approved_by=approved_by,
            approval_reference=approval_reference,
            approved_at=approved_at,
        )
        self._ledger.append(
            LearningEventType.RELEASE_APPROVED,
            experiment_id=candidate.experiment_id,
            correlation_id=correlation_id,
            occurred_at=approved_at,
            payload={
                "candidate_id": candidate_id,
                "approved_by": approved_by,
                "approval_reference": approval_reference,
                "risk_policy_sha256": current_risk_policy_sha256,
                "deploy_action": "none_external_release_pipeline_required",
            },
        )
        self._candidates[candidate_id] = approved
        return approved


def _same_experiment_except_mode(left: ExperimentSpec, right: ExperimentSpec) -> bool:
    """Check that a Shadow copy is the exact same model/data/config as the Paper champion."""
    left_material = left.identity_material()
    right_material = right.identity_material()
    left_material.pop("mode", None)
    right_material.pop("mode", None)
    return left_material == right_material


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
