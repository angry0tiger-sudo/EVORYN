"""Immutable, provenance-rich historical candle datasets and local content-addressed storage.

This module does not fetch data. Callers must provide source/license metadata and normalized
candles; no public URL, API response, or exchange permission is guessed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from evoryn.domain.models import Market, MarketCandle

DATASET_SCHEMA_VERSION = 1
_DATASET_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_VERSION_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CANDLE_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume", "source")


class DatasetIntegrityError(ValueError):
    """Raised when persisted bytes do not match the versioned dataset manifest."""


@dataclass(frozen=True, slots=True)
class HistoricalDataset:
    """A closed, immutable OHLCV snapshot with provenance and explicit gap policy.

    Candle timestamps are interpreted as candle-open times. Candle prices and volumes
    are already normalized to the units of the declared Market; the dataset never infers
    symbol aliases or converts units. The dataset's version covers both its metadata and
    canonical CSV bytes.
    """

    dataset_id: str
    market: Market
    interval: timedelta
    source_name: str
    source_uri: str
    source_revision: str
    license_id: str
    license_uri: str
    collected_at: datetime
    candles: tuple[MarketCandle, ...]
    allow_gaps: bool = False

    def __post_init__(self) -> None:
        if not _DATASET_ID_PATTERN.fullmatch(self.dataset_id):
            raise ValueError("dataset_id must be a simple identifier, not a path.")
        for name in ("symbol", "venue", "market_type"):
            value = getattr(self.market, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Market {name} must not be empty.")
        if self.interval <= timedelta(0):
            raise ValueError("Dataset interval must be positive.")
        _require_aware(self.collected_at, "collected_at")
        for name in ("source_name", "source_uri", "source_revision", "license_id", "license_uri"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} is required provenance and must not be empty.")
        if type(self.allow_gaps) is not bool:
            raise ValueError("allow_gaps must be an explicit boolean.")
        if not isinstance(self.candles, tuple) or not self.candles:
            raise ValueError("candles must be a non-empty immutable tuple.")

        previous: MarketCandle | None = None
        for candle in self.candles:
            if not isinstance(candle, MarketCandle):
                raise ValueError("Every dataset row must be a validated MarketCandle.")
            if candle.market != self.market:
                raise ValueError("Every candle must belong to the dataset's canonical market.")
            if candle.interval != self.interval:
                raise ValueError("Every candle must match the declared dataset interval.")
            if candle.source != self.source_name:
                raise ValueError("Candle source must match dataset source_name.")
            if previous is not None:
                if candle.timestamp <= previous.timestamp:
                    raise ValueError("Candles must be strictly chronological without duplicates.")
                delta = candle.timestamp - previous.timestamp
                if delta != self.interval:
                    if not self.allow_gaps:
                        raise ValueError(
                            "Candle interval gap detected; "
                            "set allow_gaps=True only when documented."
                        )
                    if delta % self.interval != timedelta(0):
                        raise ValueError(
                            "Allowed gaps must remain aligned to whole dataset intervals."
                        )
            previous = candle

        # Canonicalize time zones once so save/load equality is semantic, not offset-dependent.
        object.__setattr__(self, "collected_at", self.collected_at.astimezone(UTC))
        object.__setattr__(
            self,
            "candles",
            tuple(
                replace(candle, timestamp=candle.timestamp.astimezone(UTC))
                for candle in self.candles
            ),
        )

    @property
    def interval_microseconds(self) -> int:
        """Return exact interval resolution without passing through binary float."""
        return self.interval // timedelta(microseconds=1)

    @property
    def gap_count(self) -> int:
        """Count missing interval boundaries between adjacent candle-open timestamps."""
        return sum(
            max(0, (right.timestamp - left.timestamp) // self.interval - 1)
            for left, right in zip(self.candles, self.candles[1:])
        )

    def metadata(self) -> dict[str, Any]:
        """Return canonical metadata included in the immutable dataset version."""
        return {
            "dataset_id": self.dataset_id,
            "market": {
                "symbol": self.market.symbol,
                "venue": self.market.venue,
                "market_type": self.market.market_type,
            },
            "interval_microseconds": self.interval_microseconds,
            "timestamp_semantics": "candle_open_time_utc",
            "source_name": self.source_name,
            "source_uri": self.source_uri,
            "source_revision": self.source_revision,
            "license_id": self.license_id,
            "license_uri": self.license_uri,
            "collected_at": _iso_utc(self.collected_at),
            "candle_count": len(self.candles),
            "first_candle_open": _iso_utc(self.candles[0].timestamp),
            "last_candle_open": _iso_utc(self.candles[-1].timestamp),
            "allow_gaps": self.allow_gaps,
            "gap_count": self.gap_count,
        }

    def candles_csv_bytes(self) -> bytes:
        """Serialize candles using a stable column order, decimal strings and UTC times."""
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(_CANDLE_COLUMNS)
        for candle in self.candles:
            writer.writerow(
                (
                    _iso_utc(candle.timestamp),
                    str(candle.open),
                    str(candle.high),
                    str(candle.low),
                    str(candle.close),
                    str(candle.volume),
                    candle.source,
                )
            )
        return output.getvalue().encode("utf-8")

    @property
    def version(self) -> str:
        """Content address for metadata plus canonical candle bytes."""
        candle_bytes = self.candles_csv_bytes()
        version_input = {
            "schema_version": DATASET_SCHEMA_VERSION,
            "metadata": self.metadata(),
            "candles_sha256": hashlib.sha256(candle_bytes).hexdigest(),
        }
        return hashlib.sha256(_canonical_json_bytes(version_input)).hexdigest()


@dataclass(frozen=True, slots=True)
class DatasetReference:
    """Immutable location and integrity values returned after saving a dataset."""

    dataset_id: str
    version: str
    path: str
    candle_count: int
    candles_sha256: str
    manifest_sha256: str


class HistoricalDatasetStore:
    """Atomic, immutable, content-addressed local dataset store.

    Layout: <root>/<dataset_id>/<sha256>/manifest.json and candles.csv.
    Existing versions are never overwritten. Re-saving identical content is idempotent.
    This class operates on the filesystem only; it has no network or exchange client.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()

    def save(self, dataset: HistoricalDataset) -> DatasetReference:
        """Persist one dataset atomically; refuse any conflicting immutable version."""
        if not isinstance(dataset, HistoricalDataset):
            raise TypeError("save requires a validated HistoricalDataset.")
        candle_bytes = dataset.candles_csv_bytes()
        candle_hash = hashlib.sha256(candle_bytes).hexdigest()
        manifest = {
            "schema_version": DATASET_SCHEMA_VERSION,
            "dataset_version": dataset.version,
            "metadata": dataset.metadata(),
            "candles_sha256": candle_hash,
        }
        manifest_bytes = _canonical_json_bytes(manifest)
        destination = self._root / dataset.dataset_id / dataset.version
        destination.parent.mkdir(parents=True, exist_ok=True)

        if destination.exists():
            return self._verify_existing(
                destination, dataset, candle_bytes, manifest_bytes, candle_hash
            )

        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=destination.parent))
        try:
            (staging / "candles.csv").write_bytes(candle_bytes)
            (staging / "manifest.json").write_bytes(manifest_bytes)
            try:
                os.rename(staging, destination)
            except OSError:
                if destination.exists():
                    return self._verify_existing(
                        destination, dataset, candle_bytes, manifest_bytes, candle_hash
                    )
                raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return self._reference(destination, dataset, candle_hash, manifest_bytes)

    def load(self, dataset_id: str, version: str) -> HistoricalDataset:
        """Load and verify a version; reject path traversal, altered bytes, and schema drift."""
        if not _DATASET_ID_PATTERN.fullmatch(dataset_id):
            raise ValueError("dataset_id must be a simple identifier, not a path.")
        if not _VERSION_PATTERN.fullmatch(version):
            raise ValueError("version must be a lowercase SHA-256 hex digest.")
        directory = self._root / dataset_id / version
        try:
            manifest_bytes = (directory / "manifest.json").read_bytes()
            candle_bytes = (directory / "candles.csv").read_bytes()
            manifest = json.loads(manifest_bytes)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DatasetIntegrityError("Dataset manifest or candle file cannot be read.") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != DATASET_SCHEMA_VERSION
        ):
            raise DatasetIntegrityError("Unsupported or invalid dataset manifest schema.")
        if manifest.get("dataset_version") != version:
            raise DatasetIntegrityError(
                "Dataset manifest version does not match requested version."
            )
        if manifest.get("candles_sha256") != hashlib.sha256(candle_bytes).hexdigest():
            raise DatasetIntegrityError("Candle bytes do not match the recorded SHA-256 digest.")
        metadata = manifest.get("metadata")
        if not isinstance(metadata, dict):
            raise DatasetIntegrityError("Dataset metadata must be a JSON object.")

        dataset = _dataset_from_bytes(metadata, candle_bytes)
        if dataset.dataset_id != dataset_id or dataset.version != version:
            raise DatasetIntegrityError("Reconstructed dataset does not match its content address.")
        expected_manifest = {
            "schema_version": DATASET_SCHEMA_VERSION,
            "dataset_version": dataset.version,
            "metadata": dataset.metadata(),
            "candles_sha256": hashlib.sha256(dataset.candles_csv_bytes()).hexdigest(),
        }
        if _canonical_json_bytes(expected_manifest) != manifest_bytes:
            raise DatasetIntegrityError("Manifest is not canonical or metadata changed.")
        return dataset

    def _verify_existing(
        self,
        destination: Path,
        dataset: HistoricalDataset,
        candle_bytes: bytes,
        manifest_bytes: bytes,
        candle_hash: str,
    ) -> DatasetReference:
        """Idempotently reuse an exact existing version; never repair/overwrite it."""
        try:
            existing_candles = (destination / "candles.csv").read_bytes()
            existing_manifest = (destination / "manifest.json").read_bytes()
        except OSError as exc:
            raise DatasetIntegrityError(
                "Existing version is incomplete; refusing overwrite."
            ) from exc
        if existing_candles != candle_bytes or existing_manifest != manifest_bytes:
            raise DatasetIntegrityError("Existing content-addressed version has conflicting bytes.")
        return self._reference(destination, dataset, candle_hash, manifest_bytes)

    @staticmethod
    def _reference(
        destination: Path,
        dataset: HistoricalDataset,
        candle_hash: str,
        manifest_bytes: bytes,
    ) -> DatasetReference:
        return DatasetReference(
            dataset_id=dataset.dataset_id,
            version=dataset.version,
            path=str(destination),
            candle_count=len(dataset.candles),
            candles_sha256=candle_hash,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        )


def dataset_from_canonical_csv(
    csv_path: str | Path,
    *,
    dataset_id: str,
    market: Market,
    interval: timedelta,
    source_name: str,
    source_uri: str,
    source_revision: str,
    license_id: str,
    license_uri: str,
    collected_at: datetime,
    allow_gaps: bool = False,
) -> HistoricalDataset:
    """Import a canonical OHLCV CSV from a previously authorized source export.

    This function does not fetch data or infer a license. The input CSV must use the exact
    seven-column schema written by HistoricalDataset.candles_csv_bytes, and all provenance
    fields must be supplied by the caller.
    """
    try:
        candle_bytes = Path(csv_path).expanduser().read_bytes()
    except OSError as exc:
        raise ValueError("Authorized historical CSV cannot be read.") from exc
    candles = _candles_from_bytes(candle_bytes, market, interval)
    return HistoricalDataset(
        dataset_id=dataset_id,
        market=market,
        interval=interval,
        source_name=source_name,
        source_uri=source_uri,
        source_revision=source_revision,
        license_id=license_id,
        license_uri=license_uri,
        collected_at=collected_at,
        candles=candles,
        allow_gaps=allow_gaps,
    )


def _candles_from_bytes(
    candle_bytes: bytes,
    market: Market,
    interval: timedelta,
) -> tuple[MarketCandle, ...]:
    """Decode exact canonical columns into validated domain candles."""
    text = candle_bytes.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != _CANDLE_COLUMNS:
        raise ValueError("Unexpected candle CSV columns.")
    candles: list[MarketCandle] = []
    for row in reader:
        if None in row:
            raise ValueError("Malformed candle row.")
        timestamp = _required_string(row.get("timestamp"), "timestamp")
        open_value = _required_string(row.get("open"), "open")
        high_value = _required_string(row.get("high"), "high")
        low_value = _required_string(row.get("low"), "low")
        close_value = _required_string(row.get("close"), "close")
        volume_value = _required_string(row.get("volume"), "volume")
        source = _required_string(row.get("source"), "source")
        candles.append(
            MarketCandle(
                market=market,
                timestamp=_parse_datetime(timestamp),
                interval=interval,
                open=_parse_decimal(open_value),
                high=_parse_decimal(high_value),
                low=_parse_decimal(low_value),
                close=_parse_decimal(close_value),
                volume=_parse_decimal(volume_value),
                source=source,
            )
        )
    return tuple(candles)


def _dataset_from_bytes(metadata: dict[str, Any], candle_bytes: bytes) -> HistoricalDataset:
    """Strictly rebuild a dataset from a manifest and canonical candle file."""
    try:
        market_data = metadata["market"]
        if not isinstance(market_data, dict):
            raise ValueError("market must be an object")
        market = Market(
            symbol=_required_string(market_data.get("symbol"), "symbol"),
            venue=_required_string(market_data.get("venue"), "venue"),
            market_type=_required_string(market_data.get("market_type"), "market_type"),
        )
        interval_us = metadata["interval_microseconds"]
        if isinstance(interval_us, bool) or not isinstance(interval_us, int) or interval_us <= 0:
            raise ValueError("interval_microseconds must be a positive integer")
        candles = _candles_from_bytes(
            candle_bytes, market, timedelta(microseconds=interval_us)
        )
        dataset = HistoricalDataset(
            dataset_id=_required_string(metadata.get("dataset_id"), "dataset_id"),
            market=market,
            interval=timedelta(microseconds=interval_us),
            source_name=_required_string(metadata.get("source_name"), "source_name"),
            source_uri=_required_string(metadata.get("source_uri"), "source_uri"),
            source_revision=_required_string(metadata.get("source_revision"), "source_revision"),
            license_id=_required_string(metadata.get("license_id"), "license_id"),
            license_uri=_required_string(metadata.get("license_uri"), "license_uri"),
            collected_at=_parse_datetime(
                _required_string(metadata.get("collected_at"), "collected_at")
            ),
            candles=candles,
            allow_gaps=_required_bool(metadata.get("allow_gaps"), "allow_gaps"),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise DatasetIntegrityError("Dataset metadata or candle rows are invalid.") from exc
    if dataset.metadata() != metadata:
        raise DatasetIntegrityError("Manifest metadata differs from reconstructed records.")
    return dataset


def _canonical_json_bytes(value: object) -> bytes:
    """Encode stable JSON with no floating-point financial values."""
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _iso_utc(value: datetime) -> str:
    """Format an aware timestamp as fixed-precision UTC with Z suffix."""
    _require_aware(value, "timestamp")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Timestamp must be a non-empty string.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require_aware(parsed, "timestamp")
    return parsed


def _parse_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid Decimal in candle file.") from exc
    if not parsed.is_finite():
        raise ValueError("Financial/data Decimal values must be finite.")
    return parsed


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string.")
    return value


def _required_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be an explicit boolean.")
    return value


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone.")
