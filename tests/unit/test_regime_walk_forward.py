"""Unit tests for purged walk-forward label policies and causal HMM filtering."""

from __future__ import annotations

import copy
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from evoryn.domain.models import Market, MarketCandle
from evoryn.intelligence.regime import RegimeLabel
from evoryn.intelligence.regime_walk_forward import (
    _build_candidate_gates_by_policy,
    filtered_gaussian_hmm_probabilities,
    volatility_scaled_triple_barrier_label,
)
from evoryn.learning_lab.backtest import make_walk_forward_folds
from evoryn.learning_lab.historical_data import HistoricalDataset

MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
INTERVAL = timedelta(hours=1)
START = datetime(2026, 1, 1, tzinfo=UTC)


def candles_with_future_path(
    future_high: str,
    future_low: str,
    *,
    future_close: str = "100",
) -> tuple[MarketCandle, ...]:
    rows = (
        ("100", "100", "100"),
        (future_close, future_high, future_low),
        ("100", "101", "99"),
        ("100", "101", "99"),
    )
    return tuple(
        MarketCandle(
            market=MARKET,
            timestamp=START + INTERVAL * index,
            interval=INTERVAL,
            open=Decimal(close),
            high=Decimal(high),
            low=Decimal(low),
            close=Decimal(close),
            volume=Decimal("10"),
            source="walk-forward-unit-test",
        )
        for index, (close, high, low) in enumerate(rows)
    )


@pytest.mark.parametrize(
    ("high", "low", "expected"),
    [
        ("104", "99", RegimeLabel.BULL_TREND),
        ("101", "96", RegimeLabel.BEAR_TREND),
        ("104", "96", RegimeLabel.HIGH_VOLATILITY),
        ("101", "99", RegimeLabel.RANGE_BOUND),
    ],
)
def test_volatility_scaled_triple_barrier_uses_ohlc_first_touch(
    high: str,
    low: str,
    expected: RegimeLabel,
) -> None:
    candles = candles_with_future_path(high, low)
    label = volatility_scaled_triple_barrier_label(
        candles,
        0,
        trailing_volatility=0.01,
        horizon_bars=2,
        barrier_multiplier=1.5,
        high_volatility_threshold=0.02,
    )
    assert label is expected


def test_triple_barrier_rejects_invalid_thresholds_and_incomplete_horizon() -> None:
    candles = candles_with_future_path("101", "99")
    with pytest.raises(ValueError, match="valid range"):
        volatility_scaled_triple_barrier_label(
            candles,
            0,
            trailing_volatility=-0.01,
            horizon_bars=2,
            barrier_multiplier=1.5,
            high_volatility_threshold=0.02,
        )
    with pytest.raises(ValueError, match="complete future horizon"):
        volatility_scaled_triple_barrier_label(
            candles,
            2,
            trailing_volatility=0.01,
            horizon_bars=2,
            barrier_multiplier=1.5,
            high_volatility_threshold=0.02,
        )


def test_hmm_filtering_is_causal_and_prefix_probabilities_do_not_change() -> None:
    numpy = pytest.importorskip("numpy")

    class FittedDiagonalGaussianHMM:
        startprob_ = numpy.asarray([0.5, 0.5], dtype="float64")
        transmat_ = numpy.asarray([[0.9, 0.1], [0.1, 0.9]], dtype="float64")
        means_ = numpy.asarray([[-1.0, 0.0], [1.0, 0.0]], dtype="float64")
        covars_ = numpy.asarray(
            [numpy.eye(2, dtype="float64"), numpy.eye(2, dtype="float64")]
        )

    model = FittedDiagonalGaussianHMM()
    prefix = numpy.asarray(
        [[-1.0, 0.0], [-0.8, 0.1], [0.9, 0.0]],
        dtype="float64",
    )
    extended = numpy.vstack(
        [prefix, numpy.asarray([[100.0, -100.0]], dtype="float64")]
    )
    prefix_result = filtered_gaussian_hmm_probabilities(model, prefix)
    extended_result = filtered_gaussian_hmm_probabilities(model, extended)
    assert numpy.allclose(prefix_result, extended_result[: len(prefix)], atol=1e-12)
    assert numpy.allclose(
        extended_result.sum(axis=1),
        numpy.ones(len(extended)),
    )
    assert numpy.isfinite(extended_result).all()


def _technical_fixture(count: int = 80) -> tuple[MarketCandle, ...]:
    from decimal import Decimal

    from evoryn.domain.models import Market, MarketCandle

    market = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
    interval = timedelta(hours=1)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows: list[MarketCandle] = []
    prior_close = 100.0
    for index in range(count):
        close_value = 100.0 + index * 0.18 + math.sin(index / 3.0) * 0.7
        close = Decimal(str(close_value))
        opening = Decimal(str(prior_close))
        rows.append(
            MarketCandle(
                market=market,
                timestamp=start + index * interval,
                interval=interval,
                open=opening,
                high=max(opening, close) + Decimal("0.6"),
                low=min(opening, close) - Decimal("0.6"),
                close=close,
                volume=Decimal("100"),
                source="synthetic-technical-features",
            )
        )
        prior_close = close_value
    return tuple(rows)


def test_research_technical_features_are_causal_finite_and_volume_free() -> None:
    from evoryn.intelligence.regime_research_models import (
        FEATURE_NAMES,
        FEATURE_SCHEMA_VERSION,
        research_feature_vector,
    )

    candles = _technical_fixture()
    from_full = research_feature_vector(candles, 60, lookback=48)
    from_prefix = research_feature_vector(candles[:61], 60, lookback=48)

    assert FEATURE_SCHEMA_VERSION == "3.0.0-price-technical-only"
    assert len(from_full) == len(FEATURE_NAMES) == 21
    assert from_full == from_prefix
    assert all(math.isfinite(value) for value in from_full)
    assert not any("volume" in name.casefold() for name in FEATURE_NAMES)
    assert "rsi_14_scaled" in FEATURE_NAMES
    assert "macd_histogram_pct" in FEATURE_NAMES
    assert "bollinger_percent_b" in FEATURE_NAMES
    assert "atr_14_pct" in FEATURE_NAMES


def test_lstm_training_produces_normalized_probabilities_when_torch_is_available() -> None:
    pytest.importorskip("torch")
    numpy = pytest.importorskip("numpy")
    from evoryn.intelligence.regime_research_models import fit_lstm, lstm_probabilities

    generator = numpy.random.default_rng(41)
    features = generator.normal(size=(20, 12, 21)).astype("float32")
    targets = numpy.asarray([0, 1, 2, 3] * 5, dtype="int64")
    model, torch_module = fit_lstm(
        features,
        targets,
        random_seed=41,
        epochs=1,
    )
    probabilities = lstm_probabilities(model, torch_module, features[:4], numpy)

    assert probabilities.shape == (4, 4)
    assert numpy.isfinite(probabilities).all()
    assert numpy.allclose(probabilities.sum(axis=1), numpy.ones(4), atol=1e-6)


def test_terminal_confirmation_geometry_stays_outside_all_research_folds() -> None:
    count = 4319
    confirmation_bars = (count + 9) // 10
    research_end = count - confirmation_bars
    candles = _technical_fixture(count)
    dataset = HistoricalDataset(
        dataset_id="walk-forward-confirmation-test",
        market=MARKET,
        interval=INTERVAL,
        volume_unit="unknown",
        source_name="synthetic-technical-features",
        source_uri="urn:evoryn:test:source",
        source_revision="fixture-v1",
        license_id="test-only-unreviewed",
        license_uri="urn:evoryn:test:license",
        collected_at=START + count * INTERVAL,
        candles=candles,
    )
    test_bars = int(count * 0.035)
    folds = make_walk_forward_folds(
        dataset,
        train_bars=int(count * 0.40),
        validation_bars=int(count * 0.10),
        test_bars=test_bars,
        step_bars=test_bars,
        evaluation_end_index=research_end,
    )

    assert len(folds) >= 10
    assert all(fold.test.end <= research_end for fold in folds)
    assert folds[-1].test.end < research_end
    assert all(
        left.test.end <= right.test.start
        for left, right in zip(folds, folds[1:], strict=False)
    )
    assert len(candles[research_end:]) == confirmation_bars
    with pytest.raises(ValueError, match="evaluation_end_index"):
        make_walk_forward_folds(
            dataset,
            train_bars=10,
            validation_bars=5,
            test_bars=5,
            evaluation_end_index=count + 1,
        )


def test_lightgbm_fixed_class_order_probabilities_and_serialization(tmp_path: Path) -> None:
    lightgbm = pytest.importorskip("lightgbm")
    joblib = pytest.importorskip("joblib")
    numpy = pytest.importorskip("numpy")

    generator = numpy.random.default_rng(41)
    features = generator.normal(size=(80, 21)).astype("float64")
    targets = numpy.asarray([0, 1, 2, 3] * 20, dtype="int64")
    estimator = lightgbm.LGBMClassifier(
        objective="multiclass",
        num_class=4,
        n_estimators=20,
        learning_rate=0.04,
        max_depth=3,
        num_leaves=7,
        max_bin=31,
        min_child_samples=2,
        n_jobs=1,
        random_state=41,
        deterministic=True,
        force_col_wise=True,
        verbosity=-1,
    )
    estimator.fit(features, targets)
    probabilities = estimator.predict_proba(features[:8])

    assert tuple(int(value) for value in estimator.classes_) == (0, 1, 2, 3)
    assert probabilities.shape == (8, 4)
    assert numpy.isfinite(probabilities).all()
    assert numpy.allclose(probabilities.sum(axis=1), numpy.ones(8), atol=1e-6)

    artifact = tmp_path / "lightgbm-challenger.joblib"
    joblib.dump(
        {
            "estimator": estimator,
            "class_order": ["bull_trend", "bear_trend", "range_bound", "high_volatility"],
            "label_encoding": {
                "0": "bull_trend",
                "1": "bear_trend",
                "2": "range_bound",
                "3": "high_volatility",
            },
        },
        artifact,
    )
    restored = joblib.load(artifact)
    assert numpy.allclose(
        probabilities, restored["estimator"].predict_proba(features[:8]), atol=1e-12
    )



def test_candidate_gates_cover_every_model_policy_and_fail_closed_on_missing_backtest() -> None:
    pytest.importorskip("numpy")
    policies = (
        "forward_thresholds",
        "volatility_scaled_triple_barrier",
    )
    models = (
        "random_forest",
        "hist_gradient_boosting",
        "xgboost",
        "lightgbm",
        "gaussian_hmm",
        "lstm",
    )
    fold_reports: list[dict[str, Any]] = []
    for fold_index in range(10):
        baselines = {
            "buy-and-hold-reference": {
                "metrics": {"net_return_fraction": 0.0},
            },
            "sma-crossover-long-flat": {
                "metrics": {"net_return_fraction": 0.0},
            },
        }
        policy_reports: dict[str, Any] = {}
        for policy in policies:
            model_reports: dict[str, Any] = {}
            backtests: dict[str, Any] = {}
            for model in models:
                model_reports[model] = {
                    "status": "EVALUATED",
                    "test_metrics": {
                                "accuracy": 0.80,
                        "balanced_accuracy": 0.75,
                        "macro_f1": 0.70,
                    },
                    "training_only_majority_baseline": {
                        "accuracy": 0.60,
                        "balanced_accuracy": 0.25,
                        "macro_f1": 0.20,
                    },
                }
                backtests[model] = {
                    "metrics": {
                        "net_return_fraction": 0.02,
                        "closed_trade_count": 3,
                    },
                }
            policy_reports[policy] = {
                "models": model_reports,
                "backtests": backtests,
                "baseline_backtests": baselines,
            }
        fold_reports.append(
            {
                "fold_index": fold_index,
                "label_policies": policy_reports,
                "same_fold_baseline_backtests": baselines,
            }
        )

    gates = _build_candidate_gates_by_policy(
        fold_reports,
        expected_fold_count=10,
        random_seed=41,
    )
    assert set(gates) == set(policies)
    for policy in policies:
        assert set(gates[policy]) == set(models)
        for model in models:
            gate = gates[policy][model]
            assert gate["status"] == "EVALUATED"
            assert gate["evaluated_folds"] == 10
            assert gate["classification_gate_passed"] is True
            assert gate["economic_gate_passed"] is True
            assert gate["net_return_difference_vs_sma"]["ci95_lower"] > 0
            assert gate["empirical_candidate_gate_passed"] is True
            assert gate["runtime_promotion_allowed"] is False

    incomplete = copy.deepcopy(fold_reports)
    del incomplete[0]["label_policies"]["forward_thresholds"]["backtests"]["lightgbm"]
    incomplete_gates = _build_candidate_gates_by_policy(
        incomplete,
        expected_fold_count=10,
        random_seed=41,
    )
    lightgbm_gate = incomplete_gates["forward_thresholds"]["lightgbm"]
    assert lightgbm_gate["status"] == "INCOMPLETE_FOLD_COVERAGE"
    assert lightgbm_gate["evaluated_folds"] == 9
    assert lightgbm_gate["all_folds_gate_passed"] is False
    assert lightgbm_gate["empirical_candidate_gate_passed"] is False
