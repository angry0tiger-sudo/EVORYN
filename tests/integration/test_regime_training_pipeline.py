"""Optional real-estimator integration tests on a synthetic, fully offline OHLCV dataset."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from evoryn.domain.models import Market, MarketCandle
from evoryn.intelligence.regime import RegimeLabel
from evoryn.intelligence.regime_training import RegimeTrainingConfig, train_regime_model
from evoryn.intelligence.trained_regime import (
    load_trained_regime_detector,
    load_trained_regime_model,
)
from evoryn.learning_lab.historical_data import HistoricalDataset, HistoricalDatasetStore


MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
INTERVAL = timedelta(hours=1)
START = datetime(2025, 1, 1, tzinfo=UTC)


def synthetic_dataset() -> HistoricalDataset:
    """Create deterministic trend, reversal, range, and high-volatility regimes."""
    closes = [Decimal("100")]
    for index in range(1, 1600):
        regime = (index // 160) % 4
        prior = closes[-1]
        if regime == 0:
            multiplier = Decimal("1.002")
        elif regime == 1:
            multiplier = Decimal("0.998")
        elif regime == 2:
            multiplier = Decimal("1.0002") if index % 2 else Decimal("0.9998")
        else:
            multiplier = Decimal("1.015") if index % 2 else Decimal("0.985")
        closes.append(prior * multiplier)

    candles = tuple(
        MarketCandle(
            market=MARKET,
            timestamp=START + INTERVAL * index,
            interval=INTERVAL,
            open=close,
            high=close * Decimal("1.02"),
            low=close * Decimal("0.98"),
            close=close,
            volume=Decimal("1000") + Decimal(index % 17),
            source="synthetic-training-fixture",
        )
        for index, close in enumerate(closes)
    )
    return HistoricalDataset(
        dataset_id="synthetic-regime-integration",
        market=MARKET,
        interval=INTERVAL,
        volume_unit="base_asset",
        source_name="synthetic-training-fixture",
        source_uri="https://example.invalid/synthetic-regime-fixture",
        source_revision="fixture-v1",
        license_id="test-data",
        license_uri="https://example.invalid/test-data-license",
        collected_at=candles[-1].timestamp + INTERVAL,
        candles=candles,
    )


def test_trainer_writes_versioned_model_and_three_reproducible_holdout_backtests(
    tmp_path: Path,
) -> None:
    pytest.importorskip("numpy")
    pytest.importorskip("sklearn")
    pytest.importorskip("joblib")
    dataset = synthetic_dataset()
    store = HistoricalDatasetStore(tmp_path / "datasets")
    saved_dataset = store.save(dataset)
    verified = store.load(saved_dataset.dataset_id, saved_dataset.version)
    assert verified.version == dataset.version

    output = tmp_path / "training"
    report = train_regime_model(
        verified,
        artifact_dir=output,
        config=RegimeTrainingConfig(
            feature_lookback=12,
            horizon_bars=6,
            train_fraction=0.60,
            validation_fraction=0.20,
            trend_quantile=0.65,
            volatility_quantile=0.75,
            min_class_samples=3,
            random_seed=41,
            confidence_threshold=Decimal("0.55"),
        ),
    )

    assert (output / "regime-model.joblib").is_file()
    assert (output / "model-manifest.json").is_file()
    assert (output / "training-report.json").is_file()
    assert report["release_disposition"] == "RESEARCH_ONLY_NOT_APPROVED_FOR_LIVE_TRADING"
    metrics = report["training"]["test_classification_metrics"]
    assert metrics["sample_count"] > 100
    assert 0.0 <= metrics["accuracy"] <= 1.0
    assert 0.0 <= metrics["balanced_accuracy"] <= 1.0
    assert metrics["target_provenance"] == "weak_future-outcome_labels"
    assert report["training"]["classifier"]["calibration_validation_metrics"]["sample_count"] > 0
    assert set(report["backtests"]) == {
        "trained-regime-long-flat",
        "sma-crossover-long-flat",
        "buy-and-hold-reference",
    }
    for result in report["backtests"].values():
        assert result["dataset_version"] == dataset.version
        assert result["metrics"]["initial_equity"] == "10000"
        assert Decimal(result["metrics"]["max_drawdown_fraction"]) >= 0
        assert (output / result["report_path"]).is_file()

    model = load_trained_regime_model(
        output / "regime-model.joblib",
        output / "model-manifest.json",
    )
    prediction = model.predict_probabilities(
        dataset.candles[-model.feature_extractor.minimum_candles :]
    )
    assert set(prediction) == {
        RegimeLabel.BULL_TREND,
        RegimeLabel.BEAR_TREND,
        RegimeLabel.RANGE_BOUND,
        RegimeLabel.HIGH_VOLATILITY,
    }
    assert abs(sum(prediction.values(), Decimal("0")) - Decimal("1")) < Decimal("0.000001")

    detector = load_trained_regime_detector(
        output / "regime-model.joblib",
        output / "model-manifest.json",
        min_confidence=Decimal("0.65"),
    )
    classified = detector.classify(
        dataset.candles[-model.feature_extractor.minimum_candles :],
        now=dataset.candles[-1].timestamp + dataset.interval,
    )
    assert classified.model_id == model.model_id
    assert classified.model_fingerprint == model.model_fingerprint
    assert classified.label in {
        RegimeLabel.BULL_TREND,
        RegimeLabel.BEAR_TREND,
        RegimeLabel.RANGE_BOUND,
        RegimeLabel.HIGH_VOLATILITY,
        RegimeLabel.UNKNOWN,
    }
