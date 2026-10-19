"""Synthetic Phase 17 tests for immutable experiments, promotion gates and rollback."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from evoryn.domain.models import Market, MarketCandle
from evoryn.learning_lab.backtest import (
    BacktestConfig,
    IndexRange,
    run_backtest,
)
from evoryn.learning_lab.experiment_registry import (
    ExperimentIntegrityError,
    ExperimentMode,
    ExperimentRegistry,
    ExperimentRunEvidence,
    ExperimentSpec,
    LearningEventType,
    LearningLabLedger,
    configuration_fingerprint,
)
from evoryn.learning_lab.historical_data import HistoricalDataset
from evoryn.learning_lab.promotion import (
    ChampionChallengerController,
    ChampionChallengerGate,
    PromotionDecision,
    PromotionPolicy,
    PromotionStatus,
    ReleasePipelineGate,
    ReleaseTarget,
    RollbackPolicy,
    RollbackStatus,
    RollbackTrigger,
)
from evoryn.strategies.baseline import SmaTrendBaseline

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
DATASET_VERSION = "d" * 64
EVALUATION_CONFIG_FINGERPRINT = "e" * 64
CODE_REVISION = "a" * 40
FEATURE_SCHEMA_VERSION = "features/v1"
TRAIN = IndexRange(0, 50)
VALIDATION = IndexRange(50, 70)
TEST = IndexRange(70, 100)


def canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parameters_fingerprint(parameters: tuple[tuple[str, str], ...]) -> str:
    return sha256(canonical_bytes(dict(parameters)))


def make_spec(
    *,
    model_id: str = "test-model-a",
    model_fingerprint: str = "1" * 64,
    mode: ExperimentMode = ExperimentMode.PAPER,
    created_minutes: int = 0,
    dataset_version: str = DATASET_VERSION,
    evaluation_config_fingerprint: str = EVALUATION_CONFIG_FINGERPRINT,
    parameters: tuple[tuple[str, str], ...] = (("fast_window", "5"), ("slow_window", "20")),
    test_range: IndexRange = TEST,
) -> ExperimentSpec:
    return ExperimentSpec(
        code_revision=CODE_REVISION,
        dataset_version=dataset_version,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        evaluation_config_fingerprint=evaluation_config_fingerprint,
        model_id=model_id,
        model_version="1.0.0",
        model_fingerprint=model_fingerprint,
        parameters=parameters,
        random_seed=42,
        train_range=TRAIN,
        validation_range=VALIDATION,
        test_range=test_range,
        mode=mode,
        created_at=NOW + timedelta(minutes=created_minutes),
    )


def make_evidence(
    spec: ExperimentSpec,
    *,
    net_return: str = "0.03",
    max_drawdown: str = "0.08",
    observations: int = 120,
    closed_trades: int = 12,
    data_quality_pass: bool = True,
    risk_denials: int = 0,
    run_suffix: str = "run-a",
    collected_at: datetime = NOW + timedelta(hours=1),
) -> ExperimentRunEvidence:
    mode_origin = {
        ExperimentMode.BACKTEST: "backtest_replay",
        ExperimentMode.SHADOW: "shadow_telemetry",
        ExperimentMode.PAPER: "paper_telemetry",
    }
    return ExperimentRunEvidence(
        experiment_id=spec.experiment_id,
        mode=spec.mode,
        dataset_version=spec.dataset_version,
        test_range=spec.test_range,
        code_revision=spec.code_revision,
        feature_schema_version=spec.feature_schema_version,
        evaluation_config_fingerprint=spec.evaluation_config_fingerprint,
        model_id=spec.model_id,
        model_version=spec.model_version,
        model_fingerprint=spec.model_fingerprint,
        parameters_fingerprint=parameters_fingerprint(spec.parameters),
        random_seed=spec.random_seed,
        run_id=sha256(run_suffix.encode("utf-8")),
        observations=observations,
        closed_trade_count=closed_trades,
        net_profit=Decimal("300"),
        net_return_fraction=Decimal(net_return),
        max_drawdown_fraction=Decimal(max_drawdown),
        expectancy_per_closed_trade=Decimal("25"),
        turnover_multiple=Decimal("1.7"),
        data_quality_pass=data_quality_pass,
        risk_gate_denial_count=risk_denials,
        evidence_origin=mode_origin[spec.mode],
        collected_at=collected_at,
    )


def make_registry(root: Path) -> tuple[ExperimentRegistry, LearningLabLedger]:
    ledger = LearningLabLedger(root / "learning-lab-ledger.jsonl")
    return ExperimentRegistry(root / "experiments", ledger), ledger


class SyntheticEvidenceTrustVerifier:
    """Test-only stand-in for a separately authenticated Paper/Shadow telemetry collector."""

    def verify(self, evidence: ExperimentRunEvidence, spec: ExperimentSpec) -> bool:
        return (
            evidence.experiment_id == spec.experiment_id
            and evidence.mode in {ExperimentMode.PAPER, ExperimentMode.SHADOW}
            and evidence.evidence_origin
            in {"paper_telemetry", "shadow_telemetry"}
            and evidence.data_quality_pass
        )


def register_spec_and_evidence(
    registry: ExperimentRegistry,
    spec: ExperimentSpec,
    evidence: ExperimentRunEvidence,
) -> str:
    registry.register(spec, correlation_id=f"test:{spec.experiment_id}:register")
    registry.record_evidence(evidence, correlation_id=f"test:{spec.experiment_id}:evidence")
    return evidence.evidence_id


def test_experiment_fingerprint_binds_all_reproducibility_inputs() -> None:
    original = make_spec()
    different_data = make_spec(dataset_version="f" * 64)
    different_config = make_spec(evaluation_config_fingerprint="b" * 64)
    different_seed = replace(original, random_seed=7)
    different_parameters = make_spec(parameters=(("fast_window", "4"), ("slow_window", "20")))

    assert original.fingerprint != different_data.fingerprint
    assert original.fingerprint != different_config.fingerprint
    assert original.fingerprint != different_seed.fingerprint
    assert original.fingerprint != different_parameters.fingerprint
    assert original.experiment_id != replace(
        original, created_at=original.created_at + timedelta(seconds=1)
    ).experiment_id


def test_experiment_spec_rejects_unsorted_parameters_and_overlapping_ranges() -> None:
    with pytest.raises(ValueError, match="sorted by key"):
        make_spec(parameters=(("slow_window", "20"), ("fast_window", "5")))
    with pytest.raises(ValueError, match="chronological and disjoint"):
        replace(make_spec(), validation_range=IndexRange(40, 70))


def test_registry_stores_immutable_specs_and_detects_tampering(tmp_path: Path) -> None:
    registry, ledger = make_registry(tmp_path)
    spec = make_spec()
    experiment_id = registry.register(spec, correlation_id="experiment:register")

    assert registry.get(experiment_id) == spec
    assert ledger.verify()
    assert ledger.events[0].event_type is LearningEventType.EXPERIMENT_REGISTERED

    spec_path = tmp_path / "experiments" / "specs" / f"{experiment_id}.json"
    content = json.loads(spec_path.read_bytes())
    content["model_version"] = "tampered"
    spec_path.write_bytes(canonical_bytes(content))

    fresh_registry = ExperimentRegistry(tmp_path / "experiments", LearningLabLedger())
    with pytest.raises(ExperimentIntegrityError):
        fresh_registry.get(experiment_id)


def test_learning_lab_ledger_is_hash_chained_and_detects_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = LearningLabLedger(path)
    event = ledger.append(
        LearningEventType.EXPERIMENT_REGISTERED,
        experiment_id="a" * 64,
        correlation_id="run:test-1",
        occurred_at=NOW,
        payload={"dataset_version": DATASET_VERSION, "seed": 42},
    )
    second = ledger.append(
        LearningEventType.EVIDENCE_RECORDED,
        experiment_id="a" * 64,
        correlation_id="run:test-1",
        occurred_at=NOW + timedelta(seconds=1),
        payload={"net_return": "0.02"},
    )

    reloaded = LearningLabLedger(path)
    assert reloaded.verify()
    assert [event.event_hash for event in reloaded.events] == [
        event.event_hash,
        second.event_hash,
    ]
    assert second.previous_event_hash == event.event_hash

    raw = path.read_bytes().replace(b"payload_sha256", b"payload_sha257", 1)
    path.write_bytes(raw)
    with pytest.raises(ExperimentIntegrityError):
        LearningLabLedger(path)


def test_registry_binds_evidence_to_exact_experiment_identity(tmp_path: Path) -> None:
    registry, ledger = make_registry(tmp_path)
    spec = make_spec()
    good = make_evidence(spec)
    registry.register(spec, correlation_id="experiment:registered")
    evidence_id = registry.record_evidence(good, correlation_id="experiment:evidence")

    assert registry.get_evidence(evidence_id) == good
    recorded_evidence_event = ledger.events[-1].event_type
    assert recorded_evidence_event is LearningEventType.EVIDENCE_RECORDED

    wrong_config = replace(good, evaluation_config_fingerprint="9" * 64)
    with pytest.raises(ExperimentIntegrityError, match="identity"):
        registry.record_evidence(wrong_config, correlation_id="experiment:bad-config")


def test_backtest_result_is_recorded_only_with_exact_config_and_test_range() -> None:
    interval = timedelta(hours=1)
    market = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
    source = "synthetic-phase17-backtest"
    candles = tuple(
        MarketCandle(
            market=market,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC) + interval * index,
            interval=interval,
            open=Decimal("100") + Decimal(index) / Decimal("10"),
            high=Decimal("102") + Decimal(index) / Decimal("10"),
            low=Decimal("99") + Decimal(index) / Decimal("10"),
            close=Decimal("101") + Decimal(index) / Decimal("10"),
            volume=Decimal("1000"),
            source=source,
        )
        for index in range(100)
    )
    dataset = HistoricalDataset(
        dataset_id="phase17-synthetic-btc",
        market=market,
        interval=interval,
        volume_unit="base_asset",
        source_name=source,
        source_uri="urn:evoryn:phase17:synthetic:v1",
        source_revision="synthetic-1",
        license_id="CC0-1.0",
        license_uri="https://spdx.org/licenses/CC0-1.0.html",
        collected_at=candles[-1].timestamp + interval + timedelta(minutes=1),
        candles=candles,
    )
    strategy = SmaTrendBaseline(window=5)
    backtest_config = BacktestConfig(
        initial_cash=Decimal("10000"),
        commission_bps=Decimal("10"),
        spread_bps=Decimal("10"),
        slippage_bps=Decimal("5"),
        max_bar_participation=Decimal("0.5"),
        allocation_fraction=Decimal("0.9"),
        quantity_step=Decimal("0.001"),
        allow_gaps=False,
    )
    spec = ExperimentSpec(
        code_revision=CODE_REVISION,
        dataset_version=dataset.version,
        feature_schema_version="ohlcv-features/v1",
        evaluation_config_fingerprint=configuration_fingerprint(backtest_config.as_dict()),
        model_id=strategy.strategy_id,
        model_version=strategy.strategy_version,
        model_fingerprint=sha256(canonical_bytes(strategy.parameters())),
        parameters=tuple(sorted(strategy.parameters().items())),
        random_seed=42,
        train_range=TRAIN,
        validation_range=VALIDATION,
        test_range=TEST,
        mode=ExperimentMode.BACKTEST,
        created_at=dataset.collected_at + timedelta(minutes=1),
    )
    result = run_backtest(
        dataset,
        strategy,
        backtest_config,
        start_index=TEST.start,
        end_index=TEST.end,
        warmup_start_index=0,
        segment_name="phase17-evidence-integration",
    )
    evidence = ExperimentRunEvidence.from_backtest_result(
        spec, result, collected_at=spec.created_at + timedelta(minutes=1)
    )

    assert evidence.mode is ExperimentMode.BACKTEST
    assert evidence.dataset_version == dataset.version
    assert evidence.test_range == TEST
    assert evidence.evaluation_config_fingerprint == configuration_fingerprint(
        backtest_config.as_dict()
    )
    assert evidence.run_id == result.run_id

    with pytest.raises(ValueError, match="configuration differs"):
        wrong_spec = replace(spec, evaluation_config_fingerprint="8" * 64)
        ExperimentRunEvidence.from_backtest_result(
            wrong_spec, result, collected_at=spec.created_at + timedelta(minutes=1)
        )


def test_backtest_evidence_cannot_be_created_for_paper_experiment() -> None:
    paper_spec = make_spec(mode=ExperimentMode.PAPER)
    fake_result: Any = object()
    with pytest.raises(ValueError, match="BACKTEST mode"):
        ExperimentRunEvidence.from_backtest_result(
            paper_spec, fake_result, collected_at=NOW
        )


def test_champion_challenger_promotes_only_after_predeclared_gates(tmp_path: Path) -> None:
    registry, ledger = make_registry(tmp_path)
    champion_spec = make_spec(model_id="champion", model_fingerprint="1" * 64)
    challenger_spec = make_spec(
        model_id="challenger",
        model_fingerprint="2" * 64,
        created_minutes=1,
        parameters=(("fast_window", "4"), ("slow_window", "18")),
    )
    champion_evidence = make_evidence(
        champion_spec, net_return="0.02", max_drawdown="0.10", run_suffix="champion-run"
    )
    challenger_evidence = make_evidence(
        challenger_spec, net_return="0.03", max_drawdown="0.08", run_suffix="challenger-run"
    )
    register_spec_and_evidence(registry, champion_spec, champion_evidence)
    register_spec_and_evidence(registry, challenger_spec, challenger_evidence)
    controller = ChampionChallengerController(
        registry=registry,
        ledger=ledger,
        champion_experiment_id=champion_spec.experiment_id,
        evidence_trust_verifier=SyntheticEvidenceTrustVerifier(),
        gate=ChampionChallengerGate(
            PromotionPolicy(
                minimum_observations=100,
                minimum_closed_trades=10,
                minimum_net_return_improvement=Decimal("0.005"),
            )
        ),
    )

    decision = controller.compare_and_promote(
        champion_evidence_id=champion_evidence.evidence_id,
        challenger_experiment_id=challenger_spec.experiment_id,
        challenger_evidence_id=challenger_evidence.evidence_id,
        target_mode=ExperimentMode.PAPER,
        evaluated_at=NOW + timedelta(hours=2),
        correlation_id="paper-promotion:challenger",
    )

    assert decision.status is PromotionStatus.PROMOTED_TO_PAPER
    assert controller.active_champion_id == challenger_spec.experiment_id
    assert controller.previous_champion_id == champion_spec.experiment_id
    assert ledger.events[-1].event_type is LearningEventType.CANDIDATE_PROMOTED_TO_PAPER


def test_repeated_challengers_on_the_same_holdout_hit_the_predeclared_trial_cap(
    tmp_path: Path,
) -> None:
    registry, ledger = make_registry(tmp_path)
    champion_spec = make_spec(model_id="trial-cap-champion", model_fingerprint="1" * 64)
    first_spec = make_spec(
        model_id="trial-cap-first",
        model_fingerprint="2" * 64,
        created_minutes=1,
        parameters=(("fast_window", "4"), ("slow_window", "18")),
    )
    second_spec = make_spec(
        model_id="trial-cap-second",
        model_fingerprint="3" * 64,
        created_minutes=2,
        parameters=(("fast_window", "3"), ("slow_window", "15")),
    )
    champion_evidence = make_evidence(
        champion_spec, net_return="0.02", max_drawdown="0.10", run_suffix="trial-cap-champion"
    )
    first_evidence = make_evidence(
        first_spec, net_return="0.03", max_drawdown="0.08", run_suffix="trial-cap-first"
    )
    second_evidence = make_evidence(
        second_spec, net_return="0.04", max_drawdown="0.07", run_suffix="trial-cap-second"
    )
    for spec, evidence in (
        (champion_spec, champion_evidence),
        (first_spec, first_evidence),
        (second_spec, second_evidence),
    ):
        register_spec_and_evidence(registry, spec, evidence)
    controller = ChampionChallengerController(
        registry=registry,
        ledger=ledger,
        champion_experiment_id=champion_spec.experiment_id,
        evidence_trust_verifier=SyntheticEvidenceTrustVerifier(),
        gate=ChampionChallengerGate(
            PromotionPolicy(
                minimum_observations=100,
                minimum_closed_trades=10,
                maximum_comparisons_per_dataset_test_range=1,
            )
        ),
    )

    first_decision = controller.compare_and_promote(
        champion_evidence_id=champion_evidence.evidence_id,
        challenger_experiment_id=first_spec.experiment_id,
        challenger_evidence_id=first_evidence.evidence_id,
        target_mode=ExperimentMode.PAPER,
        evaluated_at=NOW + timedelta(hours=2),
        correlation_id="trial-cap:first",
    )
    second_decision = controller.compare_and_promote(
        champion_evidence_id=first_evidence.evidence_id,
        challenger_experiment_id=second_spec.experiment_id,
        challenger_evidence_id=second_evidence.evidence_id,
        target_mode=ExperimentMode.PAPER,
        evaluated_at=NOW + timedelta(hours=3),
        correlation_id="trial-cap:second",
    )

    assert first_decision.status is PromotionStatus.PROMOTED_TO_PAPER
    assert second_decision.status is PromotionStatus.INSUFFICIENT_EVIDENCE
    assert "MAX_HOLDOUT_COMPARISONS_REACHED" in second_decision.reasons
    assert controller.active_champion_id == first_spec.experiment_id


def test_promotion_fails_closed_without_trusted_telemetry_verifier(
    tmp_path: Path,
) -> None:
    registry, ledger = make_registry(tmp_path)
    champion_spec = make_spec(model_id="untrusted-champion", model_fingerprint="1" * 64)
    challenger_spec = make_spec(
        model_id="untrusted-challenger",
        model_fingerprint="2" * 64,
        created_minutes=1,
        parameters=(("fast_window", "4"), ("slow_window", "18")),
    )
    champion = make_evidence(champion_spec, run_suffix="untrusted-champion")
    challenger = make_evidence(challenger_spec, run_suffix="untrusted-challenger")
    register_spec_and_evidence(registry, champion_spec, champion)
    register_spec_and_evidence(registry, challenger_spec, challenger)
    controller = ChampionChallengerController(
        registry=registry,
        ledger=ledger,
        champion_experiment_id=champion_spec.experiment_id,
    )

    with pytest.raises(PermissionError, match="trusted Paper/Shadow evidence verifier"):
        controller.compare_and_promote(
            champion_evidence_id=champion.evidence_id,
            challenger_experiment_id=challenger_spec.experiment_id,
            challenger_evidence_id=challenger.evidence_id,
            target_mode=ExperimentMode.PAPER,
            evaluated_at=NOW + timedelta(hours=2),
            correlation_id="untrusted:promote",
        )

    assert controller.active_champion_id == champion_spec.experiment_id
    assert ledger.events[-1].event_type is LearningEventType.PROMOTION_REJECTED
    assert (
        json.loads(ledger.events[-1].payload_json)["reason_code"]
        == "TRUSTED_TELEMETRY_VERIFIER_NOT_CONFIGURED"
    )


def test_champion_challenger_rejects_changed_cost_configuration(tmp_path: Path) -> None:
    registry, _ = make_registry(tmp_path)
    champion_spec = make_spec(model_id="champion", model_fingerprint="1" * 64)
    challenger_spec = make_spec(
        model_id="challenger",
        model_fingerprint="2" * 64,
        created_minutes=1,
        evaluation_config_fingerprint="8" * 64,
    )
    champion = make_evidence(champion_spec, net_return="0.02", run_suffix="champion")
    challenger = make_evidence(challenger_spec, net_return="0.05", run_suffix="challenger")
    register_spec_and_evidence(registry, champion_spec, champion)
    register_spec_and_evidence(registry, challenger_spec, challenger)

    decision = ChampionChallengerGate().evaluate(
        champion_spec,
        challenger_spec,
        champion,
        challenger,
        mode=ExperimentMode.PAPER,
        evaluated_at=NOW + timedelta(hours=2),
    )

    assert decision.status is PromotionStatus.REJECTED
    assert "CHANGED_EVALUATION_CONFIG" in decision.reasons


def test_automatic_rollback_restores_last_known_good_paper_champion(tmp_path: Path) -> None:
    registry, ledger = make_registry(tmp_path)
    champion_spec = make_spec(model_id="known-good", model_fingerprint="1" * 64)
    challenger_spec = make_spec(
        model_id="challenger", model_fingerprint="2" * 64, created_minutes=1,
        parameters=(("fast_window", "4"), ("slow_window", "18"))
    )
    champion_good = make_evidence(
        champion_spec, net_return="0.02", max_drawdown="0.10", run_suffix="known-good"
    )
    challenger_good = make_evidence(
        challenger_spec, net_return="0.03", max_drawdown="0.08", run_suffix="challenger-good"
    )
    register_spec_and_evidence(registry, champion_spec, champion_good)
    register_spec_and_evidence(registry, challenger_spec, challenger_good)
    controller = ChampionChallengerController(
        registry=registry,
        ledger=ledger,
        champion_experiment_id=champion_spec.experiment_id,
        evidence_trust_verifier=SyntheticEvidenceTrustVerifier(),
        gate=ChampionChallengerGate(
            PromotionPolicy(minimum_observations=100, minimum_closed_trades=10)
        ),
        rollback_policy=RollbackPolicy(
            minimum_observations=10,
            maximum_drawdown_fraction=Decimal("0.20"),
            maximum_net_loss_fraction=Decimal("0.05"),
        ),
    )
    promoted = controller.compare_and_promote(
        champion_evidence_id=champion_good.evidence_id,
        challenger_experiment_id=challenger_spec.experiment_id,
        challenger_evidence_id=challenger_good.evidence_id,
        target_mode=ExperimentMode.PAPER,
        evaluated_at=NOW + timedelta(hours=2),
        correlation_id="paper-promotion:challenger",
    )
    assert promoted.status is PromotionStatus.PROMOTED_TO_PAPER

    reloaded_ledger = LearningLabLedger(tmp_path / "learning-lab-ledger.jsonl")
    reloaded_registry = ExperimentRegistry(tmp_path / "experiments", reloaded_ledger)
    reloaded_controller = ChampionChallengerController(
        registry=reloaded_registry,
        ledger=reloaded_ledger,
        champion_experiment_id=champion_spec.experiment_id,
        evidence_trust_verifier=SyntheticEvidenceTrustVerifier(),
        gate=ChampionChallengerGate(
            PromotionPolicy(minimum_observations=100, minimum_closed_trades=10)
        ),
        rollback_policy=RollbackPolicy(minimum_observations=10),
    )
    assert reloaded_controller.active_champion_id == challenger_spec.experiment_id
    assert reloaded_controller.previous_champion_id == champion_spec.experiment_id

    bad_paper_telemetry = make_evidence(
        challenger_spec,
        net_return="-0.10",
        max_drawdown="0.30",
        run_suffix="challenger-bad-paper",
        collected_at=NOW + timedelta(hours=3),
    )
    registry.record_evidence(
        bad_paper_telemetry, correlation_id="paper-health:challenger"
    )
    rollback = controller.monitor_paper_champion(
        bad_paper_telemetry.evidence_id,
        checked_at=NOW + timedelta(hours=4),
        correlation_id="paper-rollback:challenger",
    )

    assert rollback.status is RollbackStatus.ROLLED_BACK
    assert rollback.trigger is RollbackTrigger.MAX_DRAWDOWN_EXCEEDED
    assert controller.active_champion_id == champion_spec.experiment_id
    assert ledger.events[-2].event_type is LearningEventType.AUTO_ROLLBACK_TRIGGERED
    assert ledger.events[-1].event_type is LearningEventType.CHAMPION_HEALTH_CHECKED

    final_ledger = LearningLabLedger(tmp_path / "learning-lab-ledger.jsonl")
    final_registry = ExperimentRegistry(tmp_path / "experiments", final_ledger)
    final_controller = ChampionChallengerController(
        registry=final_registry,
        ledger=final_ledger,
        champion_experiment_id=challenger_spec.experiment_id,
    )
    assert final_controller.active_champion_id == champion_spec.experiment_id
    assert final_controller.previous_champion_id is None


def test_shadow_promotion_requires_matching_shadow_champion_and_never_changes_paper(
    tmp_path: Path,
) -> None:
    registry, ledger = make_registry(tmp_path)
    paper_champion = make_spec(model_id="champion", model_fingerprint="1" * 64)
    shadow_champion = replace(paper_champion, mode=ExperimentMode.SHADOW)
    shadow_challenger = make_spec(
        model_id="challenger",
        model_fingerprint="2" * 64,
        mode=ExperimentMode.SHADOW,
        created_minutes=1,
        parameters=(("fast_window", "4"), ("slow_window", "18")),
    )
    paper_evidence = make_evidence(paper_champion, run_suffix="paper-champion")
    shadow_champion_evidence = make_evidence(
        shadow_champion,
        net_return="0.02",
        max_drawdown="0.10",
        run_suffix="shadow-champion",
        collected_at=NOW + timedelta(hours=1),
    )
    shadow_challenger_evidence = make_evidence(
        shadow_challenger,
        run_suffix="shadow-challenger",
        collected_at=NOW + timedelta(hours=1),
    )
    for spec, evidence in (
        (paper_champion, paper_evidence),
        (shadow_champion, shadow_champion_evidence),
        (shadow_challenger, shadow_challenger_evidence),
    ):
        register_spec_and_evidence(registry, spec, evidence)
    controller = ChampionChallengerController(
        registry=registry,
        ledger=ledger,
        champion_experiment_id=paper_champion.experiment_id,
        evidence_trust_verifier=SyntheticEvidenceTrustVerifier(),
        gate=ChampionChallengerGate(
            PromotionPolicy(minimum_observations=100, minimum_closed_trades=10)
        ),
    )

    decision = controller.compare_and_promote(
        champion_evidence_id=shadow_champion_evidence.evidence_id,
        challenger_experiment_id=shadow_challenger.experiment_id,
        challenger_evidence_id=shadow_challenger_evidence.evidence_id,
        target_mode=ExperimentMode.SHADOW,
        shadow_champion_experiment_id=shadow_champion.experiment_id,
        evaluated_at=NOW + timedelta(hours=2),
        correlation_id="shadow-comparison:test",
    )

    assert decision.status is PromotionStatus.APPROVED_FOR_SHADOW
    assert controller.active_champion_id == paper_champion.experiment_id
    assert controller.approved_shadow_candidate_id == shadow_challenger.experiment_id


def passing_paper_promotion(
    registry: ExperimentRegistry,
    ledger: LearningLabLedger,
    *,
    suffix: str,
) -> tuple[ExperimentSpec, PromotionDecision]:
    """Build a real gate-passing Paper comparison for release-candidate tests."""
    champion_spec = make_spec(
        model_id=f"{suffix}-champion",
        model_fingerprint="3" * 64,
        created_minutes=20,
    )
    challenger_spec = make_spec(
        model_id=f"{suffix}-challenger",
        model_fingerprint="4" * 64,
        created_minutes=21,
        parameters=(("fast_window", "4"), ("slow_window", "18")),
    )
    champion_evidence = make_evidence(
        champion_spec,
        net_return="0.02",
        max_drawdown="0.10",
        run_suffix=f"{suffix}-champion-evidence",
    )
    challenger_evidence = make_evidence(
        challenger_spec,
        net_return="0.03",
        max_drawdown="0.08",
        run_suffix=f"{suffix}-challenger-evidence",
    )
    register_spec_and_evidence(registry, champion_spec, champion_evidence)
    register_spec_and_evidence(registry, challenger_spec, challenger_evidence)
    controller = ChampionChallengerController(
        registry=registry,
        ledger=ledger,
        champion_experiment_id=champion_spec.experiment_id,
        evidence_trust_verifier=SyntheticEvidenceTrustVerifier(),
        gate=ChampionChallengerGate(
            PromotionPolicy(minimum_observations=100, minimum_closed_trades=10)
        ),
    )
    decision = controller.compare_and_promote(
        champion_evidence_id=champion_evidence.evidence_id,
        challenger_experiment_id=challenger_spec.experiment_id,
        challenger_evidence_id=challenger_evidence.evidence_id,
        target_mode=ExperimentMode.PAPER,
        evaluated_at=NOW + timedelta(hours=2),
        correlation_id=f"{suffix}:promotion",
    )
    assert decision.status is PromotionStatus.PROMOTED_TO_PAPER
    return challenger_spec, decision


def test_release_gate_requires_approved_risk_fingerprint_and_blocks_live(tmp_path: Path) -> None:
    registry, ledger = make_registry(tmp_path)
    spec, decision = passing_paper_promotion(registry, ledger, suffix="release-live-block")
    risk_fingerprint = "9" * 64
    release_gate = ReleasePipelineGate(
        registry=registry,
        ledger=ledger,
        approved_risk_policy_sha256=risk_fingerprint,
    )

    with pytest.raises(PermissionError, match="cannot create or authorize a Live release"):
        release_gate.create_candidate(
            spec.experiment_id,
            target=ReleaseTarget.LIVE,
            promotion_decision_id=decision.decision_id,
            test_report_sha256="1" * 64,
            security_report_sha256="2" * 64,
            risk_policy_sha256=risk_fingerprint,
            created_at=NOW + timedelta(hours=3),
            correlation_id="release:attempt-live",
        )
    assert ledger.events[-1].event_type is LearningEventType.LIVE_RELEASE_BLOCKED

    candidate = release_gate.create_candidate(
        spec.experiment_id,
        target=ReleaseTarget.PAPER,
        promotion_decision_id=decision.decision_id,
        test_report_sha256="1" * 64,
        security_report_sha256="2" * 64,
        risk_policy_sha256=risk_fingerprint,
        created_at=NOW + timedelta(hours=3),
        correlation_id="release:paper-candidate",
    )
    # Reconstruct the ledger and release gate to prove candidate state survives process restart.
    reloaded_ledger = LearningLabLedger(tmp_path / "learning-lab-ledger.jsonl")
    reloaded_registry = ExperimentRegistry(tmp_path / "experiments", reloaded_ledger)
    reloaded_gate = ReleasePipelineGate(
        registry=reloaded_registry,
        ledger=reloaded_ledger,
        approved_risk_policy_sha256=risk_fingerprint,
    )
    approved = reloaded_gate.approve_candidate(
        candidate.candidate_id,
        approved_by="human-reviewer",
        approval_reference="approval-ticket-123",
        approved_at=NOW + timedelta(hours=4),
        current_risk_policy_sha256=risk_fingerprint,
        correlation_id="release:paper-approved",
    )

    assert approved.is_approved
    assert approved.promotion_decision_id == decision.decision_id
    assert reloaded_ledger.events[-1].event_type is LearningEventType.RELEASE_APPROVED


def test_release_candidate_requires_a_ledgered_passing_promotion_decision(
    tmp_path: Path,
) -> None:
    registry, ledger = make_registry(tmp_path)
    spec = make_spec(model_id="release-without-promotion", model_fingerprint="5" * 64)
    registry.register(spec, correlation_id="release:no-promotion-register")
    gate = ReleasePipelineGate(
        registry=registry,
        ledger=ledger,
        approved_risk_policy_sha256="9" * 64,
    )

    with pytest.raises(PermissionError, match="matching passing Champion/Challenger decision"):
        gate.create_candidate(
            spec.experiment_id,
            target=ReleaseTarget.PAPER,
            promotion_decision_id="f" * 64,
            test_report_sha256="1" * 64,
            security_report_sha256="2" * 64,
            risk_policy_sha256="9" * 64,
            created_at=NOW + timedelta(hours=1),
            correlation_id="release:no-promotion",
        )

    assert ledger.events[-1].event_type is LearningEventType.RELEASE_REJECTED


def test_release_gate_rejects_risk_policy_drift_at_creation_and_approval(
    tmp_path: Path,
) -> None:
    registry, ledger = make_registry(tmp_path)
    spec, decision = passing_paper_promotion(registry, ledger, suffix="release-policy-drift")
    approved_policy = "9" * 64
    release_gate = ReleasePipelineGate(
        registry=registry,
        ledger=ledger,
        approved_risk_policy_sha256=approved_policy,
    )
    with pytest.raises(PermissionError, match="Risk policy fingerprint"):
        release_gate.create_candidate(
            spec.experiment_id,
            target=ReleaseTarget.PAPER,
            promotion_decision_id=decision.decision_id,
            test_report_sha256="1" * 64,
            security_report_sha256="2" * 64,
            risk_policy_sha256="8" * 64,
            created_at=NOW + timedelta(hours=3),
            correlation_id="release:bad-policy",
        )
    assert ledger.events[-1].event_type is LearningEventType.RELEASE_REJECTED

    candidate = release_gate.create_candidate(
        spec.experiment_id,
        target=ReleaseTarget.PAPER,
        promotion_decision_id=decision.decision_id,
        test_report_sha256="1" * 64,
        security_report_sha256="2" * 64,
        risk_policy_sha256=approved_policy,
        created_at=NOW + timedelta(hours=3),
        correlation_id="release:policy-approved-candidate",
    )
    with pytest.raises(PermissionError, match="Risk policy changed"):
        release_gate.approve_candidate(
            candidate.candidate_id,
            approved_by="human-reviewer",
            approval_reference="approval-ticket-risk-drift",
            approved_at=NOW + timedelta(hours=4),
            current_risk_policy_sha256="8" * 64,
            correlation_id="release:risk-drift",
        )
    assert ledger.events[-1].event_type is LearningEventType.RELEASE_REJECTED


def test_paper_health_without_trusted_attestation_fails_closed_to_safe_hold(
    tmp_path: Path,
) -> None:
    registry, ledger = make_registry(tmp_path)
    spec = make_spec(model_id="unattested-health")
    apparently_healthy = make_evidence(
        spec,
        observations=50,
        net_return="0.03",
        max_drawdown="0.08",
        run_suffix="unattested-paper-health",
    )
    register_spec_and_evidence(registry, spec, apparently_healthy)
    controller = ChampionChallengerController(
        registry=registry,
        ledger=ledger,
        champion_experiment_id=spec.experiment_id,
        evidence_trust_verifier=None,
    )

    result = controller.monitor_paper_champion(
        apparently_healthy.evidence_id,
        checked_at=NOW + timedelta(hours=2),
        correlation_id="paper-health:missing-attestation",
    )

    assert result.status is RollbackStatus.SAFE_HOLD_NO_CHAMPION
    assert result.trigger is RollbackTrigger.TELEMETRY_ATTESTATION_FAILED
    assert result.active_champion_id is None
    assert controller.active_champion_id is None
    assert ledger.events[-2].event_type is LearningEventType.AUTO_ROLLBACK_TRIGGERED
    assert json.loads(ledger.events[-2].payload_json)["trigger"] == (
        RollbackTrigger.TELEMETRY_ATTESTATION_FAILED.value
    )


def test_rollback_distinguishes_insufficient_window_from_safe_hold(tmp_path: Path) -> None:
    registry, ledger = make_registry(tmp_path)
    spec = make_spec(model_id="single-champion", model_fingerprint="7" * 64)
    short_evidence = make_evidence(
        spec, observations=2, run_suffix="too-short", collected_at=NOW + timedelta(hours=1)
    )
    bad_evidence = make_evidence(
        spec,
        observations=25,
        net_return="-0.10",
        max_drawdown="0.30",
        run_suffix="bad-without-predecessor",
        collected_at=NOW + timedelta(hours=2),
    )
    registry.register(spec, correlation_id="rollback:register")
    registry.record_evidence(short_evidence, correlation_id="rollback:short")
    registry.record_evidence(bad_evidence, correlation_id="rollback:bad")
    controller = ChampionChallengerController(
        registry=registry,
        ledger=ledger,
        champion_experiment_id=spec.experiment_id,
        evidence_trust_verifier=SyntheticEvidenceTrustVerifier(),
        rollback_policy=RollbackPolicy(
            minimum_observations=20,
            maximum_drawdown_fraction=Decimal("0.20"),
        ),
    )

    short_result = controller.monitor_paper_champion(
        short_evidence.evidence_id,
        checked_at=NOW + timedelta(hours=3),
        correlation_id="rollback:insufficient",
    )
    assert short_result.status is RollbackStatus.INSUFFICIENT_EVIDENCE
    assert controller.active_champion_id == spec.experiment_id

    result = controller.monitor_paper_champion(
        bad_evidence.evidence_id,
        checked_at=NOW + timedelta(hours=4),
        correlation_id="rollback:no-predecessor",
    )
    assert result.status is RollbackStatus.SAFE_HOLD_NO_CHAMPION
    assert result.trigger is RollbackTrigger.MAX_DRAWDOWN_EXCEEDED
    assert result.active_champion_id is None
    assert controller.active_champion_id is None
    assert ledger.events[-2].event_type is LearningEventType.AUTO_ROLLBACK_TRIGGERED
    assert ledger.events[-1].event_type is LearningEventType.CHAMPION_HEALTH_CHECKED

    restarted_ledger = LearningLabLedger(tmp_path / "learning-lab-ledger.jsonl")
    restarted_registry = ExperimentRegistry(tmp_path / "experiments", restarted_ledger)
    restarted_controller = ChampionChallengerController(
        registry=restarted_registry,
        ledger=restarted_ledger,
        champion_experiment_id=spec.experiment_id,
    )
    assert restarted_controller.active_champion_id is None


def test_orphaned_spec_and_evidence_files_require_audit_reconciliation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiments"
    ledger = LearningLabLedger(tmp_path / "separate-ledger.jsonl")
    spec = make_spec(model_id="orphan-recovery", model_fingerprint="6" * 64)
    spec_path = root / "specs" / f"{spec.experiment_id}.json"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_bytes(canonical_bytes(spec.to_dict()))
    registry = ExperimentRegistry(root, ledger)

    with pytest.raises(ExperimentIntegrityError, match="Ledger event"):
        registry.get(spec.experiment_id)
    assert registry.register(spec, correlation_id="recovery:spec") == spec.experiment_id
    reused_registration_event = ledger.events[-1].event_type
    assert reused_registration_event is LearningEventType.EXPERIMENT_REGISTRATION_REUSED

    evidence = make_evidence(spec, run_suffix="orphan-evidence")
    evidence_path = root / "evidence" / f"{evidence.evidence_id}.json"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_bytes(canonical_bytes(evidence.to_dict()))
    with pytest.raises(ExperimentIntegrityError, match="not backed"):
        registry.get_evidence(evidence.evidence_id)

    registry.record_evidence(evidence, correlation_id="recovery:evidence")
    assert registry.get_evidence(evidence.evidence_id) == evidence
    assert ledger.events[-1].event_type is LearningEventType.EVIDENCE_RECORDED
