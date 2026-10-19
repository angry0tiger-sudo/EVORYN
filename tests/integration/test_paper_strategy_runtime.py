"""End-to-end mock-only runtime tests. No HTTP client or exchange API is invoked."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from evoryn.domain.models import (
    Market,
    MarketCandle,
    MarketSnapshot,
    OrderRequest,
    OrderType,
    Side,
)
from evoryn.execution.spot import SpotIntent
from evoryn.intelligence.regime import RegimeLabel, RegimePrediction
from evoryn.paper.broker import PaperOrderStatus
from evoryn.risk.models import RiskLimits
from evoryn.runtime.paper import (
    PaperStrategyRuntime,
    ProtectiveExitPolicy,
    RuntimeLedgerIntegrityError,
    PaperRuntimeResult,
    RuntimeStatus,
    SQLitePaperRuntimeLedger,
)
from evoryn.strategies.plugins import StrategyPluginRegistration, StrategyPluginRegistry
from evoryn.strategies.selector import StrategySelectionPolicy

MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
INTERVAL = timedelta(hours=1)
BASE_TIME = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
INITIAL_CAPITAL = Decimal("10000")


class MutableClock:
    def __init__(self, current: datetime = BASE_TIME) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def set(self, current: datetime) -> None:
        self.current = current


class MockRuntimeFeed:
    """In-memory market data with an explicit simulation-only identity."""

    simulation_only = True

    def __init__(
        self,
        candles: Sequence[MarketCandle],
        snapshot: MarketSnapshot,
    ) -> None:
        self.candles = list(candles)
        self.snapshot = snapshot

    def get_snapshot(self, market: Market) -> MarketSnapshot:
        if market != self.snapshot.market:
            raise ValueError("fixture market mismatch")
        return self.snapshot

    def get_history(
        self, market: Market, start: datetime, end: datetime
    ) -> Sequence[MarketSnapshot]:
        del market, start, end
        return ()

    def get_completed_candles(
        self,
        market: Market,
        interval: timedelta,
        *,
        now: datetime,
    ) -> Sequence[MarketCandle]:
        if market != MARKET or interval != INTERVAL:
            raise ValueError("fixture market/interval mismatch")
        return tuple(
            candle
            for candle in self.candles
            if candle.timestamp + candle.interval <= now
        )

    def add_candle(self, candle: MarketCandle, snapshot: MarketSnapshot) -> None:
        self.candles.append(candle)
        self.snapshot = snapshot


class DeterministicRegimeBrain:
    """Injected brain double returning a normalized, confidence-gated regime."""

    def __init__(self, label: RegimeLabel = RegimeLabel.BULL_TREND) -> None:
        self.label = label
        self.calls = 0

    def classify(
        self,
        completed_history: Sequence[MarketCandle],
        *,
        now: datetime,
    ) -> RegimePrediction:
        del now
        self.calls += 1
        as_of = completed_history[-1].timestamp + completed_history[-1].interval
        labels = (
            RegimeLabel.BULL_TREND,
            RegimeLabel.BEAR_TREND,
            RegimeLabel.RANGE_BOUND,
            RegimeLabel.HIGH_VOLATILITY,
        )
        probabilities = tuple(
            (label, Decimal("0.91") if label is self.label else Decimal("0.03"))
            for label in labels
        )
        return RegimePrediction(
            label=self.label,
            confidence=Decimal("0.91"),
            confidence_semantics="calibrated_probability",
            probabilities=probabilities,
            as_of=as_of,
            model_id="fixture-regime-brain",
            model_version="test-v1",
            feature_schema_version="fixture-candle-v1",
            model_fingerprint="a" * 64,
            min_confidence_threshold=Decimal("0.65"),
            max_data_age_intervals=2,
            abstained=False,
            reason_code="FIXTURE_CLASSIFIED",
        )


class LongStrategy:
    strategy_id = "fixture-long"
    strategy_version = "test-v1"

    def parameters(self) -> dict[str, str]:
        return {"mode": "long", "position_policy": "long-flat"}

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        return Decimal("1") if history else Decimal("0")


class FlatStrategy:
    strategy_id = "fixture-flat"
    strategy_version = "test-v1"

    def parameters(self) -> dict[str, str]:
        return {"mode": "flat", "position_policy": "long-flat"}

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        del history
        return Decimal("0")


def candle(timestamp: datetime, close: str = "100") -> MarketCandle:
    price = Decimal(close)
    return MarketCandle(
        market=MARKET,
        timestamp=timestamp,
        interval=INTERVAL,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=Decimal("1000"),
        source="synthetic-paper-feed",
    )


def snapshot(now: datetime, bid: str = "99", ask: str = "101") -> MarketSnapshot:
    bid_price, ask_price = Decimal(bid), Decimal(ask)
    return MarketSnapshot(
        market=MARKET,
        observed_at=now,
        source="synthetic-paper-feed",
        values={
            "bid": bid_price,
            "ask": ask_price,
            "last": (bid_price + ask_price) / Decimal("2"),
            "bid_size": Decimal("1000"),
            "ask_size": Decimal("1000"),
        },
    )


def registry() -> StrategyPluginRegistry:
    long = LongStrategy()
    flat = FlatStrategy()
    return StrategyPluginRegistry(
        (
            StrategyPluginRegistration(
                plugin_id=long.strategy_id,
                plugin_version=long.strategy_version,
                description="Deterministic long-only integration-test strategy.",
                strategy=long,
                supported_regimes=frozenset(
                    {RegimeLabel.BULL_TREND, RegimeLabel.RANGE_BOUND}
                ),
            ),
            StrategyPluginRegistration(
                plugin_id=flat.strategy_id,
                plugin_version=flat.strategy_version,
                description="Deterministic protective flat integration-test strategy.",
                strategy=flat,
                supported_regimes=frozenset(
                    {RegimeLabel.BEAR_TREND, RegimeLabel.HIGH_VOLATILITY}
                ),
            ),
        )
    )


def build_runtime(
    tmp_path: Path,
    feed: MockRuntimeFeed,
    brain: DeterministicRegimeBrain,
    clock: MutableClock,
    *,
    journal_path: Path | None = None,
) -> PaperStrategyRuntime:
    preferred = {
        RegimeLabel.BULL_TREND: ("fixture-long",),
        RegimeLabel.BEAR_TREND: ("fixture-flat",),
        RegimeLabel.RANGE_BOUND: ("fixture-long",),
        RegimeLabel.HIGH_VOLATILITY: ("fixture-flat",),
    }
    return PaperStrategyRuntime(
        market=MARKET,
        base_asset="BTC",
        quote_asset="USDT",
        feed=feed,
        regime_classifier=brain,
        registry=registry(),
        initial_capital=INITIAL_CAPITAL,
        risk_limits=RiskLimits(
            max_total_exposure=Decimal("20000"),
            max_market_exposure=Decimal("15000"),
            max_order_notional=Decimal("5000"),
            max_loss=Decimal("2000"),
            max_market_data_age_seconds=10,
        ),
        journal=SQLitePaperRuntimeLedger(
            journal_path or (tmp_path / "paper-runtime.sqlite3")
        ),
        selector_policy=StrategySelectionPolicy(
            min_confidence=Decimal("0.65"),
            required_evidence_count=2,
            required_stability_duration=INTERVAL,
            minimum_hold_duration=INTERVAL,
            maximum_prediction_age=timedelta(hours=4),
        ),
        protective_exit_policy=ProtectiveExitPolicy(
            max_adverse_move_fraction=Decimal("0.05")
        ),
        allocation_fraction=Decimal("0.25"),
        quantity_step=Decimal("0.001"),
        preferred_strategy_by_regime=preferred,
        interval=INTERVAL,
        clock=clock,
    )


def bootstrapped(
    tmp_path: Path,
) -> tuple[PaperStrategyRuntime, MockRuntimeFeed, DeterministicRegimeBrain, MutableClock, Path]:
    candles = tuple(
        candle(BASE_TIME - INTERVAL * offset)
        for offset in range(5, 0, -1)
    )
    now = candles[-1].timestamp + INTERVAL
    clock = MutableClock(now)
    feed = MockRuntimeFeed(candles, snapshot(now))
    brain = DeterministicRegimeBrain()
    journal_path = tmp_path / "paper-runtime.sqlite3"
    runtime = build_runtime(tmp_path, feed, brain, clock, journal_path=journal_path)
    result = runtime.bootstrap(now=now)
    assert result.status is RuntimeStatus.BOOTSTRAPPED
    return runtime, feed, brain, clock, journal_path


def advance(
    runtime: PaperStrategyRuntime,
    feed: MockRuntimeFeed,
    clock: MutableClock,
    *,
    bid: str = "99",
    ask: str = "101",
    close: str = "100",
) -> PaperRuntimeResult:
    candle_start = runtime.history[-1].timestamp + INTERVAL
    next_now = candle_start + INTERVAL
    clock.set(next_now)
    feed.add_candle(
        candle(candle_start, close),
        snapshot(next_now, bid=bid, ask=ask),
    )
    return runtime.step(now=next_now)


def test_brain_to_strategy_intent_fill_account_and_reconciliation(tmp_path: Path) -> None:
    runtime, feed, brain, clock, _ = bootstrapped(tmp_path)

    first = advance(runtime, feed, clock)
    assert first.status is RuntimeStatus.ABSTAINED
    assert first.selected_strategy_id is None
    assert runtime.broker.position_quantity == Decimal("0")

    second = advance(runtime, feed, clock)
    assert brain.calls == 2
    assert second.status is RuntimeStatus.PROCESSED
    assert second.regime_label == RegimeLabel.BULL_TREND.value
    assert second.selected_strategy_id == "fixture-long"
    assert second.target_exposure == Decimal("1")
    assert second.order_status == PaperOrderStatus.FILLED.value
    assert second.filled_quantity == Decimal("25")
    assert second.reconciliation_matched is True
    assert second.account is not None
    assert second.account.base_quantity == Decimal("25")
    assert second.account.portfolio.position_quantity == Decimal("25")
    assert second.account.portfolio.market_exposure == Decimal("2500")
    assert second.account.portfolio.account_reconciled is True
    assert len(runtime.execution.ledger.intents) == 1
    assert runtime.execution.ledger.reconciliations[-1].matched is True
    assert runtime.mock_venue.get_balances().base_available == Decimal("25")
    assert runtime.broker.cash == runtime.mock_venue.get_balances().quote_available
    assert runtime.journal.verify()


def test_portfolio_exposes_open_order_reservation_and_blocks_new_intents(
    tmp_path: Path,
) -> None:
    runtime, feed, _, clock, _ = bootstrapped(tmp_path)
    request = OrderRequest(
        client_order_id="paper-open-limit-1",
        market=MARKET,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("2"),
        price=Decimal("90"),
    )
    result = runtime.execution.execute(
        SpotIntent(
            correlation_id="paper-open-limit-1",
            request=request,
            created_at=clock(),
        )
    )
    assert result.order.status is PaperOrderStatus.OPEN
    account = runtime.account_builder.build(feed.get_snapshot(MARKET), reconciled=True)
    assert account.active_order_ids == (result.order.order_id,)
    assert account.portfolio.reserved_buy_notional == Decimal("180")
    assert account.portfolio.free_capital == Decimal("9820")

    advance(runtime, feed, clock)
    follow_up = advance(runtime, feed, clock)
    assert follow_up.reason_code == "ACTIVE_ORDER_RECONCILIATION_PENDING"
    assert len(runtime.broker.list_orders()) == 1


def test_protective_exit_flattens_position_after_adverse_regime_and_price_move(
    tmp_path: Path,
) -> None:
    runtime, feed, brain, clock, _ = bootstrapped(tmp_path)
    advance(runtime, feed, clock)
    entry = advance(runtime, feed, clock)
    assert entry.order_status == PaperOrderStatus.FILLED.value
    assert runtime.broker.position_quantity == Decimal("25")

    brain.label = RegimeLabel.BEAR_TREND
    exit_result = advance(runtime, feed, clock, bid="79", ask="81", close="80")
    assert exit_result.status is RuntimeStatus.PROCESSED
    assert exit_result.protective_exit is True
    assert exit_result.target_exposure == Decimal("0")
    assert exit_result.order_status == PaperOrderStatus.FILLED.value
    assert exit_result.reconciliation_matched is True
    assert runtime.broker.position_quantity == Decimal("0")
    assert runtime.broker.realized_pnl < Decimal("0")
    assert runtime.mock_venue.get_balances().base_available == Decimal("0")


def test_clean_restart_restores_fills_and_does_not_duplicate_an_order(tmp_path: Path) -> None:
    runtime, feed, brain, clock, journal_path = bootstrapped(tmp_path)
    advance(runtime, feed, clock)
    entry = advance(runtime, feed, clock)
    assert entry.order_status == PaperOrderStatus.FILLED.value
    original_orders = runtime.broker.list_orders()
    original_cash = runtime.broker.cash
    original_quantity = runtime.broker.position_quantity

    recovered = build_runtime(
        tmp_path, feed, brain, clock, journal_path=journal_path
    )
    assert recovered.halted is False
    assert recovered.broker.list_orders() == original_orders
    assert recovered.broker.cash == original_cash
    assert recovered.broker.position_quantity == original_quantity
    assert recovered.mock_venue.get_balances().base_available == original_quantity

    repeated_tick = recovered.step(now=clock())
    assert repeated_tick.status is RuntimeStatus.NO_NEW_CANDLE
    assert recovered.broker.list_orders() == original_orders
    assert recovered.broker.position_quantity == original_quantity


def test_interrupted_intent_recovery_halts_without_resubmission(tmp_path: Path) -> None:
    runtime, feed, brain, clock, journal_path = bootstrapped(tmp_path)
    advance(runtime, feed, clock)
    advance(runtime, feed, clock)
    original_orders = runtime.broker.list_orders()
    runtime.journal.append(
        "INTENT_PREPARED",
        correlation_id="simulated-process-crash:order",
        occurred_at=clock(),
        payload={
            "client_order_id": "paper-unresolved-fixture",
            "market": MARKET.symbol,
            "side": Side.BUY.value,
            "quantity": "1",
        },
    )

    recovered = build_runtime(
        tmp_path, feed, brain, clock, journal_path=journal_path
    )
    assert recovered.halted is True
    assert "interrupted" in (recovered.halt_reason or "")
    assert recovered.step(now=clock()).status is RuntimeStatus.HALTED
    assert recovered.broker.list_orders() == original_orders


def test_runtime_refuses_feed_without_explicit_mock_boundary(tmp_path: Path) -> None:
    runtime, feed, brain, clock, journal_path = bootstrapped(tmp_path)
    assert runtime.halted is False
    feed.simulation_only = False
    with pytest.raises(ValueError, match="simulation_only"):
        build_runtime(tmp_path, feed, brain, clock, journal_path=journal_path)


def test_runtime_ledger_detects_tampered_persisted_event(tmp_path: Path) -> None:
    path = tmp_path / "tampered-runtime.sqlite3"
    ledger = SQLitePaperRuntimeLedger(path)
    ledger.append(
        "FIXTURE_EVENT",
        correlation_id="fixture-1",
        occurred_at=BASE_TIME,
        payload={"value": "original"},
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE paper_runtime_events SET payload_json = ? WHERE sequence = 1",
            ('{"value":"tampered"}',),
        )
    with pytest.raises(RuntimeLedgerIntegrityError, match="hash-chain"):
        ledger.events()
