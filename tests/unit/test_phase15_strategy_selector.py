"""Synthetic tests for Phase 15 strategy plugins, hysteresis, ledger and OOS evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from evoryn.domain.models import Market, MarketCandle
from evoryn.execution.spot import ExecutionLedger
from evoryn.intelligence.regime import RegimeLabel, RegimePrediction
from evoryn.learning_lab.backtest import (
    BacktestConfig,
    chronological_split,
)
from evoryn.learning_lab.historical_data import HistoricalDataset
from evoryn.learning_lab.strategy_selection import evaluate_strategy_selector_out_of_sample
from evoryn.strategies.baseline import FlatBaseline, SmaCrossoverBaseline
from evoryn.strategies.plugins import (
    StrategyPluginRegistration,
    StrategyPluginRegistry,
    build_baseline_registry,
)
from evoryn.strategies.selector import (
    RegimeStrategySelector,
    SelectionStatus,
    StrategySelectionPolicy,
)


BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
INTERVAL = timedelta(hours=1)
MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
SOURCE = "synthetic-phase15-fixture"


def prediction(
    label: RegimeLabel,
    timestamp: datetime,
    *,
    confidence: str = "0.9",
    model_fingerprint: str = "model-fingerprint-v1",
    confidence_semantics: str = "hmm_posterior",
    abstained: bool = False,
) -> RegimePrediction:
    """Create a stable and fully audited regime prediction fixture."""
    probs = {
        RegimeLabel.BULL_TREND: Decimal("0.90"),
        RegimeLabel.BEAR_TREND: Decimal("0.04"),
        RegimeLabel.RANGE_BOUND: Decimal("0.03"),
        RegimeLabel.HIGH_VOLATILITY: Decimal("0.03"),
    }
    if label is RegimeLabel.BEAR_TREND:
        probs = {
            RegimeLabel.BULL_TREND: Decimal("0.03"),
            RegimeLabel.BEAR_TREND: Decimal("0.90"),
            RegimeLabel.RANGE_BOUND: Decimal("0.04"),
            RegimeLabel.HIGH_VOLATILITY: Decimal("0.03"),
        }
    elif label is RegimeLabel.RANGE_BOUND:
        probs = {
            RegimeLabel.BULL_TREND: Decimal("0.03"),
            RegimeLabel.BEAR_TREND: Decimal("0.03"),
            RegimeLabel.RANGE_BOUND: Decimal("0.90"),
            RegimeLabel.HIGH_VOLATILITY: Decimal("0.04"),
        }
    elif label is RegimeLabel.HIGH_VOLATILITY:
        probs = {
            RegimeLabel.BULL_TREND: Decimal("0.03"),
            RegimeLabel.BEAR_TREND: Decimal("0.03"),
            RegimeLabel.RANGE_BOUND: Decimal("0.04"),
            RegimeLabel.HIGH_VOLATILITY: Decimal("0.90"),
        }
    effective_confidence = (
        "0" if abstained or label is RegimeLabel.UNKNOWN else confidence
    )
    return RegimePrediction(
        label=label,
        confidence=Decimal(effective_confidence),
        confidence_semantics=confidence_semantics,
        probabilities=tuple(probs.items()) if not abstained else (),
        as_of=timestamp,
        model_id="synthetic-hmm",
        model_version="test-1.0.0",
        feature_schema_version="1.0.0",
        model_fingerprint=model_fingerprint,
        min_confidence_threshold=Decimal("0.65"),
        max_data_age_intervals=2,
        abstained=abstained or label is RegimeLabel.UNKNOWN,
        reason_code="TEST_FIXTURE",
    )


def selection_time(index: int) -> datetime:
    return BASE_TIME + INTERVAL * index


def select_at(
    selector: RegimeStrategySelector,
    label: RegimeLabel,
    index: int,
    *,
    confidence: str = "0.9",
    model_fingerprint: str = "model-fingerprint-v1",
    abstained: bool = False,
):
    as_of = selection_time(index)
    return selector.select(
        prediction(
            label,
            as_of,
            confidence=confidence,
            model_fingerprint=model_fingerprint,
            abstained=abstained,
        ),
        now=as_of,
        correlation_id=f"selector:{MARKET.symbol}:{index}",
        market_symbol=MARKET.symbol,
    )


def test_registry_exposes_only_explicitly_compatible_plugins_and_is_fingerprinted() -> None:
    registry = build_baseline_registry()

    assert tuple(p.plugin_id for p in registry.options_for(RegimeLabel.BULL_TREND)) == (
        "sma-crossover-long-flat",
    )
    assert tuple(p.plugin_id for p in registry.options_for(RegimeLabel.BEAR_TREND)) == (
        "flat-no-position",
    )
    assert registry.options_for(RegimeLabel.UNKNOWN) == ()
    assert len(registry.fingerprint) == 64

    plugin = registry.get("flat-no-position")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(plugin)


def test_plugin_registration_rejects_unknown_regime_as_a_route() -> None:
    flat = build_baseline_registry().get("flat-no-position")
    with pytest.raises(ValueError, match="UNKNOWN"):
        StrategyPluginRegistration(
            plugin_id=flat.plugin_id,
            plugin_version=flat.plugin_version,
            description="invalid test route",
            strategy=flat.strategy,
            supported_regimes=frozenset({RegimeLabel.UNKNOWN}),
        )


def test_hysteresis_configuration_requires_positive_time_guards() -> None:
    with pytest.raises(ValueError, match="positive timedelta"):
        StrategySelectionPolicy(required_stability_duration=timedelta(0))
    with pytest.raises(ValueError, match="positive timedelta"):
        StrategySelectionPolicy(minimum_hold_duration=timedelta(0))


def test_routing_preferences_have_an_explicit_fingerprint() -> None:
    registry = build_baseline_registry()
    defaults = RegimeStrategySelector(registry)
    alternate = RegimeStrategySelector(
        registry,
        preferred_strategy_by_regime={
            RegimeLabel.BULL_TREND: ("sma-trend-long-flat", "sma-crossover-long-flat"),
            RegimeLabel.BEAR_TREND: ("flat-no-position",),
            RegimeLabel.RANGE_BOUND: ("sma-trend-long-flat", "flat-no-position"),
            RegimeLabel.HIGH_VOLATILITY: ("flat-no-position",),
        },
    )

    assert defaults.routing_fingerprint != alternate.routing_fingerprint
    with pytest.raises(ValueError, match="preference list is required"):
        RegimeStrategySelector(registry, preferred_strategy_by_regime={})


def test_candidate_evidence_resets_after_large_observation_gaps() -> None:
    selector = RegimeStrategySelector(
        build_baseline_registry(),
        policy=StrategySelectionPolicy(
            required_evidence_count=3,
            required_stability_duration=timedelta(hours=2),
            minimum_hold_duration=timedelta(hours=2),
            maximum_prediction_age=timedelta(hours=1),
        ),
    )

    first = select_at(selector, RegimeLabel.BULL_TREND, 1)
    after_gap = select_at(selector, RegimeLabel.BULL_TREND, 4)

    assert first.evidence_count == 1
    assert after_gap.status is SelectionStatus.CANDIDATE_PENDING
    assert after_gap.evidence_count == 1
    assert after_gap.candidate_since == selection_time(4)


def test_registry_change_clears_active_selection_and_requires_fresh_evidence() -> None:
    registry = build_baseline_registry()
    selector = RegimeStrategySelector(
        registry,
        policy=StrategySelectionPolicy(
            required_evidence_count=2,
            required_stability_duration=timedelta(hours=1),
            minimum_hold_duration=timedelta(hours=1),
        ),
    )
    select_at(selector, RegimeLabel.BULL_TREND, 1)
    active = select_at(selector, RegimeLabel.BULL_TREND, 2)
    assert active.status is SelectionStatus.SWITCHED

    flat = FlatBaseline(strategy_id="flat-bull-control")
    registry.register(
        StrategyPluginRegistration(
            plugin_id=flat.strategy_id,
            plugin_version=flat.strategy_version,
            description="New registry member forces revalidation.",
            strategy=flat,
            supported_regimes=frozenset({RegimeLabel.BULL_TREND}),
        )
    )
    changed = select_at(selector, RegimeLabel.BULL_TREND, 3)

    assert changed.status is SelectionStatus.REGISTRY_CHANGED
    assert changed.selected_strategy_id is None
    assert not changed.allow_new_signals


def test_hysteresis_requires_distinct_evidence_stability_and_minimum_hold() -> None:
    ledger = ExecutionLedger()
    selector = RegimeStrategySelector(
        build_baseline_registry(),
        policy=StrategySelectionPolicy(
            min_confidence=Decimal("0.65"),
            required_evidence_count=3,
            required_stability_duration=timedelta(hours=2),
            minimum_hold_duration=timedelta(hours=6),
            maximum_prediction_age=timedelta(hours=2),
        ),
        decision_ledger=ledger,
    )

    first = select_at(selector, RegimeLabel.BULL_TREND, 1)
    second = select_at(selector, RegimeLabel.BULL_TREND, 2)
    third = select_at(selector, RegimeLabel.BULL_TREND, 3)

    assert first.status is SelectionStatus.CANDIDATE_PENDING
    assert first.selected_strategy_id is None
    assert second.evidence_count == 2
    assert third.status is SelectionStatus.SWITCHED
    assert third.selected_strategy_id == "sma-crossover-long-flat"
    assert third.previous_strategy_id is None
    assert third.evidence_count == 3
    assert third.candidate_since == selection_time(1)

    # Three new regime observations are not enough to bypass the six-hour minimum hold.
    at_four = select_at(selector, RegimeLabel.BEAR_TREND, 4)
    at_five = select_at(selector, RegimeLabel.BEAR_TREND, 5)
    at_six = select_at(selector, RegimeLabel.BEAR_TREND, 6)
    assert at_four.status is SelectionStatus.CANDIDATE_PENDING
    assert at_five.evidence_count == 2
    assert at_six.status is SelectionStatus.CANDIDATE_PENDING
    assert at_six.selected_strategy_id == "sma-crossover-long-flat"

    switched_flat = select_at(selector, RegimeLabel.BEAR_TREND, 9)
    assert switched_flat.status is SelectionStatus.SWITCHED
    assert switched_flat.previous_strategy_id == "sma-crossover-long-flat"
    assert switched_flat.selected_strategy_id == "flat-no-position"
    assert len(ledger.strategy_selections) == 7
    assert all(
        entry.market_symbol == MARKET.symbol
        for entry in ledger.strategy_selections
    )
    assert all(entry.invalidation_conditions for entry in ledger.strategy_selections)


def test_duplicate_evidence_cannot_increment_hysteresis_and_is_logged() -> None:
    ledger = ExecutionLedger()
    selector = RegimeStrategySelector(
        build_baseline_registry(),
        policy=StrategySelectionPolicy(
            required_evidence_count=3,
            required_stability_duration=timedelta(hours=2),
            minimum_hold_duration=timedelta(hours=2),
        ),
        decision_ledger=ledger,
    )

    initial = select_at(selector, RegimeLabel.BULL_TREND, 1)
    duplicate_prediction = prediction(RegimeLabel.BULL_TREND, selection_time(1))
    duplicate = selector.select(
        duplicate_prediction,
        now=selection_time(1),
        correlation_id="selector:BTC-USDT:duplicate",
        market_symbol=MARKET.symbol,
    )

    assert initial.evidence_count == 1
    assert duplicate.status is SelectionStatus.DUPLICATE_EVIDENCE
    assert duplicate.evidence_count == 1
    assert not duplicate.allow_new_signals
    assert len(ledger.strategy_selections) == 2


def test_unknown_low_confidence_stale_or_uncalibrated_scores_abstain() -> None:
    selector = RegimeStrategySelector(build_baseline_registry())
    unknown = select_at(selector, RegimeLabel.UNKNOWN, 1, abstained=True)
    low_confidence = select_at(selector, RegimeLabel.BULL_TREND, 2, confidence="0.40")
    heuristic = selector.select(
        prediction(
            RegimeLabel.BULL_TREND,
            selection_time(3),
            confidence_semantics="heuristic_score",
        ),
        now=selection_time(3),
        correlation_id="selector:BTC-USDT:heuristic",
        market_symbol=MARKET.symbol,
    )
    stale_timestamp = selection_time(4)
    stale = selector.select(
        prediction(RegimeLabel.BULL_TREND, stale_timestamp - timedelta(hours=3)),
        now=stale_timestamp,
        correlation_id="selector:BTC-USDT:stale",
        market_symbol=MARKET.symbol,
    )

    assert unknown.status is SelectionStatus.ABSTAINED
    assert low_confidence.reason_code == "REGIME_CONFIDENCE_BELOW_SELECTOR_THRESHOLD"
    assert heuristic.reason_code == "UNVALIDATED_CONFIDENCE_SEMANTICS"
    assert stale.reason_code == "STALE_REGIME_PREDICTION"
    assert not any(
        item.allow_new_signals for item in (unknown, low_confidence, heuristic, stale)
    )


def test_model_or_registry_change_resets_incumbent_before_reselection() -> None:
    selector = RegimeStrategySelector(
        build_baseline_registry(),
        policy=StrategySelectionPolicy(
            required_evidence_count=2,
            required_stability_duration=timedelta(hours=1),
            minimum_hold_duration=timedelta(hours=1),
        ),
    )
    select_at(selector, RegimeLabel.BULL_TREND, 1)
    selected = select_at(selector, RegimeLabel.BULL_TREND, 2)
    assert selected.status is SelectionStatus.SWITCHED

    changed_model = selector.select(
        prediction(
            RegimeLabel.RANGE_BOUND,
            selection_time(3),
            model_fingerprint="different-model-artifact",
        ),
        now=selection_time(3),
        correlation_id="selector:BTC-USDT:model-change",
        market_symbol=MARKET.symbol,
    )

    assert changed_model.status is SelectionStatus.MODEL_IDENTITY_CHANGED
    assert changed_model.selected_strategy_id is None
    assert not changed_model.allow_new_signals


def make_dataset(count: int = 100) -> HistoricalDataset:
    values: list[MarketCandle] = []
    for index in range(count):
        close = Decimal("100") + Decimal(index) / Decimal("10")
        values.append(
            MarketCandle(
                market=MARKET,
                timestamp=BASE_TIME + INTERVAL * index,
                interval=INTERVAL,
                open=close - Decimal("0.1"),
                high=close + Decimal("1"),
                low=close - Decimal("1"),
                close=close,
                volume=Decimal("100") + Decimal(index),
                source=SOURCE,
            )
        )
    return HistoricalDataset(
        dataset_id="synthetic-phase15-btc",
        market=MARKET,
        interval=INTERVAL,
        volume_unit="base_asset",
        source_name=SOURCE,
        source_uri="urn:evoryn:synthetic-phase15:v1",
        source_revision="synthetic-v1",
        license_id="CC0-1.0",
        license_uri="https://spdx.org/licenses/CC0-1.0.html",
        collected_at=values[-1].timestamp + INTERVAL + timedelta(minutes=1),
        candles=tuple(values),
    )


@dataclass(frozen=True, slots=True)
class FakeTrainingManifest:
    training_dataset_version: str
    training_split: str
    train_start_index: int
    train_end_index_exclusive: int
    model_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "training_dataset_version": self.training_dataset_version,
            "training_split": self.training_split,
            "train_start_index": self.train_start_index,
            "train_end_index_exclusive": self.train_end_index_exclusive,
            "model_fingerprint": self.model_fingerprint,
        }


class FakeOutOfSampleDetector:
    def __init__(self, training_manifest: FakeTrainingManifest) -> None:
        self._training_manifest = training_manifest
        self.model_metadata = {
            "model_id": "synthetic-oos-detector",
            "model_version": "test-v1",
            "model_fingerprint": training_manifest.model_fingerprint,
            "confidence_semantics": "hmm_posterior",
            "feature_schema_version": "synthetic-v1",
        }

    @property
    def training_manifest(self) -> FakeTrainingManifest:
        return self._training_manifest

    def classify(
        self, completed_history: Sequence[MarketCandle], *, now: datetime
    ) -> RegimePrediction:
        assert completed_history
        return prediction(
            RegimeLabel.BULL_TREND,
            completed_history[-1].timestamp + completed_history[-1].interval,
            model_fingerprint=self._training_manifest.model_fingerprint,
        )


def test_oos_evaluation_compares_adaptive_selector_to_fixed_strategy() -> None:
    dataset = make_dataset(100)
    partitions = chronological_split(dataset)
    detector = FakeOutOfSampleDetector(
        FakeTrainingManifest(
            training_dataset_version=dataset.version,
            training_split="train",
            train_start_index=partitions.train.start,
            train_end_index_exclusive=partitions.train.end,
            model_fingerprint="train-only-model",
        )
    )
    report = evaluate_strategy_selector_out_of_sample(
        dataset,
        partitions=partitions,
        detector=detector,  # type: ignore[arg-type]
        registry=build_baseline_registry(),
        selection_policy=StrategySelectionPolicy(
            min_confidence=Decimal("0.65"),
            required_evidence_count=2,
            required_stability_duration=timedelta(hours=1),
            minimum_hold_duration=timedelta(hours=2),
            maximum_prediction_age=timedelta(hours=2),
        ),
        fixed_baseline=SmaCrossoverBaseline(),
        backtest_config=BacktestConfig(
            initial_cash=Decimal("10000"),
            commission_bps=Decimal("10"),
            spread_bps=Decimal("10"),
            slippage_bps=Decimal("5"),
            max_bar_participation=Decimal("0.5"),
            allocation_fraction=Decimal("0.9"),
            quantity_step=Decimal("0.001"),
        ),
    )

    assert report.dataset_version == dataset.version
    assert report.train_range_end_exclusive == partitions.train.end
    assert report.test_range_start == partitions.test.start
    assert report.test_range_end_exclusive == partitions.test.end
    assert report.adaptive_result.evaluation_range == partitions.test
    assert report.fixed_baseline_result.evaluation_range == partitions.test
    assert report.adaptive_result.run_id != report.fixed_baseline_result.run_id
    assert len(report.decision_ledger_entries) == partitions.test.size
    assert report.decision_ledger_entries[0].correlation_id.startswith("backtest:BTC-USDT:")
    assert report.to_report_dict()["test_range"]["note"].startswith("Out-of-sample")
    assert report.to_json_bytes()


def test_oos_evaluation_rejects_training_leakage_from_validation_or_test() -> None:
    dataset = make_dataset(100)
    partitions = chronological_split(dataset)
    detector = FakeOutOfSampleDetector(
        FakeTrainingManifest(
            training_dataset_version=dataset.version,
            training_split="validation",
            train_start_index=partitions.validation.start,
            train_end_index_exclusive=partitions.validation.end,
            model_fingerprint="invalid-leaked-model",
        )
    )

    with pytest.raises(ValueError, match="fitted only on train"):
        evaluate_strategy_selector_out_of_sample(
            dataset,
            partitions=partitions,
            detector=detector,
            registry=build_baseline_registry(),
            selection_policy=StrategySelectionPolicy(),
            fixed_baseline=SmaCrossoverBaseline(),
            backtest_config=BacktestConfig(
                initial_cash=Decimal("10000"),
                commission_bps=Decimal("10"),
                spread_bps=Decimal("10"),
                slippage_bps=Decimal("5"),
                max_bar_participation=Decimal("0.5"),
                allocation_fraction=Decimal("0.9"),
                quantity_step=Decimal("0.001"),
            ),
        )
