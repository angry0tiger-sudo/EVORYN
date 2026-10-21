"""Run the pinned Phase 32 study against a verified local research dataset.

No market data is fetched, no credential is read, and no exchange/account/order endpoint is used.
The resulting report and model/calibrator artifacts are written once and checksum-sealed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evoryn.learning_lab.cross_fitted_meta_label import (
    CrossFittedMetaConfig,
    _canonical_json_bytes,
    run_cross_fitted_meta_label_research,
)
from evoryn.learning_lab.historical_data import HistoricalDatasetStore


def _write_immutable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise FileExistsError(f"Refusing to overwrite immutable artifact: {path}")
        return
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _load_dataset(
    root: Path,
    dataset_id: str,
    dataset_version: str,
) -> tuple[Any, str, str]:
    dataset = HistoricalDatasetStore(root).load(dataset_id, dataset_version)
    manifest_path = root / dataset_id / dataset_version / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    candle_hash = hashlib.sha256(dataset.candles_csv_bytes()).hexdigest()
    if (
        not isinstance(manifest, dict)
        or manifest.get("dataset_version") != dataset.version
        or manifest.get("candles_sha256") != candle_hash
        or manifest.get("metadata") != dataset.metadata()
    ):
        raise ValueError("Pinned dataset manifest and canonical candle bytes do not match.")
    return dataset, hashlib.sha256(manifest_bytes).hexdigest(), candle_hash


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=41)
    parser.add_argument("--expected-folds", type=int, default=8)
    parser.add_argument("--train-events", type=int, default=30)
    parser.add_argument("--validation-events", type=int, default=30)
    parser.add_argument("--test-events", type=int, default=10)
    parser.add_argument("--step-events", type=int, default=10)
    parser.add_argument("--embargo-bars", type=int, default=24)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    dataset, manifest_sha256, candle_sha256 = _load_dataset(
        args.dataset_root.expanduser().resolve(),
        args.dataset_id,
        args.dataset_version,
    )
    config = CrossFittedMetaConfig(
        random_seed=args.seed,
        expected_fold_count=args.expected_folds,
        train_event_count=args.train_events,
        validation_event_count=args.validation_events,
        test_event_count=args.test_events,
        step_event_count=args.step_events,
        embargo_bars=args.embargo_bars,
    )
    output = args.output.expanduser().resolve()
    report = run_cross_fitted_meta_label_research(
        dataset,
        output_dir=output,
        config=config,
    )
    report["dataset"]["dataset_manifest_sha256"] = manifest_sha256
    report["dataset"]["candle_csv_sha256"] = candle_sha256
    report_bytes = _canonical_json_bytes(report)
    report_hash = hashlib.sha256(report_bytes).hexdigest()
    report_path = output / "report.json"
    _write_immutable(report_path, report_bytes)
    _write_immutable(
        report_path.with_suffix(".json.sha256"),
        f"{report_hash}  {report_path.name}\n".encode(),
    )
    model_hashes = {
        key: value["model_artifacts"]
        for key, value in report["models"].items()
    }
    index = {
        "phase": 32,
        "report_path": report_path.name,
        "report_sha256": report_hash,
        "dataset_version": dataset.version,
        "dataset_manifest_sha256": manifest_sha256,
        "candle_csv_sha256": candle_sha256,
        "model_artifacts": model_hashes,
        "champion": None,
        "selected_model_for_runtime": None,
        "runtime_promotion_allowed": False,
    }
    _write_immutable(output / "artifact-index.json", _canonical_json_bytes(index))
    print(json.dumps(index, sort_keys=True))
    # A failed economic gate is an honest, valid research result, not a CI failure.
    # Structural exceptions, incomplete output integrity, or code/test errors fail the job.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
