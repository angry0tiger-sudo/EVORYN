"""Run rolling walk-forward regime research on the verified saved dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from evoryn.intelligence.regime_walk_forward import (
    WalkForwardResearchConfig,
    run_walk_forward_research,
)
from evoryn.learning_lab.historical_data import HistoricalDatasetStore


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/nobitex-regime"),
        help="Root created by scripts/train_nobitex_regime.py.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    output = args.output.expanduser().resolve()
    report_path = output / "training-report.json"
    if not report_path.is_file():
        raise SystemExit(
            "Training report is missing; run the verified public-history trainer first."
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    dataset_meta = report.get("dataset")
    if not isinstance(dataset_meta, dict):
        raise SystemExit("Training report has no dataset identity.")
    dataset_id = dataset_meta.get("dataset_id")
    dataset_version = dataset_meta.get("dataset_version")
    if not isinstance(dataset_id, str) or not isinstance(dataset_version, str):
        raise SystemExit("Training report dataset identity is invalid.")
    dataset = HistoricalDatasetStore(output / "datasets").load(dataset_id, dataset_version)
    count = len(dataset.candles)
    train_bars = int(count * 0.40)
    validation_bars = int(count * 0.10)
    test_bars = int(count * 0.05)
    config = WalkForwardResearchConfig(
        feature_lookback=48,
        horizon_bars=6,
        train_bars=train_bars,
        validation_bars=validation_bars,
        test_bars=test_bars,
        step_bars=test_bars,
        lstm_epochs=18,
        trend_quantile=0.65,
        volatility_quantile=0.75,
        triple_barrier_multiplier=1.5,
        min_class_samples=12,
        random_seed=41,
        confidence_threshold=0.65,
    )
    report_value = run_walk_forward_research(
        dataset,
        output_dir=output / "walk-forward",
        config=config,
    )
    output_path = output / "walk-forward" / "walk-forward-report.json"
    digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
    print(f"WALK_FORWARD_EXPERIMENT_ID={report_value['experiment_id']}")
    print(f"WALK_FORWARD_DATASET_VERSION={dataset.version}")
    print(f"WALK_FORWARD_FOLD_COUNT={report_value['protocol']['fold_count']}")
    print(f"WALK_FORWARD_REPORT_SHA256={digest}")
    print(f"WALK_FORWARD_REPORT={output_path}")
    print(f"VOLUME_SEMANTICS={report_value['dataset']['volume_semantics_status']}")
    print("RELEASE_DISPOSITION=RESEARCH_ONLY_NOT_APPROVED_FOR_LIVE_TRADING")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
