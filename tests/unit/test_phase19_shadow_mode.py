"""Mock-only tests for Phase 19. No test connects to a live venue or submits an order."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence

import pytest

from evoryn.domain.models import Market, MarketCandle
from evoryn.intelligence.regime import RegimeLabel, RegimePrediction
from evoryn.learning_lab.backtest import IndexRange
from evoryn.learning_lab.experiment_registry import (
    ExperimentMode,
    ExperimentRegistry,
    ExperimentSpec,
    LearningEventType,
    LearningLabLedger,
)
from evoryn.learning_lab.promotion import ChampionChallengerController
from evoryn.shadow.engine import (
    NobitexRestCandleFeed,
    ShadowConfig,
    ShadowOutcome,
    ShadowPollStatus,
    ShadowPollingService,
    ShadowRunner,
    ShadowSafetyError,
    strategy_fingerprint,
)


class FixedExposureStrategy:
    """Minimal pure strategy; it receives completed candles and no execution dependencies."""

    strategy_id = "phase19-fixed-target"
    strategy_version = "1.0.0"

    def __init__(self, exposure: Decimal = Decimal("1")) -> None:
        self.exposure = exposure

    def parameters(self) -> dict[str, str]:
        return {"target_exposure": str(self.exposure)}

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        assert history
        return self.exposure


class FakeRegimeClassifier:
    def __init__(self, *, unknown: bool = False, identity_drift_at: int | None = None) -> None:
        self.unknown = unknown
        self.identity_drift_at = identity_drift_at
        self.calls = 0

    def classify(
        self, completed_history: Sequence[MarketCandle], *, now: datetime
    ) -> RegimePrediction:
        self.calls += 1
        as_of = completed_history[-1].timestamp + completed_history[-1].interval
        drift = self.identity_drift_at is not None and self.calls >= self.identity_drift_at
        fingerprint = "e" * 64 if drift else "f" * 64
        if self.unknown:
            return RegimePrediction(
                label=RegimeLabel.UNKNOWN,
                confidence=Decimal("0"),
                confidence_semantics="heuristic_score",
                probabilities=(),
                as_of=as_of,
                model_id="fake-regime",
                model_version="1.0.0",
                feature_schema_version="regime-features/v1",
                model_fingerprint=fingerprint,
                min_confidence_threshold=Decimal("0.65"),
                max_data_age_intervals=2,
                abstained=True,
                reason_code="MODEL_NOT_READY",
            )
        return RegimePrediction(
            label=RegimeLabel.BULL_TREND,
            confidence=Decimal("0.8"),
            confidence_semantics="calibrated_probability",
            probabilities=(
                (RegimeLabel.BULL_TREND, Decimal("0.8")),
                (RegimeLabel.BEAR_TREND, Decimal("0.1")),
                (RegimeLabel.RANGE_BOUND, Decimal("0.05")),
                (RegimeLabel.HIGH_VOLATILITY, Decimal("0.05")),
            ),
            as_of=as_of,
            model_id="fake-regime",
            model_version="1.0.0",
            feature_schema_version="regime-features/v1",
            model_fingerprint=fingerprint,
            min_confidence_threshold=Decimal("0.65"),
            max_data_age_intervals=2,
            abstained=False,
            reason_code="CLASSIFIED",
        )


class FakeCandleReader:
    def __init__(self, candles: Sequence[MarketCandle] = ()) -> None:
        self.candles = tuple(candles)
        self.calls = 0
        self.fail = False

    def get_market_candles(
        self,
        market: Market,
        *,
        interval: timedelta,
        start: datetime,
        end: datetime,
    ) -> Sequence[MarketCandle]:
        self.calls += 1
        if self.fail:
            raise TimeoutError("fake timeout with secret payload must not be persisted")
        return self.candles


class FakeFeed:
    source_id = "nobitex"
    source_version = "nobitex-rest-ohlcv/v1"

    def __init__(
        self,
        market: Market,
        interval: timedelta,
        candles: Sequence[MarketCandle],
    ) -> None:
        self.market = market
        self.interval = interval
        self.candles = tuple(candles)
        self.fail = False
        self.calls = 0

    def fetch_completed(self, *, now: datetime) -> Sequence[MarketCandle]:
        self.calls += 1
        if self.fail:
            raise TimeoutError("secret=do-not-store")
        return self.candles


class Fixture:
    def __init__(
        self,
        tmp_path: Path,
        *,
        exposure: Decimal = Decimal("1"),
        unknown_regime: bool = False,
        identity_drift_at: int | None = None,
        halted: bool = False,
    ) -> None:
        self.now = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
        self.interval = timedelta(minutes=1)
        self.market = Market(symbol="BTC/USDT", venue="nobitex", market_type="spot")
        self.config = ShadowConfig(
            market=self.market,
            interval=self.interval,
            source_id="nobitex",
            source_version="nobitex-rest-ohlcv/v1",
            virtual_capital=Decimal("1000"),
            allocation_fraction=Decimal("0.5"),
            quantity_step=Decimal("0.001"),
            max_candle_age_seconds=180,
            max_future_skew_seconds=1,
            min_regime_confidence=Decimal("0.65"),
            max_history_candles=100,
        )
        self.strategy = FixedExposureStrategy(exposure)
        self.baseline_strategy = FixedExposureStrategy(Decimal("0"))
        self.regime = FakeRegimeClassifier(
            unknown=unknown_regime,
            identity_drift_at=identity_drift_at,
        )
        self.halted = halted
        self.ledger = LearningLabLedger(tmp_path / "learning-ledger.jsonl")
        self.registry = ExperimentRegistry(tmp_path / "registry", self.ledger)
        self.spec = ExperimentSpec(
            code_revision="a" * 40,
            dataset_version="b" * 64,
            feature_schema_version="shadow-market-candle/v1",
            evaluation_config_fingerprint=self.config.evaluation_config_fingerprint,
            model_id=self.strategy.strategy_id,
            model_version=self.strategy.strategy_version,
            model_fingerprint="c" * 64,
            parameters=tuple(sorted(self.strategy.parameters().items())),
            random_seed=0,
            train_range=_index_range(0, 1),
            validation_range=_index_range(1, 2),
            test_range=_index_range(2, 3),
            mode=ExperimentMode.SHADOW,
            created_at=self.now,
        )
        self.baseline_spec = ExperimentSpec(
            code_revision="d" * 40,
            dataset_version="b" * 64,
            feature_schema_version="shadow-market-candle/v1",
            evaluation_config_fingerprint=self.config.evaluation_config_fingerprint,
            model_id=self.baseline_strategy.strategy_id,
            model_version=self.baseline_strategy.strategy_version,
            model_fingerprint="e" * 64,
            parameters=tuple(sorted(self.baseline_strategy.parameters().items())),
            random_seed=0,
            train_range=_index_range(0, 1),
            validation_range=_index_range(1, 2),
            test_range=_index_range(2, 3),
            mode=ExperimentMode.PAPER,
            created_at=self.now,
        )
        self.registry.register(
            self.baseline_spec, correlation_id="test:register-paper-baseline"
        )
        self.registry.register(self.spec, correlation_id="test:register-shadow")
        self.champion_controller = ChampionChallengerController(
            registry=self.registry,
            ledger=self.ledger,
            champion_experiment_id=self.baseline_spec.experiment_id,
        )
        self.runner = ShadowRunner(
            config=self.config,
            experiment_spec=self.spec,
            baseline_experiment_spec=self.baseline_spec,
            registry=self.registry,
            strategy=self.strategy,
            baseline_strategy=self.baseline_strategy,
            champion_controller=self.champion_controller,
            regime_classifier=self.regime,
            auto_halt_status=lambda: self.halted,
            utcnow=lambda: self.now,
        )

    def candle(self, *, minutes_before_now: int = 1, close: str = "100") -> MarketCandle:
        ts = self.now - self.interval * minutes_before_now
        value = Decimal(close)
        return MarketCandle(
            market=self.market,
            timestamp=ts,
            interval=self.interval,
            open=value,
            high=value + Decimal("1"),
            low=value - Decimal("1"),
            close=value,
            volume=Decimal("10"),
            source="nobitex",
        )


def _index_range(start: int, end: int) -> IndexRange:
    return IndexRange(start, end)


def test_shadow_runner_records_hypothetical_entry_to_persistent_learning_ledger(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    candle = fixture.candle()
    snapshot = fixture.runner.process_candle(candle, received_at=fixture.now)

    assert snapshot.mode is ExperimentMode.SHADOW
    assert snapshot.outcome is ShadowOutcome.ENTER_LONG
    assert snapshot.target_exposure == Decimal("1")
    assert snapshot.target_quantity == Decimal("5.000")
    assert snapshot.quantity_delta == Decimal("5.000")
    assert snapshot.baseline_experiment_id == fixture.baseline_spec.experiment_id
    assert snapshot.baseline_strategy_id == fixture.baseline_strategy.strategy_id
    assert snapshot.baseline_target_exposure == Decimal("0")
    assert snapshot.baseline_target_quantity == Decimal("0.000")
    assert snapshot.baseline_quantity_delta == Decimal("0.000")
    assert snapshot.risk_assessment_status == "NOT_EVALUATED"
    assert snapshot.execution_authorized is False
    assert fixture.ledger.events[-1].event_type is LearningEventType.SHADOW_DECISION_RECORDED
    assert fixture.ledger.verify() is True


def test_snapshot_never_contains_auth_header_url_or_exception_body(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    fixture.runner.record_feed_failure(
        occurred_at=fixture.now,
        error_kind="TimeoutError",
        correlation_id="poll:001",
    )

    rendered = fixture.ledger.events[-1].payload_json
    assert "secret=do-not-store" not in rendered
    assert "Authorization" not in rendered
    assert "https://" not in rendered
    assert '"mode":"shadow"' in rendered
    assert '"execution_authorized":false' in rendered


def test_duplicate_candle_is_rejected_without_advancing_state(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    candle = fixture.candle()
    first = fixture.runner.process_candle(candle, received_at=fixture.now)
    event_count = len(fixture.ledger.events)
    duplicate = fixture.runner.process_candle(
        candle, received_at=fixture.now, correlation_id="duplicate:001"
    )

    assert first.outcome is ShadowOutcome.ENTER_LONG
    assert duplicate.outcome is ShadowOutcome.ABSTAIN
    assert duplicate.reason_code == "DUPLICATE_CANDLE"
    assert fixture.runner.last_candle_at == candle.timestamp
    assert len(fixture.ledger.events) == event_count + 1
    assert fixture.ledger.events[-1].event_type is LearningEventType.SHADOW_INPUT_REJECTED


def test_gap_latches_continuity_until_explicit_reconciliation(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    first = fixture.candle(minutes_before_now=3)
    fixture.runner.process_candle(first, received_at=fixture.now)
    gap = fixture.candle(minutes_before_now=1)
    rejected = fixture.runner.process_candle(gap, received_at=fixture.now)

    assert rejected.outcome is ShadowOutcome.ABSTAIN
    assert rejected.reason_code == "CANDLE_GAP"
    assert fixture.runner.continuity_blocked is True
    fixture.now += fixture.interval
    next_candle = fixture.candle(minutes_before_now=1)
    blocked = fixture.runner.process_candle(next_candle, received_at=fixture.now)
    assert blocked.reason_code == "CONTINUITY_RECONCILIATION_REQUIRED"

    history = tuple(
        _historical_candle(
            fixture.market,
            fixture.interval,
            fixture.now - fixture.interval * minutes_before_now,
            Decimal("100"),
        )
        for minutes_before_now in (4, 3, 2)
    )
    fixture.runner.reset_after_reconciliation(
        history,
        evidence_ref="incident:gap-reconciled",
        received_at=fixture.now,
    )
    assert fixture.runner.continuity_blocked is False
    assert fixture.runner.last_candle_at == history[-1].timestamp
    assert fixture.ledger.events[-1].event_type is LearningEventType.SHADOW_RECONCILIATION


def test_stale_feed_abstains_and_does_not_mutate_virtual_state(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    stale = _historical_candle(
        fixture.market,
        fixture.interval,
        fixture.now - fixture.interval * 10,
        Decimal("100"),
    )
    snapshot = fixture.runner.process_candle(stale, received_at=fixture.now)

    assert snapshot.outcome is ShadowOutcome.ABSTAIN
    assert snapshot.reason_code == "STALE_CANDLE"
    assert fixture.runner.last_candle_at is None
    assert fixture.runner.history == ()


def test_unknown_regime_abstains(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, unknown_regime=True)
    snapshot = fixture.runner.process_candle(
        fixture.candle(), received_at=fixture.now
    )

    assert snapshot.outcome is ShadowOutcome.ABSTAIN
    assert snapshot.reason_code == "REGIME_ABSTAINED_MODEL_NOT_READY"
    assert snapshot.regime_label == RegimeLabel.UNKNOWN.value
    assert snapshot.target_quantity is None


def test_regime_model_identity_drift_latches_fail_closed(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, identity_drift_at=2)
    first = fixture.candle(minutes_before_now=2)
    fixture.runner.process_candle(first, received_at=fixture.now)
    second = fixture.candle(minutes_before_now=1)
    drift = fixture.runner.process_candle(second, received_at=fixture.now)

    assert drift.outcome is ShadowOutcome.ABSTAIN
    assert drift.reason_code == "REGIME_MODEL_IDENTITY_CHANGED"
    fixture.now += fixture.interval
    next_snapshot = fixture.runner.process_candle(
        fixture.candle(minutes_before_now=1), received_at=fixture.now
    )
    assert next_snapshot.reason_code == "MODEL_IDENTITY_REVIEW_REQUIRED"


def test_auto_halt_blocks_hypothetical_signal_without_clearing_halt(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, halted=True)
    snapshot = fixture.runner.process_candle(
        fixture.candle(), received_at=fixture.now
    )

    assert snapshot.outcome is ShadowOutcome.ABSTAIN
    assert snapshot.reason_code == "AUTO_HALT_ACTIVE"
    assert fixture.runner.history == ()
    assert fixture.halted is True


def test_auto_halt_status_exception_fails_closed(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    fixture.runner._auto_halt_status = lambda: (_ for _ in ()).throw(OSError("secret"))
    snapshot = fixture.runner.process_candle(fixture.candle(), received_at=fixture.now)

    assert snapshot.outcome is ShadowOutcome.ABSTAIN
    assert snapshot.reason_code == "AUTO_HALT_STATUS_UNAVAILABLE"
    assert "secret" not in fixture.ledger.events[-1].payload_json


def test_strategy_exception_is_abstain_and_not_a_risk_approval(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)

    def fail_strategy(history: Sequence[MarketCandle]) -> Decimal:
        raise RuntimeError("sensitive diagnostic string")

    fixture.strategy.target_exposure = fail_strategy  # type: ignore[method-assign]
    snapshot = fixture.runner.process_candle(fixture.candle(), received_at=fixture.now)

    assert snapshot.outcome is ShadowOutcome.ABSTAIN
    assert snapshot.reason_code == "STRATEGY_OR_BASELINE_OUTPUT_INVALID"
    assert snapshot.target_quantity is None
    assert snapshot.baseline_target_quantity is None
    assert snapshot.risk_assessment_status == "NOT_EVALUATED"
    assert "sensitive diagnostic string" not in fixture.ledger.events[-1].payload_json


def test_paper_baseline_failure_abstains_the_whole_comparison(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)

    def fail_baseline(history: Sequence[MarketCandle]) -> Decimal:
        raise RuntimeError("baseline-private-error")

    fixture.baseline_strategy.target_exposure = fail_baseline  # type: ignore[method-assign]
    snapshot = fixture.runner.process_candle(fixture.candle(), received_at=fixture.now)

    assert snapshot.outcome is ShadowOutcome.ABSTAIN
    assert snapshot.reason_code == "STRATEGY_OR_BASELINE_OUTPUT_INVALID"
    assert snapshot.target_quantity is None
    assert snapshot.baseline_target_quantity is None
    assert fixture.runner.history == ()
    assert "baseline-private-error" not in fixture.ledger.events[-1].payload_json


def test_paper_baseline_identity_drift_latches_fail_closed(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    fixture.baseline_strategy.exposure = Decimal("0.5")

    first = fixture.runner.process_candle(fixture.candle(), received_at=fixture.now)
    assert first.outcome is ShadowOutcome.ABSTAIN
    assert first.reason_code == "PAPER_BASELINE_IDENTITY_CHANGED"
    assert fixture.runner.history == ()

    fixture.now += fixture.interval
    second = fixture.runner.process_candle(
        fixture.candle(minutes_before_now=1), received_at=fixture.now
    )
    assert second.reason_code == "MODEL_IDENTITY_REVIEW_REQUIRED"
    assert fixture.runner.history == ()


def test_baseline_must_share_registered_training_data_identity(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    mismatched = ExperimentSpec(
        code_revision=fixture.baseline_spec.code_revision,
        dataset_version="9" * 64,
        feature_schema_version=fixture.baseline_spec.feature_schema_version,
        evaluation_config_fingerprint=fixture.baseline_spec.evaluation_config_fingerprint,
        model_id=fixture.baseline_spec.model_id,
        model_version=fixture.baseline_spec.model_version,
        model_fingerprint=fixture.baseline_spec.model_fingerprint,
        parameters=fixture.baseline_spec.parameters,
        random_seed=fixture.baseline_spec.random_seed,
        train_range=fixture.baseline_spec.train_range,
        validation_range=fixture.baseline_spec.validation_range,
        test_range=fixture.baseline_spec.test_range,
        mode=ExperimentMode.PAPER,
        created_at=fixture.now + timedelta(seconds=1),
    )
    fixture.registry.register(mismatched, correlation_id="test:register-mismatched-paper")
    mismatched_controller = ChampionChallengerController(
        registry=fixture.registry,
        ledger=fixture.ledger,
        champion_experiment_id=mismatched.experiment_id,
    )
    with pytest.raises(ShadowSafetyError, match="data/split identity"):
        ShadowRunner(
            config=fixture.config,
            experiment_spec=fixture.spec,
            baseline_experiment_spec=mismatched,
            registry=fixture.registry,
            strategy=fixture.strategy,
            baseline_strategy=fixture.baseline_strategy,
            champion_controller=mismatched_controller,
            regime_classifier=fixture.regime,
            auto_halt_status=lambda: False,
            utcnow=lambda: fixture.now,
        )


def test_wrong_market_and_interval_are_rejected(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    candle = fixture.candle()
    wrong_market = MarketCandle(
        market=Market(symbol="ETH/USDT", venue="nobitex", market_type="spot"),
        timestamp=candle.timestamp,
        interval=candle.interval,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
        source="nobitex",
    )
    rejected = fixture.runner.process_candle(wrong_market, received_at=fixture.now)
    assert rejected.reason_code == "MARKET_IDENTITY_MISMATCH"
    assert rejected.outcome is ShadowOutcome.ABSTAIN


def test_runner_rejects_non_shadow_experiment(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    paper_spec = ExperimentSpec(
        code_revision=fixture.spec.code_revision,
        dataset_version=fixture.spec.dataset_version,
        feature_schema_version=fixture.spec.feature_schema_version,
        evaluation_config_fingerprint=fixture.spec.evaluation_config_fingerprint,
        model_id=fixture.spec.model_id,
        model_version=fixture.spec.model_version,
        model_fingerprint=fixture.spec.model_fingerprint,
        parameters=fixture.spec.parameters,
        random_seed=fixture.spec.random_seed,
        train_range=fixture.spec.train_range,
        validation_range=fixture.spec.validation_range,
        test_range=fixture.spec.test_range,
        mode=ExperimentMode.PAPER,
        created_at=fixture.now,
    )
    with pytest.raises(ShadowSafetyError, match="mode must be SHADOW"):
        ShadowRunner(
            config=fixture.config,
            experiment_spec=paper_spec,
            baseline_experiment_spec=fixture.baseline_spec,
            registry=fixture.registry,
            strategy=fixture.strategy,
            baseline_strategy=fixture.baseline_strategy,
            regime_classifier=fixture.regime,
            auto_halt_status=lambda: False,
            utcnow=lambda: fixture.now,
        )


def test_rest_feed_filters_forming_candle_and_poll_bootstraps_history(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    candles = tuple(
        _historical_candle(
            fixture.market,
            fixture.interval,
            fixture.now - fixture.interval * offset,
            Decimal("100"),
        )
        for offset in (3, 2, 1, 0)
    )
    # The final candle opens at now and is not complete until the next interval.
    reader = FakeCandleReader(candles)
    feed = NobitexRestCandleFeed(
        reader,
        market=fixture.market,
        interval=fixture.interval,
        lookback_candles=10,
    )
    fetched = feed.fetch_completed(now=fixture.now)
    assert len(fetched) == 3

    # Bootstrap the initial feed window without logging historic candles as live decisions.
    poll_feed = FakeFeed(fixture.market, fixture.interval, fetched)
    poller = ShadowPollingService(
        feed=poll_feed,
        runner=fixture.runner,
        utcnow=lambda: fixture.now,
    )
    result = poller.poll_once(now=fixture.now)
    assert result.status is ShadowPollStatus.OBSERVED
    assert len(result.snapshots) == 1
    assert result.snapshots[0].outcome is ShadowOutcome.ENTER_LONG
    assert fixture.runner.last_candle_at == fetched[-1].timestamp


def test_feed_timeout_is_ledgered_without_exception_text(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    feed = FakeFeed(fixture.market, fixture.interval, ())
    feed.fail = True
    poller = ShadowPollingService(
        feed=feed,
        runner=fixture.runner,
        utcnow=lambda: fixture.now,
    )
    result = poller.poll_once(now=fixture.now)

    assert result.status is ShadowPollStatus.FEED_FAILURE
    event = fixture.ledger.events[-1]
    assert event.event_type is LearningEventType.SHADOW_FEED_FAILURE
    assert "secret=do-not-store" not in event.payload_json
    assert "TimeoutError" in event.payload_json


def test_unregistered_experiment_is_rejected(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    other_ledger = LearningLabLedger(tmp_path / "other.jsonl")
    other_registry = ExperimentRegistry(tmp_path / "other-registry", other_ledger)
    with pytest.raises(KeyError):
        ShadowRunner(
            config=fixture.config,
            experiment_spec=fixture.spec,
            baseline_experiment_spec=fixture.baseline_spec,
            registry=other_registry,
            strategy=fixture.strategy,
            baseline_strategy=fixture.baseline_strategy,
            regime_classifier=fixture.regime,
            auto_halt_status=lambda: False,
            utcnow=lambda: fixture.now,
        )


def test_non_persistent_learning_ledger_is_rejected(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    memory_ledger = LearningLabLedger()
    memory_registry = ExperimentRegistry(tmp_path / "memory-registry", memory_ledger)
    memory_registry.register(fixture.spec, correlation_id="test:memory-only")
    with pytest.raises(ShadowSafetyError, match="fsync-persistent"):
        ShadowRunner(
            config=fixture.config,
            experiment_spec=fixture.spec,
            baseline_experiment_spec=fixture.baseline_spec,
            registry=memory_registry,
            strategy=fixture.strategy,
            baseline_strategy=fixture.baseline_strategy,
            regime_classifier=fixture.regime,
            auto_halt_status=lambda: False,
            utcnow=lambda: fixture.now,
        )


def _historical_candle(
    market: Market, interval: timedelta, timestamp: datetime, close: Decimal
) -> MarketCandle:
    return MarketCandle(
        market=market,
        timestamp=timestamp,
        interval=interval,
        open=close,
        high=close + Decimal("1"),
        low=close - Decimal("1"),
        close=close,
        volume=Decimal("10"),
        source="nobitex",
    )
