"""Causal price/technical features and research-only model adapters (no execution ports)."""

from __future__ import annotations

import importlib
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from evoryn.domain.models import MarketCandle
from evoryn.intelligence.regime import RegimeLabel
from evoryn.intelligence.regime_training import _temperature_scale

FEATURE_SCHEMA_VERSION = "3.0.0-price-technical-only"
SEQUENCE_LENGTH = 12
FEATURE_NAMES = (
    "return_1", "return_3", "return_6", "return_12", "return_24",
    "mean_return_6", "realized_volatility_6", "realized_volatility_12",
    "realized_volatility_24", "sma_gap_12", "sma_gap_24", "rsi_14_scaled",
    "macd_line_pct", "macd_signal_pct", "macd_histogram_pct",
    "bollinger_percent_b", "bollinger_bandwidth", "atr_14_pct",
    "trend_strength_48", "range_position_24", "volatility_ratio_6_24",
)
VOLATILITY_FEATURE_INDEX = FEATURE_NAMES.index("realized_volatility_24")
LABELS = (
    RegimeLabel.BULL_TREND,
    RegimeLabel.BEAR_TREND,
    RegimeLabel.RANGE_BOUND,
    RegimeLabel.HIGH_VOLATILITY,
)


def _ema(values: Sequence[float], period: int) -> tuple[float, ...]:
    if not values or period <= 0:
        raise ValueError("EMA requires values and a positive period.")
    alpha = 2.0 / (period + 1.0)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1.0 - alpha) * result[-1])
    return tuple(result)


def research_feature_vector(
    candles: Sequence[MarketCandle], end_index: int, *, lookback: int = 48
) -> tuple[float, ...]:
    """Compute causal indicators from the trailing window ending at end_index."""
    if type(end_index) is not int or end_index < lookback or end_index >= len(candles):
        raise ValueError("Research feature index is outside the complete lookback range.")
    window = candles[end_index - lookback : end_index + 1]
    close = tuple(float(c.close) for c in window)
    high = tuple(float(c.high) for c in window)
    low = tuple(float(c.low) for c in window)
    if len(close) != lookback + 1 or any(
        not math.isfinite(v) or v <= 0 for v in (*close, *high, *low)
    ):
        raise ValueError("Research feature input has invalid OHLC values.")
    returns = tuple(close[i] / close[i - 1] - 1.0 for i in range(1, len(close)))

    def mean(values: Sequence[float]) -> float:
        return sum(values) / len(values)

    def std(values: Sequence[float]) -> float:
        center = mean(values)
        return math.sqrt(mean(tuple((v - center) ** 2 for v in values)))

    def ret(period: int) -> float:
        return close[-1] / close[-1 - period] - 1.0

    vol6, vol12, vol24 = std(returns[-6:]), std(returns[-12:]), std(returns[-24:])
    sma12, sma24 = mean(close[-12:]), mean(close[-24:])
    changes = tuple(close[i] - close[i - 1] for i in range(len(close) - 14, len(close)))
    gain = mean(tuple(max(v, 0.0) for v in changes))
    loss = mean(tuple(max(-v, 0.0) for v in changes))
    if loss <= 1e-15:
        rsi = 1.0 if gain > 1e-15 else 0.0
    else:
        rsi_value = 100.0 - 100.0 / (1.0 + gain / loss)
        rsi = (rsi_value - 50.0) / 50.0

    macd = tuple(a - b for a, b in zip(_ema(close, 12), _ema(close, 26), strict=True))
    signal = _ema(macd, 9)
    macd_line_pct, macd_signal_pct = macd[-1] / close[-1], signal[-1] / close[-1]
    macd_hist_pct = (macd[-1] - signal[-1]) / close[-1]

    bb = close[-20:]
    bb_mean, bb_std = mean(bb), std(bb)
    bb_width = 4.0 * bb_std / bb_mean if bb_mean > 0 else 0.0
    bb_percent = (
        (close[-1] - (bb_mean - 2.0 * bb_std)) / (4.0 * bb_std)
        if bb_std > 1e-15
        else 0.5
    )

    true_ranges: list[float] = []
    for i in range(len(window) - 14, len(window)):
        prev = close[i - 1]
        true_ranges.append(max(high[i] - low[i], abs(high[i] - prev), abs(low[i] - prev)))
    atr_pct = mean(true_ranges) / close[-1]
    range_high, range_low = max(high[-24:]), min(low[-24:])
    range_position = (
        (close[-1] - range_low) / (range_high - range_low)
        if range_high > range_low
        else 0.5
    )
    vol_ratio = vol6 / vol24 if vol24 > 1e-12 else 0.0
    features = (
        ret(1), ret(3), ret(6), ret(12), ret(24), mean(returns[-6:]),
        vol6, vol12, vol24, close[-1] / sma12 - 1.0, close[-1] / sma24 - 1.0,
        rsi, macd_line_pct, macd_signal_pct, macd_hist_pct, bb_percent, bb_width,
        atr_pct, ret(48), range_position, vol_ratio,
    )
    if len(features) != len(FEATURE_NAMES) or not all(math.isfinite(v) for v in features):
        raise ValueError("Research features contain non-finite values or schema mismatch.")
    return features


def cached_research_feature(
    history: Sequence[MarketCandle],
    end_index: int,
    lookback: int,
    cache: dict[datetime, tuple[float, ...]],
) -> tuple[float, ...]:
    timestamp = history[end_index].timestamp
    feature = cache.get(timestamp)
    if feature is None:
        feature = research_feature_vector(history, end_index, lookback=lookback)
        cache[timestamp] = feature
    return feature


def sequence_matrix(
    features: Mapping[int, Sequence[float]], indices: Sequence[int],
    sequence_length: int, numpy: Any,
) -> Any:
    """Build chronological, fixed-length sequence tensors with labels aligned to the last row."""
    sequences: list[Sequence[Sequence[float]]] = []
    for index in indices:
        positions = range(index - sequence_length + 1, index + 1)
        if any(position not in features for position in positions):
            raise ValueError("Sequence input crosses a missing feature row.")
        sequences.append([features[position] for position in positions])
    if not sequences:
        raise ValueError("No complete chronological sequences were generated.")
    matrix = numpy.asarray(sequences, dtype="float32")
    if matrix.ndim != 3 or not numpy.isfinite(matrix).all():
        raise ValueError("Sequence matrix has invalid shape or non-finite values.")
    return matrix


def _new_lstm(torch: Any, input_size: int, hidden_size: int = 32) -> Any:
    nn = torch.nn

    class CompactRegimeLSTM(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.recurrent = nn.LSTM(
                input_size=input_size, hidden_size=hidden_size, batch_first=True
            )
            self.dropout = nn.Dropout(p=0.10)
            self.classifier = nn.Linear(hidden_size, len(LABELS))

        def forward(self, values: Any) -> Any:
            outputs, _ = self.recurrent(values)
            return self.classifier(self.dropout(outputs[:, -1, :]))

    return CompactRegimeLSTM()


def fit_lstm(
    sequences: Any, target_ids: Any, *, random_seed: int, epochs: int
) -> tuple[Any, Any]:
    """Fit a deterministic class-weighted LSTM using training sequences only."""
    torch = importlib.import_module("torch")
    torch.manual_seed(random_seed)
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True, warn_only=True)
    model = _new_lstm(torch, int(sequences.shape[2]))
    x = torch.as_tensor(sequences, dtype=torch.float32)
    y = torch.as_tensor(target_ids, dtype=torch.long)
    counts = torch.bincount(y, minlength=len(LABELS)).to(dtype=torch.float32)
    weights = counts.sum() / (len(LABELS) * counts.clamp(min=1.0))
    criterion = torch.nn.CrossEntropyLoss(weight=weights)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.002, weight_decay=0.01)
    model.train()
    for _ in range(epochs):
        order = torch.randperm(len(x))
        for start in range(0, len(x), 64):
            batch = order[start : start + 64]
            optimizer.zero_grad(set_to_none=True)
            loss = criterion(model(x[batch]), y[batch])
            if not bool(torch.isfinite(loss)):
                raise ValueError("LSTM training generated a non-finite loss.")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
    model.eval()
    return model, torch


def lstm_probabilities(model: Any, torch: Any, sequences: Any, numpy: Any) -> Any:
    if len(sequences) == 0:
        raise ValueError("LSTM prediction requires a sequence.")
    model.eval()
    with torch.no_grad():
        probs = torch.softmax(
            model(torch.as_tensor(sequences, dtype=torch.float32)), dim=1
        ).cpu().numpy()
    if probs.shape != (len(sequences), len(LABELS)) or not numpy.isfinite(probs).all():
        raise ValueError("LSTM probability output is invalid.")
    return probs


def bootstrap_mean_interval(
    values: Sequence[float], *, random_seed: int, resamples: int = 10000
) -> dict[str, float]:
    """Descriptive paired-fold bootstrap interval; temporal dependence remains a limitation."""
    numpy = importlib.import_module("numpy")
    array = numpy.asarray(values, dtype="float64")
    if array.ndim != 1 or not len(array) or not numpy.isfinite(array).all():
        raise ValueError("Bootstrap input must be a non-empty finite series.")
    rng = numpy.random.default_rng(random_seed)
    means = array[rng.integers(0, len(array), size=(resamples, len(array)))].mean(axis=1)
    low, high = numpy.quantile(means, [0.025, 0.975])
    return {
        "mean_difference": float(array.mean()),
        "ci95_lower": float(low),
        "ci95_upper": float(high),
        "fold_count": int(len(array)),
        "resamples": resamples,
    }


class EstimatorAdapter:
    """Fold-scoped tree-classifier adapter used only by offline backtests."""

    def __init__(
        self, estimator: Any, *, class_order: tuple[RegimeLabel, ...],
        temperature: float, lookback: int, model_name: str, fingerprint: str,
    ) -> None:
        self.estimator = estimator
        self.class_order = class_order
        self.temperature = temperature
        self.lookback = lookback
        self.cache: dict[datetime, tuple[float, ...]] = {}
        self.model_fingerprint = fingerprint
        self.model_version = fingerprint[:16]
        self.model_id = f"evoryn.walk-forward-{model_name}"
        self.confidence_semantics = "calibrated_probability"
        self.is_ready = True

    def predict_probabilities(
        self, history: Sequence[MarketCandle]
    ) -> Mapping[RegimeLabel, Decimal]:
        numpy = importlib.import_module("numpy")
        vector = cached_research_feature(history, len(history) - 1, self.lookback, self.cache)
        raw = self.estimator.predict_proba(numpy.asarray([vector], dtype="float64"))[0]
        calibrated = _temperature_scale([float(v) for v in raw], self.temperature)
        return {label: Decimal(str(calibrated[i])) for i, label in enumerate(self.class_order)}


class HMMAdapter:
    """Forward-filtered HMM posterior and state-to-label mapping fitted on train labels only."""

    def __init__(
        self, model: Any, scaler: Any, state_map: Any, *, lookback: int,
        first_index: int, temperature: float, fingerprint: str,
    ) -> None:
        self.model, self.scaler, self.state_map = model, scaler, state_map
        self.lookback, self.first_index, self.temperature = lookback, first_index, temperature
        self.cache: dict[datetime, tuple[float, ...]] = {}
        self.model_fingerprint = fingerprint
        self.model_version = fingerprint[:16]
        self.model_id = "evoryn.walk-forward-gaussian-hmm"
        self.confidence_semantics = "calibrated_probability"
        self.is_ready = True

    def predict_probabilities(
        self, history: Sequence[MarketCandle]
    ) -> Mapping[RegimeLabel, Decimal]:
        numpy = importlib.import_module("numpy")
        rows = [
            cached_research_feature(history, i, self.lookback, self.cache)
            for i in range(self.first_index, len(history))
        ]
        scaled = self.scaler.transform(numpy.asarray(rows, dtype="float64"))
        from evoryn.intelligence.regime_walk_forward import (
            filtered_gaussian_hmm_probabilities,
        )

        hidden = filtered_gaussian_hmm_probabilities(self.model, scaled)
        raw = hidden[-1] @ self.state_map
        calibrated = _temperature_scale([float(v) for v in raw], self.temperature)
        return {label: Decimal(str(calibrated[i])) for i, label in enumerate(LABELS)}


class LSTMAdapter:
    """LSTM adapter using the latest twelve completed feature rows."""

    def __init__(
        self, model: Any, torch: Any, scaler: Any, *, lookback: int,
        sequence_length: int, temperature: float, fingerprint: str,
    ) -> None:
        self.model, self.torch, self.scaler = model, torch, scaler
        self.lookback, self.sequence_length = lookback, sequence_length
        self.temperature = temperature
        self.cache: dict[datetime, tuple[float, ...]] = {}
        self.model_fingerprint = fingerprint
        self.model_version = fingerprint[:16]
        self.model_id = "evoryn.walk-forward-lstm"
        self.confidence_semantics = "calibrated_probability"
        self.is_ready = True

    def predict_probabilities(
        self, history: Sequence[MarketCandle]
    ) -> Mapping[RegimeLabel, Decimal]:
        numpy = importlib.import_module("numpy")
        first = len(history) - self.sequence_length
        if first < self.lookback:
            raise ValueError("LSTM history does not cover its required feature window.")
        rows = [
            cached_research_feature(history, i, self.lookback, self.cache)
            for i in range(first, len(history))
        ]
        values = self.scaler.transform(numpy.asarray(rows, dtype="float64"))
        output = lstm_probabilities(
            self.model, self.torch, values.astype("float32", copy=False)[None, :, :], numpy
        )[0]
        calibrated = _temperature_scale([float(v) for v in output], self.temperature)
        return {label: Decimal(str(calibrated[i])) for i, label in enumerate(LABELS)}


class LongFlatResearchStrategy:
    """Map advisory probability output to a deterministic long/flat backtest policy."""

    def __init__(self, model: Any, confidence_threshold: float) -> None:
        self.model = model
        self.confidence_threshold = Decimal(str(confidence_threshold))
        self.strategy_id = str(model.model_id)
        self.strategy_version = str(model.model_version)

    def parameters(self) -> dict[str, str]:
        return {
            "model_id": self.strategy_id,
            "model_version": self.strategy_version,
            "confidence_threshold": str(self.confidence_threshold),
            "policy": "bull-regime-long-otherwise-flat",
        }

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        probabilities = self.model.predict_probabilities(history)
        winner = max(probabilities, key=probabilities.__getitem__)
        return (
            Decimal("1")
            if winner is RegimeLabel.BULL_TREND
            and probabilities[winner] >= self.confidence_threshold
            else Decimal("0")
        )
