"""Offline invariants for advanced causal feature and SMA meta-label primitives."""

from __future__ import annotations

import importlib
import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from evoryn.domain.models import Market, MarketCandle
from evoryn.learning_lab.historical_data import HistoricalDataset, HistoricalDatasetStore
from evoryn.learning_lab.advanced_meta_label import (
    ADVANCED_FEATURE_SCHEMA_VERSION,
    advanced_feature_names,
    build_advanced_feature_matrix,
    build_sma_meta_labels,
    select_features_train_only,
    tune_meta_labeler,
)

MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
INTERVAL = timedelta(hours=1)
START = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)


def make_candles(count: int = 720) -> tuple[MarketCandle, ...]:
    result: list[MarketCandle] = []
    prior_close = 100.0
    for index in range(count):
        close_value = 100.0 + 0.004 * index + 5.0 * math.sin(index / 12.0)
        close = Decimal(str(close_value))
        opening = Decimal(str(prior_close))
        result.append(
            MarketCandle(
                market=MARKET,
                timestamp=START + index * INTERVAL,
                interval=INTERVAL,
                open=opening,
                high=max(opening, close) + Decimal("0.7"),
                low=min(opening, close) - Decimal("0.7"),
                close=close,
                volume=Decimal("17.3"),
                source="synthetic-advanced-meta-label",
            )
        )
        prior_close = close_value
    return tuple(result)


def test_advanced_features_are_finite_causal_and_versioned() -> None:
    candles = make_candles()
    names, indices, rows = build_advanced_feature_matrix(candles, lookback=48)

    assert ADVANCED_FEATURE_SCHEMA_VERSION == "1.0.0-mtf-fibonacci-swing-proxy"
    assert names == advanced_feature_names()
    assert len(indices) == len(rows) == len(candles) - 48
    assert len(set(names)) == len(names)
    assert len(rows[-1]) == len(names)
    assert all(math.isfinite(value) for row in rows for value in row)
    assert {
        "fib_distance_618_48",
        "bollinger_bandwidth_20",
        "confirmed_swing_high_count_48",
        "mtf_4h_return_6",
        "mtf_1d_return_7",
    }.issubset(names)
    # No absolute/relative volume or live-order-book features are invented.
    assert not any("volume" in name.casefold() or "order_book" in name.casefold() for name in names)


def test_feature_prefix_is_unchanged_when_future_candles_are_removed() -> None:
    candles = make_candles(720)
    index = 520
    names_all, indices_all, rows_all = build_advanced_feature_matrix(candles)
    names_prefix, indices_prefix, rows_prefix = build_advanced_feature_matrix(candles[: index + 1])

    all_row = rows_all[indices_all.index(index)]
    prefix_row = rows_prefix[indices_prefix.index(index)]
    assert names_all == names_prefix
    assert all_row == prefix_row


def test_advanced_features_reject_gaps_and_non_hourly_data() -> None:
    candles = make_candles(100)
    with pytest.raises(ValueError, match="gapped"):
        build_advanced_feature_matrix(candles[:40] + candles[41:])
    half_hour = tuple(
        MarketCandle(
            market=candle.market,
            timestamp=candle.timestamp,
            interval=timedelta(minutes=30),
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            source=candle.source,
        )
        for candle in candles
    )
    with pytest.raises(ValueError, match="1-hour"):
        build_advanced_feature_matrix(half_hour)


def test_sma_meta_labels_are_next_open_cost_aware_and_exclude_incomplete_horizons() -> None:
    candles = make_candles()
    events = build_sma_meta_labels(
        candles,
        short_window=4,
        long_window=20,
        horizon_bars=6,
        round_trip_cost_bps=30.0,
    )

    assert events
    assert all(event.entry_index == event.signal_index + 1 for event in events)
    assert all(event.exit_index == event.signal_index + 6 for event in events)
    assert all(
        event.net_return_after_costs
        == pytest.approx(event.gross_return - 0.003)
        for event in events
    )
    assert all(event.label == int(event.net_return_after_costs > 0) for event in events)
    assert all(event.exit_index < len(candles) for event in events)


def test_meta_label_input_rejects_invalid_costs_and_windows() -> None:
    candles = make_candles(100)
    with pytest.raises(ValueError, match="round_trip_cost_bps"):
        build_sma_meta_labels(candles, round_trip_cost_bps=10000.0)
    with pytest.raises(ValueError, match="SMA windows"):
        build_sma_meta_labels(candles, short_window=48, long_window=12)


def test_feature_selection_uses_only_supplied_train_rows() -> None:
    pytest.importorskip("numpy")
    pytest.importorskip("sklearn")
    # Clear synthetic dependence helps verify deterministic ranking and return shape.
    np = importlib.import_module("numpy")

    rng = np.random.default_rng(41)
    signal = rng.normal(size=240)
    x_train = np.column_stack((signal, rng.normal(size=240), signal * 0.1))
    y_train = (signal > 0).astype("int64")
    selected = select_features_train_only(
        x_train, y_train, ("signal", "noise", "scaled_signal"), top_k=2, random_seed=41
    )
    repeated = select_features_train_only(
        x_train, y_train, ("signal", "noise", "scaled_signal"), top_k=2, random_seed=41
    )
    assert selected == repeated
    assert len(selected) == 2
    assert "signal" in selected


def test_tuning_trial_count_fails_closed_before_optional_imports() -> None:
    with pytest.raises(ValueError, match="n_trials"):
        tune_meta_labeler(
            "xgboost",
            [[0.0], [1.0]] * 10,
            [0, 1] * 10,
            [[0.0], [1.0]] * 5,
            [0, 1] * 5,
            n_trials=1,
        )


def test_advanced_evaluator_loads_the_versioned_dataset_store_reference(
    tmp_path: Path,
) -> None:
    evaluator = importlib.import_module("scripts.evaluate_advanced_meta_label")

    dataset_id = "nobitex-btcusdt-1h"
    dataset_version = "a" * 64
    report = {
        "dataset": {"not_the_identity": "intentionally-not-used"},
        "dataset_store": {
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
        },
    }
    (tmp_path / "training-report.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    fake_dataset = cast(
        HistoricalDataset,
        SimpleNamespace(candles=tuple(range(2500))),
    )

    with patch.object(
        HistoricalDatasetStore, "load", return_value=fake_dataset
    ) as load_dataset:
        loaded = evaluator._load_dataset(tmp_path)

    assert loaded is cast(Any, fake_dataset)
    load_dataset.assert_called_once_with(dataset_id, dataset_version)
