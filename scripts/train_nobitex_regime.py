"""Fetch public Nobitex OHLCV and train/evaluate a research-only regime model.

Only the public history GET route is used. No API token, account query, order endpoint, or order
submission exists in this script. All outputs are ephemeral research artifacts uploaded by CI.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from evoryn.domain.models import Market
from evoryn.intelligence.regime_training import RegimeTrainingConfig, train_regime_model
from evoryn.learning_lab.historical_data import HistoricalDataset, HistoricalDatasetStore
from evoryn.learning_lab.nobitex_history import (
    NOBITEX_HISTORY_URI,
    NobitexHistoricalClient,
    NobitexHistoryConfig,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT", help="Compact public Nobitex market symbol.")
    parser.add_argument(
        "--resolution",
        default="60",
        help="UDF candle resolution; default is 1 hour.",
    )
    parser.add_argument("--lookback-days", type=int, default=180)
    parser.add_argument("--max-pages", type=int, default=24)
    parser.add_argument("--output", type=Path, default=Path("artifacts/nobitex-regime"))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    now = datetime.now(UTC)
    if not args.symbol.endswith("USDT") or len(args.symbol) <= 4:
        raise SystemExit("--symbol must be a compact USDT-quoted Nobitex market, e.g. BTCUSDT.")
    if not 30 <= args.lookback_days <= 365:
        raise SystemExit("--lookback-days must be in [30, 365].")
    interval_seconds = {
        "1": 60,
        "5": 300,
        "15": 900,
        "30": 1800,
        "60": 3600,
        "180": 10800,
        "240": 14400,
        "360": 21600,
        "720": 43200,
        "D": 86400,
        "2D": 172800,
        "3D": 259200,
    }.get(args.resolution)
    if interval_seconds is None:
        raise SystemExit("Unsupported documented UDF resolution.")
    to_unix = int(now.timestamp())
    from_unix = to_unix - args.lookback_days * 86400
    from_unix -= from_unix % interval_seconds
    config = NobitexHistoryConfig(
        symbol=args.symbol,
        resolution=args.resolution,
        from_unix_seconds=from_unix,
        to_unix_seconds=to_unix,
        max_pages=args.max_pages,
        timeout_seconds=10,
        throttle_seconds=1.05,
        minimum_candles=1000,
    )
    market = Market(
        symbol=f"{args.symbol[:-4]}-{args.symbol[-4:]}",
        venue="nobitex",
        market_type="spot",
    )
    client = NobitexHistoricalClient(config)
    candles = client.fetch(market)
    collected_at = datetime.now(UTC)
    request_uri = (
        f"{NOBITEX_HISTORY_URI}?symbol={args.symbol}&resolution={args.resolution}"
        f"&from={from_unix}&to={to_unix}"
    )
    dataset = HistoricalDataset(
        dataset_id=f"nobitex-{market.symbol.lower()}-{args.resolution}m",
        market=market,
        interval=timedelta(seconds=interval_seconds),
        volume_unit="base_asset",
        source_name="nobitex-public-ohlcv",
        source_uri=request_uri,
        source_revision=f"udf-history:{args.symbol}:{args.resolution}:{from_unix}:{to_unix}",
        license_id="UNREVIEWED_PROVIDER_TERMS",
        license_uri="https://apidocs.nobitex.ir/",
        collected_at=collected_at,
        candles=candles,
        allow_gaps=False,
    )
    output = args.output.expanduser().resolve()
    store = HistoricalDatasetStore(output / "datasets")
    dataset_reference = store.save(dataset)
    report = train_regime_model(
        dataset,
        artifact_dir=output,
        config=RegimeTrainingConfig(
            feature_lookback=20,
            horizon_bars=6,
            train_fraction=0.60,
            validation_fraction=0.20,
            trend_quantile=0.65,
            volatility_quantile=0.75,
            min_class_samples=8,
            random_seed=41,
            confidence_threshold=Decimal("0.65"),
        ),
    )
    dataset_directory = Path(dataset_reference.path)
    report["dataset"]["dataset_manifest_status"] = "content_addressed_and_verified"
    report["dataset_store"] = {
        "dataset_id": dataset_reference.dataset_id,
        "dataset_version": dataset_reference.version,
        "candle_count": dataset_reference.candle_count,
        "candles_sha256": dataset_reference.candles_sha256,
        "manifest_sha256": dataset_reference.manifest_sha256,
        "manifest_path": str((dataset_directory / "manifest.json").relative_to(output)),
        "csv_path": str((dataset_directory / "candles.csv").relative_to(output)),
    }
    report["download"] = {
        "public_endpoint": NOBITEX_HISTORY_URI,
        "authenticated": False,
        "http_method": "GET",
        "route_scope": "/market/udf/history",
        "pages_fetched": client.pages_fetched,
        "configured_page_limit": args.max_pages,
        "configured_lookback_days": args.lookback_days,
    }
    report_path = output / "training-report.json"
    report_bytes = json.dumps(
        report, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    report_path.write_bytes(report_bytes + b"\n")
    print(f"DATASET_VERSION={dataset.version}")
    print(f"CANDLE_COUNT={len(dataset.candles)}")
    print(f"PAGES_FETCHED={client.pages_fetched}")
    print(f"FIRST_CANDLE={dataset.candles[0].timestamp.isoformat()}")
    print(f"LAST_CANDLE={dataset.candles[-1].timestamp.isoformat()}")
    print(f"DATASET_SHA256={dataset_reference.candles_sha256}")
    print(f"MODEL_SHA256={report['model_artifact_sha256']}")
    print(f"TRAINING_REPORT={output / 'training-report.json'}")
    for strategy_id, result in report["backtests"].items():
        metrics = result["metrics"]
        print(
            "BACKTEST "
            f"{strategy_id} "
            f"return={metrics['net_return_fraction']} "
            f"max_drawdown={metrics['max_drawdown_fraction']} "
            f"closed_trades={metrics['closed_trade_count']}"
        )
    print("RELEASE_DISPOSITION=RESEARCH_ONLY_NOT_APPROVED_FOR_LIVE_TRADING")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
