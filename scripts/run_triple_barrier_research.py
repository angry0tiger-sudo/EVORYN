"""Build an immutable, local-only Phase 31 Triple-Barrier research report.

The command reads a previously content-addressed HistoricalDatasetStore entry. It never
fetches data, loads credentials, trains a model, reaches an exchange, or changes Runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from evoryn.learning_lab.historical_data import HistoricalDataset, HistoricalDatasetStore
from evoryn.learning_lab.triple_barrier import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    TRIPLE_BARRIER_SCHEMA_VERSION,
    TripleBarrierConfig,
    TripleBarrierEvent,
    build_triple_barrier_events,
    label_distribution,
    make_purged_event_folds,
)


def _canonical_json_bytes(value: object) -> bytes:
    """Return the deterministic UTF-8 JSON representation used for report identities."""
    return (
        json.dumps(
            value,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _event_ledger_sha256(events: Sequence[TripleBarrierEvent]) -> str:
    """Hash the reproducible event/feature/label ledger without publishing per-event prices."""
    ledger = [
        {
            "event_id": event.event_id,
            "event_index": event.event_index,
            "event_timestamp": event.event_timestamp.isoformat(),
            "label_start_index": event.label_start_index,
            "label_end_index": event.label_end_index,
            "label_start_timestamp": event.label_start_timestamp.isoformat(),
            "label_end_timestamp": event.label_end_timestamp.isoformat(),
            "reference_price": str(event.reference_price),
            "profit_taking_price": str(event.profit_taking_price),
            "stop_loss_price": str(event.stop_loss_price),
            "trailing_volatility": event.trailing_volatility,
            "feature_names": event.feature_names,
            "feature_values": event.feature_values,
            "feature_schema_version": event.feature_schema_version,
            "outcome": event.outcome.value,
            "exit_price": str(event.exit_price),
            "gross_return_fraction": event.gross_return_fraction,
            "net_return_fraction": event.net_return_fraction,
            "round_trip_cost_bps": event.round_trip_cost_bps,
            "holding_bars": event.holding_bars,
            "ambiguous_horizontal_touch": event.ambiguous_horizontal_touch,
        }
        for event in events
    ]
    return hashlib.sha256(_canonical_json_bytes(ledger)).hexdigest()


def _verify_dataset_manifest(dataset: HistoricalDataset, manifest_path: Path) -> tuple[str, str]:
    """Verify the source manifest and return exact manifest/candle hashes."""
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("The selected dataset manifest is unreadable.") from exc
    candle_bytes = dataset.candles_csv_bytes()
    candle_sha256 = hashlib.sha256(candle_bytes).hexdigest()
    if (
        not isinstance(manifest, dict)
        or manifest.get("dataset_version") != dataset.version
        or manifest.get("candles_sha256") != candle_sha256
        or manifest.get("metadata") != dataset.metadata()
    ):
        raise ValueError("Dataset manifest and canonical candle bytes do not match.")
    return hashlib.sha256(manifest_bytes).hexdigest(), candle_sha256


def _build_report(
    dataset: HistoricalDataset,
    *,
    manifest_sha256: str,
    candle_sha256: str,
    config: TripleBarrierConfig,
    expected_fold_count: int,
    train_event_count: int,
    validation_event_count: int,
    test_event_count: int,
    step_event_count: int,
    embargo_bars: int,
) -> dict[str, Any]:
    """Build a summary-only report with event-ledger integrity and an explicit no-model state."""
    built = build_triple_barrier_events(dataset.candles, config)
    folds = make_purged_event_folds(
        built.events,
        train_event_count=train_event_count,
        validation_event_count=validation_event_count,
        test_event_count=test_event_count,
        step_event_count=step_event_count,
        expected_fold_count=expected_fold_count,
        embargo_bars=embargo_bars,
    )
    fold_rows = [
        {
            "fold_index": fold.fold_index,
            "status": fold.status,
            "train_event_count_after_purge": len(fold.train_events),
            "validation_event_count_after_purge": len(fold.validation_events),
            "test_event_count": len(fold.test_events),
            "purged_train_count": fold.purged_train_count,
            "purged_validation_count": fold.purged_validation_count,
            "embargo_bars": fold.embargo_bars,
        }
        for fold in folds.folds
    ]
    event_counts = label_distribution(built.events)
    experiment_configuration = {
        "triple_barrier_schema_version": TRIPLE_BARRIER_SCHEMA_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "config": asdict(config),
        "split_plan": {
            "train_event_count": train_event_count,
            "validation_event_count": validation_event_count,
            "test_event_count": test_event_count,
            "step_event_count": step_event_count,
            "expected_fold_count": expected_fold_count,
            "embargo_bars": embargo_bars,
        },
    }
    configuration_sha256 = hashlib.sha256(
        _canonical_json_bytes(experiment_configuration)
    ).hexdigest()
    return {
        "schema_version": 1,
        "experiment": {
            "phase": 31,
            "name": "triple-barrier-event-labeling",
            "configuration_sha256": configuration_sha256,
            "research_only": True,
        },
        "dataset": {
            "dataset_id": dataset.dataset_id,
            "dataset_version": dataset.version,
            "candle_count": len(dataset.candles),
            "candles_sha256": candle_sha256,
            "manifest_sha256": manifest_sha256,
            "market": dataset.metadata()["market"],
            "first_candle_open": dataset.metadata()["first_candle_open"],
            "last_candle_open": dataset.metadata()["last_candle_open"],
            "gap_count": dataset.gap_count,
            "allow_gaps": dataset.allow_gaps,
            "volume_unit": dataset.volume_unit,
            "volume_features_used": False,
            "source_name": dataset.source_name,
            "source_revision": dataset.source_revision,
            "license_id": dataset.license_id,
            "data_use_review": "UNREVIEWED",
        },
        "configuration": experiment_configuration,
        "feature_contract": {
            "feature_names": FEATURE_NAMES,
            "schema_version": FEATURE_SCHEMA_VERSION,
            "information_set": "completed bars at or before event time only",
            "future_ohlc_used_for_features": False,
            "future_ohlc_used_for_label_only": True,
        },
        "labeling": {
            "detected_bullish_crossovers": built.detected_bullish_crossovers,
            "emitted_event_count": built.emitted_event_count,
            "incomplete_horizon_skipped": built.incomplete_horizon_skipped,
            "zero_volatility_skipped": built.zero_volatility_skipped,
            "invalid_barrier_geometry_skipped": built.invalid_barrier_geometry_skipped,
            "label_interval_semantics": "inclusive event bar index through outcome bar index",
            "same_bar_horizontal_tie": "conservative stop-loss, adverse stop gap where applicable",
            "favorable_target_gap": "capped at frozen target price",
            "event_ledger_sha256": _event_ledger_sha256(built.events),
            "outcome_distribution_and_event_returns": event_counts,
            "round_trip_cost_bps": config.round_trip_cost_bps,
            "cost_interpretation": (
                "fixed simulation assumption, not measured spread/slippage "
                "or realized fills"
            ),
        },
        "purged_chronological_splits": {
            "expected_fold_count": folds.expected_fold_count,
            "actual_fold_count": folds.actual_fold_count,
            "coverage_complete": folds.coverage_complete,
            "folds": fold_rows,
            "split_order": "expanding chronological train, validation, then test",
            "purge_rule": "remove training labels overlapping evaluation intervals",
            "embargo_rule": "expand each evaluation interval by embargo_bars on both sides",
        },
        "model": {
            "training_executed": False,
            "model_sha256": None,
            "not_applicable_reason": (
                "Phase 31 creates labels and split manifests; no model is trained."
            ),
        },
        "acceptance": {
            "phase31_labeling_only": True,
            "split_coverage_gate_passed": folds.coverage_complete,
            "economic_gate": "NOT_EVALUATED_LABELING_PHASE_ONLY",
            "champion": None,
            "selected_model_for_runtime": None,
            "runtime_promotion_allowed": False,
        },
        "limitations": [
            "Public-provider data-use terms have not been reviewed.",
            "Historical volume units are unverified and volume is not used as a feature.",
            "Historical order-book depth is unavailable.",
            "Fixed cost assumptions are not observed spreads, slippage, or executable fills.",
            (
                "Event-return summaries are labels, not portfolio returns "
                "or evidence of profitability."
            ),
        ],
    }


def _write_immutable(path: Path, content: bytes) -> None:
    """Create a file once; permit exact idempotent reuse but refuse conflicting overwrites."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise FileExistsError(f"Refusing to overwrite immutable research artifact: {path}")
        return
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-fold-count", type=int, default=5)
    parser.add_argument("--train-events", type=int, default=20)
    parser.add_argument("--validation-events", type=int, default=5)
    parser.add_argument("--test-events", type=int, default=5)
    parser.add_argument("--step-events", type=int, default=5)
    parser.add_argument("--embargo-bars", type=int, default=24)
    parser.add_argument("--round-trip-cost-bps", type=float, default=30.0)
    parser.add_argument("--vertical-horizon-bars", type=int, default=24)
    parser.add_argument("--profit-taking-multiple", type=float, default=1.5)
    parser.add_argument("--stop-loss-multiple", type=float, default=1.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Load a pinned local dataset, build a deterministic report, and write its SHA sidecar."""
    args = _parser().parse_args(argv)
    dataset_root = args.dataset_root.expanduser().resolve()
    dataset = HistoricalDatasetStore(dataset_root).load(args.dataset_id, args.dataset_version)
    manifest_path = dataset_root / args.dataset_id / args.dataset_version / "manifest.json"
    manifest_sha256, candle_sha256 = _verify_dataset_manifest(dataset, manifest_path)
    config = TripleBarrierConfig(
        vertical_horizon_bars=args.vertical_horizon_bars,
        profit_taking_multiple=args.profit_taking_multiple,
        stop_loss_multiple=args.stop_loss_multiple,
        round_trip_cost_bps=args.round_trip_cost_bps,
    )
    report = _build_report(
        dataset,
        manifest_sha256=manifest_sha256,
        candle_sha256=candle_sha256,
        config=config,
        expected_fold_count=args.expected_fold_count,
        train_event_count=args.train_events,
        validation_event_count=args.validation_events,
        test_event_count=args.test_events,
        step_event_count=args.step_events,
        embargo_bars=args.embargo_bars,
    )
    report_bytes = _canonical_json_bytes(report)
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    report_path = args.output.expanduser().resolve()
    _write_immutable(report_path, report_bytes)
    sidecar = f"{report_sha256}  {report_path.name}\n".encode()
    _write_immutable(report_path.with_suffix(report_path.suffix + ".sha256"), sidecar)
    print(json.dumps({"report": str(report_path), "report_sha256": report_sha256}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
