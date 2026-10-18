"""Network-free Phase 14 tests for causal features, regime uncertainty and HMM boundaries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from evoryn.domain.models import Market, MarketCandle
from evoryn.intelligence.hmmlearn_adapter import GaussianHMMRegimeModel
from evoryn.intelligence.regime import (
    AbstainingRegimeDetector,
    InvalidRegimeHistory,
    RegimeFeatureExtractor,
    RegimeLabel,
    RegimeThresholds,
    RuleBasedRegimeBaseline,
)


BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
INTERVAL = timedelta(hours=1)
MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
SOURCE = "synthetic-regime-fixture"


def candles(
    count: int = 80,
    *,
    closes: Sequence[str] | None = None,
    gap_at: int | None = None,
    market: Market = MARKET,
    source: str = SOURCE,
) -> tuple[MarketCandle, ...]:
    """Create a strictly synthetic closed-candle sequence with explicit identity."""
    close_values = closes or tuple(
        str(Decimal("100") + Decimal(index) / Decimal("10"))
        for index in range(count)
    )
    if len(close_values) != count:
        raise ValueError("close fixture length must equal count")
    result: list[MarketCandle] = []
    gap_offset = 0
    for index, raw_close in enumerate(close_values):
        if gap_at is not None and index >= gap_at:
            gap_offset = 1
        close = Decimal(raw_close)
        result.append(
            MarketCandle(
                market=market,
                timestamp=BASE_TIME + INTERVAL * (index + gap_offset),
                interval=INTERVAL,
                open=close,
                high=close + Decimal("1"),
                low=close - Decimal("1"),
                close=close,
                volume=Decimal("100") + Decimal(index),
                source=source,
            )
        )
    return tuple(result)


class FixedProbabilityModel:
    """Small fake model for testing the detector without installing ML dependencies."""

    model_id = "test.fixed-regime-model"
    model_version = "test-1.0.0"
    model_fingerprint = "test-artifact-sha256"
    confidence_semantics = "hmm_posterior"

    def __init__(
        self,
        probabilities: Mapping[RegimeLabel, Decimal | float],
        *,
        ready: bool = True,
    ) -> None:
        self.probabilities = probabilities
        self._ready = ready
        self.calls = 0

    @property
    def is_ready(self) -> bool:
        return self._ready

    def predict_probabilities(
        self, completed_history: Sequence[MarketCandle]
    ) -> Mapping[RegimeLabel, Decimal | float]:
        self.calls += 1
        assert completed_history
        return self.probabilities


def strong_bull_probabilities() -> dict[RegimeLabel, Decimal]:
    return {
        RegimeLabel.BULL_TREND: Decimal("0.82"),
        RegimeLabel.BEAR_TREND: Decimal("0.06"),
        RegimeLabel.RANGE_BOUND: Decimal("0.08"),
        RegimeLabel.HIGH_VOLATILITY: Decimal("0.04"),
    }


def test_feature_extractor_uses_only_the_completed_trailing_window() -> None:
    history = candles(50)
    extractor = RegimeFeatureExtractor(lookback=10)

    prefix_features = extractor.extract(history[:25])
    extended_features = extractor.extract(history)

    assert prefix_features == extractor.extract(history[:25])
    assert prefix_features.as_of == history[24].timestamp + INTERVAL
    assert extended_features.as_of == history[-1].timestamp + INTERVAL
    assert prefix_features.trend_strength > 0
    assert prefix_features.realized_volatility >= 0
    assert len(extractor.extract_series(history)) == len(history) - extractor.lookback


def test_feature_extractor_rejects_insufficient_history_and_time_gaps() -> None:
    extractor = RegimeFeatureExtractor(lookback=10)
    with pytest.raises(ValueError, match="at least"):
        extractor.extract(candles(5))

    with pytest.raises(InvalidRegimeHistory, match="interval gap"):
        extractor.extract(candles(30, gap_at=20))


def test_feature_extractor_requires_canonical_utc_timestamps() -> None:
    source = candles(25)
    offset = timezone(timedelta(hours=4))
    first = source[0]
    shifted = MarketCandle(
        market=first.market,
        timestamp=first.timestamp.astimezone(offset),
        interval=first.interval,
        open=first.open,
        high=first.high,
        low=first.low,
        close=first.close,
        volume=first.volume,
        source=first.source,
    )
    with pytest.raises(InvalidRegimeHistory, match="canonical UTC"):
        RegimeFeatureExtractor(lookback=5).extract((shifted,) + source[1:])


def test_feature_extractor_rejects_mixed_market_interval_or_source() -> None:
    extractor = RegimeFeatureExtractor(lookback=5)
    history = list(candles(12))
    history[-1] = MarketCandle(
        market=Market(symbol="ETH-USDT", venue="nobitex", market_type="spot"),
        timestamp=history[-1].timestamp,
        interval=INTERVAL,
        open=history[-1].open,
        high=history[-1].high,
        low=history[-1].low,
        close=history[-1].close,
        volume=history[-1].volume,
        source=SOURCE,
    )
    with pytest.raises(InvalidRegimeHistory, match="market identities"):
        extractor.extract(history)


def test_detector_classifies_only_when_confidence_meets_threshold() -> None:
    history = candles(40)
    model = FixedProbabilityModel(strong_bull_probabilities())
    detector = AbstainingRegimeDetector(
        model,
        feature_extractor=RegimeFeatureExtractor(lookback=10),
        thresholds=RegimeThresholds(min_confidence=Decimal("0.65")),
    )
    now = history[-1].timestamp + INTERVAL + timedelta(minutes=5)

    result = detector.classify(history, now=now)

    assert result.label is RegimeLabel.BULL_TREND
    assert result.confidence == Decimal("0.82")
    assert not result.abstained
    assert result.reason_code == "CLASSIFIED"
    assert result.model_fingerprint == model.model_fingerprint
    assert result.min_confidence_threshold == Decimal("0.65")
    assert result.max_data_age_intervals == 2
    assert sum((p for _, p in result.probabilities), Decimal("0")) == Decimal("1")


def test_low_confidence_returns_unknown_not_a_forced_regime() -> None:
    history = candles(40)
    probabilities = {
        RegimeLabel.BULL_TREND: Decimal("0.30"),
        RegimeLabel.BEAR_TREND: Decimal("0.25"),
        RegimeLabel.RANGE_BOUND: Decimal("0.25"),
        RegimeLabel.HIGH_VOLATILITY: Decimal("0.20"),
    }
    model = FixedProbabilityModel(probabilities)
    detector = AbstainingRegimeDetector(
        model,
        feature_extractor=RegimeFeatureExtractor(lookback=10),
        thresholds=RegimeThresholds(min_confidence=Decimal("0.65")),
    )

    result = detector.classify(
        history, now=history[-1].timestamp + INTERVAL + timedelta(minutes=1)
    )

    assert result.label is RegimeLabel.UNKNOWN
    assert result.abstained
    assert result.reason_code == "LOW_CONFIDENCE"
    assert result.confidence == Decimal("0.30")


@pytest.mark.parametrize(
    ("now_delta", "expected_reason"),
    [
        (timedelta(hours=3), "STALE_DATA"),
        (-INTERVAL, "FUTURE_CANDLE"),
    ],
)
def test_stale_or_future_candles_force_abstention(
    now_delta: timedelta, expected_reason: str
) -> None:
    history = candles(40)
    model = FixedProbabilityModel(strong_bull_probabilities())
    detector = AbstainingRegimeDetector(
        model,
        feature_extractor=RegimeFeatureExtractor(lookback=10),
        thresholds=RegimeThresholds(max_data_age_intervals=2),
    )
    close_time = history[-1].timestamp + INTERVAL

    result = detector.classify(history, now=close_time + now_delta)

    assert result.label is RegimeLabel.UNKNOWN
    assert result.reason_code == expected_reason
    assert model.calls == 0


def test_unfitted_model_and_short_history_abstain_before_inference() -> None:
    model = FixedProbabilityModel(strong_bull_probabilities(), ready=False)
    detector = AbstainingRegimeDetector(
        model, feature_extractor=RegimeFeatureExtractor(lookback=10)
    )
    history = candles(40)
    now = history[-1].timestamp + INTERVAL

    short_history = history[:5]
    assert detector.classify(
        short_history, now=short_history[-1].timestamp + INTERVAL
    ).reason_code == "INSUFFICIENT_HISTORY"
    result = detector.classify(history, now=now)

    assert result.label is RegimeLabel.UNKNOWN
    assert result.reason_code == "MODEL_NOT_READY"
    assert model.calls == 0


def test_invalid_model_probabilities_abstain() -> None:
    invalid = {
        RegimeLabel.BULL_TREND: Decimal("0.8"),
        RegimeLabel.BEAR_TREND: Decimal("0.8"),
        RegimeLabel.RANGE_BOUND: Decimal("0.1"),
        RegimeLabel.HIGH_VOLATILITY: Decimal("0.1"),
    }
    history = candles(40)
    detector = AbstainingRegimeDetector(
        FixedProbabilityModel(invalid),
        feature_extractor=RegimeFeatureExtractor(lookback=10),
    )

    result = detector.classify(history, now=history[-1].timestamp + INTERVAL)

    assert result.label is RegimeLabel.UNKNOWN
    assert result.reason_code == "INVALID_MODEL_OUTPUT"


def test_rule_baseline_scores_are_not_treated_as_calibrated_confidence() -> None:
    history = candles(40)
    features = RegimeFeatureExtractor(lookback=10)
    baseline = RuleBasedRegimeBaseline(features)
    detector = AbstainingRegimeDetector(
        baseline,
        feature_extractor=features,
        thresholds=RegimeThresholds(min_confidence=Decimal("0.65")),
    )

    result = detector.classify(history, now=history[-1].timestamp + INTERVAL)

    assert result.label is RegimeLabel.UNKNOWN
    assert result.reason_code == "UNVALIDATED_CONFIDENCE"
    assert result.confidence_semantics == "heuristic_score"


def test_rule_baseline_is_deterministic_and_has_no_future_data_port() -> None:
    history = candles(40)
    features = RegimeFeatureExtractor(lookback=10)
    baseline = RuleBasedRegimeBaseline(features)
    prefix_probabilities = baseline.predict_probabilities(history[:25])
    repeated_probabilities = baseline.predict_probabilities(history[:25])

    assert prefix_probabilities == repeated_probabilities
    assert set(prefix_probabilities) == {
        RegimeLabel.BULL_TREND,
        RegimeLabel.BEAR_TREND,
        RegimeLabel.RANGE_BOUND,
        RegimeLabel.HIGH_VOLATILITY,
    }
    assert sum(prefix_probabilities.values(), Decimal("0")) == Decimal("1")


def test_hmm_training_refuses_validation_or_test_split_without_importing_optional_model() -> None:
    history = candles(30)
    model = GaussianHMMRegimeModel(feature_extractor=RegimeFeatureExtractor(lookback=5))

    with pytest.raises(ValueError, match="only split_name='train'"):
        model.fit(
            history,
            dataset_version="a" * 64,
            split_name="test",
            train_start_index=0,
            train_end_index_exclusive=len(history),
        )

    assert not model.is_ready
    assert model.training_manifest is None


def test_hmm_training_requires_declared_range_to_match_candle_count() -> None:
    history = candles(30)
    model = GaussianHMMRegimeModel(feature_extractor=RegimeFeatureExtractor(lookback=5))

    with pytest.raises(ValueError, match="must match"):
        model.fit(
            history,
            dataset_version="a" * 64,
            split_name="train",
            train_start_index=0,
            train_end_index_exclusive=len(history) - 1,
        )

    assert not model.is_ready


def test_unknown_is_not_a_learned_probability_class() -> None:
    history = candles(40)
    detector = AbstainingRegimeDetector(
        FixedProbabilityModel(strong_bull_probabilities()),
        feature_extractor=RegimeFeatureExtractor(lookback=10),
    )
    result = detector.classify(history, now=history[-1].timestamp + INTERVAL)

    assert RegimeLabel.UNKNOWN not in {label for label, _ in result.probabilities}
    assert result.label is not RegimeLabel.UNKNOWN
    assert not result.abstained
