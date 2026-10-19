"""Immutable experiment registry and hash-chained Learning Lab audit ledger.

The registry records exact code/data/model/features/seeds/ranges and evidence. It cannot access
RiskEngine, exchange writes or Live configuration. Paper/Shadow promotion logic lives separately.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any

from evoryn.learning_lab.backtest import BacktestResult, IndexRange


_SCHEMA_VERSION = 1


class ExperimentMode(StrEnum):
    """Execution scope in which an experiment was evaluated."""

    BACKTEST = "backtest"
    SHADOW = "shadow"
    PAPER = "paper"


class LearningEventType(StrEnum):
    """Auditable Learning Lab lifecycle events."""

    EXPERIMENT_REGISTERED = "experiment_registered"
    EXPERIMENT_REGISTRATION_REUSED = "experiment_registration_reused"
    EVIDENCE_RECORDED = "evidence_recorded"
    CANDIDATE_COMPARED = "candidate_compared"
    CANDIDATE_APPROVED_FOR_SHADOW = "candidate_approved_for_shadow"
    CANDIDATE_PROMOTED_TO_PAPER = "candidate_promoted_to_paper"
    PROMOTION_REJECTED = "promotion_rejected"
    AUTO_ROLLBACK_TRIGGERED = "auto_rollback_triggered"
    RELEASE_CANDIDATE_CREATED = "release_candidate_created"
    RELEASE_APPROVED = "release_approved"
    RELEASE_REJECTED = "release_rejected"
    LIVE_RELEASE_BLOCKED = "live_release_blocked"


class ExperimentIntegrityError(ValueError):
    """Raised when immutable experiment bytes or the Learning Lab hash chain do not verify."""


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    """Content-defined experiment identity; all fields required to reproduce a trial."""

    code_revision: str
    dataset_version: str
    feature_schema_version: str
    evaluation_config_fingerprint: str
    model_id: str
    model_version: str
    model_fingerprint: str
    parameters: tuple[tuple[str, str], ...]
    random_seed: int
    train_range: IndexRange
    validation_range: IndexRange
    test_range: IndexRange
    mode: ExperimentMode
    created_at: datetime

    def __post_init__(self) -> None:
        _require_sha(code=self.code_revision, name="code_revision", length=40)
        _require_sha(code=self.dataset_version, name="dataset_version", length=64)
        _require_sha(code=self.model_fingerprint, name="model_fingerprint", length=64)
        _require_sha(
            code=self.evaluation_config_fingerprint,
            name="evaluation_config_fingerprint",
            length=64,
        )
        for name in ("feature_schema_version", "model_id", "model_version"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty.")
        if type(self.random_seed) is not int or self.random_seed < 0:
            raise ValueError("random_seed must be a non-negative integer.")
        if not isinstance(self.mode, ExperimentMode):
            raise ValueError("mode must be a canonical ExperimentMode.")
        _require_aware_utc(self.created_at, "created_at")
        if not self.parameters:
            raise ValueError("Experiment parameters must be recorded explicitly.")
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in self.parameters
        ):
            raise ValueError("Experiment parameters must be string key/value pairs.")
        if tuple(sorted(self.parameters)) != self.parameters:
            raise ValueError("Experiment parameters must be sorted by key.")
        if len({key for key, _ in self.parameters}) != len(self.parameters):
            raise ValueError("Experiment parameter keys must be unique.")
        if any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            for key, value in self.parameters
        ):
            raise ValueError("Experiment parameters must use non-empty keys and string values.")
        if not (
            self.train_range.end <= self.validation_range.start
            and self.validation_range.end <= self.test_range.start
        ):
            raise ValueError(
                "Train, validation and test ranges must be chronological and disjoint."
            )

    def identity_material(self) -> dict[str, Any]:
        """Return every experiment input except created_at, which is registration metadata."""
        return {
            "schema_version": _SCHEMA_VERSION,
            "code_revision": self.code_revision,
            "dataset_version": self.dataset_version,
            "feature_schema_version": self.feature_schema_version,
            "evaluation_config_fingerprint": self.evaluation_config_fingerprint,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "model_fingerprint": self.model_fingerprint,
            "parameters": {key: value for key, value in self.parameters},
            "random_seed": self.random_seed,
            "train_range": _range_dict(self.train_range),
            "validation_range": _range_dict(self.validation_range),
            "test_range": _range_dict(self.test_range),
            "mode": self.mode.value,
        }

    @property
    def fingerprint(self) -> str:
        """Stable reproducibility fingerprint over code/data/model/features/seed/ranges/params."""
        return _sha256(_canonical_json_bytes(self.identity_material()))

    @property
    def experiment_id(self) -> str:
        """Unique immutable registry ID including creation time to distinguish repeated trials."""
        return _sha256(
            _canonical_json_bytes(
                {
                    "fingerprint": self.fingerprint,
                    "created_at": _iso_utc(self.created_at),
                }
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_material(),
            "experiment_id": self.experiment_id,
            "experiment_fingerprint": self.fingerprint,
            "created_at": _iso_utc(self.created_at),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ExperimentSpec:
        try:
            spec = cls(
                code_revision=value["code_revision"],
                dataset_version=value["dataset_version"],
                feature_schema_version=value["feature_schema_version"],
                evaluation_config_fingerprint=value["evaluation_config_fingerprint"],
                model_id=value["model_id"],
                model_version=value["model_version"],
                model_fingerprint=value["model_fingerprint"],
                parameters=tuple(sorted(value["parameters"].items())),
                random_seed=value["random_seed"],
                train_range=_range_from_dict(value["train_range"]),
                validation_range=_range_from_dict(value["validation_range"]),
                test_range=_range_from_dict(value["test_range"]),
                mode=ExperimentMode(value["mode"]),
                created_at=datetime.fromisoformat(value["created_at"].replace("Z", "+00:00")),
            )
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ExperimentIntegrityError("Stored experiment specification is invalid.") from exc
        if (
            value.get("schema_version") != _SCHEMA_VERSION
            or value.get("experiment_id") != spec.experiment_id
            or value.get("experiment_fingerprint") != spec.fingerprint
            or _canonical_json_bytes(value) != _canonical_json_bytes(spec.to_dict())
        ):
            raise ExperimentIntegrityError("Experiment specification identity/hash mismatch.")
        return spec


@dataclass(frozen=True, slots=True)
class ExperimentRunEvidence:
    """Immutable metrics snapshot from one Backtest, Shadow or Paper evaluation window."""

    experiment_id: str
    mode: ExperimentMode
    dataset_version: str
    test_range: IndexRange
    code_revision: str
    feature_schema_version: str
    evaluation_config_fingerprint: str
    model_id: str
    model_version: str
    model_fingerprint: str
    parameters_fingerprint: str
    random_seed: int
    run_id: str
    observations: int
    closed_trade_count: int
    net_profit: Decimal
    net_return_fraction: Decimal
    max_drawdown_fraction: Decimal
    expectancy_per_closed_trade: Decimal | None
    turnover_multiple: Decimal
    data_quality_pass: bool
    risk_gate_denial_count: int
    evidence_origin: str
    collected_at: datetime

    def __post_init__(self) -> None:
        _require_sha(code=self.experiment_id, name="experiment_id", length=64)
        for name in (
            "feature_schema_version",
            "model_id",
            "model_version",
            "evidence_origin",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty.")
        _require_sha(code=self.code_revision, name="code_revision", length=40)
        for name in (
            "dataset_version",
            "model_fingerprint",
            "parameters_fingerprint",
            "evaluation_config_fingerprint",
            "run_id",
        ):
            _require_sha(code=getattr(self, name), name=name, length=64)
        if not isinstance(self.mode, ExperimentMode):
            raise ValueError("mode must be a canonical ExperimentMode.")
        _require_aware_utc(self.collected_at, "collected_at")
        if type(self.random_seed) is not int or self.random_seed < 0:
            raise ValueError("random_seed must be a non-negative integer.")
        if type(self.observations) is not int or self.observations <= 0:
            raise ValueError("observations must be a positive integer.")
        if type(self.closed_trade_count) is not int or self.closed_trade_count < 0:
            raise ValueError("closed_trade_count must be a non-negative integer.")
        if type(self.risk_gate_denial_count) is not int or self.risk_gate_denial_count < 0:
            raise ValueError("risk_gate_denial_count must be non-negative.")
        if type(self.data_quality_pass) is not bool:
            raise ValueError("data_quality_pass must be explicit.")
        for name in (
            "net_profit",
            "net_return_fraction",
            "max_drawdown_fraction",
            "turnover_multiple",
        ):
            _require_finite_decimal(getattr(self, name), name)
        if not 0 <= self.max_drawdown_fraction <= 1:
            raise ValueError("max_drawdown_fraction must be in [0, 1].")
        if self.turnover_multiple < 0:
            raise ValueError("turnover_multiple must be non-negative.")
        if self.expectancy_per_closed_trade is not None:
            _require_finite_decimal(self.expectancy_per_closed_trade, "expectancy")
        if self.mode is ExperimentMode.PAPER and self.evidence_origin != "paper_telemetry":
            raise ValueError("PAPER evidence must originate from paper_telemetry.")
        if self.mode is ExperimentMode.SHADOW and self.evidence_origin != "shadow_telemetry":
            raise ValueError("SHADOW evidence must originate from shadow_telemetry.")
        if self.mode is ExperimentMode.BACKTEST and self.evidence_origin != "backtest_replay":
            raise ValueError("BACKTEST evidence must originate from backtest_replay.")

    @property
    def evidence_id(self) -> str:
        return _sha256(_canonical_json_bytes(self.to_dict(include_evidence_id=False)))

    @property
    def is_promotion_eligible(self) -> bool:
        return self.mode in {ExperimentMode.PAPER, ExperimentMode.SHADOW}

    def to_dict(self, *, include_evidence_id: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "experiment_id": self.experiment_id,
            "mode": self.mode.value,
            "dataset_version": self.dataset_version,
            "test_range": _range_dict(self.test_range),
            "code_revision": self.code_revision,
            "feature_schema_version": self.feature_schema_version,
            "evaluation_config_fingerprint": self.evaluation_config_fingerprint,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "model_fingerprint": self.model_fingerprint,
            "parameters_fingerprint": self.parameters_fingerprint,
            "random_seed": self.random_seed,
            "run_id": self.run_id,
            "observations": self.observations,
            "closed_trade_count": self.closed_trade_count,
            "net_profit": str(self.net_profit),
            "net_return_fraction": str(self.net_return_fraction),
            "max_drawdown_fraction": str(self.max_drawdown_fraction),
            "expectancy_per_closed_trade": (
                str(self.expectancy_per_closed_trade)
                if self.expectancy_per_closed_trade is not None
                else None
            ),
            "turnover_multiple": str(self.turnover_multiple),
            "data_quality_pass": self.data_quality_pass,
            "risk_gate_denial_count": self.risk_gate_denial_count,
            "evidence_origin": self.evidence_origin,
            "collected_at": _iso_utc(self.collected_at),
        }
        if include_evidence_id:
            value["evidence_id"] = self.evidence_id
        return value

    @classmethod
    def from_backtest_result(
        cls,
        spec: ExperimentSpec,
        result: BacktestResult,
        *,
        collected_at: datetime,
    ) -> ExperimentRunEvidence:
        """Capture a backtest result under a spec; it remains BACKTEST-only evidence."""
        if spec.mode is not ExperimentMode.BACKTEST:
            raise ValueError(
                "Backtest replay evidence requires an experiment registered in BACKTEST mode."
            )
        if result.dataset_version != spec.dataset_version:
            raise ValueError("Backtest result dataset differs from experiment specification.")
        if result.evaluation_range != spec.test_range:
            raise ValueError("Backtest result range differs from the registered test window.")
        expected_config_fingerprint = _sha256(
            _canonical_json_bytes(result.config.as_dict())
        )
        if expected_config_fingerprint != spec.evaluation_config_fingerprint:
            raise ValueError("Backtest configuration differs from the registered fingerprint.")
        if result.strategy_parameters != tuple(spec.parameters):
            raise ValueError(
                "Backtest strategy parameters differ from the experiment specification."
            )
        return cls(
            experiment_id=spec.experiment_id,
            mode=ExperimentMode.BACKTEST,
            dataset_version=spec.dataset_version,
            test_range=result.evaluation_range,
            code_revision=spec.code_revision,
            feature_schema_version=spec.feature_schema_version,
            evaluation_config_fingerprint=spec.evaluation_config_fingerprint,
            model_id=spec.model_id,
            model_version=spec.model_version,
            model_fingerprint=spec.model_fingerprint,
            parameters_fingerprint=_sha256(
                _canonical_json_bytes(dict(spec.parameters))
            ),
            random_seed=spec.random_seed,
            run_id=result.run_id,
            observations=result.evaluation_range.size,
            closed_trade_count=result.metrics.closed_trade_count,
            net_profit=result.metrics.net_profit,
            net_return_fraction=result.metrics.net_return_fraction,
            max_drawdown_fraction=result.metrics.max_drawdown_fraction,
            expectancy_per_closed_trade=result.metrics.expectancy_per_closed_trade,
            turnover_multiple=result.metrics.turnover_multiple,
            data_quality_pass=True,
            risk_gate_denial_count=0,
            evidence_origin="backtest_replay",
            collected_at=collected_at,
        )


@dataclass(frozen=True, slots=True)
class LearningLabLedgerEvent:
    """One immutable event in the hash-chained Learning Lab Ledger."""

    sequence: int
    event_id: str
    correlation_id: str
    event_type: LearningEventType
    experiment_id: str
    occurred_at: datetime
    payload_json: str
    payload_sha256: str
    previous_event_hash: str
    event_hash: str

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise ValueError("Ledger event sequence must be positive.")
        for name in ("event_id", "correlation_id", "experiment_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty.")
        _require_aware_utc(self.occurred_at, "occurred_at")
        for name in ("event_id", "payload_sha256", "previous_event_hash", "event_hash"):
            _require_sha(code=getattr(self, name), name=name, length=64)
        parsed = json.loads(self.payload_json)
        if (
            not isinstance(parsed, dict)
            or _canonical_json_bytes(parsed).decode("utf-8") != self.payload_json
        ):
            raise ValueError("Ledger payload must be a canonical JSON object.")
        if _sha256(self.payload_json.encode("utf-8")) != self.payload_sha256:
            raise ValueError("Ledger payload hash is invalid.")

    def identity_material(self) -> dict[str, Any]:
        """Return fields used to derive the stable event ID before adding the event hash."""
        return {
            "sequence": self.sequence,
            "event_type": self.event_type.value,
            "experiment_id": self.experiment_id,
            "correlation_id": self.correlation_id,
            "occurred_at": _iso_utc(self.occurred_at),
            "payload_sha256": self.payload_sha256,
            "previous_event_hash": self.previous_event_hash,
        }

    def hash_material(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "correlation_id": self.correlation_id,
            "event_type": self.event_type.value,
            "experiment_id": self.experiment_id,
            "occurred_at": _iso_utc(self.occurred_at),
            "payload_json": self.payload_json,
            "payload_sha256": self.payload_sha256,
            "previous_event_hash": self.previous_event_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.hash_material(), "event_hash": self.event_hash}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LearningLabLedgerEvent:
        try:
            event = cls(
                sequence=value["sequence"],
                event_id=value["event_id"],
                correlation_id=value["correlation_id"],
                event_type=LearningEventType(value["event_type"]),
                experiment_id=value["experiment_id"],
                occurred_at=datetime.fromisoformat(value["occurred_at"].replace("Z", "+00:00")),
                payload_json=value["payload_json"],
                payload_sha256=value["payload_sha256"],
                previous_event_hash=value["previous_event_hash"],
                event_hash=value["event_hash"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExperimentIntegrityError("Stored Learning Lab event is invalid.") from exc
        if (
            value.get("schema_version") != _SCHEMA_VERSION
            or _canonical_json_bytes(value) != _canonical_json_bytes(event.to_dict())
        ):
            raise ExperimentIntegrityError("Stored Learning Lab event is not canonical.")
        return event


class LearningLabLedger:
    """Append-only hash chain with optional fsync-backed JSONL persistence and replay verify."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = None if path is None else Path(path).expanduser().resolve()
        self._lock = RLock()
        self._events: list[LearningLabLedgerEvent] = []
        if self._path is not None and self._path.exists():
            self._load_and_verify()

    @property
    def events(self) -> tuple[LearningLabLedgerEvent, ...]:
        with self._lock:
            return tuple(self._events)

    @property
    def last_event_hash(self) -> str:
        with self._lock:
            return self._events[-1].event_hash if self._events else "0" * 64

    def append(
        self,
        event_type: LearningEventType,
        *,
        experiment_id: str,
        correlation_id: str,
        occurred_at: datetime,
        payload: dict[str, Any],
    ) -> LearningLabLedgerEvent:
        _require_aware_utc(occurred_at, "occurred_at")
        if not experiment_id.strip() or not correlation_id.strip():
            raise ValueError("Ledger event requires experiment_id and correlation_id.")
        if not isinstance(event_type, LearningEventType):
            raise ValueError("event_type must be a canonical LearningEventType.")
        payload_json = _canonical_json_bytes(payload).decode("utf-8")
        payload_sha = _sha256(payload_json.encode("utf-8"))
        with self._lock:
            sequence = len(self._events) + 1
            previous_hash = self.last_event_hash
            event_id = _sha256(
                _canonical_json_bytes(
                    {
                        "sequence": sequence,
                        "event_type": event_type.value,
                        "experiment_id": experiment_id,
                        "correlation_id": correlation_id,
                        "occurred_at": _iso_utc(occurred_at),
                        "payload_sha256": payload_sha,
                        "previous_event_hash": previous_hash,
                    }
                )
            )
            material = {
                "schema_version": _SCHEMA_VERSION,
                "sequence": sequence,
                "event_id": event_id,
                "correlation_id": correlation_id,
                "event_type": event_type.value,
                "experiment_id": experiment_id,
                "occurred_at": _iso_utc(occurred_at),
                "payload_json": payload_json,
                "payload_sha256": payload_sha,
                "previous_event_hash": previous_hash,
            }
            event_hash = _sha256(_canonical_json_bytes(material))
            event = LearningLabLedgerEvent(
                sequence=sequence,
                event_id=event_id,
                correlation_id=correlation_id,
                event_type=event_type,
                experiment_id=experiment_id,
                occurred_at=occurred_at.astimezone(UTC),
                payload_json=payload_json,
                payload_sha256=payload_sha,
                previous_event_hash=previous_hash,
                event_hash=event_hash,
            )
            if self._path is not None:
                self._append_persisted(event)
            self._events.append(event)
            return event

    def verify(self) -> bool:
        """Recompute each link, payload digest, sequence and event digest."""
        with self._lock:
            previous_hash = "0" * 64
            for expected_sequence, event in enumerate(self._events, start=1):
                if (
                    event.sequence != expected_sequence
                    or event.previous_event_hash != previous_hash
                ):
                    raise ExperimentIntegrityError(
                        "Learning Lab hash-chain sequence/link mismatch."
                    )
                expected_event_id = _sha256(
                    _canonical_json_bytes(event.identity_material())
                )
                if expected_event_id != event.event_id:
                    raise ExperimentIntegrityError("Learning Lab event identity hash mismatch.")
                if _sha256(_canonical_json_bytes(event.hash_material())) != event.event_hash:
                    raise ExperimentIntegrityError("Learning Lab event hash mismatch.")
                previous_hash = event.event_hash
            return True

    def _append_persisted(self, event: LearningLabLedgerEvent) -> None:
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = _canonical_json_bytes(event.to_dict())
        descriptor = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            view = memoryview(line)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("Short write while appending Learning Lab Ledger event.")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _load_and_verify(self) -> None:
        assert self._path is not None
        try:
            raw_lines = self._path.read_bytes().splitlines()
            if self._path.stat().st_size and not self._path.read_bytes().endswith(b"\n"):
                raise ExperimentIntegrityError("Learning Lab Ledger ends with a partial line.")
            events: list[LearningLabLedgerEvent] = []
            for raw in raw_lines:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ExperimentIntegrityError("Ledger line must be a JSON object.")
                events.append(LearningLabLedgerEvent.from_dict(parsed))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ExperimentIntegrityError("Learning Lab Ledger cannot be read/decoded.") from exc
        self._events = events
        self.verify()


class ExperimentRegistry:
    """Immutable JSON-backed specs/evidence with every transition recorded in the ledger."""

    def __init__(self, root: str | Path, ledger: LearningLabLedger) -> None:
        self._root = Path(root).expanduser().resolve()
        self._ledger = ledger
        self._lock = RLock()
        self._specs: dict[str, ExperimentSpec] = {}
        self._evidence: dict[str, ExperimentRunEvidence] = {}

    @property
    def ledger(self) -> LearningLabLedger:
        return self._ledger

    def register(self, spec: ExperimentSpec, *, correlation_id: str) -> str:
        """Persist one immutable spec and ensure its registration is present in the ledger."""
        if not correlation_id.strip():
            raise ValueError("correlation_id must not be empty.")
        with self._lock:
            existing = self._specs.get(spec.experiment_id)
            if existing is not None and existing.to_dict() != spec.to_dict():
                raise ExperimentIntegrityError("Existing experiment ID has conflicting metadata.")
            stored = self._store_immutable_json(
                "specs", f"{spec.experiment_id}.json", spec.to_dict()
            )
            if not stored:
                existing_spec = self._load_spec_file(spec.experiment_id)
                if existing_spec.to_dict() != spec.to_dict():
                    raise ExperimentIntegrityError(
                        "Existing experiment file conflicts with registration."
                    )
                spec = existing_spec

            registered_events = tuple(
                event
                for event in self._ledger.events
                if event.experiment_id == spec.experiment_id
                and event.event_type
                in {
                    LearningEventType.EXPERIMENT_REGISTERED,
                    LearningEventType.EXPERIMENT_REGISTRATION_REUSED,
                }
            )
            expected_payload = {
                "experiment_fingerprint": spec.fingerprint,
                "mode": spec.mode.value,
                "dataset_version": spec.dataset_version,
                "code_revision": spec.code_revision,
                "model_fingerprint": spec.model_fingerprint,
            }
            if registered_events:
                if any(
                    json.loads(event.payload_json) != expected_payload
                    for event in registered_events
                ):
                    raise ExperimentIntegrityError(
                        "Learning Lab ledger registration differs from the stored experiment."
                    )
            else:
                event_type = (
                    LearningEventType.EXPERIMENT_REGISTERED
                    if stored
                    else LearningEventType.EXPERIMENT_REGISTRATION_REUSED
                )
                self._ledger.append(
                    event_type,
                    experiment_id=spec.experiment_id,
                    correlation_id=correlation_id,
                    occurred_at=spec.created_at,
                    payload=expected_payload,
                )
            self._specs[spec.experiment_id] = spec
            return spec.experiment_id

    def get(self, experiment_id: str) -> ExperimentSpec:
        """Load an experiment only when immutable bytes and its ledger registration agree."""
        _require_sha(code=experiment_id, name="experiment_id", length=64)
        with self._lock:
            spec = self._specs.get(experiment_id)
            if spec is None:
                spec = self._load_spec_file(experiment_id)
                self._specs[experiment_id] = spec
            self._require_spec_ledger_record(spec)
            return spec

    def _load_spec_file(self, experiment_id: str) -> ExperimentSpec:
        """Decode and verify the immutable spec file without requiring its ledger event."""
        path = self._root / "specs" / f"{experiment_id}.json"
        try:
            value = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise KeyError(f"Unknown experiment ID: {experiment_id}") from exc
        if not isinstance(value, dict):
            raise ExperimentIntegrityError("Experiment file must be a JSON object.")
        spec = ExperimentSpec.from_dict(value)
        if spec.experiment_id != experiment_id:
            raise ExperimentIntegrityError(
                "Experiment ID differs from the requested file name."
            )
        return spec

    def _require_spec_ledger_record(self, spec: ExperimentSpec) -> None:
        """Fail closed if a registered experiment has no exact append-only ledger event."""
        expected_payload = {
            "experiment_fingerprint": spec.fingerprint,
            "mode": spec.mode.value,
            "dataset_version": spec.dataset_version,
            "code_revision": spec.code_revision,
            "model_fingerprint": spec.model_fingerprint,
        }
        matching_events = tuple(
            event
            for event in self._ledger.events
            if event.experiment_id == spec.experiment_id
            and event.event_type
            in {
                LearningEventType.EXPERIMENT_REGISTERED,
                LearningEventType.EXPERIMENT_REGISTRATION_REUSED,
            }
        )
        if not matching_events or any(
            json.loads(event.payload_json) != expected_payload
            for event in matching_events
        ):
            raise ExperimentIntegrityError(
                "Experiment specification lacks a matching Learning Lab Ledger event."
            )

    def record_evidence(
        self,
        evidence: ExperimentRunEvidence,
        *,
        correlation_id: str,
    ) -> str:
        """Persist evidence only when registered inputs match and an audit event exists."""
        if not correlation_id.strip():
            raise ValueError("correlation_id must not be empty.")
        spec = self.get(evidence.experiment_id)
        expected_parameters_fingerprint = _sha256(
            _canonical_json_bytes(dict(spec.parameters))
        )
        if (
            evidence.dataset_version != spec.dataset_version
            or evidence.code_revision != spec.code_revision
            or evidence.feature_schema_version != spec.feature_schema_version
            or evidence.evaluation_config_fingerprint != spec.evaluation_config_fingerprint
            or evidence.model_id != spec.model_id
            or evidence.model_version != spec.model_version
            or evidence.model_fingerprint != spec.model_fingerprint
            or evidence.parameters_fingerprint != expected_parameters_fingerprint
            or evidence.random_seed != spec.random_seed
            or evidence.test_range != spec.test_range
            or evidence.mode is not spec.mode
        ):
            raise ExperimentIntegrityError(
                "Run evidence differs from the registered reproducibility identity."
            )
        with self._lock:
            cached = self._evidence.get(evidence.evidence_id)
            if cached is not None and cached.to_dict() != evidence.to_dict():
                raise ExperimentIntegrityError("Evidence ID has conflicting content.")
            stored = self._store_immutable_json(
                "evidence", f"{evidence.evidence_id}.json", evidence.to_dict()
            )
            if not stored:
                existing_path = self._root / "evidence" / f"{evidence.evidence_id}.json"
                try:
                    existing_value = json.loads(existing_path.read_bytes())
                except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise ExperimentIntegrityError("Existing evidence file is invalid.") from exc
                if _canonical_json_bytes(existing_value) != _canonical_json_bytes(
                    evidence.to_dict()
                ):
                    raise ExperimentIntegrityError("Existing evidence file has conflicting bytes.")
                evidence = _evidence_from_dict(existing_value)

            self._ensure_evidence_ledger_event(evidence, correlation_id=correlation_id)
            self._evidence[evidence.evidence_id] = evidence
            return evidence.evidence_id

    def _ensure_evidence_ledger_event(
        self,
        evidence: ExperimentRunEvidence,
        *,
        correlation_id: str,
    ) -> None:
        """Reconcile an orphaned evidence file with the append-only audit ledger."""
        expected_payload = {
            "evidence_id": evidence.evidence_id,
            "run_id": evidence.run_id,
            "mode": evidence.mode.value,
            "observations": evidence.observations,
            "closed_trade_count": evidence.closed_trade_count,
            "net_return_fraction": str(evidence.net_return_fraction),
            "max_drawdown_fraction": str(evidence.max_drawdown_fraction),
            "data_quality_pass": evidence.data_quality_pass,
            "risk_gate_denial_count": evidence.risk_gate_denial_count,
        }
        matches = tuple(
            event
            for event in self._ledger.events
            if event.event_type is LearningEventType.EVIDENCE_RECORDED
            and json.loads(event.payload_json).get("evidence_id") == evidence.evidence_id
        )
        if matches:
            if not any(
                event.experiment_id == evidence.experiment_id
                and json.loads(event.payload_json) == expected_payload
                for event in matches
            ):
                raise ExperimentIntegrityError(
                    "Learning Lab evidence event differs from the immutable evidence file."
                )
            return
        self._ledger.append(
            LearningEventType.EVIDENCE_RECORDED,
            experiment_id=evidence.experiment_id,
            correlation_id=correlation_id,
            occurred_at=evidence.collected_at,
            payload=expected_payload,
        )

    def get_evidence(self, evidence_id: str) -> ExperimentRunEvidence:
        """Load only canonical evidence with a matching append-only ledger record."""
        _require_sha(code=evidence_id, name="evidence_id", length=64)
        with self._lock:
            cached = self._evidence.get(evidence_id)
            if cached is not None:
                self._require_evidence_ledger_record(cached)
                return cached
            path = self._root / "evidence" / f"{evidence_id}.json"
            try:
                value = json.loads(path.read_bytes())
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise KeyError(f"Unknown evidence ID: {evidence_id}") from exc
            if not isinstance(value, dict):
                raise ExperimentIntegrityError("Evidence file must be a JSON object.")
            evidence = _evidence_from_dict(value)
            if evidence.evidence_id != evidence_id:
                raise ExperimentIntegrityError("Evidence content hash differs from file identity.")
            self._require_evidence_ledger_record(evidence)
            self._evidence[evidence_id] = evidence
            return evidence

    def _require_evidence_ledger_record(self, evidence: ExperimentRunEvidence) -> None:
        """Fail closed when a result artifact is missing its audit event or the event differs."""
        expected_payload = {
            "evidence_id": evidence.evidence_id,
            "run_id": evidence.run_id,
            "mode": evidence.mode.value,
            "observations": evidence.observations,
            "closed_trade_count": evidence.closed_trade_count,
            "net_return_fraction": str(evidence.net_return_fraction),
            "max_drawdown_fraction": str(evidence.max_drawdown_fraction),
            "data_quality_pass": evidence.data_quality_pass,
            "risk_gate_denial_count": evidence.risk_gate_denial_count,
        }
        matching_events = tuple(
            event
            for event in self._ledger.events
            if event.event_type is LearningEventType.EVIDENCE_RECORDED
            and json.loads(event.payload_json).get("evidence_id") == evidence.evidence_id
        )
        if not matching_events or any(
            event.experiment_id != evidence.experiment_id
            or json.loads(event.payload_json) != expected_payload
            for event in matching_events
        ):
            raise ExperimentIntegrityError(
                "Experiment evidence is not backed by a matching Learning Lab Ledger event."
            )

    def _store_immutable_json(self, folder: str, filename: str, value: dict[str, Any]) -> bool:
        directory = self._root / folder
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / filename
        payload = _canonical_json_bytes(value)
        if destination.exists():
            if destination.read_bytes() != payload:
                raise ExperimentIntegrityError("Existing immutable artifact has different bytes.")
            return False
        fd, staged_name = tempfile.mkstemp(prefix=".stage-", suffix=".tmp", dir=directory)
        staged = Path(staged_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(staged, destination)
            except FileExistsError:
                if destination.read_bytes() != payload:
                    raise ExperimentIntegrityError(
                        "Concurrent immutable write produced conflicting bytes."
                    )
                return False
        finally:
            if staged.exists():
                staged.unlink()
        return True


def _evidence_from_dict(value: dict[str, Any]) -> ExperimentRunEvidence:
    try:
        evidence = ExperimentRunEvidence(
            experiment_id=value["experiment_id"],
            mode=ExperimentMode(value["mode"]),
            dataset_version=value["dataset_version"],
            test_range=_range_from_dict(value["test_range"]),
            code_revision=value["code_revision"],
            feature_schema_version=value["feature_schema_version"],
            evaluation_config_fingerprint=value["evaluation_config_fingerprint"],
            model_id=value["model_id"],
            model_version=value["model_version"],
            model_fingerprint=value["model_fingerprint"],
            parameters_fingerprint=value["parameters_fingerprint"],
            random_seed=value["random_seed"],
            run_id=value["run_id"],
            observations=value["observations"],
            closed_trade_count=value["closed_trade_count"],
            net_profit=Decimal(value["net_profit"]),
            net_return_fraction=Decimal(value["net_return_fraction"]),
            max_drawdown_fraction=Decimal(value["max_drawdown_fraction"]),
            expectancy_per_closed_trade=(
                Decimal(value["expectancy_per_closed_trade"])
                if value["expectancy_per_closed_trade"] is not None
                else None
            ),
            turnover_multiple=Decimal(value["turnover_multiple"]),
            data_quality_pass=value["data_quality_pass"],
            risk_gate_denial_count=value["risk_gate_denial_count"],
            evidence_origin=value["evidence_origin"],
            collected_at=datetime.fromisoformat(value["collected_at"].replace("Z", "+00:00")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ExperimentIntegrityError("Stored run evidence is invalid.") from exc
    if (
        value.get("schema_version") != _SCHEMA_VERSION
        or value.get("evidence_id") != evidence.evidence_id
        or _canonical_json_bytes(value) != _canonical_json_bytes(evidence.to_dict())
    ):
        raise ExperimentIntegrityError("Stored run evidence identity/hash mismatch.")
    return evidence


def _range_dict(value: IndexRange) -> dict[str, int]:
    return {"start": value.start, "end": value.end}


def _range_from_dict(value: dict[str, Any]) -> IndexRange:
    if not isinstance(value, dict):
        raise ValueError("Stored index range must be a JSON object.")
    return IndexRange(start=value["start"], end=value["end"])


def _require_sha(*, code: str, name: str, length: int) -> None:
    if (
        not isinstance(code, str)
        or len(code) != length
        or any(character not in "0123456789abcdef" for character in code)
    ):
        raise ValueError(f"{name} must be a lowercase SHA digest of length {length}.")


def _require_aware_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{name} must be expressed in UTC.")


def _require_finite_decimal(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal.")


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _iso_utc(value: datetime) -> str:
    _require_aware_utc(value, "timestamp")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
