"""Regime-to-strategy selection with evidence hysteresis and an append-only decision ledger.

This module is a research selector only. It does not produce order requests or call execution/risk.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from evoryn.domain.strategy_decisions import StrategySelectionLedgerEntry
from evoryn.intelligence.regime import RegimeLabel, RegimePrediction
from evoryn.strategies.plugins import (
    StrategyPluginRegistration,
    StrategyPluginRegistry,
)


class StrategySelectionLedgerSink(Protocol):
    """Minimal append-only decision sink; selection has no dependency on execution services."""

    def record_strategy_selection(self, entry: StrategySelectionLedgerEntry) -> None:
        """Append an auditable strategy-selection event."""
        ...


class SelectionStatus(StrEnum):
    """Machine-readable strategy-selection outcomes for audit and evaluation."""

    SWITCHED = "switched"
    RETAINED = "retained"
    CANDIDATE_PENDING = "candidate_pending"
    ABSTAINED = "abstained"
    DUPLICATE_EVIDENCE = "duplicate_evidence"
    NO_COMPATIBLE_PLUGIN = "no_compatible_plugin"
    MODEL_IDENTITY_CHANGED = "model_identity_changed"
    REGISTRY_CHANGED = "registry_changed"


@dataclass(frozen=True, slots=True)
class StrategySelectionPolicy:
    """Hard bounds on regime confidence, evidence count, and strategy churn."""

    min_confidence: Decimal = Decimal("0.65")
    required_evidence_count: int = 3
    required_stability_duration: timedelta = timedelta(hours=2)
    minimum_hold_duration: timedelta = timedelta(hours=6)
    maximum_prediction_age: timedelta = timedelta(hours=2)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.min_confidence, Decimal)
            or not self.min_confidence.is_finite()
            or self.min_confidence <= 0
            or self.min_confidence > 1
        ):
            raise ValueError("min_confidence must be a finite Decimal in (0, 1].")
        if type(self.required_evidence_count) is not int or self.required_evidence_count < 2:
            raise ValueError("required_evidence_count must be an integer >= 2.")
        for name in (
            "required_stability_duration",
            "minimum_hold_duration",
            "maximum_prediction_age",
        ):
            value = getattr(self, name)
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError(f"{name} must be a positive timedelta.")

    def as_dict(self) -> dict[str, object]:
        return {
            "min_confidence": str(self.min_confidence),
            "required_evidence_count": self.required_evidence_count,
            "required_stability_seconds": int(self.required_stability_duration.total_seconds()),
            "minimum_hold_seconds": int(self.minimum_hold_duration.total_seconds()),
            "maximum_prediction_age_seconds": int(self.maximum_prediction_age.total_seconds()),
        }


@dataclass(frozen=True, slots=True)
class StrategySelectionDecision:
    """One immutable decision, including incumbent/candidate, evidence, and invalidation rules."""

    decision_id: str
    correlation_id: str
    market_symbol: str
    recorded_at: datetime
    regime_as_of: datetime
    regime_label: RegimeLabel
    regime_confidence: Decimal
    confidence_semantics: str
    model_id: str
    model_version: str
    model_fingerprint: str
    feature_schema_version: str
    registry_fingerprint: str
    routing_fingerprint: str
    previous_strategy_id: str | None
    candidate_strategy_id: str | None
    selected_strategy_id: str | None
    alternative_strategy_ids: tuple[str, ...]
    status: SelectionStatus
    reason_code: str
    reason: str
    evidence_count: int
    candidate_since: datetime | None
    last_switch_at: datetime | None
    policy: StrategySelectionPolicy
    allow_new_signals: bool
    invalidation_conditions: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("recorded_at", "regime_as_of"):
            _require_aware(getattr(self, name), name)
        for name in (
            "decision_id",
            "correlation_id",
            "market_symbol",
            "confidence_semantics",
            "model_id",
            "model_version",
            "model_fingerprint",
            "feature_schema_version",
            "registry_fingerprint",
            "reason_code",
            "reason",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty.")
        if self.candidate_since is not None:
            _require_aware(self.candidate_since, "candidate_since")
        if self.last_switch_at is not None:
            _require_aware(self.last_switch_at, "last_switch_at")
        if (
            not self.regime_confidence.is_finite()
            or self.regime_confidence < 0
            or self.regime_confidence > 1
        ):
            raise ValueError("regime_confidence must be finite and in [0, 1].")
        if type(self.evidence_count) is not int or self.evidence_count < 0:
            raise ValueError("evidence_count must be a non-negative integer.")
        if type(self.allow_new_signals) is not bool:
            raise ValueError("allow_new_signals must be an explicit boolean.")
        if self.status is SelectionStatus.ABSTAINED and self.allow_new_signals:
            raise ValueError("Abstention must prohibit new strategy signals.")
        if len(set(self.alternative_strategy_ids)) != len(self.alternative_strategy_ids):
            raise ValueError("Alternative strategy IDs must be unique.")

    def to_ledger_entry(self) -> StrategySelectionLedgerEntry:
        """Adapt the pure selection result into the existing Phase 10 append-only ledger."""
        return StrategySelectionLedgerEntry(
            event_id=self.decision_id,
            correlation_id=self.correlation_id,
            recorded_at=self.recorded_at,
            market_symbol=self.market_symbol,
            regime_label=self.regime_label.value,
            regime_confidence=self.regime_confidence,
            confidence_semantics=self.confidence_semantics,
            model_id=self.model_id,
            model_version=self.model_version,
            model_fingerprint=self.model_fingerprint,
            feature_schema_version=self.feature_schema_version,
            candidate_strategy_id=self.candidate_strategy_id,
            active_strategy_id=self.selected_strategy_id,
            alternative_strategy_ids=self.alternative_strategy_ids,
            status=self.status.value,
            reason_code=self.reason_code,
            reason=self.reason,
            evidence_count=min(self.evidence_count, self.policy.required_evidence_count),
            required_evidence_count=self.policy.required_evidence_count,
            candidate_since=self.candidate_since,
            last_switch_at=self.last_switch_at,
            required_stability_seconds=int(
                self.policy.required_stability_duration.total_seconds()
            ),
            minimum_hold_seconds=int(self.policy.minimum_hold_duration.total_seconds()),
            confidence_threshold=self.policy.min_confidence,
            allow_new_signals=self.allow_new_signals,
            invalidation_conditions=self.invalidation_conditions,
            registry_fingerprint=self.registry_fingerprint,
            routing_fingerprint=self.routing_fingerprint,
        )




class RegimeStrategySelector:
    """Stateful selector requiring repeated, time-separated evidence before switching plugins."""

    _ACCEPTED_CONFIDENCE_SEMANTICS = frozenset(
        {"hmm_posterior", "calibrated_probability"}
    )

    def __init__(
        self,
        registry: StrategyPluginRegistry,
        *,
        policy: StrategySelectionPolicy | None = None,
        preferred_strategy_by_regime: Mapping[RegimeLabel, tuple[str, ...]] | None = None,
        decision_ledger: StrategySelectionLedgerSink | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy or StrategySelectionPolicy()
        preferences = (
            {
                RegimeLabel.BULL_TREND: (
                    "sma-crossover-long-flat",
                    "sma-trend-long-flat",
                ),
                RegimeLabel.BEAR_TREND: ("flat-no-position",),
                RegimeLabel.RANGE_BOUND: ("sma-trend-long-flat", "flat-no-position"),
                RegimeLabel.HIGH_VOLATILITY: ("flat-no-position",),
            }
            if preferred_strategy_by_regime is None
            else preferred_strategy_by_regime
        )
        self._preferences: Mapping[RegimeLabel, tuple[str, ...]] = MappingProxyType(
            {label: tuple(choices) for label, choices in preferences.items()}
        )
        self._validate_preferences()
        self._routing_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    label.value: list(choices)
                    for label, choices in sorted(
                        self._preferences.items(), key=lambda item: item[0].value
                    )
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self._ledger = decision_ledger
        self._current_strategy_id: str | None = None
        self._candidate_strategy_id: str | None = None
        self._candidate_since: datetime | None = None
        self._candidate_evidence_count = 0
        self._last_prediction_as_of: datetime | None = None
        self._last_switch_at: datetime | None = None
        self._model_identity: tuple[str, str, str, str] | None = None
        self._registry_fingerprint = registry.fingerprint
        self._decision_sequence = 0

    @property
    def current_strategy_id(self) -> str | None:
        """Return the currently selected plugin ID; None means no validated selection yet."""
        return self._current_strategy_id

    @property
    def registry_fingerprint(self) -> str:
        return self._registry_fingerprint

    @property
    def policy(self) -> StrategySelectionPolicy:
        return self._policy

    @property
    def routing_fingerprint(self) -> str:
        """Return the immutable regime-to-plugin preference fingerprint."""
        return self._routing_fingerprint

    def select(
        self,
        prediction: RegimePrediction,
        *,
        now: datetime,
        correlation_id: str,
        market_symbol: str,
    ) -> StrategySelectionDecision:
        """Apply gates, update hysteresis state, and append the decision when a ledger exists."""
        _require_aware(now, "now")
        if not correlation_id.strip():
            raise ValueError("correlation_id must not be empty.")
        if not market_symbol.strip():
            raise ValueError("market_symbol must not be empty.")
        if prediction.as_of.utcoffset() != timedelta(0):
            self._clear_candidate()
            return self._make_decision(
                prediction,
                now=now,
                correlation_id=correlation_id,
                market_symbol=market_symbol,
                status=SelectionStatus.ABSTAINED,
                reason_code="NON_CANONICAL_REGIME_TIMESTAMP",
                reason="Regime prediction timestamps must be canonical UTC values.",
                candidate_id=None,
                alternatives=(),
                allow_new_signals=False,
            )
        if prediction.as_of > now:
            self._clear_candidate()
            return self._make_decision(
                prediction,
                now=now,
                correlation_id=correlation_id,
                market_symbol=market_symbol,
                status=SelectionStatus.ABSTAINED,
                reason_code="FUTURE_REGIME_PREDICTION",
                reason="The regime prediction timestamp is later than the selector decision time.",
                candidate_id=None,
                alternatives=(),
                allow_new_signals=False,
            )
        if now - prediction.as_of > self._policy.maximum_prediction_age:
            self._clear_candidate()
            return self._make_decision(
                prediction,
                now=now,
                correlation_id=correlation_id,
                market_symbol=market_symbol,
                status=SelectionStatus.ABSTAINED,
                reason_code="STALE_REGIME_PREDICTION",
                reason="The regime prediction exceeded the selector's maximum allowed age.",
                candidate_id=None,
                alternatives=(),
                allow_new_signals=False,
            )

        current_registry_fingerprint = self._registry.fingerprint
        if current_registry_fingerprint != self._registry_fingerprint:
            self._reset_after_external_change(current_registry_fingerprint, prediction)
            return self._make_decision(
                prediction,
                now=now,
                correlation_id=correlation_id,
                market_symbol=market_symbol,
                status=SelectionStatus.REGISTRY_CHANGED,
                reason_code="REGISTRY_FINGERPRINT_CHANGED",
                reason="The plugin registry changed; selection state was cleared for revalidation.",
                candidate_id=None,
                alternatives=(),
                allow_new_signals=False,
            )

        identity = (
            prediction.model_id,
            prediction.model_version,
            prediction.model_fingerprint,
            prediction.feature_schema_version,
        )
        if self._model_identity is not None and identity != self._model_identity:
            self._reset_after_external_change(current_registry_fingerprint, prediction)
            self._model_identity = identity
            return self._make_decision(
                prediction,
                now=now,
                correlation_id=correlation_id,
                market_symbol=market_symbol,
                status=SelectionStatus.MODEL_IDENTITY_CHANGED,
                reason_code="MODEL_FINGERPRINT_CHANGED",
                reason="The model identity changed; the incumbent was cleared for revalidation.",
                candidate_id=None,
                alternatives=(),
                allow_new_signals=False,
            )
        self._model_identity = identity

        if (
            self._last_prediction_as_of is not None
            and prediction.as_of <= self._last_prediction_as_of
        ):
            return self._make_decision(
                prediction,
                now=now,
                correlation_id=correlation_id,
                market_symbol=market_symbol,
                status=SelectionStatus.DUPLICATE_EVIDENCE,
                reason_code="NON_INCREASING_REGIME_TIMESTAMP",
                reason="This prediction is duplicate or older and cannot satisfy new evidence.",
                candidate_id=self._candidate_strategy_id,
                alternatives=(),
                allow_new_signals=False,
            )
        if (
            self._last_prediction_as_of is not None
            and prediction.as_of - self._last_prediction_as_of
            > self._policy.maximum_prediction_age
        ):
            self._clear_candidate()
        self._last_prediction_as_of = prediction.as_of

        if (
            prediction.abstained
            or prediction.label is RegimeLabel.UNKNOWN
            or prediction.confidence_semantics not in self._ACCEPTED_CONFIDENCE_SEMANTICS
            or prediction.confidence < self._policy.min_confidence
        ):
            self._clear_candidate()
            reason_code = (
                "REGIME_ABSTAINED"
                if prediction.abstained or prediction.label is RegimeLabel.UNKNOWN
                else "REGIME_CONFIDENCE_BELOW_SELECTOR_THRESHOLD"
                if prediction.confidence < self._policy.min_confidence
                else "UNVALIDATED_CONFIDENCE_SEMANTICS"
            )
            return self._make_decision(
                prediction,
                now=now,
                correlation_id=correlation_id,
                market_symbol=market_symbol,
                status=SelectionStatus.ABSTAINED,
                reason_code=reason_code,
                reason="No new strategy signal is permitted for this uncertain regime output.",
                candidate_id=None,
                alternatives=(),
                allow_new_signals=False,
            )

        options = self._registry.options_for(prediction.label)
        ordered_options = self._ordered_options(prediction.label, options)
        if not ordered_options:
            self._clear_candidate()
            return self._make_decision(
                prediction,
                now=now,
                correlation_id=correlation_id,
                market_symbol=market_symbol,
                status=SelectionStatus.NO_COMPATIBLE_PLUGIN,
                reason_code="NO_ENABLED_STRATEGY_FOR_REGIME",
                reason="No enabled plugin explicitly supports the detected regime.",
                candidate_id=None,
                alternatives=(),
                allow_new_signals=False,
            )
        desired = ordered_options[0]
        alternative_ids = tuple(plugin.plugin_id for plugin in ordered_options)

        if desired == self._current_strategy_id:
            self._clear_candidate()
            return self._make_decision(
                prediction,
                now=now,
                correlation_id=correlation_id,
                market_symbol=market_symbol,
                status=SelectionStatus.RETAINED,
                reason_code="CURRENT_STRATEGY_COMPATIBLE",
                reason="The current plugin remains the configured choice for this regime.",
                candidate_id=desired,
                alternatives=alternative_ids,
                allow_new_signals=True,
            )

        self._observe_candidate(desired, prediction.as_of)
        stability_elapsed = (
            prediction.as_of - self._candidate_since
            if self._candidate_since is not None
            else timedelta(0)
        )
        hold_elapsed = (
            now - self._last_switch_at if self._last_switch_at is not None else None
        )
        evidence_ready = self._candidate_evidence_count >= self._policy.required_evidence_count
        stability_ready = stability_elapsed >= self._policy.required_stability_duration
        hold_ready = hold_elapsed is None or hold_elapsed >= self._policy.minimum_hold_duration
        if evidence_ready and stability_ready and hold_ready:
            previous = self._current_strategy_id
            self._current_strategy_id = desired
            self._last_switch_at = now
            decision = self._make_decision(
                prediction,
                now=now,
                correlation_id=correlation_id,
                market_symbol=market_symbol,
                status=SelectionStatus.SWITCHED,
                reason_code="HYSTERESIS_REQUIREMENTS_MET",
                reason=(
                    f"Selected {desired!r} after {self._policy.required_evidence_count} "
                    "distinct regime observations, stable-duration and hold-duration checks."
                ),
                candidate_id=desired,
                alternatives=alternative_ids,
                allow_new_signals=True,
                previous_strategy_id=previous,
            )
            self._clear_candidate()
            return decision

        reasons: list[str] = []
        if not evidence_ready:
            reasons.append(
                f"evidence {self._candidate_evidence_count}/"
                f"{self._policy.required_evidence_count}"
            )
        if not stability_ready:
            reasons.append(
                f"candidate stability {int(stability_elapsed.total_seconds())}/"
                f"{int(self._policy.required_stability_duration.total_seconds())} seconds"
            )
        if not hold_ready and hold_elapsed is not None:
            reasons.append(
                f"minimum hold {int(hold_elapsed.total_seconds())}/"
                f"{int(self._policy.minimum_hold_duration.total_seconds())} seconds"
            )
        return self._make_decision(
            prediction,
            now=now,
            correlation_id=correlation_id,
            market_symbol=market_symbol,
            status=SelectionStatus.CANDIDATE_PENDING,
            reason_code="HYSTERESIS_PENDING",
            reason="Strategy switch withheld: " + "; ".join(reasons) + ".",
            candidate_id=desired,
            alternatives=alternative_ids,
            allow_new_signals=self._current_strategy_id is not None,
        )

    def _validate_preferences(self) -> None:
        known_regimes = {
            RegimeLabel.BULL_TREND,
            RegimeLabel.BEAR_TREND,
            RegimeLabel.RANGE_BOUND,
            RegimeLabel.HIGH_VOLATILITY,
        }
        if set(self._preferences) != known_regimes:
            raise ValueError("A strategy preference list is required for every known regime.")
        for label, choices in self._preferences.items():
            if not isinstance(label, RegimeLabel) or label is RegimeLabel.UNKNOWN:
                raise ValueError("Strategy preferences may only name known RegimeLabel values.")
            if not choices or any(not choice.strip() for choice in choices):
                raise ValueError("Every mapped regime must have non-empty preferred plugin IDs.")
            if len(set(choices)) != len(choices):
                raise ValueError("Preferred plugin IDs must be unique per regime.")
            for plugin_id in choices:
                try:
                    plugin = self._registry.get(plugin_id)
                except KeyError as exc:
                    raise ValueError(
                        f"Preferred strategy plugin {plugin_id!r} is not registered."
                    ) from exc
                if label not in plugin.supported_regimes:
                    raise ValueError(
                        f"Plugin {plugin_id!r} is not registered for regime {label.value!r}."
                    )

    def _ordered_options(
        self,
        regime: RegimeLabel,
        options: tuple[StrategyPluginRegistration, ...],
    ) -> tuple[StrategyPluginRegistration, ...]:
        by_id = {plugin.plugin_id: plugin for plugin in options}
        preferred = self._preferences.get(regime, ())
        ordered_ids = [plugin_id for plugin_id in preferred if plugin_id in by_id]
        ordered_ids.extend(sorted(plugin_id for plugin_id in by_id if plugin_id not in ordered_ids))
        return tuple(by_id[plugin_id] for plugin_id in ordered_ids)

    def _observe_candidate(self, plugin_id: str, as_of: datetime) -> None:
        if plugin_id != self._candidate_strategy_id:
            self._candidate_strategy_id = plugin_id
            self._candidate_since = as_of
            self._candidate_evidence_count = 1
        else:
            # The public select() method rejects duplicate/non-increasing as_of timestamps first.
            self._candidate_evidence_count += 1

    def _clear_candidate(self) -> None:
        self._candidate_strategy_id = None
        self._candidate_since = None
        self._candidate_evidence_count = 0

    def _reset_after_external_change(
        self, registry_fingerprint: str, prediction: RegimePrediction
    ) -> None:
        self._current_strategy_id = None
        self._last_switch_at = None
        self._clear_candidate()
        self._last_prediction_as_of = prediction.as_of
        self._registry_fingerprint = registry_fingerprint

    def _make_decision(
        self,
        prediction: RegimePrediction,
        *,
        now: datetime,
        correlation_id: str,
        market_symbol: str,
        status: SelectionStatus,
        reason_code: str,
        reason: str,
        candidate_id: str | None,
        alternatives: tuple[str, ...],
        allow_new_signals: bool,
        previous_strategy_id: str | None = None,
    ) -> StrategySelectionDecision:
        self._decision_sequence += 1
        decision_id = hashlib.sha256(
            f"{correlation_id}|{self._decision_sequence}|{reason_code}".encode("utf-8")
        ).hexdigest()
        decision = StrategySelectionDecision(
            decision_id=decision_id,
            correlation_id=correlation_id,
            market_symbol=market_symbol,
            recorded_at=now.astimezone(UTC),
            regime_as_of=prediction.as_of,
            regime_label=prediction.label,
            regime_confidence=prediction.confidence,
            confidence_semantics=prediction.confidence_semantics,
            model_id=prediction.model_id,
            model_version=prediction.model_version,
            model_fingerprint=prediction.model_fingerprint,
            feature_schema_version=prediction.feature_schema_version,
            registry_fingerprint=self._registry_fingerprint,
            routing_fingerprint=self._routing_fingerprint,
            previous_strategy_id=(
                previous_strategy_id
                if status is SelectionStatus.SWITCHED
                else self._current_strategy_id
            ),
            candidate_strategy_id=candidate_id,
            selected_strategy_id=self._current_strategy_id,
            alternative_strategy_ids=alternatives,
            status=status,
            reason_code=reason_code,
            reason=reason,
            evidence_count=self._candidate_evidence_count,
            candidate_since=self._candidate_since,
            last_switch_at=self._last_switch_at,
            policy=self._policy,
            allow_new_signals=allow_new_signals,
            invalidation_conditions=(
                "regime_unknown_or_abstained",
                "confidence_below_threshold_or_uncalibrated",
                "stale_or_future_regime_timestamp",
                "model_fingerprint_or_feature_schema_changed",
                "strategy_registry_fingerprint_changed",
                "strategy_routing_fingerprint_changed",
                "hard_risk_gate_denies_or_halts_execution",
                "out_of_sample_evidence_fails_or_is_missing",
            ),
        )
        if self._ledger is not None:
            self._ledger.record_strategy_selection(decision.to_ledger_entry())
        return decision


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")
