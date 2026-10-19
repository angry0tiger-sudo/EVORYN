"""Synthetic, network-free tests for dataset provenance and Phase 13 backtesting."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from evoryn.domain.models import Market, MarketCandle, Side
from evoryn.learning_lab.backtest import (
    BacktestConfig,
    BacktestResultStore,
    chronological_split,
    evaluate_train_validation_test,
    evaluate_walk_forward,
    make_walk_forward_folds,
    run_backtest,
)
from evoryn.learning_lab.historical_data import (
    DatasetIntegrityError,
    HistoricalDataset,
    HistoricalDatasetStore,
    dataset_from_canonical_csv,
)
from evoryn.strategies.baseline import SmaCrossoverBaseline, SmaTrendBaseline

NOW = datetime(2026, 1, 1, tzinfo=UTC)
MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
INTERVAL = timedelta(hours=1)
SOURCE = "synthetic-phase13-fixture"


def dataset(
    count: int = 60,
    *,
    dataset_id: str = "synthetic-btc-hourly",
    market_type: str = "spot",
    closes: tuple[str, ...] | None = None,
    opens: tuple[str, ...] | None = None,
    volume: str = "100",
    volume_unit: str = "base_asset",
    volumes: tuple[str, ...] | None = None,
    gap_indices: tuple[int, ...] = (),
    allow_gaps: bool = False,
    source_revision: str = "synthetic-v1",
    license_id: str = "CC0-1.0",
) -> HistoricalDataset:
    """Create a deterministic fixture whose provenance explicitly declares synthetic data."""
    close_values = closes or tuple(str(Decimal("100") + Decimal(index)) for index in range(count))
    open_values = opens or tuple(str(Decimal(value) - Decimal("0.25")) for value in close_values)
    volume_values = volumes or tuple(volume for _ in range(count))
    if (
        len(close_values) != count
        or len(open_values) != count
        or len(volume_values) != count
    ):
        raise ValueError("Fixture OHLCV values must match count.")
    market = Market(symbol=MARKET.symbol, venue=MARKET.venue, market_type=market_type)
    candles: list[MarketCandle] = []
    gap_offset = 0
    for index in range(count):
        if index in gap_indices:
            gap_offset += 1
        open_price = Decimal(open_values[index])
        close_price = Decimal(close_values[index])
        high = max(open_price, close_price) + Decimal("1")
        low = min(open_price, close_price) - Decimal("1")
        candles.append(
            MarketCandle(
                market=market,
                timestamp=NOW + INTERVAL * (index + gap_offset),
                interval=INTERVAL,
                open=open_price,
                high=high,
                low=low,
                close=close_price,
                volume=Decimal(volume_values[index]),
                source=SOURCE,
            )
        )
    return HistoricalDataset(
        dataset_id=dataset_id,
        market=market,
        interval=INTERVAL,
        volume_unit=volume_unit,
        source_name=SOURCE,
        source_uri="urn:evoryn:synthetic-fixture:phase13:v1",
        source_revision=source_revision,
        license_id=license_id,
        license_uri="https://spdx.org/licenses/CC0-1.0.html",
        collected_at=NOW + INTERVAL * (count + gap_offset) + timedelta(minutes=1),
        candles=tuple(candles),
        allow_gaps=allow_gaps,
    )


def config(
    *,
    initial_cash: str = "1000",
    commission_bps: str = "10",
    spread_bps: str = "20",
    slippage_bps: str = "10",
    participation: str = "1",
    allocation: str = "0.9",
    quantity_step: str = "0.001",
    allow_gaps: bool = False,
) -> BacktestConfig:
    """Make every economic assumption explicit in the test fixture."""
    return BacktestConfig(
        initial_cash=Decimal(initial_cash),
        commission_bps=Decimal(commission_bps),
        spread_bps=Decimal(spread_bps),
        slippage_bps=Decimal(slippage_bps),
        max_bar_participation=Decimal(participation),
        allocation_fraction=Decimal(allocation),
        quantity_step=Decimal(quantity_step),
        allow_gaps=allow_gaps,
    )


class BuyThenFlat:
    """Test-only causal signal: buy after the first completed candle, then exit."""

    strategy_id = "test-buy-then-flat"
    strategy_version = "1.0.0"

    def parameters(self) -> dict[str, str]:
        return {"pattern": "one-completed-bar-long"}

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        return Decimal("1") if len(history) == 1 else Decimal("0")


class BuyAndHold:
    """Test-only causal signal that goes long after any completed candle."""

    strategy_id = "test-buy-and-hold"
    strategy_version = "1.0.0"

    def parameters(self) -> dict[str, str]:
        return {"pattern": "hold-long-after-first-close"}

    def target_exposure(self, history: Sequence[MarketCandle]) -> Decimal:
        return Decimal("1") if history else Decimal("0")


def test_dataset_version_covers_metadata_and_canonical_candle_content() -> None:
    original = dataset()
    alternate_revision = dataset(source_revision="synthetic-v2")
    alternate_license = dataset(license_id="OTHER-TEST-LICENSE")

    assert len(original.version) == 64
    assert original.version == dataset().version
    assert original.version != alternate_revision.version
    assert original.version != alternate_license.version
    assert original.gap_count == 0


def test_dataset_requires_source_revision_and_license_provenance() -> None:
    with pytest.raises(ValueError, match="license_id"):
        HistoricalDataset(
            dataset_id="bad-dataset",
            market=MARKET,
            interval=INTERVAL,
            volume_unit="base_asset",
            source_name=SOURCE,
            source_uri="urn:fixture",
            source_revision="rev1",
            license_id="",
            license_uri="https://example.invalid/license",
            collected_at=NOW,
            candles=(dataset(count=1).candles[0],),
        )


def test_dataset_rejects_out_of_order_duplicate_or_unrecorded_gaps() -> None:
    source = dataset(count=3)
    with pytest.raises(ValueError, match="strictly chronological"):
        HistoricalDataset(
            dataset_id="duplicate-candles",
            market=MARKET,
            interval=INTERVAL,
            volume_unit="base_asset",
            source_name=SOURCE,
            source_uri="urn:fixture",
            source_revision="rev1",
            license_id="CC0-1.0",
            license_uri="https://spdx.org/licenses/CC0-1.0.html",
            collected_at=NOW,
            candles=(source.candles[0], source.candles[0]),
        )

    with pytest.raises(ValueError, match="gap detected"):
        dataset(count=4, gap_indices=(2,), allow_gaps=False)

    documented_gaps = dataset(count=5, gap_indices=(2, 3), allow_gaps=True)
    assert documented_gaps.gap_count == 2


def test_canonical_csv_ingestion_requires_and_preserves_explicit_provenance(
    tmp_path: Path,
) -> None:
    original = dataset(count=5)
    source_export = tmp_path / "authorized-export.csv"
    source_export.write_bytes(original.candles_csv_bytes())

    imported = dataset_from_canonical_csv(
        source_export,
        dataset_id=original.dataset_id,
        market=original.market,
        interval=original.interval,
        volume_unit=original.volume_unit,
        source_name=original.source_name,
        source_uri=original.source_uri,
        source_revision=original.source_revision,
        license_id=original.license_id,
        license_uri=original.license_uri,
        collected_at=original.collected_at,
    )

    assert imported == original
    assert imported.version == original.version


def test_dataset_rejects_candles_not_closed_by_collection_time() -> None:
    source = dataset(count=3)

    with pytest.raises(ValueError, match="partial candles"):
        HistoricalDataset(
            dataset_id=source.dataset_id,
            market=source.market,
            interval=source.interval,
            volume_unit=source.volume_unit,
            source_name=source.source_name,
            source_uri=source.source_uri,
            source_revision=source.source_revision,
            license_id=source.license_id,
            license_uri=source.license_uri,
            collected_at=source.candles[-1].timestamp,
            candles=source.candles,
            allow_gaps=source.allow_gaps,
        )


def test_dataset_store_saves_loads_verifies_and_is_idempotent(tmp_path: Path) -> None:
    source = dataset()
    store = HistoricalDatasetStore(tmp_path)

    first = store.save(source)
    repeated = store.save(source)
    restored = store.load(first.dataset_id, first.version)

    assert first == repeated
    assert first.version == source.version
    assert first.candle_count == len(source.candles)
    assert first.candles_sha256 == repeated.candles_sha256
    assert restored == source


def test_dataset_store_refuses_tampered_candle_bytes(tmp_path: Path) -> None:
    source = dataset(count=5)
    store = HistoricalDatasetStore(tmp_path)
    reference = store.save(source)
    candle_path = tmp_path / source.dataset_id / source.version / "candles.csv"
    candle_path.write_bytes(candle_path.read_bytes() + b"# tampered\n")

    with pytest.raises(DatasetIntegrityError, match="SHA-256"):
        store.load(reference.dataset_id, reference.version)


def test_dataset_store_rejects_unsafe_ids_and_non_digest_versions(tmp_path: Path) -> None:
    store = HistoricalDatasetStore(tmp_path)
    with pytest.raises(ValueError, match="simple identifier"):
        store.load("../escape", "0" * 64)
    with pytest.raises(ValueError, match="SHA-256"):
        store.load("safe-id", "../../manifest")


def test_sma_baselines_are_deterministic_and_only_accept_supplied_history() -> None:
    candles = dataset(count=40).candles
    crossover = SmaCrossoverBaseline(fast_window=3, slow_window=6)
    trend = SmaTrendBaseline(window=5)

    assert crossover.target_exposure(candles[:5]) == Decimal("0")
    assert crossover.target_exposure(candles[:10]) == Decimal("1")
    assert trend.target_exposure(candles[:4]) == Decimal("0")
    assert trend.target_exposure(candles[:8]) == Decimal("1")
    same_prefix_with_different_future = tuple(candles[:10]) + tuple(
        reversed(candles[10:])
    )
    assert crossover.target_exposure(candles[:10]) == crossover.target_exposure(
        same_prefix_with_different_future[:10]
    )


def test_backtest_uses_completed_history_then_fills_only_at_next_open() -> None:
    bars = dataset(
        count=4,
        opens=("100", "102", "110", "108"),
        closes=("100", "105", "109", "108"),
    )
    result = run_backtest(bars, BuyThenFlat(), config(), segment_name="causality-check")

    assert len(result.signals) == 2
    assert result.signals[0].based_on_candle_open == bars.candles[0].timestamp
    assert result.signals[0].decision_at == bars.candles[0].timestamp + INTERVAL
    assert result.signals[0].first_eligible_at == bars.candles[1].timestamp
    assert len(result.fills) == 2
    assert result.fills[0].side is Side.BUY
    assert result.fills[1].side is Side.SELL
    assert result.fills[0].timestamp == bars.candles[1].timestamp
    assert result.fills[1].timestamp == bars.candles[2].timestamp
    assert all(fill.timestamp >= next(
        signal.first_eligible_at for signal in result.signals
        if signal.target_exposure == (Decimal("1") if fill.side is Side.BUY else Decimal("0"))
    ) for fill in result.fills)


def test_open_fill_capacity_never_uses_the_current_candle_full_volume() -> None:
    base = dataset(
        count=4,
        opens=("100", "102", "110", "108"),
        closes=("100", "105", "109", "108"),
        volumes=("100", "0", "100", "100"),
    )
    changed_current_volume = dataset(
        count=4,
        opens=("100", "102", "110", "108"),
        closes=("100", "105", "109", "108"),
        volumes=("100", "1000000", "100", "100"),
    )

    base_result = run_backtest(base, BuyAndHold(), config(participation="0.1"))
    changed_result = run_backtest(
        changed_current_volume, BuyAndHold(), config(participation="0.1")
    )

    assert base_result.fills[0].timestamp == base.candles[1].timestamp
    assert changed_result.fills[0].timestamp == changed_current_volume.candles[1].timestamp
    assert base_result.fills[0].liquidity_reference_volume == Decimal("100")
    assert changed_result.fills[0].liquidity_reference_volume == Decimal("100")
    assert base_result.fills[0].quantity == changed_result.fills[0].quantity


def test_backtest_accounts_for_commission_spread_slippage_and_closed_trade_metrics() -> None:
    bars = dataset(
        count=4,
        opens=("100", "102", "110", "108"),
        closes=("100", "105", "109", "108"),
    )
    costed = run_backtest(bars, BuyThenFlat(), config(), segment_name="costed")
    frictionless = run_backtest(
        bars,
        BuyThenFlat(),
        config(
            commission_bps="0",
            spread_bps="0",
            slippage_bps="0",
        ),
        segment_name="frictionless",
    )

    metrics = costed.metrics
    assert metrics.closed_trade_count == 1
    assert metrics.expectancy_per_closed_trade == costed.trades[0].net_pnl
    assert metrics.net_profit == metrics.final_equity - metrics.initial_equity
    assert metrics.final_equity < frictionless.metrics.final_equity
    assert metrics.commission_paid > 0
    assert metrics.estimated_spread_cost > 0
    assert metrics.estimated_slippage_cost > 0
    assert metrics.gross_turnover > 0
    assert metrics.turnover_multiple == metrics.gross_turnover / metrics.initial_equity
    assert metrics.max_drawdown_fraction >= 0
    assert metrics.fill_count == 2
    assert costed.trades[0].net_pnl == (
        costed.trades[0].gross_pnl - costed.trades[0].commission_paid
    )


def test_partial_fills_respect_per_bar_volume_participation_and_remain_open() -> None:
    bars = dataset(count=5, volume="1")
    result = run_backtest(
        bars,
        BuyAndHold(),
        config(participation="0.1"),
        segment_name="partial-liquidity",
    )

    assert result.metrics.fill_count > 0
    assert result.metrics.partial_fill_count > 0
    assert result.metrics.open_order_count_at_end == 1
    assert result.metrics.open_position_quantity > 0
    for fill in result.fills:
        candle_index = next(
            index for index, candle in enumerate(bars.candles)
            if candle.timestamp == fill.timestamp
        )
        assert candle_index > 0
        prior_candle = bars.candles[candle_index - 1]
        assert fill.liquidity_reference_candle_open == prior_candle.timestamp
        assert fill.liquidity_reference_volume == prior_candle.volume
        assert fill.quantity <= prior_candle.volume * Decimal("0.1")
        assert fill.volume_participation <= Decimal("0.1")


def test_open_position_is_marked_to_market_but_not_counted_as_closed_trade() -> None:
    bars = dataset(count=10)
    result = run_backtest(bars, BuyAndHold(), config(), segment_name="open-at-end")

    assert result.metrics.open_position_quantity > 0
    assert result.metrics.closed_trade_count == 0
    assert result.metrics.expectancy_per_closed_trade is None
    assert result.metrics.final_equity == (
        result.metrics.initial_equity + result.metrics.net_profit
    )


def test_temporal_partitions_are_disjoint_and_strictly_ordered() -> None:
    source = dataset(count=100)
    split = chronological_split(
        source,
        train_fraction=Decimal("0.6"),
        validation_fraction=Decimal("0.2"),
    )

    assert split.train == type(split.train)(0, 60)
    assert split.validation == type(split.validation)(60, 80)
    assert split.test == type(split.test)(80, 100)
    assert split.train.end == split.validation.start
    assert split.validation.end == split.test.start
    assert split.dataset_version == source.version


def test_train_validation_test_and_walk_forward_evaluations_are_version_bound() -> None:
    source = dataset(count=100)
    settings = config()
    strategy = SmaCrossoverBaseline(fast_window=3, slow_window=8)

    split = evaluate_train_validation_test(source, strategy, settings)
    folds = make_walk_forward_folds(
        source, train_bars=40, validation_bars=20, test_bars=10
    )
    walk_forward = evaluate_walk_forward(
        source, strategy, settings, train_bars=40, validation_bars=20, test_bars=10
    )

    assert split.train.dataset_version == source.version
    assert split.validation.dataset_version == source.version
    assert split.test.dataset_version == source.version
    assert len(folds) == 4
    assert len(walk_forward.folds) == len(folds)
    for index, fold_result in enumerate(walk_forward.folds):
        assert fold_result.fold.fold_index == index
        assert fold_result.fold.train.end == fold_result.fold.validation.start
        assert fold_result.fold.validation.end == fold_result.fold.test.start
        assert fold_result.test_result.evaluation_range == fold_result.fold.test
        assert fold_result.test_result.warmup_start_index == fold_result.fold.train.start
        assert fold_result.test_result.last_evaluation_close <= (
            source.candles[-1].timestamp + INTERVAL
        )


def test_walk_forward_rejects_overlapping_test_windows_and_short_data() -> None:
    source = dataset(count=50)
    with pytest.raises(ValueError, match="must be >="):
        make_walk_forward_folds(
            source, train_bars=20, validation_bars=10, test_bars=10, step_bars=5
        )
    with pytest.raises(ValueError, match="too short"):
        make_walk_forward_folds(
            dataset(count=20), train_bars=10, validation_bars=8, test_bars=5
        )


def test_backtest_rejects_non_spot_dataset_for_spot_only_accounting() -> None:
    margin_dataset = dataset(count=10, market_type="margin")
    with pytest.raises(ValueError, match="supports Spot datasets only"):
        run_backtest(
            margin_dataset,
            SmaTrendBaseline(window=2),
            config(),
            segment_name="margin-must-use-separate-model",
        )


def test_backtest_rejects_quote_volume_when_fill_cap_requires_base_volume() -> None:
    quote_volume_dataset = dataset(count=10, volume_unit="quote_asset")
    with pytest.raises(ValueError, match="require base-asset volume"):
        run_backtest(
            quote_volume_dataset,
            SmaTrendBaseline(window=2),
            config(),
            segment_name="invalid-volume-unit",
        )


def test_gapped_dataset_requires_explicit_backtest_opt_in() -> None:
    gapped = dataset(count=6, gap_indices=(3,), allow_gaps=True)
    strategy = SmaTrendBaseline(window=2)

    with pytest.raises(ValueError, match="allow_gaps"):
        run_backtest(gapped, strategy, config(), segment_name="must-refuse")
    allowed = run_backtest(
        gapped, strategy, config(allow_gaps=True), segment_name="explicit-gap-policy"
    )
    assert allowed.dataset_version == gapped.version


def test_backtest_runs_and_json_reports_are_reproducible_and_immutable(tmp_path: Path) -> None:
    source = dataset(count=12)
    strategy = SmaTrendBaseline(window=3)
    first = run_backtest(source, strategy, config(), segment_name="reproducibility")
    repeated = run_backtest(source, strategy, config(), segment_name="reproducibility")

    assert first.run_id == repeated.run_id
    assert first.to_json_bytes() == repeated.to_json_bytes()
    store = BacktestResultStore(tmp_path)
    with pytest.raises(ValueError, match="SHA-256"):
        store.save(replace(first, run_id="../unsafe"))
    mismatched_id = "0" * 64
    with pytest.raises(ValueError, match="does not match"):
        store.save(replace(first, run_id=mismatched_id))
    assert not (tmp_path / f"{mismatched_id}.json").exists()
    path = store.save(first)
    assert store.save(repeated) == path
    assert path.read_bytes() == first.to_json_bytes()
    stored_report = store.load(first.run_id)
    assert stored_report["run_id"] == first.run_id
    assert stored_report["dataset_version"] == source.version
    assert stored_report["dataset_provenance"]["license_id"] == source.license_id
    assert stored_report["dataset_provenance"]["source_revision"] == source.source_revision
    expected_collected_at = (
        source.collected_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    assert stored_report["dataset_provenance"]["collected_at"] == expected_collected_at
    assert (tmp_path / f"{first.run_id}.json.sha256").exists()

    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="checksum"):
        store.load(first.run_id)


def test_config_requires_explicit_valid_cost_and_fill_assumptions() -> None:
    with pytest.raises(ValueError, match="commission_bps"):
        config(commission_bps="-1")
    with pytest.raises(ValueError, match="max_bar_participation"):
        config(participation="0")
    with pytest.raises(ValueError, match="quantity_step"):
        config(quantity_step="0")
    with pytest.raises(ValueError, match="Combined adverse"):
        config(spread_bps="15000", slippage_bps="10000")


def test_sma_strategies_reject_invalid_windows() -> None:
    with pytest.raises(ValueError, match="fast_window"):
        SmaCrossoverBaseline(fast_window=10, slow_window=5)
    with pytest.raises(ValueError, match="positive"):
        SmaTrendBaseline(window=0)
