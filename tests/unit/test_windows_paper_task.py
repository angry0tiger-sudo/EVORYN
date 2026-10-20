"""Tests for the offline-only Paper task entrypoint and Windows Scheduler boundary."""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.windows_paper_task import (
    DEFAULT_TASK_NAME,
    OfflineCsvPaperFeed,
    PaperTaskConfigurationError,
    _read_csv_candles,
    _task_name_is_owned,
    load_and_validate_config,
    preflight,
)

INTERVAL = timedelta(hours=1)


def _write_fixture(tmp_path: Path, *, stale: bool = False, gap: bool = False) -> Path:
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    now = datetime.now(UTC)
    last_open = now.replace(minute=0, second=0, microsecond=0) - INTERVAL
    if stale:
        last_open -= timedelta(days=3)
    timestamps = [last_open - 2 * INTERVAL, last_open - INTERVAL, last_open]
    if gap:
        timestamps[1] -= INTERVAL
    path = data / "candles.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("timestamp", "open", "high", "low", "close", "volume"))
        for index, stamp in enumerate(timestamps):
            close = str(100 + index)
            writer.writerow((stamp.isoformat(), close, str(101 + index), str(99 + index), close, "10"))
    return path


def _write_config(tmp_path: Path, csv_path: Path, *, extra: dict[str, object] | None = None) -> Path:
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    payload: dict[str, object] = {
        "schema_version": 1,
        "runtime_mode": "paper_only",
        "simulation_only": True,
        "market_symbol": "BTC-USDT",
        "interval_seconds": 3600,
        "cycle_interval_seconds": 60,
        "max_market_data_age_seconds": 3600,
        "initial_capital": "10000",
        "max_total_exposure": "20000",
        "max_market_exposure": "15000",
        "max_order_notional": "5000",
        "max_loss": "2000",
        "csv_path": str(csv_path),
        "journal_path": str(data / "paper.sqlite3"),
        "log_path": str(logs / "paper.log"),
    }
    if extra:
        payload.update(extra)
    path = tmp_path / "paper-config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_valid_config_and_fresh_offline_csv_preflight(tmp_path: Path) -> None:
    candles_path = _write_fixture(tmp_path)
    config_path = _write_config(tmp_path, candles_path)

    config = load_and_validate_config(config_path)
    assert config["runtime_mode"] == "paper_only"
    assert config["simulation_only"] is True
    report = preflight(config_path)

    assert report["status"] == "ready"
    assert report["champion"] == "none"
    assert report["selected_model_for_runtime"] == "null"
    assert report["live_connection"] == "disabled"
    assert report["real_orders"] == "disabled"


def test_offline_csv_feed_builds_simulation_only_snapshot(tmp_path: Path) -> None:
    candles_path = _write_fixture(tmp_path)
    market = __import__("evoryn.domain.models", fromlist=["Market"]).Market(
        symbol="BTC-USDT", venue="nobitex", market_type="spot"
    )
    feed = OfflineCsvPaperFeed(market=market, interval=INTERVAL, csv_path=candles_path)
    now = datetime.now(UTC)
    candles = feed.get_completed_candles(market, INTERVAL, now=now)
    snapshot = feed.get_snapshot(market)

    assert len(candles) == 3
    assert feed.simulation_only is True
    assert snapshot.source == "evoryn-offline-csv-paper-only-v1"
    assert "no_exchange_connection" in snapshot.quality_flags
    assert snapshot.values["bid"] < snapshot.values["last"] < snapshot.values["ask"]


@pytest.mark.parametrize("stale,gap", [(True, False), (False, True)])
def test_offline_csv_rejects_stale_or_gapped_data(tmp_path: Path, stale: bool, gap: bool) -> None:
    path = _write_fixture(tmp_path, stale=stale, gap=gap)
    from evoryn.domain.models import Market

    market = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
    with pytest.raises(ValueError):
        _read_csv_candles(path, market, INTERVAL)
    if stale:
        config = _write_config(tmp_path, path)
        with pytest.raises(PaperTaskConfigurationError, match="stale"):
            preflight(config)


def test_config_rejects_live_mode_and_unknown_model_fields(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    live_config = _write_config(tmp_path, path, extra={"simulation_only": False})
    with pytest.raises(PaperTaskConfigurationError, match="paper_only"):
        load_and_validate_config(live_config)

    model_config = _write_config(tmp_path, path, extra={"model_path": "champion.joblib"})
    with pytest.raises(PaperTaskConfigurationError, match="Unknown configuration keys"):
        load_and_validate_config(model_config)


def test_config_rejects_unc_source_path(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    config = _write_config(tmp_path, path, extra={"csv_path": r"\\server\share\candles.csv"})
    with pytest.raises(PaperTaskConfigurationError, match="UNC/network"):
        load_and_validate_config(config)


def test_task_name_scope_is_narrow_and_explicit() -> None:
    assert _task_name_is_owned(DEFAULT_TASK_NAME)
    assert _task_name_is_owned("EVORYN-CI-PaperOnly-1234-abcd")
    assert not _task_name_is_owned("Microsoft-Windows-Update")
    assert not _task_name_is_owned("EVORYN-LiveTrading")


@pytest.mark.skipif(sys.platform != "win32", reason="Native Task Scheduler integration runs only on Windows CI.")
def test_windows_task_scheduler_register_status_unregister_round_trip(tmp_path: Path) -> None:
    from scripts.windows_paper_task import _run_windows_task_action

    csv_path = _write_fixture(tmp_path)
    config_path = _write_config(tmp_path, csv_path)
    task_name = f"EVORYN-CI-PaperOnly-{os.getpid()}-{datetime.now(UTC).strftime('%H%M%S')}"

    try:
        assert _run_windows_task_action("register", config_path, task_name) == 0
        assert _run_windows_task_action("status", config_path, task_name) == 0
    finally:
        assert _run_windows_task_action("unregister", config_path, task_name) == 0
