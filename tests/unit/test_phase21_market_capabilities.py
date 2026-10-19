"""Unit tests for Phase 21's evidence-backed market and Futures domain contracts."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from evoryn.domain.market_capabilities import (
    CapabilityFeature,
    CapabilityMatrix,
    CapabilityNotSupportedError,
    CapabilityStatus,
    FundingObservation,
    FuturesAccountModeSnapshot,
    FuturesContractKind,
    FuturesMarginMode,
    FuturesPositionMode,
    FuturesRiskSnapshot,
    FuturesSettlementType,
    InstrumentIdentity,
    InstrumentTradingRules,
    MarketType,
    VenueCapability,
)

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
OFFICIAL_SOURCE = "https://example.org/official-api-docs"


def capability(
    status: CapabilityStatus = CapabilityStatus.SUPPORTED,
    *,
    feature: CapabilityFeature = CapabilityFeature.MARKET_DATA_READ,
    market_type: MarketType = MarketType.SPOT,
) -> VenueCapability:
    """Build a source-backed capability row for deterministic tests."""
    return VenueCapability(
        venue="fixture",
        market_type=market_type,
        feature=feature,
        status=status,
        evidence_url=OFFICIAL_SOURCE,
        observed_at=NOW,
        rationale="Explicit test policy." if status is CapabilityStatus.RESTRICTED else None,
    )


def spot_instrument() -> InstrumentIdentity:
    """Return a source-backed Spot instrument identity."""
    return InstrumentIdentity(
        venue="fixture",
        market_type=MarketType.SPOT,
        canonical_symbol="BTC/USDT",
        venue_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        observed_at=NOW,
        evidence_url=OFFICIAL_SOURCE,
    )


def futures_instrument(
    settlement_type: FuturesSettlementType = FuturesSettlementType.PERPETUAL,
) -> InstrumentIdentity:
    """Return a fully identified futures fixture without claiming venue support."""
    expiry = NOW + timedelta(days=30) if settlement_type is FuturesSettlementType.EXPIRING else None
    canonical = "BTC/USDT:USDT-PERP" if expiry is None else "BTC/USDT:USDT-20261118"
    return InstrumentIdentity(
        venue="fixture",
        market_type=MarketType.FUTURES,
        canonical_symbol=canonical,
        venue_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        settlement_asset="USDT",
        contract_kind=FuturesContractKind.LINEAR,
        settlement_type=settlement_type,
        expiry_at=expiry,
        contract_size=Decimal("0.001"),
        observed_at=NOW,
        evidence_url=OFFICIAL_SOURCE,
    )


def test_missing_market_capability_defaults_to_unknown() -> None:
    matrix = CapabilityMatrix(entries=())

    assert (
        matrix.status_for(
            "nobitex", MarketType.FUTURES, CapabilityFeature.MARKET_DATA_READ
        )
        is CapabilityStatus.UNKNOWN
    )


def test_supported_capability_requires_evidence_and_timezone() -> None:
    with pytest.raises(ValueError, match="require HTTPS evidence"):
        VenueCapability(
            venue="fixture",
            market_type=MarketType.SPOT,
            feature=CapabilityFeature.MARKET_DATA_READ,
            status=CapabilityStatus.SUPPORTED,
        )

    with pytest.raises(ValueError, match="include a timezone"):
        VenueCapability(
            venue="fixture",
            market_type=MarketType.SPOT,
            feature=CapabilityFeature.MARKET_DATA_READ,
            status=CapabilityStatus.SUPPORTED,
            evidence_url=OFFICIAL_SOURCE,
            observed_at=datetime(2026, 10, 19, 12),
        )


@pytest.mark.parametrize(
    "status",
    [
        CapabilityStatus.UNKNOWN,
        CapabilityStatus.UNSUPPORTED,
        CapabilityStatus.RESTRICTED,
    ],
)
def test_non_supported_capabilities_never_pass_gate(status: CapabilityStatus) -> None:
    row = capability(status)
    matrix = CapabilityMatrix(entries=(row,))

    with pytest.raises(CapabilityNotSupportedError) as error:
        matrix.require_supported("FIXTURE", MarketType.SPOT, CapabilityFeature.MARKET_DATA_READ)

    assert error.value.status is status


def test_supported_capability_is_scoped_to_exact_market_and_feature() -> None:
    matrix = CapabilityMatrix(entries=(capability(),))

    assert matrix.require_supported(
        "fixture", MarketType.SPOT, CapabilityFeature.MARKET_DATA_READ
    ).status is CapabilityStatus.SUPPORTED
    assert (
        matrix.status_for(
            "fixture", MarketType.MARGIN, CapabilityFeature.MARKET_DATA_READ
        )
        is CapabilityStatus.UNKNOWN
    )
    assert (
        matrix.status_for("fixture", MarketType.SPOT, CapabilityFeature.ORDER_SUBMIT)
        is CapabilityStatus.UNKNOWN
    )


def test_duplicate_capability_keys_are_rejected() -> None:
    row = capability()
    with pytest.raises(ValueError, match="duplicate"):
        CapabilityMatrix(entries=(row, row))


def test_restricted_capability_requires_a_rationale() -> None:
    with pytest.raises(ValueError, match="policy rationale"):
        VenueCapability(
            venue="fixture",
            market_type=MarketType.SPOT,
            feature=CapabilityFeature.ORDER_SUBMIT,
            status=CapabilityStatus.RESTRICTED,
        )


def test_spot_identity_cannot_carry_futures_fields() -> None:
    with pytest.raises(ValueError, match="Futures-only fields"):
        InstrumentIdentity(
            venue="fixture",
            market_type=MarketType.SPOT,
            canonical_symbol="BTC/USDT",
            venue_symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            observed_at=NOW,
            evidence_url=OFFICIAL_SOURCE,
            settlement_asset="USDT",
        )


def test_futures_identity_requires_explicit_contract_semantics() -> None:
    with pytest.raises(ValueError, match="requires settlement asset"):
        InstrumentIdentity(
            venue="fixture",
            market_type=MarketType.FUTURES,
            canonical_symbol="BTC/USDT:USDT-PERP",
            venue_symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            observed_at=NOW,
            evidence_url=OFFICIAL_SOURCE,
        )


def test_expiring_futures_require_explicit_expiry() -> None:
    with pytest.raises(ValueError, match="explicit expiry_at"):
        InstrumentIdentity(
            venue="fixture",
            market_type=MarketType.FUTURES,
            canonical_symbol="BTC/USDT:USDT-EXPIRY",
            venue_symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            settlement_asset="USDT",
            contract_kind=FuturesContractKind.LINEAR,
            settlement_type=FuturesSettlementType.EXPIRING,
            contract_size=Decimal("1"),
            observed_at=NOW,
            evidence_url=OFFICIAL_SOURCE,
        )


def test_unknown_minimum_notional_does_not_complete_order_rules() -> None:
    instrument = spot_instrument()
    unknown_rules = InstrumentTradingRules(
        instrument=instrument,
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("0.0001"),
        minimum_quantity=Decimal("0.0001"),
        minimum_notional=None,
        observed_at=NOW,
        evidence_url=OFFICIAL_SOURCE,
    )
    explicit_no_minimum = InstrumentTradingRules(
        instrument=instrument,
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("0.0001"),
        minimum_quantity=Decimal("0.0001"),
        minimum_notional=Decimal("0"),
        observed_at=NOW,
        evidence_url=OFFICIAL_SOURCE,
    )

    assert not unknown_rules.is_complete_for_order_validation
    assert explicit_no_minimum.is_complete_for_order_validation


def test_invalid_precision_rule_is_rejected() -> None:
    with pytest.raises(ValueError, match="price_tick must be finite and positive"):
        InstrumentTradingRules(
            instrument=spot_instrument(),
            price_tick=Decimal("0"),
            quantity_step=Decimal("0.001"),
            minimum_quantity=Decimal("0.001"),
            minimum_notional=Decimal("5"),
            observed_at=NOW,
            evidence_url=OFFICIAL_SOURCE,
        )


def test_futures_account_modes_remain_incomplete_until_both_are_observed() -> None:
    snapshot = FuturesAccountModeSnapshot(
        instrument=futures_instrument(),
        position_mode=None,
        margin_mode=FuturesMarginMode.ISOLATED,
        observed_at=NOW,
        evidence_url=OFFICIAL_SOURCE,
    )

    assert not snapshot.is_complete


def test_negative_funding_rate_is_valid_and_next_time_is_separate_evidence() -> None:
    observation = FundingObservation(
        instrument=futures_instrument(),
        funding_rate=Decimal("-0.0005"),
        funding_at=NOW,
        observed_at=NOW,
        next_funding_at=NOW + timedelta(hours=8),
        evidence_url=OFFICIAL_SOURCE,
    )

    assert observation.funding_rate < 0
    assert observation.is_complete_for_forward_cost_estimate


def test_futures_risk_snapshot_abstains_when_liquidation_or_modes_are_unknown() -> None:
    incomplete = FuturesRiskSnapshot(
        instrument=futures_instrument(),
        signed_contract_quantity=Decimal("2"),
        entry_price=Decimal("64000"),
        mark_price=Decimal("63000"),
        initial_margin=Decimal("500"),
        maintenance_margin=Decimal("100"),
        funding_accrued=Decimal("0"),
        position_mode=FuturesPositionMode.ONE_WAY,
        margin_mode=None,
        liquidation_price=None,
        observed_at=NOW,
        evidence_url=OFFICIAL_SOURCE,
    )

    assert not incomplete.has_minimum_risk_inputs


def test_futures_risk_snapshot_rejects_zero_position_and_non_finite_prices() -> None:
    with pytest.raises(ValueError, match="finite and non-zero"):
        FuturesRiskSnapshot(
            instrument=futures_instrument(),
            signed_contract_quantity=Decimal("0"),
            entry_price=Decimal("64000"),
            mark_price=Decimal("63000"),
            initial_margin=Decimal("500"),
            maintenance_margin=Decimal("100"),
            funding_accrued=Decimal("0"),
            position_mode=FuturesPositionMode.ONE_WAY,
            margin_mode=FuturesMarginMode.ISOLATED,
            liquidation_price=Decimal("58000"),
            observed_at=NOW,
            evidence_url=OFFICIAL_SOURCE,
        )
