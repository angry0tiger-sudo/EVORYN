"""Read-only Shadow decision capture over an explicitly authorized candle feed.

This module has no execution port, order submit/cancel API, account mutation, or RiskEngine
dependency. Hypothetical quantities are analytical projections from an explicit virtual budget.
Every decision is ledgered before in-memory state advances; missing trust/data gates abstain.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from enum import StrEnum
from typing import Any, Protocol

from evoryn.domain.models import Market, MarketCandle
from evoryn.intelligence.regime import RegimeLabel, RegimePrediction
from evoryn.learning_lab.experiment_registry import (
    ExperimentMode,
    ExperimentRegistry,
    ExperimentSpec,
    LearningEventType,
    LearningLabLedger,
    configuration_fingerprint,
)
from evoryn.learning_lab.promotion import ChampionChallengerController
from evoryn.strategies.contracts import CausalTargetStrategy


class ShadowSafetyError(RuntimeError):
    """Shadow configuration or ledger integrity could not be trusted."""


class ShadowLedgerUnavailable(RuntimeError):
    """The Learning Lab ledger could not durably record a Shadow event."""


class ShadowOutcome(StrEnum):
    ABSTAIN = "abstain"
    REJECT = "reject"
    STAY_FLAT = "stay_flat"
    HOLD_LONG = "hold_long"
    ENTER_LONG = "enter_long"
    EXIT_LONG = "exit_long"
    ADJUST_LONG_UP = "adjust_long_up"
    ADJUST_LONG_DOWN = "adjust_long_down"


class ShadowPollStatus(StrEnum):
    OBSERVED = "observed"
    NO_COMPLETED_CANDLES = "no_completed_candles"
    FEED_FAILURE = "feed_failure"


@dataclass(frozen=True, slots=True)
class ShadowConfig:
    """Immutable policy used in experiment fingerprints and per-candle validation."""

    market: Market
    interval: timedelta
    source_id: str
    source_version: str
    virtual_capital: Decimal
    allocation_fraction: Decimal
    quantity_step: Decimal
    max_candle_age_seconds: float
    max_future_skew_seconds: float = 1.0
    min_regime_confidence: Decimal = Decimal("0.65")
    max_history_candles: int = 500

    def __post_init__(self) -> None:
        if not isinstance(self.market, Market):
            raise ValueError("market must be a canonical Market.")
        if not isinstance(self.interval, timedelta) or self.interval <= timedelta(0):
            raise ValueError("interval must be a positive timedelta.")
        for name in ("source_id", "source_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty.")
        for name in ("virtual_capital", "quantity_step"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a finite positive Decimal.")
        if (
            not isinstance(self.allocation_fraction, Decimal)
            or not self.allocation_fraction.is_finite()
            or not Decimal("0") < self.allocation_fraction <= Decimal("1")
        ):
            raise ValueError("allocation_fraction must be a Decimal in (0, 1].")
        if not math.isfinite(self.max_candle_age_seconds) or self.max_candle_age_seconds <= 0:
            raise ValueError("max_candle_age_seconds must be finite and positive.")
        if not math.isfinite(self.max_future_skew_seconds) or self.max_future_skew_seconds < 0:
            raise ValueError("max_future_skew_seconds must be finite and non-negative.")
        if (
            not isinstance(self.min_regime_confidence, Decimal)
            or not self.min_regime_confidence.is_finite()
            or not Decimal("0") < self.min_regime_confidence <= Decimal("1")
        ):
            raise ValueError("min_regime_confidence must be a Decimal in (0, 1].")
        if type(self.max_history_candles) is not int or not 2 <= self.max_history_candles <= 500:
            raise ValueError("max_history_candles must be an integer in [2, 500].")

    @property
    def feed_manifest_fingerprint(self) -> str:
        """Fingerprint the authorized feed contract, not pretend the stream is a static dataset."""
        return configuration_fingerprint(
            {
                "schema": "shadow-feed-manifest/v1",
                "source_id": self.source_id,
                "source_version": self.source_version,
                "market": {
                    "symbol": self.market.symbol,
                    "venue": self.market.venue,
                    "market_type": self.market.market_type,
                },
                "interval_seconds": int(self.interval.total_seconds()),
                "timestamp_semantics": "candle_open_time_utc",
            }
        )

    @property
    def projection_config_fingerprint(self) -> str:
        return configuration_fingerprint(
            {
                "schema": "shadow-evaluation/v1",
                "virtual_capital": str(self.virtual_capital),
                "allocation_fraction": str(self.allocation_fraction),
                "quantity_step": str(self.quantity_step),
                "max_candle_age_seconds": self.max_candle_age_seconds,
                "max_future_skew_seconds": self.max_future_skew_seconds,
                "min_regime_confidence": str(self.min_regime_confidence),
                "max_history_candles": self.max_history_candles,
                "fill_model": "none_shadow_only",
                "fee_model": "not_applied_to_signal_projection",
            }
        )


def strategy_fingerprint(strategy: CausalTargetStrategy) -> str:
    """Fingerprint the exact deterministic strategy identity and sorted string parameters."""
    strategy_id = getattr(strategy, "strategy_id", None)
    strategy_version = getattr(strategy, "strategy_version", None)
    if not isinstance(strategy_id, str) or not strategy_id.strip():
        raise ValueError("Shadow strategy must expose a non-empty strategy_id.")
    if not isinstance(strategy_version, str) or not strategy_version.strip():
        raise ValueError("Shadow strategy must expose a non-empty strategy_version.")
    parameters_method = getattr(strategy, "parameters", None)
    if not callable(parameters_method):
        raise ValueError("Shadow strategy must expose parameters().")
    parameters = parameters_method()
    if not isinstance(parameters, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in parameters.items()
    ):
        raise ValueError("Shadow strategy parameters must be string key/value pairs.")
    material = {
        "schema": "shadow-strategy-fingerprint/v1",
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "parameters": dict(sorted(parameters.items())),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ShadowRiskAssessmentStatus(StrEnum):
    """Observational risk check outcomes; none authorizes real or Paper execution."""

    OBSERVATIONAL_ACCEPTED = "observational_accepted"
    REJECT = "reject"
    HALT = "halt"
    ABSTAIN = "abstain"


@dataclass(frozen=True, slots=True)
class ShadowRiskAssessment:
    """Sanitized output from an injected, read-only virtual-risk assessment."""

    status: ShadowRiskAssessmentStatus
    reason_code: str
    explanation: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, ShadowRiskAssessmentStatus):
            raise ValueError("Risk assessment status must be a canonical value.")
        _validate_id(self.reason_code, "reason_code")
        if (
            not isinstance(self.explanation, str)
            or not self.explanation.strip()
            or len(self.explanation) > 256
            or any(ord(char) < 32 for char in self.explanation)
        ):
            raise ValueError(
                "Risk assessment explanation must be sanitized text of at most 256 chars."
            )


class ShadowRiskAssessmentPort(Protocol):
    """Assess a hypothetical target without placing orders or mutating live risk state.

    Implementations must be pure/read-only and must not call the exchange execution API.
    """

    def assess(
        self,
        *,
        market: Market,
        candle: MarketCandle,
        target_exposure: Decimal,
        target_quantity: Decimal,
        quantity_delta: Decimal,
        baseline_target_quantity: Decimal,
        virtual_capital: Decimal,
    ) -> ShadowRiskAssessment:
        """Return a sanitized observational pass/reject/halt/abstain result."""
        ...


class RegimeClassifierPort(Protocol):
    """Only the pre-existing advisory regime API is visible to Shadow."""

    def classify(
        self, completed_history: Sequence[MarketCandle], *, now: datetime
    ) -> RegimePrediction:
        """Return a classified or explicitly abstaining regime prediction."""
        ...


class CandleHistoryPort(Protocol):
    """Narrow read-only surface required by the REST poller; no exchange-write methods exist."""

    def get_market_candles(
        self,
        market: Market,
        *,
        interval: timedelta,
        start: datetime,
        end: datetime,
    ) -> Sequence[MarketCandle]:
        """Fetch normalized candles for a bounded UTC interval."""
        ...


class ReadOnlyCandleFeed(Protocol):
    """Feed contract for one bounded polling operation."""

    source_id: str
    source_version: str
    market: Market
    interval: timedelta

    def fetch_completed(self, *, now: datetime) -> Sequence[MarketCandle]:
        """Return only completed candles, or raise an exception for fail-closed handling."""
        ...


@dataclass(frozen=True, slots=True)
class ShadowDecisionSnapshot:
    """Immutable decision record; invariant execution_authorized is always False."""

    event_id: str
    correlation_id: str
    experiment_id: str
    run_id: str
    evaluation_config_fingerprint: str
    projection_config_fingerprint: str
    feed_manifest_fingerprint: str
    mode: ExperimentMode
    recorded_at: datetime
    observed_at: datetime
    received_at: datetime
    market_symbol: str
    venue: str
    market_type: str
    source_id: str
    source_version: str
    candle_interval_seconds: int
    strategy_id: str
    strategy_version: str
    strategy_fingerprint: str
    model_id: str
    model_version: str
    model_fingerprint: str
    feature_schema_version: str
    baseline_experiment_id: str
    baseline_model_id: str
    baseline_model_version: str
    baseline_model_fingerprint: str
    baseline_strategy_id: str
    baseline_strategy_version: str
    baseline_strategy_fingerprint: str
    baseline_target_exposure: Decimal | None
    baseline_target_quantity: Decimal | None
    baseline_quantity_delta: Decimal | None
    regime_label: str
    regime_confidence: Decimal | None
    regime_as_of: datetime | None
    regime_model_id: str | None
    regime_model_version: str | None
    regime_model_fingerprint: str | None
    strategy_confidence: Decimal | None
    reference_price: Decimal | None
    target_exposure: Decimal | None
    target_quantity: Decimal | None
    quantity_delta: Decimal | None
    estimated_notional_delta: Decimal | None
    outcome: ShadowOutcome
    reason_code: str
    reason: str
    risk_assessment_status: str
    risk_assessment_reason_code: str
    risk_assessment_reason: str
    auto_halt_status: str
    execution_authorized: bool = False

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "correlation_id",
            "experiment_id",
            "run_id",
            "evaluation_config_fingerprint",
            "projection_config_fingerprint",
            "feed_manifest_fingerprint",
            "market_symbol",
            "venue",
            "market_type",
            "source_id",
            "source_version",
            "strategy_id",
            "strategy_version",
            "strategy_fingerprint",
            "model_id",
            "model_version",
            "model_fingerprint",
            "feature_schema_version",
            "baseline_experiment_id",
            "baseline_model_id",
            "baseline_model_version",
            "baseline_model_fingerprint",
            "baseline_strategy_id",
            "baseline_strategy_version",
            "baseline_strategy_fingerprint",
            "regime_label",
            "reason_code",
            "reason",
            "risk_assessment_status",
            "risk_assessment_reason_code",
            "risk_assessment_reason",
            "auto_halt_status",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must be non-empty.")
        if self.mode is not ExperimentMode.SHADOW:
            raise ValueError("Shadow snapshots must be explicitly mode=SHADOW.")
        if self.execution_authorized is not False:
            raise ValueError("Shadow snapshots can never authorize execution.")
        for name in ("recorded_at", "observed_at", "received_at"):
            _require_utc(getattr(self, name), name)
        if self.regime_as_of is not None:
            _require_utc(self.regime_as_of, "regime_as_of")
        if type(self.candle_interval_seconds) is not int or self.candle_interval_seconds <= 0:
            raise ValueError("candle_interval_seconds must be positive.")
        for name in ("regime_confidence", "strategy_confidence"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value < 0
                or value > 1
            ):
                raise ValueError(f"{name} must be None or a finite Decimal in [0, 1].")
        if self.reference_price is not None and (
            not self.reference_price.is_finite() or self.reference_price <= 0
        ):
            raise ValueError("reference_price must be a finite positive Decimal.")
        if self.target_exposure is not None and (
            not self.target_exposure.is_finite()
            or self.target_exposure < 0
            or self.target_exposure > 1
        ):
            raise ValueError("target_exposure must be None or in [0, 1].")
        for name in (
            "target_quantity",
            "quantity_delta",
            "estimated_notional_delta",
            "baseline_target_quantity",
            "baseline_quantity_delta",
        ):
            value = getattr(self, name)
            if value is not None and not value.is_finite():
                raise ValueError(f"{name} must be None or finite.")
        if self.baseline_target_exposure is not None and (
            not self.baseline_target_exposure.is_finite()
            or self.baseline_target_exposure < 0
            or self.baseline_target_exposure > 1
        ):
            raise ValueError("baseline_target_exposure must be None or in [0, 1].")

    def to_payload(self) -> dict[str, Any]:
        """Serialize without any URL, credential, request body, or order-routing material."""
        return {
            "schema": "shadow-decision/v1",
            "event_id": self.event_id,
            "correlation_id": self.correlation_id,
            "experiment_id": self.experiment_id,
            "run_id": self.run_id,
            "evaluation_config_fingerprint": self.evaluation_config_fingerprint,
            "projection_config_fingerprint": self.projection_config_fingerprint,
            "feed_manifest_fingerprint": self.feed_manifest_fingerprint,
            "mode": self.mode.value,
            "recorded_at": _iso(self.recorded_at),
            "observed_at": _iso(self.observed_at),
            "received_at": _iso(self.received_at),
            "market": {
                "symbol": self.market_symbol,
                "venue": self.venue,
                "market_type": self.market_type,
            },
            "feed": {
                "source_id": self.source_id,
                "source_version": self.source_version,
                "candle_interval_seconds": self.candle_interval_seconds,
            },
            "strategy": {
                "id": self.strategy_id,
                "version": self.strategy_version,
                "fingerprint": self.strategy_fingerprint,
                "confidence": _decimal_or_none(self.strategy_confidence),
            },
            "model": {
                "id": self.model_id,
                "version": self.model_version,
                "fingerprint": self.model_fingerprint,
                "feature_schema_version": self.feature_schema_version,
            },
            "paper_baseline": {
                "experiment_id": self.baseline_experiment_id,
                "model_id": self.baseline_model_id,
                "model_version": self.baseline_model_version,
                "model_fingerprint": self.baseline_model_fingerprint,
                "strategy_id": self.baseline_strategy_id,
                "strategy_version": self.baseline_strategy_version,
                "strategy_fingerprint": self.baseline_strategy_fingerprint,
                "target_exposure": _decimal_or_none(self.baseline_target_exposure),
                "target_quantity": _decimal_or_none(self.baseline_target_quantity),
                "quantity_delta": _decimal_or_none(self.baseline_quantity_delta),
                "comparison_status": (
                    "COMPUTED"
                    if self.baseline_target_exposure is not None
                    else "NOT_EVALUATED"
                ),
            },
            "regime": {
                "label": self.regime_label,
                "confidence": _decimal_or_none(self.regime_confidence),
                "as_of": _iso_or_none(self.regime_as_of),
                "model_id": self.regime_model_id,
                "model_version": self.regime_model_version,
                "model_fingerprint": self.regime_model_fingerprint,
            },
            "reference_price": _decimal_or_none(self.reference_price),
            "target_exposure": _decimal_or_none(self.target_exposure),
            "target_quantity": _decimal_or_none(self.target_quantity),
            "quantity_delta": _decimal_or_none(self.quantity_delta),
            "estimated_notional_delta": _decimal_or_none(self.estimated_notional_delta),
            "outcome": self.outcome.value,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "risk_assessment": {
                "status": self.risk_assessment_status,
                "reason_code": self.risk_assessment_reason_code,
                "reason": self.risk_assessment_reason,
            },
            "auto_halt_status": self.auto_halt_status,
            "execution_authorized": False,
        }


@dataclass(frozen=True, slots=True)
class ShadowPollResult:
    status: ShadowPollStatus
    snapshots: tuple[ShadowDecisionSnapshot, ...]
    reason_code: str


class NobitexRestCandleFeed:
    """Poll bounded candles through the existing allowlisted, read-only Nobitex adapter."""

    def __init__(
        self,
        reader: CandleHistoryPort,
        *,
        market: Market,
        interval: timedelta,
        lookback_candles: int = 100,
        source_version: str = "nobitex-rest-ohlcv/v1",
    ) -> None:
        if market.market_type.strip().lower() != "spot":
            raise ValueError("Phase 19 currently accepts only the reviewed Spot candle feed.")
        if interval <= timedelta(0):
            raise ValueError("interval must be positive.")
        if type(lookback_candles) is not int or not 2 <= lookback_candles <= 500:
            raise ValueError("lookback_candles must be in [2, 500].")
        if not source_version.strip():
            raise ValueError("source_version must be non-empty.")
        self._reader = reader
        self.market = market
        self.interval = interval
        self.lookback_candles = lookback_candles
        self.source_id = "nobitex"
        self.source_version = source_version

    def fetch_completed(self, *, now: datetime) -> Sequence[MarketCandle]:
        now_utc = _require_utc(now, "now")
        start = now_utc - self.interval * self.lookback_candles
        # The provider may return the currently forming candle; filter it out below.
        rows = self._reader.get_market_candles(
            self.market,
            interval=self.interval,
            start=start,
            end=now_utc,
        )
        completed: list[MarketCandle] = []
        for candle in rows:
            if candle.market != self.market or candle.interval != self.interval:
                raise ValueError(
                    "Nobitex returned a candle with unexpected market identity/interval."
                )
            if candle.timestamp.tzinfo is None or candle.timestamp.utcoffset() != timedelta(0):
                raise ValueError("Nobitex returned a candle with a non-canonical timestamp.")
            if candle.source != self.source_id:
                raise ValueError("Nobitex returned a candle with unexpected source identity.")
            if candle.timestamp + candle.interval <= now_utc:
                completed.append(candle)
        ordered = tuple(sorted(completed, key=lambda item: item.timestamp))
        if len(ordered) > self.lookback_candles:
            raise ValueError("Nobitex returned more candles than the configured lookback bound.")
        return ordered


class ShadowRunner:
    """Deterministic target-exposure observer that persists before mutating its local state."""

    def __init__(
        self,
        *,
        config: ShadowConfig,
        experiment_spec: ExperimentSpec,
        baseline_experiment_spec: ExperimentSpec,
        registry: ExperimentRegistry,
        strategy: CausalTargetStrategy,
        baseline_strategy: CausalTargetStrategy,
        champion_controller: ChampionChallengerController,
        regime_classifier: RegimeClassifierPort,
        risk_assessor: ShadowRiskAssessmentPort,
        auto_halt_status: Callable[[], object],
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if experiment_spec.mode is not ExperimentMode.SHADOW:
            raise ShadowSafetyError(
                "Experiment spec mode must be SHADOW; PAPER/LIVE are forbidden."
            )
        if registry.get(experiment_spec.experiment_id) != experiment_spec:
            raise ShadowSafetyError(
                "Shadow experiment is not identically registered in the ledger."
            )
        if not registry.ledger.is_persistent:
            raise ShadowSafetyError(
                "Shadow requires a path-backed, fsync-persistent Learning Lab ledger."
            )
        if not registry.ledger.verify():
            raise ShadowSafetyError("Learning Lab ledger verification failed.")
        actual_fingerprint = strategy_fingerprint(strategy)
        strategy_parameters = tuple(sorted(strategy.parameters().items()))
        if (
            experiment_spec.model_id != strategy.strategy_id
            or experiment_spec.model_version != strategy.strategy_version
            or experiment_spec.parameters != strategy_parameters
        ):
            raise ShadowSafetyError(
                "Strategy identity/parameters do not match the registered experiment."
            )
        if baseline_experiment_spec.mode is not ExperimentMode.PAPER:
            raise ShadowSafetyError("The baseline experiment must be mode=PAPER.")
        if (
            champion_controller.registry is not registry
            or champion_controller.ledger is not registry.ledger
        ):
            raise ShadowSafetyError(
                "Paper Champion controller must use this exact registry and persistent ledger."
            )
        if champion_controller.active_champion_id != baseline_experiment_spec.experiment_id:
            raise ShadowSafetyError(
                "The selected Paper baseline is not the current active Paper Champion."
            )
        if registry.get(baseline_experiment_spec.experiment_id) != baseline_experiment_spec:
            raise ShadowSafetyError("Paper baseline experiment is not identically registered.")
        if (
            baseline_experiment_spec.feature_schema_version
            != experiment_spec.feature_schema_version
        ):
            raise ShadowSafetyError("Paper baseline and Shadow feature schemas differ.")
        if (
            baseline_experiment_spec.evaluation_config_fingerprint
            != experiment_spec.evaluation_config_fingerprint
        ):
            raise ShadowSafetyError(
                "Paper baseline and Shadow experiment evaluation fingerprints differ."
            )
        if (
            baseline_experiment_spec.dataset_version != experiment_spec.dataset_version
            or baseline_experiment_spec.random_seed != experiment_spec.random_seed
            or baseline_experiment_spec.train_range != experiment_spec.train_range
            or baseline_experiment_spec.validation_range != experiment_spec.validation_range
            or baseline_experiment_spec.test_range != experiment_spec.test_range
        ):
            raise ShadowSafetyError(
                "Paper baseline and Shadow experiment data/split identity differs."
            )
        baseline_fingerprint = strategy_fingerprint(baseline_strategy)
        baseline_parameters = tuple(sorted(baseline_strategy.parameters().items()))
        if (
            baseline_experiment_spec.model_id != baseline_strategy.strategy_id
            or baseline_experiment_spec.model_version != baseline_strategy.strategy_version
            or baseline_experiment_spec.parameters != baseline_parameters
        ):
            raise ShadowSafetyError(
                "Paper baseline strategy does not match its registered experiment."
            )
        if not callable(getattr(risk_assessor, "assess", None)):
            raise ValueError("A read-only observational risk assessor is required.")
        if not callable(auto_halt_status):
            raise ValueError("A read-only Auto-Halt status reader is required.")
        self._config = config
        self._spec = experiment_spec
        self._baseline_spec = baseline_experiment_spec
        self._champion_controller = champion_controller
        self._registry = registry
        self._ledger: LearningLabLedger = registry.ledger
        self._strategy = strategy
        self._baseline_strategy = baseline_strategy
        self._strategy_fingerprint = actual_fingerprint
        self._baseline_strategy_fingerprint = baseline_fingerprint
        self._regime_classifier = regime_classifier
        self._risk_assessor = risk_assessor
        self._auto_halt_status = auto_halt_status
        self._utcnow = utcnow
        self._history: deque[MarketCandle] = deque(maxlen=config.max_history_candles)
        self._last_candle_at: datetime | None = None
        self._target_quantity = Decimal("0")
        self._baseline_target_quantity = Decimal("0")
        self._regime_identity: tuple[str, str, str, str] | None = None
        self._continuity_blocked = False
        self._identity_blocked = False
        self._run_id = uuid.uuid4().hex
        self._append_event(
            LearningEventType.SHADOW_RUN_STARTED,
            correlation_id=f"shadow-run:{self._run_id}",
            occurred_at=_require_utc(self._utcnow(), "utcnow"),
            payload={
                "schema": "shadow-run/v1",
                "run_id": self._run_id,
                "mode": ExperimentMode.SHADOW.value,
                "experiment_id": self._spec.experiment_id,
                "experiment_fingerprint": self._spec.fingerprint,
                "model_id": self._spec.model_id,
                "model_version": self._spec.model_version,
                "model_fingerprint": self._spec.model_fingerprint,
                "feature_schema_version": self._spec.feature_schema_version,
                "strategy_fingerprint": self._strategy_fingerprint,
                "registered_dataset_version": self._spec.dataset_version,
                "paper_baseline": {
                    "experiment_id": self._baseline_spec.experiment_id,
                    "model_id": self._baseline_spec.model_id,
                    "model_version": self._baseline_spec.model_version,
                    "model_fingerprint": self._baseline_spec.model_fingerprint,
                    "strategy_fingerprint": self._baseline_strategy_fingerprint,
                },
                "feed_manifest_fingerprint": self._config.feed_manifest_fingerprint,
                "experiment_evaluation_config_fingerprint": (
                    self._spec.evaluation_config_fingerprint
                ),
                "projection_config_fingerprint": self._config.projection_config_fingerprint,
                "execution_authorized": False,
            },
        )

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def last_candle_at(self) -> datetime | None:
        return self._last_candle_at

    @property
    def history(self) -> tuple[MarketCandle, ...]:
        return tuple(self._history)

    @property
    def config(self) -> ShadowConfig:
        return self._config

    @property
    def continuity_blocked(self) -> bool:
        return self._continuity_blocked

    def bootstrap_history(
        self,
        candles: Sequence[MarketCandle],
        *,
        received_at: datetime,
    ) -> None:
        """Warm up indicators from one complete feed window before recording live decisions."""
        received = _require_utc(received_at, "received_at")
        rows = tuple(candles)
        if self._history or self._last_candle_at is not None:
            raise ShadowSafetyError("Initial history bootstrap is allowed only once per fresh run.")
        if self._continuity_blocked or self._identity_blocked:
            raise ShadowSafetyError("A blocked Shadow run requires explicit recovery/replacement.")
        if not 2 <= len(rows) <= self._config.max_history_candles:
            raise ShadowSafetyError(
                "Initial feed window must contain 2..max_history_candles candles."
            )
        previous: MarketCandle | None = None
        for candle in rows:
            # Older warm-up candles are expected to be old; validate shape at their own close time.
            historical_close = candle.timestamp + candle.interval
            invalid = self._validate_candle(
                candle, historical_close, compare_sequence=False
            )
            if invalid is not None:
                raise ShadowSafetyError(f"Initial feed window rejected: {invalid}.")
            if (
                previous is not None
                and candle.timestamp - previous.timestamp != self._config.interval
            ):
                raise ShadowSafetyError("Initial feed window contains a gap or duplicate.")
            previous = candle
        last = rows[-1]
        final_age = (received - (last.timestamp + last.interval)).total_seconds()
        if final_age < -self._config.max_future_skew_seconds:
            raise ShadowSafetyError("Initial feed window ends in the future.")
        if final_age > self._config.max_candle_age_seconds:
            raise ShadowSafetyError("Initial feed window ends with a stale candle.")
        warmup = rows[:-1]
        event_time = received
        self._append_event(
            LearningEventType.SHADOW_RECONCILIATION,
            correlation_id=f"shadow-bootstrap:{self._run_id}",
            occurred_at=event_time,
            payload={
                "schema": "shadow-initial-bootstrap/v1",
                "run_id": self._run_id,
                "experiment_id": self._spec.experiment_id,
                "mode": ExperimentMode.SHADOW.value,
                "source_id": self._config.source_id,
                "source_version": self._config.source_version,
                "market_symbol": self._config.market.symbol,
                "interval_seconds": int(self._config.interval.total_seconds()),
                "warmup_candle_count": len(warmup),
                "first_candle_at": _iso(warmup[0].timestamp),
                "last_warmup_candle_at": _iso(warmup[-1].timestamp),
                "next_decision_candle_at": _iso(last.timestamp),
                "bootstrap_kind": "initial_complete_read_only_feed_window",
                "execution_authorized": False,
            },
        )
        self._history.extend(warmup[-self._config.max_history_candles :])
        self._last_candle_at = warmup[-1].timestamp

    def process_candle(
        self,
        candle: object,
        *,
        received_at: datetime,
        correlation_id: str | None = None,
    ) -> ShadowDecisionSnapshot:
        """Evaluate one completed candle. Invalid, repeated, late, or uncertain input abstains."""
        received = _require_utc(received_at, "received_at")
        cid = _safe_correlation(correlation_id)
        if not isinstance(candle, MarketCandle):
            self._append_event(
                LearningEventType.SHADOW_INPUT_REJECTED,
                correlation_id=cid,
                occurred_at=received,
                payload={
                    "schema": "shadow-input-rejection/v1",
                    "run_id": self._run_id,
                    "experiment_id": self._spec.experiment_id,
                    "mode": ExperimentMode.SHADOW.value,
                    "reason_code": "INVALID_CANDLE_TYPE",
                    "action": "ABSTAIN",
                    "execution_authorized": False,
                },
            )
            raise ShadowSafetyError("Shadow input was not a canonical MarketCandle.")
        base_input_reason = self._validate_candle(
            candle, received, compare_sequence=False
        )
        reason = base_input_reason
        if (
            reason is None
            and not (self._continuity_blocked or self._identity_blocked)
        ):
            reason = self._validate_candle(candle, received, compare_sequence=True)
        if reason is None:
            reason = self._strategy_identity_failure()
        if (
            reason is None
            and self._champion_controller.active_champion_id
            != self._baseline_spec.experiment_id
        ):
            reason = "PAPER_CHAMPION_CHANGED"
        if reason is None and self._continuity_blocked:
            reason = "CONTINUITY_RECONCILIATION_REQUIRED"
        if reason is None and self._identity_blocked:
            reason = "MODEL_IDENTITY_REVIEW_REQUIRED"

        candidate_history = tuple(self._history) + (candle,)
        regime: RegimePrediction | None = None
        halt_status = "NOT_CHECKED"
        if reason is None:
            try:
                halted = self._auto_halt_status()
            except Exception:
                halt_status = "UNAVAILABLE"
                reason = "AUTO_HALT_STATUS_UNAVAILABLE"
            else:
                if type(halted) is not bool:
                    halt_status = "UNKNOWN"
                    reason = "AUTO_HALT_STATUS_INVALID"
                elif halted:
                    halt_status = "ACTIVE"
                    reason = "AUTO_HALT_ACTIVE"
                else:
                    halt_status = "INACTIVE"

        if reason is None:
            try:
                regime = self._regime_classifier.classify(candidate_history, now=received)
            except Exception:
                reason = "REGIME_CLASSIFIER_ERROR"
            else:
                reason = self._validate_regime(regime, received)

        proposed_exposure: Decimal | None = None
        proposed_quantity: Decimal | None = None
        quantity_delta: Decimal | None = None
        notional_delta: Decimal | None = None
        baseline_exposure: Decimal | None = None
        baseline_quantity: Decimal | None = None
        baseline_quantity_delta: Decimal | None = None
        risk_status = "NOT_EVALUATED"
        risk_reason_code = "PRIOR_GATE_BLOCKED"
        risk_reason = "Risk assessment not run because a prior input gate abstained."
        outcome = ShadowOutcome.ABSTAIN
        reason_code = reason or "UNSET"
        explanation = _reason_explanation(reason_code)

        if reason is None:
            try:
                shadow_target_exposure = self._strategy.target_exposure(candidate_history)
                paper_target_exposure = self._baseline_strategy.target_exposure(candidate_history)
                if (
                    not isinstance(shadow_target_exposure, Decimal)
                    or not shadow_target_exposure.is_finite()
                    or shadow_target_exposure < 0
                    or shadow_target_exposure > 1
                ):
                    raise ValueError("Shadow strategy target exposure is outside [0, 1].")
                if (
                    not isinstance(paper_target_exposure, Decimal)
                    or not paper_target_exposure.is_finite()
                    or paper_target_exposure < 0
                    or paper_target_exposure > 1
                ):
                    raise ValueError("Paper baseline target exposure is outside [0, 1].")
                shadow_target_quantity = _floor_step(
                    (
                        self._config.virtual_capital
                        * self._config.allocation_fraction
                        * shadow_target_exposure
                    )
                    / candle.close,
                    self._config.quantity_step,
                )
                paper_target_quantity = _floor_step(
                    (
                        self._config.virtual_capital
                        * self._config.allocation_fraction
                        * paper_target_exposure
                    )
                    / candle.close,
                    self._config.quantity_step,
                )
                proposed_exposure = shadow_target_exposure
                baseline_exposure = paper_target_exposure
                proposed_quantity = shadow_target_quantity
                baseline_quantity = paper_target_quantity
            except Exception:
                proposed_exposure = None
                proposed_quantity = None
                baseline_exposure = None
                baseline_quantity = None
                reason_code = "STRATEGY_OR_BASELINE_OUTPUT_INVALID"
                explanation = (
                    "Shadow strategy or registered Paper baseline raised an exception or "
                    "returned an invalid target exposure; the complete comparison abstains."
                )
                outcome = ShadowOutcome.ABSTAIN
            else:
                quantity_delta = shadow_target_quantity - self._target_quantity
                baseline_quantity_delta = (
                    paper_target_quantity - self._baseline_target_quantity
                )
                notional_delta = quantity_delta * candle.close
                try:
                    risk_result = self._risk_assessor.assess(
                        market=self._config.market,
                        candle=candle,
                        target_exposure=shadow_target_exposure,
                        target_quantity=shadow_target_quantity,
                        quantity_delta=quantity_delta,
                        baseline_target_quantity=paper_target_quantity,
                        virtual_capital=self._config.virtual_capital,
                    )
                    if not isinstance(risk_result, ShadowRiskAssessment):
                        raise ValueError("Risk assessor returned an invalid result type.")
                except Exception:
                    risk_status = "UNAVAILABLE"
                    risk_reason_code = "RISK_ASSESSMENT_UNAVAILABLE"
                    risk_reason = "Read-only risk assessment failed or returned an invalid result."
                    reason_code = "RISK_ASSESSMENT_UNAVAILABLE"
                    explanation = risk_reason
                    outcome = ShadowOutcome.ABSTAIN
                else:
                    risk_status = risk_result.status.value
                    risk_reason_code = risk_result.reason_code
                    risk_reason = risk_result.explanation
                    if risk_result.status is ShadowRiskAssessmentStatus.OBSERVATIONAL_ACCEPTED:
                        outcome = _outcome_for(self._target_quantity, shadow_target_quantity)
                        reason_code = "SHADOW_TARGET_COMPUTED"
                        explanation = (
                            "The candidate and registered Paper baseline were evaluated on the "
                            "same completed history and the read-only Shadow risk assessor passed. "
                            "No order, fill or execution authorization was created."
                        )
                    elif risk_result.status is ShadowRiskAssessmentStatus.REJECT:
                        outcome = ShadowOutcome.REJECT
                        reason_code = "RISK_GATE_REJECTED"
                        explanation = risk_result.explanation
                    elif risk_result.status is ShadowRiskAssessmentStatus.HALT:
                        outcome = ShadowOutcome.REJECT
                        reason_code = "RISK_GATE_HALTED"
                        explanation = risk_result.explanation
                    else:
                        outcome = ShadowOutcome.ABSTAIN
                        reason_code = "RISK_GATE_ABSTAINED"
                        explanation = risk_result.explanation

        snapshot = self._build_snapshot(
            candle=candle,
            received_at=received,
            correlation_id=cid,
            regime=regime,
            outcome=outcome,
            reason_code=reason_code,
            reason=explanation,
            reference_price=candle.close if base_input_reason is None else None,
            target_exposure=proposed_exposure,
            target_quantity=proposed_quantity,
            quantity_delta=quantity_delta,
            estimated_notional_delta=notional_delta,
            auto_halt_status=halt_status,
            baseline_experiment_id=self._baseline_spec.experiment_id,
            baseline_model_id=self._baseline_spec.model_id,
            baseline_model_version=self._baseline_spec.model_version,
            baseline_model_fingerprint=self._baseline_spec.model_fingerprint,
            baseline_strategy_id=self._baseline_strategy.strategy_id,
            baseline_strategy_version=self._baseline_strategy.strategy_version,
            baseline_strategy_fingerprint=self._baseline_strategy_fingerprint,
            baseline_target_exposure=baseline_exposure,
            baseline_target_quantity=baseline_quantity,
            baseline_quantity_delta=baseline_quantity_delta,
            risk_assessment_status=risk_status,
            risk_assessment_reason_code=risk_reason_code,
            risk_assessment_reason=risk_reason,
        )
        if outcome is ShadowOutcome.REJECT:
            event_type = LearningEventType.SHADOW_RISK_REJECTED
        elif outcome is ShadowOutcome.ABSTAIN:
            event_type = LearningEventType.SHADOW_INPUT_REJECTED
        else:
            event_type = LearningEventType.SHADOW_DECISION_RECORDED
        self._append_event(
            event_type,
            correlation_id=cid,
            occurred_at=received,
            payload=snapshot.to_payload(),
        )

        # No state mutation occurs until the append-only Learning Lab ledger has accepted the event.
        if outcome not in {ShadowOutcome.ABSTAIN, ShadowOutcome.REJECT}:
            self._history.append(candle)
            self._last_candle_at = candle.timestamp
            self._target_quantity = proposed_quantity or Decimal("0")
            self._baseline_target_quantity = baseline_quantity or Decimal("0")
            if regime is not None:
                self._remember_regime_identity(regime)
        elif reason_code == "CANDLE_GAP":
            self._continuity_blocked = True
        elif reason_code in {
            "REGIME_MODEL_IDENTITY_CHANGED",
            "SHADOW_STRATEGY_IDENTITY_CHANGED",
            "PAPER_BASELINE_IDENTITY_CHANGED",
            "PAPER_CHAMPION_CHANGED",
            "STRATEGY_IDENTITY_UNVERIFIABLE",
        }:
            self._identity_blocked = True
        return snapshot

    def record_feed_failure(
        self,
        *,
        occurred_at: datetime,
        error_kind: str,
        correlation_id: str | None = None,
    ) -> None:
        """Record a sanitized feed fault; never store exception text, URL, body, or headers."""
        when = _require_utc(occurred_at, "occurred_at")
        safe_kind = "".join(char for char in error_kind if char.isalnum() or char in "_.-")[:80]
        if not safe_kind:
            safe_kind = "UnknownError"
        self._append_event(
            LearningEventType.SHADOW_FEED_FAILURE,
            correlation_id=_safe_correlation(correlation_id),
            occurred_at=when,
            payload={
                "schema": "shadow-feed-failure/v1",
                "run_id": self._run_id,
                "experiment_id": self._spec.experiment_id,
                "mode": ExperimentMode.SHADOW.value,
                "market_symbol": self._config.market.symbol,
                "source_id": self._config.source_id,
                "error_kind": safe_kind,
                "action": "ABSTAIN",
                "execution_authorized": False,
            },
        )

    def reset_after_reconciliation(
        self,
        candles: Sequence[MarketCandle],
        *,
        evidence_ref: str,
        received_at: datetime,
    ) -> None:
        """Replace local history only after a caller supplies contiguous and fresh data."""
        ref = _validate_id(evidence_ref, "evidence_ref")
        received = _require_utc(received_at, "received_at")
        rows = tuple(candles)
        if len(rows) < 2 or len(rows) > self._config.max_history_candles:
            raise ValueError("Reconciliation history length is outside configured bounds.")
        previous: MarketCandle | None = None
        for candle in rows:
            # Historic candles are checked at their close time; only the newest must be fresh now.
            reason = self._validate_candle(
                candle, candle.timestamp + candle.interval, compare_sequence=False
            )
            if reason is not None:
                raise ShadowSafetyError(f"Reconciliation data rejected: {reason}.")
            if (
                previous is not None
                and candle.timestamp - previous.timestamp != self._config.interval
            ):
                raise ShadowSafetyError("Reconciliation history has a gap or duplicate timestamp.")
            previous = candle
        latest_age = (received - (rows[-1].timestamp + rows[-1].interval)).total_seconds()
        if latest_age < -self._config.max_future_skew_seconds:
            raise ShadowSafetyError("Reconciliation window ends in the future.")
        if latest_age > self._config.max_candle_age_seconds:
            raise ShadowSafetyError("Reconciliation window ends with a stale candle.")
        if self._identity_blocked:
            raise ShadowSafetyError(
                "Regime model identity changed; create/register a new experiment and runner."
            )
        try:
            halted = self._auto_halt_status()
            regime = self._regime_classifier.classify(rows, now=received)
            exposure = self._strategy.target_exposure(rows)
            baseline_exposure = self._baseline_strategy.target_exposure(rows)
        except Exception as exc:
            raise ShadowSafetyError("Reconciliation prerequisites could not be evaluated.") from exc
        if type(halted) is not bool or halted:
            raise ShadowSafetyError("Auto-Halt is active or its status is invalid.")
        reason = self._validate_regime(regime, received)
        if reason is not None:
            raise ShadowSafetyError(f"Reconciliation regime rejected: {reason}.")
        if (
            not isinstance(exposure, Decimal)
            or not exposure.is_finite()
            or exposure < 0
            or exposure > 1
        ):
            raise ShadowSafetyError("Reconciliation strategy output is invalid.")
        if (
            not isinstance(baseline_exposure, Decimal)
            or not baseline_exposure.is_finite()
            or baseline_exposure < 0
            or baseline_exposure > 1
        ):
            raise ShadowSafetyError("Reconciliation Paper baseline output is invalid.")
        last = rows[-1]
        target_quantity = _floor_step(
            (
                self._config.virtual_capital
                * self._config.allocation_fraction
                * exposure
            )
            / last.close,
            self._config.quantity_step,
        )
        baseline_target_quantity = _floor_step(
            (
                self._config.virtual_capital
                * self._config.allocation_fraction
                * baseline_exposure
            )
            / last.close,
            self._config.quantity_step,
        )
        current_delta = target_quantity - self._target_quantity
        try:
            risk_result = self._risk_assessor.assess(
                market=self._config.market,
                candle=last,
                target_exposure=exposure,
                target_quantity=target_quantity,
                quantity_delta=current_delta,
                baseline_target_quantity=baseline_target_quantity,
                virtual_capital=self._config.virtual_capital,
            )
        except Exception as exc:
            raise ShadowSafetyError(
                "Risk assessment failed during feed reconciliation; state was not reset."
            ) from exc
        if (
            not isinstance(risk_result, ShadowRiskAssessment)
            or risk_result.status is not ShadowRiskAssessmentStatus.OBSERVATIONAL_ACCEPTED
        ):
            raise ShadowSafetyError(
                "Risk assessment did not pass for reconciled Shadow target; state was not reset."
            )
        self._append_event(
            LearningEventType.SHADOW_RECONCILIATION,
            correlation_id=f"shadow-reconcile:{self._run_id}",
            occurred_at=received,
            payload={
                "schema": "shadow-reconciliation/v1",
                "run_id": self._run_id,
                "experiment_id": self._spec.experiment_id,
                "mode": ExperimentMode.SHADOW.value,
                "evidence_ref": ref,
                "candle_count": len(rows),
                "first_candle_at": _iso(rows[0].timestamp),
                "last_candle_at": _iso(last.timestamp),
                "target_exposure": str(exposure),
                "target_quantity": str(target_quantity),
                "risk_assessment_status": risk_result.status.value,
                "risk_assessment_reason_code": risk_result.reason_code,
                "risk_assessment_reason": risk_result.explanation,
                "paper_baseline": {
                    "experiment_id": self._baseline_spec.experiment_id,
                    "target_exposure": str(baseline_exposure),
                    "target_quantity": str(baseline_target_quantity),
                },
                "regime_model_fingerprint": regime.model_fingerprint,
                "execution_authorized": False,
            },
        )
        self._history.clear()
        self._history.extend(rows[-self._config.max_history_candles :])
        self._last_candle_at = last.timestamp
        self._target_quantity = target_quantity
        self._baseline_target_quantity = baseline_target_quantity
        self._continuity_blocked = False
        self._remember_regime_identity(regime)

    def _strategy_identity_failure(self) -> str | None:
        """Reject mutable strategy/parameter identity changes during an active experiment."""
        try:
            current_candidate_fingerprint = strategy_fingerprint(self._strategy)
            current_baseline_fingerprint = strategy_fingerprint(self._baseline_strategy)
            candidate_parameters = tuple(sorted(self._strategy.parameters().items()))
            baseline_parameters = tuple(sorted(self._baseline_strategy.parameters().items()))
        except Exception:
            return "STRATEGY_IDENTITY_UNVERIFIABLE"
        if (
            current_candidate_fingerprint != self._strategy_fingerprint
            or self._strategy.strategy_id != self._spec.model_id
            or self._strategy.strategy_version != self._spec.model_version
            or candidate_parameters != self._spec.parameters
        ):
            return "SHADOW_STRATEGY_IDENTITY_CHANGED"
        if (
            current_baseline_fingerprint != self._baseline_strategy_fingerprint
            or self._baseline_strategy.strategy_id != self._baseline_spec.model_id
            or self._baseline_strategy.strategy_version != self._baseline_spec.model_version
            or baseline_parameters != self._baseline_spec.parameters
        ):
            return "PAPER_BASELINE_IDENTITY_CHANGED"
        return None

    def _validate_candle(
        self,
        candle: object,
        received_at: datetime,
        *,
        compare_sequence: bool = True,
    ) -> str | None:
        if not isinstance(candle, MarketCandle):
            return "INVALID_CANDLE_TYPE"
        if candle.market != self._config.market:
            return "MARKET_IDENTITY_MISMATCH"
        if candle.interval != self._config.interval:
            return "CANDLE_INTERVAL_MISMATCH"
        if candle.source != self._config.source_id:
            return "FEED_SOURCE_MISMATCH"
        try:
            opened = _require_utc(candle.timestamp, "candle.timestamp")
        except ValueError:
            return "INVALID_CANDLE_TIMESTAMP"
        closed_at = opened + candle.interval
        age = (received_at - closed_at).total_seconds()
        if age < -self._config.max_future_skew_seconds:
            return "FUTURE_CANDLE"
        if age > self._config.max_candle_age_seconds:
            return "STALE_CANDLE"
        if compare_sequence and self._last_candle_at is not None:
            delta = opened - self._last_candle_at
            if delta == timedelta(0):
                return "DUPLICATE_CANDLE"
            if delta < timedelta(0):
                return "OUT_OF_ORDER_CANDLE"
            if delta != self._config.interval:
                return "CANDLE_GAP"
        return None

    def _validate_regime(
        self, prediction: object, received_at: datetime
    ) -> str | None:
        if not isinstance(prediction, RegimePrediction):
            return "REGIME_OUTPUT_INVALID"
        try:
            as_of = _require_utc(prediction.as_of, "regime.as_of")
        except ValueError:
            return "REGIME_TIMESTAMP_INVALID"
        age = (received_at - as_of).total_seconds()
        if age < -self._config.max_future_skew_seconds:
            return "REGIME_FROM_FUTURE"
        if age > self._config.max_candle_age_seconds:
            return "REGIME_STALE"
        identity = (
            prediction.model_id,
            prediction.model_version,
            prediction.model_fingerprint,
            prediction.feature_schema_version,
        )
        if self._regime_identity is not None and identity != self._regime_identity:
            return "REGIME_MODEL_IDENTITY_CHANGED"
        if prediction.abstained or prediction.label is RegimeLabel.UNKNOWN:
            return f"REGIME_ABSTAINED_{prediction.reason_code}"
        if prediction.confidence < self._config.min_regime_confidence:
            return "REGIME_CONFIDENCE_BELOW_THRESHOLD"
        return None

    def _remember_regime_identity(self, prediction: RegimePrediction) -> None:
        self._regime_identity = (
            prediction.model_id,
            prediction.model_version,
            prediction.model_fingerprint,
            prediction.feature_schema_version,
        )

    def _build_snapshot(
        self,
        *,
        candle: MarketCandle,
        received_at: datetime,
        correlation_id: str,
        regime: RegimePrediction | None,
        outcome: ShadowOutcome,
        reason_code: str,
        reason: str,
        reference_price: Decimal | None,
        target_exposure: Decimal | None,
        target_quantity: Decimal | None,
        quantity_delta: Decimal | None,
        estimated_notional_delta: Decimal | None,
        auto_halt_status: str,
        baseline_experiment_id: str,
        baseline_model_id: str,
        baseline_model_version: str,
        baseline_model_fingerprint: str,
        baseline_strategy_id: str,
        baseline_strategy_version: str,
        baseline_strategy_fingerprint: str,
        baseline_target_exposure: Decimal | None,
        baseline_target_quantity: Decimal | None,
        baseline_quantity_delta: Decimal | None,
        risk_assessment_status: str,
        risk_assessment_reason_code: str,
        risk_assessment_reason: str,
    ) -> ShadowDecisionSnapshot:
        return ShadowDecisionSnapshot(
            event_id=uuid.uuid4().hex,
            correlation_id=correlation_id,
            experiment_id=self._spec.experiment_id,
            run_id=self._run_id,
            evaluation_config_fingerprint=self._spec.evaluation_config_fingerprint,
            projection_config_fingerprint=self._config.projection_config_fingerprint,
            feed_manifest_fingerprint=self._config.feed_manifest_fingerprint,
            mode=ExperimentMode.SHADOW,
            recorded_at=_require_utc(self._utcnow(), "utcnow"),
            observed_at=_safe_audit_timestamp(candle.timestamp + candle.interval, received_at),
            received_at=received_at,
            market_symbol=candle.market.symbol,
            venue=candle.market.venue,
            market_type=candle.market.market_type,
            source_id=candle.source,
            source_version=self._config.source_version,
            candle_interval_seconds=int(candle.interval.total_seconds()),
            strategy_id=self._strategy.strategy_id,
            strategy_version=self._strategy.strategy_version,
            strategy_fingerprint=self._strategy_fingerprint,
            model_id=self._spec.model_id,
            model_version=self._spec.model_version,
            model_fingerprint=self._spec.model_fingerprint,
            feature_schema_version=self._spec.feature_schema_version,
            baseline_experiment_id=baseline_experiment_id,
            baseline_model_id=baseline_model_id,
            baseline_model_version=baseline_model_version,
            baseline_model_fingerprint=baseline_model_fingerprint,
            baseline_strategy_id=baseline_strategy_id,
            baseline_strategy_version=baseline_strategy_version,
            baseline_strategy_fingerprint=baseline_strategy_fingerprint,
            baseline_target_exposure=baseline_target_exposure,
            baseline_target_quantity=baseline_target_quantity,
            baseline_quantity_delta=baseline_quantity_delta,
            regime_label=regime.label.value if regime is not None else RegimeLabel.UNKNOWN.value,
            regime_confidence=regime.confidence if regime is not None else None,
            regime_as_of=(
                _safe_audit_timestamp(regime.as_of, received_at)
                if regime is not None
                else None
            ),
            regime_model_id=regime.model_id if regime is not None else None,
            regime_model_version=regime.model_version if regime is not None else None,
            regime_model_fingerprint=regime.model_fingerprint if regime is not None else None,
            strategy_confidence=None,  # CausalTargetStrategy deliberately has no confidence output.
            reference_price=reference_price,
            target_exposure=target_exposure,
            target_quantity=target_quantity,
            quantity_delta=quantity_delta,
            estimated_notional_delta=estimated_notional_delta,
            outcome=outcome,
            reason_code=reason_code,
            reason=reason,
            risk_assessment_status=risk_assessment_status,
            risk_assessment_reason_code=risk_assessment_reason_code,
            risk_assessment_reason=risk_assessment_reason,
            auto_halt_status=auto_halt_status,
            execution_authorized=False,
        )

    def _append_event(
        self,
        event_type: LearningEventType,
        *,
        correlation_id: str,
        occurred_at: datetime,
        payload: dict[str, Any],
    ) -> None:
        try:
            self._ledger.append(
                event_type,
                experiment_id=self._spec.experiment_id,
                correlation_id=correlation_id,
                occurred_at=_require_utc(occurred_at, "occurred_at"),
                payload=payload,
            )
        except Exception as exc:
            raise ShadowLedgerUnavailable(
                "Learning Lab ledger append failed; Shadow processing is blocked."
            ) from exc


class ShadowPollingService:
    """Single bounded polling step; a scheduler invokes poll_once at the configured cadence."""

    def __init__(
        self,
        *,
        feed: ReadOnlyCandleFeed,
        runner: ShadowRunner,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if feed.market != runner.config.market or feed.interval != runner.config.interval:
            raise ValueError("Feed market/interval do not match the Shadow runner.")
        if (
            feed.source_id != runner.config.source_id
            or feed.source_version != runner.config.source_version
        ):
            raise ValueError("Feed source identity does not match the Shadow experiment.")
        self._feed = feed
        self._runner = runner
        self._utcnow = utcnow

    def poll_once(self, *, now: datetime | None = None) -> ShadowPollResult:
        """Fetch one bounded REST window; errors are ledgered and do not trigger retry loops."""
        observed_now = _require_utc(now or self._utcnow(), "now")
        try:
            candles = tuple(self._feed.fetch_completed(now=observed_now))
            candles_to_process: tuple[MarketCandle, ...]
            if (
                self._runner.last_candle_at is None
                and not self._runner.history
                and len(candles) > 1
            ):
                self._runner.bootstrap_history(candles, received_at=observed_now)
                candles_to_process = (candles[-1],)
            else:
                last_seen = self._runner.last_candle_at
                candles_to_process = tuple(
                    candle for candle in candles
                    if last_seen is None or candle.timestamp > last_seen
                )
        except Exception as exc:
            self._runner.record_feed_failure(
                occurred_at=observed_now,
                error_kind=type(exc).__name__,
            )
            return ShadowPollResult(
                status=ShadowPollStatus.FEED_FAILURE,
                snapshots=(),
                reason_code="FEED_REQUEST_FAILED_OR_WINDOW_REJECTED",
            )
        snapshots = tuple(
            self._runner.process_candle(candle, received_at=observed_now)
            for candle in candles_to_process
        )
        if not snapshots:
            return ShadowPollResult(
                status=ShadowPollStatus.NO_COMPLETED_CANDLES,
                snapshots=(),
                reason_code="NO_COMPLETED_CANDLES",
            )
        return ShadowPollResult(
            status=ShadowPollStatus.OBSERVED,
            snapshots=snapshots,
            reason_code="SHADOW_SNAPSHOTS_RECORDED",
        )


def _outcome_for(previous: Decimal, target: Decimal) -> ShadowOutcome:
    if previous == 0 and target > 0:
        return ShadowOutcome.ENTER_LONG
    if previous > 0 and target == 0:
        return ShadowOutcome.EXIT_LONG
    if target > previous:
        return ShadowOutcome.ADJUST_LONG_UP
    if 0 < target < previous:
        return ShadowOutcome.ADJUST_LONG_DOWN
    if target > 0:
        return ShadowOutcome.HOLD_LONG
    return ShadowOutcome.STAY_FLAT


def _floor_step(quantity: Decimal, step: Decimal) -> Decimal:
    if not quantity.is_finite() or quantity < 0:
        raise ValueError("Hypothetical quantity must be finite and non-negative.")
    units = (quantity / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def _safe_correlation(value: str | None) -> str:
    if value is None:
        return f"shadow:{uuid.uuid4().hex}"
    return _validate_id(value, "correlation_id")


def _validate_id(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 128
        or any(not (char.isalnum() or char in "_.:-") for char in value)
    ):
        raise ValueError(f"{name} must be a non-empty safe identifier of at most 128 characters.")
    return value


def _require_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime.")
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware canonical UTC.")
    return value.astimezone(UTC)


def _safe_audit_timestamp(value: object, fallback: datetime) -> datetime:
    """Normalize an aware source timestamp for rejection telemetry; never trust a naive clock."""
    if not isinstance(value, datetime):
        return _require_utc(fallback, "fallback")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError):
        return _require_utc(fallback, "fallback")
    if value.tzinfo is None or offset is None:
        return _require_utc(fallback, "fallback")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _require_utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _iso_or_none(value: datetime | None) -> str | None:
    return None if value is None else _iso(value)


def _decimal_or_none(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _reason_explanation(reason_code: str) -> str:
    mapping = {
        "CONTINUITY_RECONCILIATION_REQUIRED": (
            "Feed continuity is blocked until explicit reconciliation."
        ),
        "MODEL_IDENTITY_REVIEW_REQUIRED": (
            "Model identity changed; a new registered experiment is required."
        ),
        "SHADOW_STRATEGY_IDENTITY_CHANGED": (
            "Shadow strategy identity changed; the current experiment is no longer trusted."
        ),
        "PAPER_BASELINE_IDENTITY_CHANGED": (
            "Paper baseline identity changed; the comparison is no longer trusted."
        ),
        "PAPER_CHAMPION_CHANGED": (
            "Active Paper Champion changed; a new registered Shadow comparison is required."
        ),
        "STRATEGY_IDENTITY_UNVERIFIABLE": (
            "Strategy identity could not be revalidated; the comparison abstains."
        ),
        "AUTO_HALT_STATUS_UNAVAILABLE": "Auto-Halt status could not be established; fail closed.",
        "AUTO_HALT_STATUS_INVALID": "Auto-Halt reader returned a non-boolean state; fail closed.",
        "AUTO_HALT_ACTIVE": "Auto-Halt is active; no hypothetical target is emitted.",
        "REGIME_CLASSIFIER_ERROR": "Regime classifier failed; the decision abstains.",
        "REGIME_OUTPUT_INVALID": "Regime classifier returned an invalid output.",
        "REGIME_MODEL_IDENTITY_CHANGED": "Regime model identity changed during the Shadow run.",
        "REGIME_CONFIDENCE_BELOW_THRESHOLD": (
            "Regime confidence did not meet the configured threshold."
        ),
        "REGIME_FROM_FUTURE": "Regime prediction timestamp is future-dated.",
        "REGIME_STALE": "Regime prediction is stale.",
        "CANDLE_GAP": "Candle sequence has a gap; reconciliation is required.",
        "DUPLICATE_CANDLE": "Repeated candle was rejected and not processed twice.",
        "OUT_OF_ORDER_CANDLE": "Out-of-order candle was rejected.",
        "STALE_CANDLE": "Candle freshness gate rejected stale input.",
        "FUTURE_CANDLE": "Candle timestamp is implausibly in the future.",
        "STRATEGY_OUTPUT_INVALID": "Strategy output was invalid or strategy raised an exception.",
        "MARKET_IDENTITY_MISMATCH": (
            "Candle market identity differs from the registered Shadow market."
        ),
        "CANDLE_INTERVAL_MISMATCH": "Candle interval differs from the registered Shadow interval.",
        "FEED_SOURCE_MISMATCH": "Candle source differs from the registered Shadow feed.",
    }
    return mapping.get(reason_code, f"Shadow precondition failed: {reason_code}.")
