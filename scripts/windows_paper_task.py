"""Fail-closed, offline-only Windows Task Scheduler entrypoint for EVORYN Paper mode.

This script deliberately has no HTTP client, exchange adapter, account credential, model loader,
or live-order route. It consumes a local CSV and uses a no-Champion abstaining classifier.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event
from typing import Any

from evoryn.domain.models import Market, MarketCandle, MarketSnapshot
from evoryn.intelligence.regime import RegimeLabel, RegimePrediction
from evoryn.risk.models import RiskLimits
from evoryn.runtime.paper import (
    PaperRuntimeSupervisor,
    PaperStrategyRuntime,
    SQLitePaperRuntimeLedger,
)
from evoryn.runtime.scheduler import PaperRuntimeScheduler
from evoryn.runtime.single_instance import PaperRuntimeLocks, RuntimeAlreadyRunning
from evoryn.strategies.plugins import build_baseline_registry
from evoryn.strategies.selector import StrategySelectionPolicy

DEFAULT_TASK_NAME = "EVORYN-PaperOnly"
_TASK_SCRIPT = Path(__file__).with_name("windows_paper_task.ps1")
_ALLOWED_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "runtime_mode",
        "simulation_only",
        "market_symbol",
        "interval_seconds",
        "cycle_interval_seconds",
        "max_market_data_age_seconds",
        "initial_capital",
        "max_total_exposure",
        "max_market_exposure",
        "max_order_notional",
        "max_loss",
        "csv_path",
        "journal_path",
        "log_path",
    }
)
_CSV_FIELDS = ("timestamp", "open", "high", "low", "close", "volume")
_MAX_CSV_BYTES = 16 * 1024 * 1024
_MAX_CSV_ROWS = 10_000
_SOURCE = "evoryn-offline-csv-paper-only-v1"
_SPREAD_FRACTION = Decimal("0.0005")


class PaperTaskConfigurationError(ValueError):
    """Configuration is absent, unsafe, ambiguous, or not Paper-only."""


class OfflineCsvPaperFeed:
    """Local CSV market data source. It never opens sockets or reads environment credentials."""

    simulation_only = True

    def __init__(
        self,
        *,
        market: Market,
        interval: timedelta,
        csv_path: Path,
        max_stale_intervals: int = 2,
        clock: Any = None,
    ) -> None:
        self.market = market
        self.interval = interval
        self.csv_path = csv_path
        self.max_stale_intervals = max_stale_intervals
        self._clock = clock or (lambda: datetime.now(UTC))

    def get_snapshot(self, market: Market) -> MarketSnapshot:
        if market != self.market:
            raise ValueError("Offline feed market does not match the requested market.")
        now = _utc_now(self._clock)
        candles = self._completed_candles(now)
        close = candles[-1].close
        bid = close * (Decimal("1") - _SPREAD_FRACTION)
        ask = close * (Decimal("1") + _SPREAD_FRACTION)
        return MarketSnapshot(
            market=self.market,
            observed_at=now,
            source=_SOURCE,
            values={"bid": bid, "ask": ask, "last": close},
            quality_flags=("simulation_only", "offline_csv", "no_exchange_connection"),
        )

    def get_history(
        self,
        market: Market,
        start: datetime,
        end: datetime,
    ) -> Sequence[MarketSnapshot]:
        if market != self.market:
            raise ValueError("Offline feed market does not match the requested market.")
        if start.tzinfo is None or end.tzinfo is None or end < start:
            raise ValueError("Offline feed history bounds must be ordered aware timestamps.")
        return ()

    def get_completed_candles(
        self,
        market: Market,
        interval: timedelta,
        *,
        now: datetime,
    ) -> Sequence[MarketCandle]:
        if market != self.market or interval != self.interval:
            raise ValueError("Offline feed market or interval does not match the request.")
        return self._completed_candles(_utc_now(lambda: now))

    def _completed_candles(self, now: datetime) -> tuple[MarketCandle, ...]:
        candles = _read_csv_candles(self.csv_path, self.market, self.interval)
        completed = tuple(c for c in candles if c.timestamp + c.interval <= now)
        if not completed:
            raise ValueError("Offline Paper CSV has no completed candles.")
        last_close_at = completed[-1].timestamp + completed[-1].interval
        if now < last_close_at:
            raise ValueError("Offline Paper CSV has a future-dated last candle.")
        if now - last_close_at > self.interval * self.max_stale_intervals:
            raise ValueError("Offline Paper CSV is stale; runtime must fail closed.")
        return completed


class NoChampionClassifier:
    """Abstain on every candle until a Champion is separately approved."""

    def classify(
        self,
        completed_history: Sequence[MarketCandle],
        *,
        now: datetime,
    ) -> RegimePrediction:
        as_of = (
            completed_history[-1].timestamp + completed_history[-1].interval
            if completed_history
            else _utc_now(lambda: now)
        )
        return RegimePrediction(
            label=RegimeLabel.UNKNOWN,
            confidence=Decimal("0"),
            confidence_semantics="heuristic_score",
            probabilities=(),
            as_of=as_of,
            model_id="evoryn.no-champion",
            model_version="none-selected",
            feature_schema_version="no-model-no-signals-v1",
            model_fingerprint="0" * 64,
            min_confidence_threshold=Decimal("0.65"),
            max_data_age_intervals=2,
            abstained=True,
            reason_code="NO_CHAMPION_SELECTED",
        )


def load_and_validate_config(config_path: Path) -> dict[str, Any]:
    """Load the exact allow-listed schema; reject unknown fields and network-backed paths."""
    if not config_path.is_file() or config_path.is_symlink():
        raise PaperTaskConfigurationError("Configuration must be an existing regular local file.")
    if config_path.stat().st_size > 64 * 1024:
        raise PaperTaskConfigurationError("Configuration file exceeds 64 KiB.")
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PaperTaskConfigurationError("Configuration is unreadable or invalid JSON.") from exc
    if not isinstance(value, dict):
        raise PaperTaskConfigurationError("Configuration root must be a JSON object.")
    unknown = set(value) - _ALLOWED_CONFIG_KEYS
    missing = _ALLOWED_CONFIG_KEYS - set(value)
    if unknown:
        raise PaperTaskConfigurationError(f"Unknown configuration keys: {sorted(unknown)}.")
    if missing:
        raise PaperTaskConfigurationError(
            f"Required configuration keys are missing: {sorted(missing)}."
        )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PaperTaskConfigurationError("schema_version must be integer 1.")
    if value["runtime_mode"] != "paper_only" or value["simulation_only"] is not True:
        raise PaperTaskConfigurationError(
            "Only runtime_mode=paper_only and simulation_only=true are accepted."
        )
    if value["market_symbol"] != "BTC-USDT":
        raise PaperTaskConfigurationError("The offline Paper task is restricted to BTC-USDT.")
    interval_seconds = _positive_int(value, "interval_seconds")
    if interval_seconds not in {60, 300, 900, 1800, 3600, 14400, 86400}:
        raise PaperTaskConfigurationError("interval_seconds must be a supported candle interval.")
    _positive_int(value, "cycle_interval_seconds")
    _positive_int(value, "max_market_data_age_seconds")
    for key in (
        "initial_capital",
        "max_total_exposure",
        "max_market_exposure",
        "max_order_notional",
        "max_loss",
    ):
        _positive_decimal(value, key)
    base = config_path.resolve().parent
    paths: dict[str, Path] = {}
    for key in ("csv_path", "journal_path", "log_path"):
        raw = value[key]
        if not isinstance(raw, str) or not raw.strip():
            raise PaperTaskConfigurationError(f"{key} must be a non-empty path string.")
        if "\x00" in raw or '"' in raw or "\r" in raw or "\n" in raw:
            raise PaperTaskConfigurationError(f"{key} contains invalid path characters.")
        if raw.startswith(("\\\\", "//")):
            raise PaperTaskConfigurationError(f"{key} must not be a UNC/network path.")
        path = Path(raw)
        path = (base / path).resolve() if not path.is_absolute() else path.resolve()
        paths[key] = path
    csv_path = paths["csv_path"]
    journal_path = paths["journal_path"]
    log_path = paths["log_path"]
    if not csv_path.is_file() or csv_path.is_symlink():
        raise PaperTaskConfigurationError(
            "csv_path must point to an existing regular local CSV file."
        )
    if csv_path.stat().st_size > _MAX_CSV_BYTES:
        raise PaperTaskConfigurationError("CSV file exceeds the configured size ceiling.")
    if not journal_path.parent.is_dir() or not log_path.parent.is_dir():
        raise PaperTaskConfigurationError("Journal and log parent directories must already exist.")
    if journal_path.exists() and (journal_path.is_symlink() or not journal_path.is_file()):
        raise PaperTaskConfigurationError(
            "journal_path must be a regular file when it already exists."
        )
    if log_path.exists() and (log_path.is_symlink() or not log_path.is_file()):
        raise PaperTaskConfigurationError("log_path must be a regular file when it already exists.")
    value["csv_path"] = csv_path
    value["journal_path"] = journal_path
    value["log_path"] = log_path
    return value


def _positive_int(config: dict[str, Any], key: str) -> int:
    raw = config[key]
    if type(raw) is not int or raw <= 0:
        raise PaperTaskConfigurationError(f"{key} must be a positive integer.")
    return raw


def _positive_decimal(config: dict[str, Any], key: str) -> Decimal:
    raw = config[key]
    if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
        raise PaperTaskConfigurationError(f"{key} must be a positive decimal string or number.")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise PaperTaskConfigurationError(f"{key} must be a valid decimal.") from exc
    if not value.is_finite() or value <= 0:
        raise PaperTaskConfigurationError(f"{key} must be finite and positive.")
    return value


def _read_csv_candles(
    path: Path,
    market: Market,
    interval: timedelta,
) -> tuple[MarketCandle, ...]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Offline Paper CSV is missing or not a regular file.")
    if path.stat().st_size > _MAX_CSV_BYTES:
        raise ValueError("Offline Paper CSV exceeds the configured size ceiling.")
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != _CSV_FIELDS:
                raise ValueError(f"CSV header must be exactly: {','.join(_CSV_FIELDS)}.")
            candles: list[MarketCandle] = []
            for row_number, row in enumerate(reader, start=2):
                if row_number > _MAX_CSV_ROWS + 1:
                    raise ValueError("Offline Paper CSV contains too many rows.")
                if None in row or any(row.get(field) is None for field in _CSV_FIELDS):
                    raise ValueError(f"CSV row {row_number} has missing or extra columns.")
                timestamp = _parse_utc(str(row["timestamp"]), row_number)
                values = {
                    key: _parse_positive_decimal(str(row[key]), key, row_number)
                    for key in ("open", "high", "low", "close")
                }
                volume = _parse_nonnegative_decimal(str(row["volume"]), row_number)
                if (
                    values["low"] > min(values["open"], values["close"])
                    or values["high"] < max(values["open"], values["close"])
                    or values["high"] < values["low"]
                ):
                    raise ValueError(f"CSV row {row_number} violates OHLC invariants.")
                candles.append(
                    MarketCandle(
                        market=market,
                        timestamp=timestamp,
                        interval=interval,
                        open=values["open"],
                        high=values["high"],
                        low=values["low"],
                        close=values["close"],
                        volume=volume,
                        source=_SOURCE,
                    )
                )
    except OSError as exc:
        raise ValueError("Offline Paper CSV could not be read.") from exc
    if not candles:
        raise ValueError("Offline Paper CSV contains no candles.")
    for previous, current in zip(candles, candles[1:], strict=False):
        if current.timestamp != previous.timestamp + interval:
            raise ValueError("Offline Paper CSV has duplicate, unordered, or gapped candles.")
    return tuple(candles)


def _parse_utc(value: str, row_number: int) -> datetime:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"CSV row {row_number} has an invalid timestamp.") from exc
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise ValueError(f"CSV row {row_number} timestamp must be explicit UTC.")
    return stamp.astimezone(UTC)


def _parse_positive_decimal(value: str, field: str, row_number: int) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"CSV row {row_number} has invalid {field}.") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"CSV row {row_number} {field} must be finite and positive.")
    return parsed


def _parse_nonnegative_decimal(value: str, row_number: int) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"CSV row {row_number} has invalid volume.") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"CSV row {row_number} volume must be finite and non-negative.")
    return parsed


def _utc_now(clock: Any) -> datetime:
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("Runtime clock must return an aware UTC timestamp.")
    return now.astimezone(UTC)


def preflight(config_path: Path) -> dict[str, str]:
    config = load_and_validate_config(config_path)
    market = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
    interval = timedelta(seconds=config["interval_seconds"])
    candles = _read_csv_candles(config["csv_path"], market, interval)
    now = datetime.now(UTC)
    completed = tuple(c for c in candles if c.timestamp + interval <= now)
    if not completed:
        raise PaperTaskConfigurationError("CSV has no completed candles at current UTC time.")
    age = now - (completed[-1].timestamp + interval)
    if age < timedelta(0) or age > interval * 2:
        raise PaperTaskConfigurationError("Latest completed CSV candle is stale or future-dated.")
    return {
        "status": "ready",
        "mode": "paper_only",
        "source": "offline_csv",
        "champion": "none",
        "selected_model_for_runtime": "null",
        "live_connection": "disabled",
        "real_orders": "disabled",
        "candle_count": str(len(completed)),
        "latest_candle_close": (completed[-1].timestamp + interval).isoformat(),
    }


def _build_runtime(config: dict[str, Any]) -> PaperStrategyRuntime:
    market = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
    interval = timedelta(seconds=config["interval_seconds"])
    feed = OfflineCsvPaperFeed(
        market=market,
        interval=interval,
        csv_path=config["csv_path"],
    )
    risk_limits = RiskLimits(
        max_total_exposure=_positive_decimal(config, "max_total_exposure"),
        max_market_exposure=_positive_decimal(config, "max_market_exposure"),
        max_order_notional=_positive_decimal(config, "max_order_notional"),
        max_loss=_positive_decimal(config, "max_loss"),
        max_market_data_age_seconds=_positive_int(config, "max_market_data_age_seconds"),
    )
    return PaperStrategyRuntime(
        market=market,
        base_asset="BTC",
        quote_asset="USDT",
        feed=feed,
        regime_classifier=NoChampionClassifier(),
        registry=build_baseline_registry(),
        initial_capital=_positive_decimal(config, "initial_capital"),
        risk_limits=risk_limits,
        journal=SQLitePaperRuntimeLedger(config["journal_path"]),
        selector_policy=StrategySelectionPolicy(
            min_confidence=Decimal("0.65"),
            required_evidence_count=2,
            required_stability_duration=interval,
            minimum_hold_duration=interval,
            maximum_prediction_age=interval * 2,
        ),
        interval=interval,
        history_limit=256,
        clock=lambda: datetime.now(UTC),
    )


def _run_paper(config_path: Path, *, max_cycles: int | None) -> int:
    config = load_and_validate_config(config_path)
    report = preflight(config_path)
    journal_path: Path = config["journal_path"]
    try:
        with PaperRuntimeLocks(journal_path):
            log_handler = RotatingFileHandler(
                config["log_path"], maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            logging.basicConfig(
                handlers=[log_handler],
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(message)s",
                force=True,
            )
            logging.info("paper_task_start mode=paper_only source=offline_csv champion=none")
            supervisor = PaperRuntimeSupervisor(lambda: _build_runtime(config))
            stop_event = Event()
            scheduler = PaperRuntimeScheduler(
                supervisor,
                interval_seconds=float(_positive_int(config, "cycle_interval_seconds")),
                stop_event=stop_event,
                on_result=lambda result: logging.info(
                    "paper_cycle status=%s reason=%s correlation_id=%s",
                    result.status.value,
                    result.reason_code,
                    result.correlation_id,
                ),
            )
            prior_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
            for sig in prior_handlers:
                signal.signal(sig, lambda _signum, _frame: stop_event.set())
            try:
                result = scheduler.run(max_cycles=max_cycles)
            finally:
                for sig, handler in prior_handlers.items():
                    signal.signal(sig, handler)
            logging.info(
                "paper_task_exit cycles=%s status=%s reason=%s",
                result.cycles_completed,
                result.last_status,
                result.exit_reason,
            )
            print(
                json.dumps(
                    {
                        **report,
                        "cycles_completed": result.cycles_completed,
                        "last_status": result.last_status,
                        "exit_reason": result.exit_reason,
                    }
                )
            )
            return 2 if result.last_status == "halted" else 0
    except RuntimeAlreadyRunning:
        logging.warning("paper_task_exit reason=already_running")
        print("Paper task already running; journal was not opened.", file=sys.stderr)
        return 3
    except Exception as exc:
        logging.error("paper_task_exit reason=fail_closed exception=%s", type(exc).__name__)
        print(f"Paper task stopped fail-closed: {type(exc).__name__}.", file=sys.stderr)
        return 4


def _run_windows_task_action(action: str, config_path: Path, task_name: str) -> int:
    if os.name != "nt":
        raise PaperTaskConfigurationError("Windows Task Scheduler commands require Windows.")
    if not _TASK_SCRIPT.is_file():
        raise PaperTaskConfigurationError("Windows Task Scheduler PowerShell helper is missing.")
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        raise PaperTaskConfigurationError("Windows PowerShell was not found.")
    if action == "register":
        preflight(config_path)
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(_TASK_SCRIPT.resolve()),
            "-Action",
            action,
            "-PythonPath",
            str(Path(sys.executable).resolve()),
            "-RunnerScript",
            str(Path(__file__).resolve()),
            "-ConfigPath",
            str(config_path.resolve()),
            "-TaskName",
            task_name,
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
        shell=False,
    )
    if completed.stdout.strip():
        print(completed.stdout.strip())
    if completed.returncode != 0:
        if completed.stderr.strip():
            print(completed.stderr.strip(), file=sys.stderr)
        return completed.returncode
    return 0


def _scrub_credentials_from_environment() -> None:
    """Remove credential-bearing variables before constructing any Paper components."""
    sensitive_markers = (
        "API_KEY", "PRIVATE_KEY", "SECRET", "TOKEN",
        "LIVE_TRADING", "ORDER_WRITES", "ENABLE_REAL_TRADING",
    )
    for name in tuple(os.environ):
        upper = name.upper()
        if upper.startswith(("EVORYN_", "NOBITEX_", "BINANCE_")) or any(
            marker in upper for marker in sensitive_markers
        ):
            os.environ.pop(name, None)


def _task_name_is_owned(task_name: str) -> bool:
    import re
    return task_name == DEFAULT_TASK_NAME or bool(
        re.fullmatch(r"EVORYN-CI-PaperOnly-[A-Za-z0-9-]{1,48}", task_name)
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    preflight_parser = sub.add_parser(
        "preflight",
        help="Validate config and local candle freshness without mutation.",
    )
    preflight_parser.add_argument("--config", required=True, type=Path)
    run_parser = sub.add_parser("run", help="Run offline, no-Champion Paper supervision.")
    run_parser.add_argument("--config", required=True, type=Path)
    run_parser.add_argument("--max-cycles", type=int, default=None)
    for name in ("register", "unregister", "status"):
        item = sub.add_parser(name, help=f"{name.title()} the user-scoped Windows Paper task.")
        item.add_argument("--config", required=(name == "register"), type=Path)
        item.add_argument("--task-name", default=DEFAULT_TASK_NAME)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    _scrub_credentials_from_environment()
    try:
        if args.command == "preflight":
            print(json.dumps(preflight(args.config), sort_keys=True))
            return 0
        if args.command == "run":
            if args.max_cycles is not None and args.max_cycles <= 0:
                raise PaperTaskConfigurationError("--max-cycles must be positive.")
            return _run_paper(args.config, max_cycles=args.max_cycles)
        if not _task_name_is_owned(args.task_name):
            raise PaperTaskConfigurationError(
                "Refusing to manage a Task Scheduler name outside the EVORYN Paper namespace."
            )
        config_path = (
            args.config
            if args.config is not None
            else Path.cwd() / "unused-paper-config.json"
        )
        if args.command == "register":
            load_and_validate_config(config_path)
        return _run_windows_task_action(args.command, config_path, args.task_name)
    except (PaperTaskConfigurationError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"Paper task preflight rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
