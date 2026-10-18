"""Deterministic, long/flat OHLCV backtesting with causal signals and explicit costs.

Signals use only completed historical bars. Orders become eligible at the next candle open.
Fills are constrained by configured participation of the latest completed candle's
reported base volume, known before the next-open execution. Spread and slippage are
configurable assumptions because OHLCV alone does not reveal
historical bid/ask spread or true order-book depth. Nothing in this module sends orders.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any

from evoryn.domain.models import MarketCandle, Side
from evoryn.learning_lab.historical_data import HistoricalDataset
from evoryn.strategies.contracts import CausalTargetStrategy


BACKTEST_ENGINE_VERSION = "1.0.0"
_BPS_DENOMINATOR = Decimal("10000")
_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """All economically material simulator assumptions are mandatory and fingerprinted."""

    initial_cash: Decimal
    commission_bps: Decimal
    spread_bps: Decimal
    slippage_bps: Decimal
    max_bar_participation: Decimal
    allocation_fraction: Decimal
    quantity_step: Decimal
    allow_gaps: bool = False

    def __post_init__(self) -> None:
        _positive(self.initial_cash, "initial_cash")
        for name in ("commission_bps", "spread_bps", "slippage_bps"):
            _nonnegative(getattr(self, name), name)
        if (
            not isinstance(self.max_bar_participation, Decimal)
            or not self.max_bar_participation.is_finite()
            or self.max_bar_participation <= 0
            or self.max_bar_participation > 1
        ):
            raise ValueError("max_bar_participation must be in (0, 1].")
        if (
            not isinstance(self.allocation_fraction, Decimal)
            or not self.allocation_fraction.is_finite()
            or self.allocation_fraction <= 0
            or self.allocation_fraction > 1
        ):
            raise ValueError("allocation_fraction must be in (0, 1].")
        _positive(self.quantity_step, "quantity_step")
        if type(self.allow_gaps) is not bool:
            raise ValueError("allow_gaps must be an explicit boolean.")
        if self.spread_bps / Decimal("20000") + self.slippage_bps / _BPS_DENOMINATOR >= 1:
            raise ValueError("Combined adverse spread and slippage must be less than 100%.")

    @property
    def commission_rate(self) -> Decimal:
        return self.commission_bps / _BPS_DENOMINATOR

    @property
    def half_spread_rate(self) -> Decimal:
        return self.spread_bps / Decimal("20000")

    @property
    def slippage_rate(self) -> Decimal:
        return self.slippage_bps / _BPS_DENOMINATOR

    def as_dict(self) -> dict[str, Any]:
        return {
            "initial_cash": str(self.initial_cash),
            "commission_bps": str(self.commission_bps),
            "spread_bps": str(self.spread_bps),
            "slippage_bps": str(self.slippage_bps),
            "max_bar_participation": str(self.max_bar_participation),
            "allocation_fraction": str(self.allocation_fraction),
            "quantity_step": str(self.quantity_step),
            "allow_gaps": self.allow_gaps,
        }


@dataclass(frozen=True, slots=True)
class IndexRange:
    """Half-open candle index interval [start, end)."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError("IndexRange requires 0 <= start < end.")

    @property
    def size(self) -> int:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class TemporalPartitions:
    """Disjoint chronological train/validation/test index ranges for one dataset version."""

    dataset_version: str
    train: IndexRange
    validation: IndexRange
    test: IndexRange
    total_bars: int

    def __post_init__(self) -> None:
        if not self.dataset_version:
            raise ValueError("dataset_version is required.")
        if self.train.start != 0:
            raise ValueError("Training range must start at candle zero.")
        if self.train.end != self.validation.start:
            raise ValueError("Train and validation partitions must be adjacent and disjoint.")
        if self.validation.end != self.test.start:
            raise ValueError("Validation and test partitions must be adjacent and disjoint.")
        if self.test.end != self.total_bars:
            raise ValueError("Test partition must end at the dataset boundary.")


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """Rolling train/validation/test ranges; the test is always later than its inputs."""

    fold_index: int
    train: IndexRange
    validation: IndexRange
    test: IndexRange

    def __post_init__(self) -> None:
        if self.fold_index < 0:
            raise ValueError("fold_index must be non-negative.")
        if not (
            self.train.end == self.validation.start
            and self.validation.end == self.test.start
        ):
            raise ValueError("Walk-forward windows must be chronological and non-overlapping.")


@dataclass(frozen=True, slots=True)
class SignalEvent:
    """A target-exposure change with explicit observation and next-eligible times."""

    strategy_id: str
    target_exposure: Decimal
    based_on_candle_open: datetime
    decision_at: datetime
    first_eligible_at: datetime

    def __post_init__(self) -> None:
        if self.target_exposure not in (_ZERO, _ONE):
            raise ValueError("Baseline signals currently support only flat/long targets.")
        for name in ("based_on_candle_open", "decision_at", "first_eligible_at"):
            _require_aware(getattr(self, name), name)
        if self.decision_at < self.based_on_candle_open:
            raise ValueError("Decision cannot precede the candle used to make it.")
        if self.first_eligible_at < self.decision_at:
            raise ValueError("Signal cannot be eligible before its decision time.")


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    """One paper-only fill at the next bar open with explicit execution drag."""

    order_id: str
    side: Side
    timestamp: datetime
    requested_quantity: Decimal
    quantity: Decimal
    reference_open: Decimal
    liquidity_reference_candle_open: datetime
    liquidity_reference_volume: Decimal
    execution_price: Decimal
    notional: Decimal
    commission: Decimal
    estimated_spread_cost: Decimal
    estimated_slippage_cost: Decimal
    volume_participation: Decimal
    partial: bool

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "fill timestamp")
        _require_aware(self.liquidity_reference_candle_open, "liquidity reference timestamp")
        if self.quantity <= 0 or self.quantity > self.requested_quantity:
            raise ValueError("Fill quantity must be positive and no greater than requested.")
        if self.volume_participation < 0 or self.volume_participation > 1:
            raise ValueError("Fill participation must be in [0, 1].")


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """One completed long/flat position episode after explicit transaction costs."""

    entry_at: datetime
    exit_at: datetime
    quantity: Decimal
    entry_vwap: Decimal
    exit_vwap: Decimal
    gross_pnl: Decimal
    commission_paid: Decimal
    net_pnl: Decimal
    net_return_fraction: Decimal

    def __post_init__(self) -> None:
        _require_aware(self.entry_at, "trade entry time")
        _require_aware(self.exit_at, "trade exit time")
        if self.exit_at < self.entry_at:
            raise ValueError("Trade exit cannot precede trade entry.")
        if self.quantity <= 0:
            raise ValueError("Closed trade quantity must be positive.")


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """Account equity marked at a candle open before orders or a candle close afterward."""

    timestamp: datetime
    cash: Decimal
    position_quantity: Decimal
    mark_price: Decimal
    equity: Decimal

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "equity timestamp")


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    """Clearly defined return, risk, trade, turnover, and execution-cost statistics."""

    initial_equity: Decimal
    final_equity: Decimal
    net_profit: Decimal
    net_return_fraction: Decimal
    max_drawdown_fraction: Decimal
    expectancy_per_closed_trade: Decimal | None
    win_rate_fraction: Decimal | None
    closed_trade_count: int
    winning_trade_count: int
    losing_trade_count: int
    gross_turnover: Decimal
    turnover_multiple: Decimal
    commission_paid: Decimal
    estimated_spread_cost: Decimal
    estimated_slippage_cost: Decimal
    submitted_order_count: int
    completed_order_count: int
    cancelled_order_count: int
    open_order_count_at_end: int
    fill_count: int
    partial_fill_count: int
    time_in_market_fraction: Decimal
    open_position_quantity: Decimal


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Content-addressed research result tied to exact dataset, strategy, config and range."""

    run_id: str
    engine_version: str
    dataset_id: str
    dataset_version: str
    dataset_metadata_json: str
    strategy_id: str
    strategy_version: str
    strategy_parameters: tuple[tuple[str, str], ...]
    segment_name: str
    evaluation_range: IndexRange
    warmup_start_index: int
    config: BacktestConfig
    metrics: BacktestMetrics
    signals: tuple[SignalEvent, ...]
    fills: tuple[SimulatedFill, ...]
    trades: tuple[ClosedTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    first_evaluation_open: datetime
    last_evaluation_close: datetime

    def to_report_dict(self) -> dict[str, Any]:
        """Serialize all material result data deterministically for audit and reproduction."""
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "engine_version": self.engine_version,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_provenance": json.loads(self.dataset_metadata_json),
            "strategy": {
                "id": self.strategy_id,
                "version": self.strategy_version,
                "parameters": dict(self.strategy_parameters),
            },
            "segment_name": self.segment_name,
            "evaluation_range": {
                "start": self.evaluation_range.start,
                "end": self.evaluation_range.end,
            },
            "warmup_start_index": self.warmup_start_index,
            "first_evaluation_open": _iso_utc(self.first_evaluation_open),
            "last_evaluation_close": _iso_utc(self.last_evaluation_close),
            "config": self.config.as_dict(),
            "metrics": _metrics_dict(self.metrics),
            "signals": [
                {
                    "strategy_id": signal.strategy_id,
                    "target_exposure": str(signal.target_exposure),
                    "based_on_candle_open": _iso_utc(signal.based_on_candle_open),
                    "decision_at": _iso_utc(signal.decision_at),
                    "first_eligible_at": _iso_utc(signal.first_eligible_at),
                }
                for signal in self.signals
            ],
            "fills": [
                {
                    "order_id": fill.order_id,
                    "side": fill.side.value,
                    "timestamp": _iso_utc(fill.timestamp),
                    "requested_quantity": str(fill.requested_quantity),
                    "quantity": str(fill.quantity),
                    "reference_open": str(fill.reference_open),
                    "liquidity_reference_candle_open": _iso_utc(
                        fill.liquidity_reference_candle_open
                    ),
                    "liquidity_reference_volume": str(fill.liquidity_reference_volume),
                    "execution_price": str(fill.execution_price),
                    "notional": str(fill.notional),
                    "commission": str(fill.commission),
                    "estimated_spread_cost": str(fill.estimated_spread_cost),
                    "estimated_slippage_cost": str(fill.estimated_slippage_cost),
                    "volume_participation": str(fill.volume_participation),
                    "partial": fill.partial,
                }
                for fill in self.fills
            ],
            "trades": [
                {
                    "entry_at": _iso_utc(trade.entry_at),
                    "exit_at": _iso_utc(trade.exit_at),
                    "quantity": str(trade.quantity),
                    "entry_vwap": str(trade.entry_vwap),
                    "exit_vwap": str(trade.exit_vwap),
                    "gross_pnl": str(trade.gross_pnl),
                    "commission_paid": str(trade.commission_paid),
                    "net_pnl": str(trade.net_pnl),
                    "net_return_fraction": str(trade.net_return_fraction),
                }
                for trade in self.trades
            ],
            "equity_curve": [
                {
                    "timestamp": _iso_utc(point.timestamp),
                    "cash": str(point.cash),
                    "position_quantity": str(point.position_quantity),
                    "mark_price": str(point.mark_price),
                    "equity": str(point.equity),
                }
                for point in self.equity_curve
            ],
        }

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self.to_report_dict())


class BacktestResultStore:
    """Immutable run-report store with a checksum sidecar and identity verification."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()

    def save(self, result: BacktestResult) -> Path:
        if not _is_sha256(result.run_id):
            raise ValueError("run_id must be a lowercase SHA-256 digest.")
        report_dict = result.to_report_dict()
        if _report_identity_hash(report_dict) != result.run_id:
            raise ValueError("Run ID does not match the report's input identity.")
        report_bytes = _canonical_json_bytes(report_dict)
        report_hash = hashlib.sha256(report_bytes).hexdigest()
        destination = self._root / f"{result.run_id}.json"
        checksum_path = self._root / f"{result.run_id}.json.sha256"
        self._root.mkdir(parents=True, exist_ok=True)

        if destination.exists() or checksum_path.exists():
            return self._verify_existing(destination, checksum_path, report_bytes, report_hash)

        report_fd, report_tmp_name = tempfile.mkstemp(
            prefix=".run-", suffix=".json.tmp", dir=self._root
        )
        checksum_fd, checksum_tmp_name = tempfile.mkstemp(
            prefix=".run-", suffix=".sha256.tmp", dir=self._root
        )
        report_tmp = Path(report_tmp_name)
        checksum_tmp = Path(checksum_tmp_name)
        try:
            with os.fdopen(report_fd, "wb") as report_file:
                report_file.write(report_bytes)
            with os.fdopen(checksum_fd, "w", encoding="ascii", newline="") as checksum_file:
                checksum_file.write(f"{report_hash}\n")
            try:
                os.link(report_tmp, destination)
            except FileExistsError:
                return self._verify_existing(
                    destination, checksum_path, report_bytes, report_hash
                )
            try:
                os.link(checksum_tmp, checksum_path)
            except OSError:
                # A report without its checksum is incomplete and will not be returned as saved.
                if destination.exists() and not checksum_path.exists():
                    destination.unlink()
                raise
        finally:
            if report_tmp.exists():
                report_tmp.unlink()
            if checksum_tmp.exists():
                checksum_tmp.unlink()
        self.load(result.run_id)
        return destination

    def load(self, run_id: str) -> dict[str, Any]:
        """Read a canonical report only when its checksum and input-derived run ID verify."""
        if not _is_sha256(run_id):
            raise ValueError("run_id must be a lowercase SHA-256 digest.")
        report_path = self._root / f"{run_id}.json"
        checksum_path = self._root / f"{run_id}.json.sha256"
        try:
            report_bytes = report_path.read_bytes()
            checksum_text = checksum_path.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError("Backtest report or checksum sidecar is missing or invalid.") from exc
        actual_hash = hashlib.sha256(report_bytes).hexdigest()
        if checksum_text != f"{actual_hash}\n":
            raise ValueError("Backtest report checksum does not match stored bytes.")
        try:
            report = json.loads(report_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Backtest report is not valid JSON.") from exc
        if not isinstance(report, dict) or report.get("run_id") != run_id:
            raise ValueError("Backtest report identity does not match the requested run ID.")
        if _canonical_json_bytes(report) != report_bytes:
            raise ValueError("Backtest report is not in canonical JSON form.")
        expected_run_id = _report_identity_hash(report)
        if expected_run_id != run_id:
            raise ValueError("Run ID does not match the report's input identity.")
        return report

    @staticmethod
    def _verify_existing(
        destination: Path,
        checksum_path: Path,
        report_bytes: bytes,
        report_hash: str,
    ) -> Path:
        try:
            existing_report = destination.read_bytes()
            existing_checksum = checksum_path.read_text(encoding="ascii")
        except OSError as exc:
            raise ValueError("Existing report package is incomplete; refusing overwrite.") from exc
        if existing_report != report_bytes or existing_checksum != f"{report_hash}\n":
            raise ValueError("Existing run ID has conflicting report/checksum bytes.")
        return destination


@dataclass(frozen=True, slots=True)
class SplitEvaluation:
    """Independent portfolio resets for train/validation/test diagnostics."""

    partitions: TemporalPartitions
    train: BacktestResult
    validation: BacktestResult
    test: BacktestResult


@dataclass(frozen=True, slots=True)
class WalkForwardFoldResult:
    """One out-of-sample test result whose warm-up is strictly earlier than the test."""

    fold: WalkForwardFold
    test_result: BacktestResult


@dataclass(frozen=True, slots=True)
class WalkForwardEvaluation:
    """A collection of disjoint out-of-sample folds; metrics stay per-fold, not conflated."""

    dataset_version: str
    folds: tuple[WalkForwardFoldResult, ...]


def chronological_split(
    dataset: HistoricalDataset,
    *,
    train_fraction: Decimal = Decimal("0.6"),
    validation_fraction: Decimal = Decimal("0.2"),
) -> TemporalPartitions:
    """Make contiguous, non-overlapping train/validation/test partitions in time order.

    The fractions specify train and validation; test receives the remainder. These
    baseline strategies are not fitted, so split evaluation diagnoses time stability and
    preserves protocol for future models without claiming the baselines are trained ML.
    """
    _fraction_below_one(train_fraction, "train_fraction")
    _fraction_below_one(validation_fraction, "validation_fraction")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("train_fraction + validation_fraction must be less than 1.")
    count = len(dataset.candles)
    train_end = int(Decimal(count) * train_fraction)
    validation_end = train_end + int(Decimal(count) * validation_fraction)
    if train_end <= 0 or validation_end <= train_end or validation_end >= count:
        raise ValueError("Dataset is too short for non-empty train/validation/test partitions.")
    return TemporalPartitions(
        dataset_version=dataset.version,
        train=IndexRange(0, train_end),
        validation=IndexRange(train_end, validation_end),
        test=IndexRange(validation_end, count),
        total_bars=count,
    )


def make_walk_forward_folds(
    dataset: HistoricalDataset,
    *,
    train_bars: int,
    validation_bars: int,
    test_bars: int,
    step_bars: int | None = None,
) -> tuple[WalkForwardFold, ...]:
    """Create rolling, fixed-size, chronological windows without overlapping test periods."""
    for name, value in (
        ("train_bars", train_bars),
        ("validation_bars", validation_bars),
        ("test_bars", test_bars),
    ):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer.")
    step = test_bars if step_bars is None else step_bars
    if type(step) is not int or step <= 0:
        raise ValueError("step_bars must be a positive integer.")
    if step < test_bars:
        raise ValueError(
            "step_bars must be >= test_bars so out-of-sample test windows do not overlap."
        )
    folds: list[WalkForwardFold] = []
    start = 0
    index = 0
    total = len(dataset.candles)
    while start + train_bars + validation_bars + test_bars <= total:
        train_end = start + train_bars
        validation_end = train_end + validation_bars
        test_end = validation_end + test_bars
        folds.append(
            WalkForwardFold(
                fold_index=index,
                train=IndexRange(start, train_end),
                validation=IndexRange(train_end, validation_end),
                test=IndexRange(validation_end, test_end),
            )
        )
        index += 1
        start += step
    if not folds:
        raise ValueError("Dataset is too short for one complete walk-forward fold.")
    return tuple(folds)


def run_backtest(
    dataset: HistoricalDataset,
    strategy: CausalTargetStrategy,
    config: BacktestConfig,
    *,
    start_index: int = 0,
    end_index: int | None = None,
    warmup_start_index: int = 0,
    segment_name: str = "full",
) -> BacktestResult:
    """Run one long/flat simulation on a half-open period from the versioned dataset.

    At evaluation bar i the strategy sees dataset.candles[warmup_start_index:i]. Its
    order may fill only at bar i open, so a candle's close/high/low/volume is never used
    to decide a fill at that same candle open. A pending market order can partially fill
    across later bars using each bar's explicit participation cap.
    """
    if dataset.market.market_type.casefold() != "spot":
        raise ValueError("Phase 13 backtester supports Spot datasets only, not Margin or derivatives.")
    total = len(dataset.candles)
    end = total if end_index is None else end_index
    if start_index < 0 or end <= start_index or end > total:
        raise ValueError("Evaluation range must be a non-empty valid dataset index range.")
    if warmup_start_index < 0 or warmup_start_index > start_index:
        raise ValueError("warmup_start_index must be between zero and evaluation start.")
    if not segment_name.strip():
        raise ValueError("segment_name must not be empty.")
    if dataset.volume_unit != "base_asset":
        raise ValueError(
            "Backtest fill constraints require base-asset volume; "
            "quote-volume datasets are unsupported."
        )
    if dataset.gap_count and not config.allow_gaps:
        raise ValueError("Dataset contains gaps; allow_gaps must be explicitly enabled in config.")
    evaluation = dataset.candles[start_index:end]
    warmup = dataset.candles[warmup_start_index:start_index]
    if warmup and warmup[-1].timestamp + warmup[-1].interval > evaluation[0].timestamp:
        raise ValueError("Warm-up candles overlap the evaluation period.")
    if not getattr(strategy, "strategy_id", "").strip():
        raise ValueError("Strategy must declare a non-empty strategy_id.")
    strategy_version = getattr(strategy, "strategy_version", "")
    if not isinstance(strategy_version, str) or not strategy_version.strip():
        raise ValueError("Strategy must declare a non-empty strategy_version.")
    parameters = strategy.parameters()
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in parameters.items()
    ):
        raise ValueError("Strategy parameters must be serializable string pairs.")

    cash = config.initial_cash
    position_quantity = _ZERO
    position_cost_basis = _ZERO
    entry_fees_remaining = _ZERO
    episode_entry_time: datetime | None = None
    episode_quantity_bought = _ZERO
    episode_entry_notional = _ZERO
    episode_entry_fees = _ZERO
    episode_exit_notional = _ZERO
    episode_exit_fees = _ZERO
    episode_exit_quantity = _ZERO
    pending: _PendingOrder | None = None
    order_sequence = 0
    submitted_orders = 0
    completed_orders = 0
    cancelled_orders = 0
    partial_fill_count = 0
    gross_turnover = _ZERO
    commission_paid = _ZERO
    spread_cost_total = _ZERO
    slippage_cost_total = _ZERO
    exposure_bars = 0
    fills: list[SimulatedFill] = []
    trades: list[ClosedTrade] = []
    signals: list[SignalEvent] = []
    equity_curve: list[EquityPoint] = [
        EquityPoint(
            timestamp=evaluation[0].timestamp,
            cash=cash,
            position_quantity=position_quantity,
            mark_price=evaluation[0].open,
            equity=cash,
        )
    ]
    prior_target = _ZERO
    segment_key = f"{strategy.strategy_id}:{segment_name}:{start_index}:{end}"

    for local_index, candle in enumerate(evaluation):
        global_index = start_index + local_index
        history = dataset.candles[warmup_start_index:global_index]
        target = strategy.target_exposure(history)
        if not isinstance(target, Decimal) or not target.is_finite() or target not in (_ZERO, _ONE):
            raise ValueError("Baseline strategy must return Decimal('0') or Decimal('1').")
        if not history and target != _ZERO:
            raise ValueError("A strategy must remain flat until at least one candle has closed.")

        if target != prior_target and history:
            last_known = history[-1]
            decision_at = last_known.timestamp + last_known.interval
            if decision_at > candle.timestamp:
                raise ValueError("Backtest attempted to use a signal before its candle closed.")
            signals.append(
                SignalEvent(
                    strategy_id=strategy.strategy_id,
                    target_exposure=target,
                    based_on_candle_open=last_known.timestamp,
                    decision_at=decision_at,
                    first_eligible_at=candle.timestamp,
                )
            )
        prior_target = target

        if pending is not None and pending.target_exposure != target:
            cancelled_orders += 1
            pending = None

        if pending is None and target == _ONE and position_quantity == _ZERO and history:
            reference_close = history[-1].close
            marked_equity = cash + position_quantity * reference_close
            estimated_cost_per_unit = reference_close * (
                _ONE + config.half_spread_rate + config.slippage_rate + config.commission_rate
            )
            target_quantity = _floor_to_step(
                marked_equity * config.allocation_fraction / estimated_cost_per_unit,
                config.quantity_step,
            )
            if target_quantity > 0:
                order_sequence += 1
                pending = _PendingOrder(
                    order_id=f"{segment_key}:order:{order_sequence}",
                    side=Side.BUY,
                    requested_quantity=target_quantity,
                    remaining_quantity=target_quantity,
                    target_exposure=_ONE,
                    created_at=history[-1].timestamp + history[-1].interval,
                )
                submitted_orders += 1
        elif pending is None and target == _ZERO and position_quantity > _ZERO and history:
            order_sequence += 1
            pending = _PendingOrder(
                order_id=f"{segment_key}:order:{order_sequence}",
                side=Side.SELL,
                requested_quantity=position_quantity,
                remaining_quantity=position_quantity,
                target_exposure=_ZERO,
                created_at=history[-1].timestamp + history[-1].interval,
            )
            submitted_orders += 1

        if pending is not None:
            # At the current open, only the previous completed bar's volume is knowable.
            if not history:
                raise RuntimeError("Pending order exists without completed history.")
            liquidity_reference = history[-1]
            available = _floor_to_step(
                liquidity_reference.volume * config.max_bar_participation,
                config.quantity_step,
            )
            raw_fill = min(pending.remaining_quantity, available)
            if pending.side is Side.BUY:
                execution_price = candle.open * (
                    _ONE + config.half_spread_rate + config.slippage_rate
                )
                cost_per_unit = execution_price * (_ONE + config.commission_rate)
                affordable = _floor_to_step(cash / cost_per_unit, config.quantity_step)
                quantity = min(raw_fill, affordable)
            else:
                execution_price = candle.open * (
                    _ONE - config.half_spread_rate - config.slippage_rate
                )
                quantity = min(raw_fill, position_quantity)
            quantity = _floor_to_step(quantity, config.quantity_step)

            if quantity > 0:
                notional = quantity * execution_price
                fee = notional * config.commission_rate
                spread_cost = candle.open * quantity * config.half_spread_rate
                slippage_cost = candle.open * quantity * config.slippage_rate
                was_partial = quantity < pending.remaining_quantity
                fill = SimulatedFill(
                    order_id=pending.order_id,
                    side=pending.side,
                    timestamp=candle.timestamp,
                    requested_quantity=pending.requested_quantity,
                    quantity=quantity,
                    reference_open=candle.open,
                    liquidity_reference_candle_open=liquidity_reference.timestamp,
                    liquidity_reference_volume=liquidity_reference.volume,
                    execution_price=execution_price,
                    notional=notional,
                    commission=fee,
                    estimated_spread_cost=spread_cost,
                    estimated_slippage_cost=slippage_cost,
                    volume_participation=(
                        quantity / liquidity_reference.volume
                        if liquidity_reference.volume > 0
                        else _ZERO
                    ),
                    partial=was_partial,
                )
                fills.append(fill)
                gross_turnover += notional
                commission_paid += fee
                spread_cost_total += spread_cost
                slippage_cost_total += slippage_cost

                if pending.side is Side.BUY:
                    if position_quantity == _ZERO:
                        episode_entry_time = candle.timestamp
                        episode_quantity_bought = _ZERO
                        episode_entry_notional = _ZERO
                        episode_entry_fees = _ZERO
                        episode_exit_notional = _ZERO
                        episode_exit_fees = _ZERO
                        episode_exit_quantity = _ZERO
                    cash -= notional + fee
                    position_quantity += quantity
                    position_cost_basis += notional
                    entry_fees_remaining += fee
                    episode_quantity_bought += quantity
                    episode_entry_notional += notional
                    episode_entry_fees += fee
                else:
                    before_quantity = position_quantity
                    allocated_cost = position_cost_basis * quantity / before_quantity
                    allocated_entry_fee = entry_fees_remaining * quantity / before_quantity
                    cash += notional - fee
                    position_quantity -= quantity
                    position_cost_basis -= allocated_cost
                    entry_fees_remaining -= allocated_entry_fee
                    episode_exit_notional += notional
                    episode_exit_fees += fee
                    episode_exit_quantity += quantity

                pending.remaining_quantity -= quantity
                if pending.remaining_quantity <= 0:
                    completed_orders += 1
                    pending = None
                elif was_partial:
                    partial_fill_count += 1

                if pending is None and position_quantity == _ZERO and episode_quantity_bought > 0:
                    if episode_entry_time is None or episode_exit_quantity <= 0:
                        raise RuntimeError(
                            "Closed episode lacks corresponding entry/exit accounting."
                        )
                    gross_pnl = episode_exit_notional - episode_entry_notional
                    total_episode_fees = episode_entry_fees + episode_exit_fees
                    net_pnl = gross_pnl - total_episode_fees
                    entry_cost_including_fee = episode_entry_notional + episode_entry_fees
                    trades.append(
                        ClosedTrade(
                            entry_at=episode_entry_time,
                            exit_at=candle.timestamp,
                            quantity=episode_quantity_bought,
                            entry_vwap=episode_entry_notional / episode_quantity_bought,
                            exit_vwap=episode_exit_notional / episode_exit_quantity,
                            gross_pnl=gross_pnl,
                            commission_paid=total_episode_fees,
                            net_pnl=net_pnl,
                            net_return_fraction=net_pnl / entry_cost_including_fee,
                        )
                    )
                    position_cost_basis = _ZERO
                    entry_fees_remaining = _ZERO
                    episode_entry_time = None
                    episode_quantity_bought = _ZERO
                    episode_entry_notional = _ZERO
                    episode_entry_fees = _ZERO
                    episode_exit_notional = _ZERO
                    episode_exit_fees = _ZERO
                    episode_exit_quantity = _ZERO

        if position_quantity > 0:
            exposure_bars += 1
        equity = cash + position_quantity * candle.close
        equity_curve.append(
            EquityPoint(
                timestamp=candle.timestamp + candle.interval,
                cash=cash,
                position_quantity=position_quantity,
                mark_price=candle.close,
                equity=equity,
            )
        )

    final_mark = evaluation[-1].close
    final_equity = cash + position_quantity * final_mark
    if equity_curve[-1].equity != final_equity:
        raise RuntimeError("Final equity does not match final marked account state.")
    metrics = _build_metrics(
        initial_cash=config.initial_cash,
        final_equity=final_equity,
        equity_curve=tuple(equity_curve),
        trades=tuple(trades),
        gross_turnover=gross_turnover,
        commission_paid=commission_paid,
        spread_cost_total=spread_cost_total,
        slippage_cost_total=slippage_cost_total,
        submitted_orders=submitted_orders,
        completed_orders=completed_orders,
        cancelled_orders=cancelled_orders,
        open_orders=1 if pending is not None else 0,
        fills=tuple(fills),
        partial_fill_count=partial_fill_count,
        exposure_bars=exposure_bars,
        bar_count=len(evaluation),
        open_position_quantity=position_quantity,
    )
    parameters_sorted = tuple(sorted(parameters.items()))
    run_material = {
        "engine_version": BACKTEST_ENGINE_VERSION,
        "dataset_version": dataset.version,
        "strategy_id": strategy.strategy_id,
        "strategy_version": strategy_version,
        "strategy_parameters": dict(parameters_sorted),
        "config": config.as_dict(),
        "segment_name": segment_name,
        "evaluation_range": {"start": start_index, "end": end},
        "warmup_start_index": warmup_start_index,
    }
    run_id = hashlib.sha256(_canonical_json_bytes(run_material)).hexdigest()
    return BacktestResult(
        run_id=run_id,
        engine_version=BACKTEST_ENGINE_VERSION,
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        dataset_metadata_json=_canonical_json_bytes(dataset.metadata()).decode("utf-8"),
        strategy_id=strategy.strategy_id,
        strategy_version=strategy_version,
        strategy_parameters=parameters_sorted,
        segment_name=segment_name,
        evaluation_range=IndexRange(start_index, end),
        warmup_start_index=warmup_start_index,
        config=config,
        metrics=metrics,
        signals=tuple(signals),
        fills=tuple(fills),
        trades=tuple(trades),
        equity_curve=tuple(equity_curve),
        first_evaluation_open=evaluation[0].timestamp,
        last_evaluation_close=evaluation[-1].timestamp + evaluation[-1].interval,
    )


def evaluate_train_validation_test(
    dataset: HistoricalDataset,
    strategy: CausalTargetStrategy,
    config: BacktestConfig,
    *,
    train_fraction: Decimal = Decimal("0.6"),
    validation_fraction: Decimal = Decimal("0.2"),
) -> SplitEvaluation:
    """Run independent segments, using only earlier bars as indicator warm-up.

    Each segment starts with its own configured initial cash. Metrics across these reset
    segments must not be naively added together; their purpose is temporal diagnostics.
    """
    partitions = chronological_split(
        dataset,
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
    )
    return SplitEvaluation(
        partitions=partitions,
        train=run_backtest(
            dataset, strategy, config,
            start_index=partitions.train.start,
            end_index=partitions.train.end,
            warmup_start_index=partitions.train.start,
            segment_name="train",
        ),
        validation=run_backtest(
            dataset, strategy, config,
            start_index=partitions.validation.start,
            end_index=partitions.validation.end,
            warmup_start_index=0,
            segment_name="validation",
        ),
        test=run_backtest(
            dataset, strategy, config,
            start_index=partitions.test.start,
            end_index=partitions.test.end,
            warmup_start_index=0,
            segment_name="test",
        ),
    )


def evaluate_walk_forward(
    dataset: HistoricalDataset,
    strategy: CausalTargetStrategy,
    config: BacktestConfig,
    *,
    train_bars: int,
    validation_bars: int,
    test_bars: int,
    step_bars: int | None = None,
) -> WalkForwardEvaluation:
    """Run disjoint out-of-sample folds with a fresh simulated account per test window."""
    folds = make_walk_forward_folds(
        dataset,
        train_bars=train_bars,
        validation_bars=validation_bars,
        test_bars=test_bars,
        step_bars=step_bars,
    )
    results = tuple(
        WalkForwardFoldResult(
            fold=fold,
            test_result=run_backtest(
                dataset,
                strategy,
                config,
                start_index=fold.test.start,
                end_index=fold.test.end,
                warmup_start_index=fold.train.start,
                segment_name=f"walk-forward-{fold.fold_index}",
            ),
        )
        for fold in folds
    )
    return WalkForwardEvaluation(dataset_version=dataset.version, folds=results)


@dataclass(slots=True)
class _PendingOrder:
    order_id: str
    side: Side
    requested_quantity: Decimal
    remaining_quantity: Decimal
    target_exposure: Decimal
    created_at: datetime


def _build_metrics(
    *,
    initial_cash: Decimal,
    final_equity: Decimal,
    equity_curve: tuple[EquityPoint, ...],
    trades: tuple[ClosedTrade, ...],
    gross_turnover: Decimal,
    commission_paid: Decimal,
    spread_cost_total: Decimal,
    slippage_cost_total: Decimal,
    submitted_orders: int,
    completed_orders: int,
    cancelled_orders: int,
    open_orders: int,
    fills: tuple[SimulatedFill, ...],
    partial_fill_count: int,
    exposure_bars: int,
    bar_count: int,
    open_position_quantity: Decimal,
) -> BacktestMetrics:
    net_profit = final_equity - initial_cash
    peak = equity_curve[0].equity
    max_drawdown = _ZERO
    for point in equity_curve:
        if point.equity > peak:
            peak = point.equity
        if peak > 0:
            drawdown = (peak - point.equity) / peak
            if drawdown > max_drawdown:
                max_drawdown = drawdown
    completed = len(trades)
    winners = sum(1 for trade in trades if trade.net_pnl > 0)
    losers = sum(1 for trade in trades if trade.net_pnl < 0)
    expectancy = (
        sum((trade.net_pnl for trade in trades), _ZERO) / Decimal(completed)
        if completed
        else None
    )
    win_rate = Decimal(winners) / Decimal(completed) if completed else None
    return BacktestMetrics(
        initial_equity=initial_cash,
        final_equity=final_equity,
        net_profit=net_profit,
        net_return_fraction=net_profit / initial_cash,
        max_drawdown_fraction=max_drawdown,
        expectancy_per_closed_trade=expectancy,
        win_rate_fraction=win_rate,
        closed_trade_count=completed,
        winning_trade_count=winners,
        losing_trade_count=losers,
        gross_turnover=gross_turnover,
        turnover_multiple=gross_turnover / initial_cash,
        commission_paid=commission_paid,
        estimated_spread_cost=spread_cost_total,
        estimated_slippage_cost=slippage_cost_total,
        submitted_order_count=submitted_orders,
        completed_order_count=completed_orders,
        cancelled_order_count=cancelled_orders,
        open_order_count_at_end=open_orders,
        fill_count=len(fills),
        partial_fill_count=partial_fill_count,
        time_in_market_fraction=Decimal(exposure_bars) / Decimal(bar_count),
        open_position_quantity=open_position_quantity,
    )



def _report_identity_hash(report: dict[str, Any]) -> str:
    """Derive the immutable run identity from canonical strategy/config/range fields."""
    try:
        strategy = report["strategy"]
        run_material = {
            "engine_version": report["engine_version"],
            "dataset_version": report["dataset_version"],
            "strategy_id": strategy["id"],
            "strategy_version": strategy["version"],
            "strategy_parameters": strategy["parameters"],
            "config": report["config"],
            "segment_name": report["segment_name"],
            "evaluation_range": report["evaluation_range"],
            "warmup_start_index": report["warmup_start_index"],
        }
    except (KeyError, TypeError) as exc:
        raise ValueError("Backtest report omits run-identity fields.") from exc
    return hashlib.sha256(_canonical_json_bytes(run_material)).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _metrics_dict(metrics: BacktestMetrics) -> dict[str, Any]:
    return {
        "initial_equity": str(metrics.initial_equity),
        "final_equity": str(metrics.final_equity),
        "net_profit": str(metrics.net_profit),
        "net_return_fraction": str(metrics.net_return_fraction),
        "max_drawdown_fraction": str(metrics.max_drawdown_fraction),
        "expectancy_per_closed_trade": (
            str(metrics.expectancy_per_closed_trade)
            if metrics.expectancy_per_closed_trade is not None
            else None
        ),
        "win_rate_fraction": (
            str(metrics.win_rate_fraction)
            if metrics.win_rate_fraction is not None
            else None
        ),
        "closed_trade_count": metrics.closed_trade_count,
        "winning_trade_count": metrics.winning_trade_count,
        "losing_trade_count": metrics.losing_trade_count,
        "gross_turnover": str(metrics.gross_turnover),
        "turnover_multiple": str(metrics.turnover_multiple),
        "commission_paid": str(metrics.commission_paid),
        "estimated_spread_cost": str(metrics.estimated_spread_cost),
        "estimated_slippage_cost": str(metrics.estimated_slippage_cost),
        "submitted_order_count": metrics.submitted_order_count,
        "completed_order_count": metrics.completed_order_count,
        "cancelled_order_count": metrics.cancelled_order_count,
        "open_order_count_at_end": metrics.open_order_count_at_end,
        "fill_count": metrics.fill_count,
        "partial_fill_count": metrics.partial_fill_count,
        "time_in_market_fraction": str(metrics.time_in_market_fraction),
        "open_position_quantity": str(metrics.open_position_quantity),
    }


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if value <= 0:
        return _ZERO
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _positive(value: Decimal, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{label} must be finite and positive.")


def _nonnegative(value: Decimal, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{label} must be finite and non-negative.")


def _fraction_below_one(value: Decimal, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0 or value >= 1:
        raise ValueError(f"{label} must be a Decimal in (0, 1).")


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone.")


def _iso_utc(value: datetime) -> str:
    _require_aware(value, "timestamp")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
