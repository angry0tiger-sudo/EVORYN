"""Evidence-backed market capability and derivative instrument domain contracts.

These types describe declared capability and observed facts only. They do not create
network access, authorize orders, or calculate an exchange's liquidation formula.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from urllib.parse import urlsplit

_VENUE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")


class MarketType(StrEnum):
    """Distinct market semantics; one market type never implies another."""

    SPOT = "spot"
    MARGIN = "margin"
    FUTURES = "futures"


class CapabilityStatus(StrEnum):
    """Evidence state for one venue/market/feature tuple."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    RESTRICTED = "restricted"


class CapabilityFeature(StrEnum):
    """Provider-independent capabilities which require separate evidence."""

    MARKET_DATA_READ = "market_data_read"
    ACCOUNT_READ = "account_read"
    ORDER_STATUS_READ = "order_status_read"
    ORDER_SUBMIT = "order_submit"
    ORDER_CANCEL = "order_cancel"
    POSITION_READ = "position_read"
    PRICE_TICK_RULES = "price_tick_rules"
    QUANTITY_RULES = "quantity_rules"
    CONTRACT_SIZING = "contract_sizing"
    FUNDING_DATA_READ = "funding_data_read"
    INITIAL_MARGIN_DATA_READ = "initial_margin_data_read"
    MAINTENANCE_MARGIN_DATA_READ = "maintenance_margin_data_read"
    LIQUIDATION_DATA_READ = "liquidation_data_read"
    POSITION_MODE_READ = "position_mode_read"


class FuturesContractKind(StrEnum):
    """Only contract forms that have an explicit representation in this schema."""

    LINEAR = "linear"
    INVERSE = "inverse"


class FuturesSettlementType(StrEnum):
    """Settlement lifecycle of a derivative instrument."""

    PERPETUAL = "perpetual"
    EXPIRING = "expiring"


class FuturesPositionMode(StrEnum):
    """Explicitly observed position accounting mode."""

    ONE_WAY = "one_way"
    HEDGE = "hedge"


class FuturesMarginMode(StrEnum):
    """Explicitly observed margin mode; no default is inferred."""

    ISOLATED = "isolated"
    CROSS = "cross"


def _require_aware(value: datetime, *, field_name: str) -> None:
    """Require timezone-aware timestamps before they can become evidence."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone.")


def _require_https_url(value: str, *, field_name: str = "evidence_url") -> None:
    """Accept only explicit HTTPS source references for evidence-bearing records."""
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{field_name} must be an absolute HTTPS URL.")


def _require_venue_id(value: str) -> None:
    """Keep provider identity a stable identifier, not a path or import expression."""
    if not _VENUE_ID.fullmatch(value):
        raise ValueError("Venue ID must be a lowercase stable identifier.")


def _require_positive(value: Decimal, *, field_name: str) -> None:
    """Require finite positive decimal values for price and sizing rules."""
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{field_name} must be finite and positive.")


def _require_non_negative(value: Decimal, *, field_name: str) -> None:
    """Require finite non-negative decimal values for margins and thresholds."""
    if not value.is_finite() or value < 0:
        raise ValueError(f"{field_name} must be finite and non-negative.")


class CapabilityNotSupportedError(RuntimeError):
    """Raised whenever a capability is not affirmatively supported by evidence."""

    def __init__(
        self,
        venue: str,
        market_type: MarketType,
        feature: CapabilityFeature,
        status: CapabilityStatus,
    ) -> None:
        self.venue = venue
        self.market_type = market_type
        self.feature = feature
        self.status = status
        super().__init__(
            f"Capability {venue}/{market_type.value}/{feature.value} is {status.value}."
        )


@dataclass(frozen=True, slots=True)
class VenueCapability:
    """Evidence for exactly one venue, market type, and capability.

    SUPPORTED and UNSUPPORTED are evidence claims and require a source plus an
    observed timestamp. RESTRICTED records need an explicit policy rationale.
    UNKNOWN is the safe default when evidence is incomplete.
    """

    venue: str
    market_type: MarketType
    feature: CapabilityFeature
    status: CapabilityStatus
    evidence_url: str | None = None
    observed_at: datetime | None = None
    rationale: str | None = None

    def __post_init__(self) -> None:
        """Reject ambiguous identifiers and unsupported evidence claims."""
        _require_venue_id(self.venue)
        if self.evidence_url is not None:
            _require_https_url(self.evidence_url)
        if self.observed_at is not None:
            _require_aware(self.observed_at, field_name="observed_at")
        if self.status in {CapabilityStatus.SUPPORTED, CapabilityStatus.UNSUPPORTED} and (
            self.evidence_url is None or self.observed_at is None
        ):
            raise ValueError(
                "Supported/unsupported claims require HTTPS evidence and observed_at."
            )
        if self.status is CapabilityStatus.RESTRICTED and not (
            self.rationale and self.rationale.strip()
        ):
            raise ValueError("Restricted capability requires a policy rationale.")


@dataclass(frozen=True, slots=True)
class CapabilityMatrix:
    """Immutable capability evidence set; omitted entries resolve to UNKNOWN."""

    entries: tuple[VenueCapability, ...]

    def __post_init__(self) -> None:
        """Prevent duplicate keys from silently shadowing an existing decision."""
        keys = [
            (item.venue, item.market_type, item.feature)
            for item in self.entries
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("Capability matrix contains duplicate venue/market/feature keys.")

    def lookup(
        self,
        venue: str,
        market_type: MarketType,
        feature: CapabilityFeature,
    ) -> VenueCapability | None:
        """Return the exact evidence row; never infer from another market or feature."""
        normalized_venue = venue.casefold()
        _require_venue_id(normalized_venue)
        return next(
            (
                item
                for item in self.entries
                if item.venue == normalized_venue
                and item.market_type is market_type
                and item.feature is feature
            ),
            None,
        )

    def status_for(
        self,
        venue: str,
        market_type: MarketType,
        feature: CapabilityFeature,
    ) -> CapabilityStatus:
        """Return UNKNOWN for missing evidence; absence never means supported."""
        item = self.lookup(venue, market_type, feature)
        return CapabilityStatus.UNKNOWN if item is None else item.status

    def require_supported(
        self,
        venue: str,
        market_type: MarketType,
        feature: CapabilityFeature,
        *,
        now: datetime,
        max_age: timedelta,
    ) -> VenueCapability:
        """Require affirmative, fresh evidence for the exact capability.

        Callers must supply the current time and an explicit freshness policy.
        Stale or future-dated evidence is reported as UNKNOWN and cannot pass.
        """
        _require_aware(now, field_name="now")
        if max_age <= timedelta(0):
            raise ValueError("max_age must be positive.")

        item = self.lookup(venue, market_type, feature)
        if item is None or item.status is not CapabilityStatus.SUPPORTED:
            status = CapabilityStatus.UNKNOWN if item is None else item.status
            raise CapabilityNotSupportedError(
                venue.casefold(), market_type, feature, status
            )

        if item.observed_at is None:
            raise CapabilityNotSupportedError(
                venue.casefold(), market_type, feature, CapabilityStatus.UNKNOWN
            )
        age = now - item.observed_at
        if age < timedelta(0) or age > max_age:
            raise CapabilityNotSupportedError(
                venue.casefold(), market_type, feature, CapabilityStatus.UNKNOWN
            )
        return item


@dataclass(frozen=True, slots=True)
class InstrumentIdentity:
    """Canonical identity plus provider symbol and source provenance.

    For Futures, canonical_symbol must distinguish contract and settlement semantics
    from Spot/Margin instruments. venue_symbol preserves the exact provider symbol.
    """

    venue: str
    market_type: MarketType
    canonical_symbol: str
    venue_symbol: str
    base_asset: str
    quote_asset: str
    observed_at: datetime
    evidence_url: str
    settlement_asset: str | None = None
    contract_kind: FuturesContractKind | None = None
    settlement_type: FuturesSettlementType | None = None
    expiry_at: datetime | None = None
    contract_size: Decimal | None = None

    def __post_init__(self) -> None:
        """Require source-backed identity and explicit Futures contract semantics."""
        _require_venue_id(self.venue)
        for field_name in ("canonical_symbol", "venue_symbol", "base_asset", "quote_asset"):
            value = getattr(self, field_name)
            if not value or not value.strip():
                raise ValueError(f"{field_name} must not be empty.")
        if self.canonical_symbol != self.canonical_symbol.upper():
            raise ValueError("canonical_symbol must use uppercase canonical notation.")
        for field_name in ("base_asset", "quote_asset", "settlement_asset"):
            value = getattr(self, field_name)
            if value is not None and value != value.upper():
                raise ValueError(f"{field_name} must be uppercase.")
        _require_aware(self.observed_at, field_name="observed_at")
        _require_https_url(self.evidence_url)
        if self.expiry_at is not None:
            _require_aware(self.expiry_at, field_name="expiry_at")

        futures_fields = (
            self.settlement_asset,
            self.contract_kind,
            self.settlement_type,
            self.contract_size,
        )
        if self.market_type is MarketType.FUTURES:
            if any(value is None for value in futures_fields):
                raise ValueError(
                    "Futures identity requires settlement asset, contract kind, "
                    "settlement type, and contract size."
                )
            if self.contract_size is None:
                raise ValueError("Futures contract_size must not be missing.")
            _require_positive(self.contract_size, field_name="contract_size")
            if self.settlement_type is FuturesSettlementType.EXPIRING:
                if self.expiry_at is None:
                    raise ValueError("Expiring Futures require an explicit expiry_at.")
            elif self.expiry_at is not None:
                raise ValueError("Perpetual Futures must not declare expiry_at.")
            if self.settlement_asset is None:
                raise ValueError("Futures require a settlement asset.")
        elif any(value is not None for value in (*futures_fields, self.expiry_at)):
            raise ValueError("Futures-only fields must not be attached to Spot or Margin.")

    @property
    def identity_key(self) -> str:
        """Return a stable venue-scoped identity without trusting symbol aliases."""
        return f"{self.venue}:{self.market_type.value}:{self.canonical_symbol}:{self.venue_symbol}"


@dataclass(frozen=True, slots=True)
class InstrumentTradingRules:
    """Evidence-backed sizing constraints; missing minimum notional remains unknown."""

    instrument: InstrumentIdentity
    price_tick: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal | None
    observed_at: datetime
    evidence_url: str

    def __post_init__(self) -> None:
        """Validate decimal precision rules without rounding or float coercion."""
        _require_positive(self.price_tick, field_name="price_tick")
        _require_positive(self.quantity_step, field_name="quantity_step")
        _require_positive(self.minimum_quantity, field_name="minimum_quantity")
        if self.minimum_notional is not None:
            _require_non_negative(self.minimum_notional, field_name="minimum_notional")
        _require_aware(self.observed_at, field_name="observed_at")
        _require_https_url(self.evidence_url)

    @property
    def is_complete_for_order_validation(self) -> bool:
        """Do not treat an undocumented minimum-notional rule as no restriction."""
        return self.minimum_notional is not None


@dataclass(frozen=True, slots=True)
class FuturesAccountModeSnapshot:
    """Read-only account mode observation; it grants no permission to change modes."""

    instrument: InstrumentIdentity
    position_mode: FuturesPositionMode | None
    margin_mode: FuturesMarginMode | None
    observed_at: datetime
    evidence_url: str

    def __post_init__(self) -> None:
        """Prevent Spot or Margin identity from being used as Futures account state."""
        if self.instrument.market_type is not MarketType.FUTURES:
            raise ValueError("Futures account mode requires a Futures instrument.")
        _require_aware(self.observed_at, field_name="observed_at")
        _require_https_url(self.evidence_url)

    @property
    def is_complete(self) -> bool:
        """Missing position or margin mode is an unresolved risk input."""
        return self.position_mode is not None and self.margin_mode is not None


@dataclass(frozen=True, slots=True)
class FundingObservation:
    """Signed funding observation with source and event timestamps."""

    instrument: InstrumentIdentity
    funding_rate: Decimal
    funding_at: datetime
    observed_at: datetime
    evidence_url: str
    next_funding_at: datetime | None = None

    def __post_init__(self) -> None:
        """Keep funding rates signed and require timezone-aware provenance."""
        if self.instrument.market_type is not MarketType.FUTURES:
            raise ValueError("Funding observations require a Futures instrument.")
        if not self.funding_rate.is_finite():
            raise ValueError("funding_rate must be finite.")
        _require_aware(self.funding_at, field_name="funding_at")
        _require_aware(self.observed_at, field_name="observed_at")
        if self.next_funding_at is not None:
            _require_aware(self.next_funding_at, field_name="next_funding_at")
        _require_https_url(self.evidence_url)

    @property
    def is_complete_for_forward_cost_estimate(self) -> bool:
        """A past/last funding rate alone does not establish the next charge time."""
        return self.next_funding_at is not None


@dataclass(frozen=True, slots=True)
class FuturesRiskSnapshot:
    """Observed Futures risk inputs, not a risk decision or liquidation formula."""

    instrument: InstrumentIdentity
    signed_contract_quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    initial_margin: Decimal
    maintenance_margin: Decimal
    funding_accrued: Decimal
    position_mode: FuturesPositionMode | None
    margin_mode: FuturesMarginMode | None
    liquidation_price: Decimal | None
    observed_at: datetime
    evidence_url: str

    def __post_init__(self) -> None:
        """Reject invalid observations; never manufacture absent venue risk fields."""
        if self.instrument.market_type is not MarketType.FUTURES:
            raise ValueError("Futures risk snapshot requires a Futures instrument.")
        if not self.signed_contract_quantity.is_finite() or self.signed_contract_quantity == 0:
            raise ValueError("signed_contract_quantity must be finite and non-zero.")
        _require_positive(self.entry_price, field_name="entry_price")
        _require_positive(self.mark_price, field_name="mark_price")
        _require_non_negative(self.initial_margin, field_name="initial_margin")
        _require_non_negative(self.maintenance_margin, field_name="maintenance_margin")
        if not self.funding_accrued.is_finite():
            raise ValueError("funding_accrued must be finite.")
        if self.liquidation_price is not None:
            _require_positive(self.liquidation_price, field_name="liquidation_price")
        _require_aware(self.observed_at, field_name="observed_at")
        _require_https_url(self.evidence_url)

    @property
    def has_minimum_risk_inputs(self) -> bool:
        """Incomplete snapshots must abstain; True still does not authorize a trade."""
        return (
            self.liquidation_price is not None
            and self.position_mode is not None
            and self.margin_mode is not None
        )
