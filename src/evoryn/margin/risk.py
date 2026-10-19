"""Independent deterministic risk checks for Nobitex delegated Margin."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from evoryn.config import MarketPolicy
from evoryn.margin.models import (
    DelegationLimitSnapshot,
    MarginHealth,
    MarginLiquidityPoolSnapshot,
    MarginLiquidityPoolStatus,
    MarginMarketSettings,
    MarginMode,
    MarginOpenIntent,
    MarginOrderDisposition,
    MarginOrderRiskResult,
    MarginPosition,
    MarginPositionStatus,
    MarginReconciliationResult,
    MarginRiskAssessment,
)


class MarginCalculator:
    """Pure Margin estimates; results are not treated as venue-authoritative values."""

    @staticmethod
    def estimate_initial_collateral(notional: Decimal, leverage: Decimal) -> Decimal:
        """Return the generic planning estimate notional/leverage.

        Nobitex's actual collateral and liability semantics are venue-reported. This
        estimate is for scenario planning only and must not replace the API collateral.
        """
        _positive(notional, "notional")
        _positive(leverage, "leverage")
        if leverage < Decimal("1"):
            raise ValueError("leverage must be at least 1.")
        return notional / leverage

    @staticmethod
    def estimate_extension_fee(
        delegated_amount: Decimal,
        mark_price: Decimal,
        daily_position_fee_rate: Decimal,
        elapsed_days: Decimal,
    ) -> Decimal:
        """Estimate holding/renewal cost for scenario analysis only.

        Formula: delegated amount * mark price * observed daily rate * elapsed days.
        Nobitex documents tiered daily renewal fees; actual extensionFee from the
        Position response is authoritative. No rounding, tier calculation, or hidden
        fee assumptions are invented here.
        """
        _nonnegative(delegated_amount, "delegated_amount")
        _positive(mark_price, "mark_price")
        _nonnegative(daily_position_fee_rate, "daily_position_fee_rate")
        _nonnegative(elapsed_days, "elapsed_days")
        return delegated_amount * mark_price * daily_position_fee_rate * elapsed_days

    @staticmethod
    def liability_value_in_quote(liability: Decimal, mark_price: Decimal) -> Decimal:
        """Value a source-currency liability in the destination currency."""
        _nonnegative(liability, "liability")
        _positive(mark_price, "mark_price")
        return liability * mark_price


class MarginRiskPolicy:
    """Fail-closed policy for opening additional Margin risk and assessing positions.

    Nobitex's documented initial commitment ratio is 2, the minimum safe ratio is 1.1,
    and current documented mode is Isolated Margin; Cross Margin is described as future.
    This policy does not derive a liquidation price formula. It evaluates the reported
    venue liquidationPrice as a threshold and labels missing/stale data UNKNOWN.
    """

    def __init__(
        self,
        *,
        initial_margin_ratio: Decimal = Decimal("2"),
        maintenance_margin_ratio: Decimal = Decimal("1.1"),
        max_data_age_seconds: int = 30,
        cross_margin_enabled: bool = False,
        market_policy: MarketPolicy | None = None,
    ) -> None:
        _positive(initial_margin_ratio, "initial_margin_ratio")
        _positive(maintenance_margin_ratio, "maintenance_margin_ratio")
        if initial_margin_ratio <= maintenance_margin_ratio:
            raise ValueError("Initial margin ratio must exceed maintenance margin ratio.")
        if max_data_age_seconds <= 0:
            raise ValueError("max_data_age_seconds must be positive.")
        if type(cross_margin_enabled) is not bool:
            raise ValueError("cross_margin_enabled must be an explicit boolean.")
        self.initial_margin_ratio = initial_margin_ratio
        self.maintenance_margin_ratio = maintenance_margin_ratio
        self.max_data_age_seconds = max_data_age_seconds
        self.cross_margin_enabled = cross_margin_enabled
        self.market_policy = market_policy or MarketPolicy.from_environment()

    def evaluate_open_intent(
        self,
        intent: MarginOpenIntent,
        settings: MarginMarketSettings,
        delegation_limits: DelegationLimitSnapshot,
        liquidity_pool: MarginLiquidityPoolSnapshot,
        *,
        now: datetime,
    ) -> MarginOrderRiskResult:
        """Validate an opening intent against venue settings and side-specific limits."""
        if not _is_aware(now):
            return _order_result(
                MarginOrderDisposition.HALT,
                "invalid_evaluation_time",
                "Margin evaluation time must be timezone-aware.",
            )
        intent_quote = self.market_policy.quote_currency_for_symbol(intent.market_symbol)
        if (
            settings.dst_currency.strip().upper() not in self.market_policy.allowed_quote_currencies
            or intent_quote != settings.dst_currency.strip().upper()
            or intent_quote not in self.market_policy.allowed_quote_currencies
        ):
            return _order_result(
                MarginOrderDisposition.REJECT,
                "quote_currency_not_allowed",
                "Only the configured USDT quote currency is permitted for Margin risk.",
            )
        if (
            intent.market_symbol.casefold() != settings.market_symbol.casefold()
            or delegation_limits.market_symbol.casefold()
            != settings.market_symbol.casefold()
        ):
            return _order_result(
                MarginOrderDisposition.REJECT,
                "margin_market_mismatch",
                "Intent, market settings, and delegation limits must identify the same market.",
            )
        if (
            not _is_aware(intent.observed_at)
            or not _is_aware(settings.observed_at)
            or not _is_aware(delegation_limits.observed_at)
            or not _is_aware(liquidity_pool.observed_at)
        ):
            return _order_result(
                MarginOrderDisposition.HALT,
                "invalid_margin_timestamp",
                "Intent, market settings, and delegation limits require aware timestamps.",
            )
        intent_age = now - intent.observed_at
        settings_age = now - settings.observed_at
        limits_age = now - delegation_limits.observed_at
        pool_age = now - liquidity_pool.observed_at
        if (
            intent_age < timedelta(0)
            or settings_age < timedelta(0)
            or limits_age < timedelta(0)
            or pool_age < timedelta(0)
            or intent_age.total_seconds() > self.max_data_age_seconds
            or settings_age.total_seconds() > self.max_data_age_seconds
            or limits_age.total_seconds() > self.max_data_age_seconds
            or pool_age.total_seconds() > self.max_data_age_seconds
        ):
            return _order_result(
                MarginOrderDisposition.HALT,
                "stale_margin_inputs",
                "Intent, market capability, or delegation-limit data is stale or from the future.",
            )
        if liquidity_pool.currency.casefold() != settings.src_currency.casefold():
            return _order_result(
                MarginOrderDisposition.REJECT,
                "margin_pool_currency_mismatch",
                "The liquidity-pool snapshot does not match the market source currency.",
            )
        if liquidity_pool.status is MarginLiquidityPoolStatus.UNKNOWN:
            return _order_result(
                MarginOrderDisposition.HALT,
                "margin_pool_state_unknown",
                "Active-pool state is unknown; additional Margin risk must stop.",
            )
        if liquidity_pool.status is MarginLiquidityPoolStatus.INACTIVE:
            return _order_result(
                MarginOrderDisposition.REJECT,
                "margin_pool_inactive",
                "Nobitex does not list an active liquidity pool for this source currency.",
            )
        available_pool_capacity = liquidity_pool.available_capacity
        if available_pool_capacity is None:
            return _order_result(
                MarginOrderDisposition.HALT,
                "margin_pool_capacity_unknown",
                "Reported active-pool capacity is incomplete; additional risk must stop.",
            )
        if available_pool_capacity < Decimal("0"):
            return _order_result(
                MarginOrderDisposition.HALT,
                "margin_pool_capacity_inconsistent",
                "Reported filledCapacity exceeds capacity; the pool snapshot is inconsistent.",
            )
        if available_pool_capacity == Decimal("0"):
            return _order_result(
                MarginOrderDisposition.REJECT,
                "margin_pool_capacity_exhausted",
                "The source-currency liquidity pool has no remaining capacity.",
            )
        if intent.amount > available_pool_capacity:
            return _order_result(
                MarginOrderDisposition.REJECT,
                "margin_pool_capacity_exceeded",
                "Requested source-currency amount exceeds the active pool's remaining capacity.",
            )

        side_enabled = (
            settings.buy_enabled if intent.side.value == "buy" else settings.sell_enabled
        )
        if not side_enabled:
            return _order_result(
                MarginOrderDisposition.REJECT,
                "margin_side_disabled",
                "Nobitex does not report this Margin direction as enabled.",
            )
        if intent.leverage > settings.max_leverage:
            return _order_result(
                MarginOrderDisposition.REJECT,
                "leverage_exceeds_market_limit",
                "Requested leverage exceeds the source-reported market maximum.",
            )
        matching = tuple(
            item
            for item in delegation_limits.limits
            if item.side is intent.side and item.leverage == intent.leverage
        )
        if len(matching) != 1:
            return _order_result(
                MarginOrderDisposition.HALT,
                "delegation_limit_unknown",
                "A unique source-reported limit for this side and leverage is required.",
            )
        side_limit = matching[0].limit
        requested_limit_value = (
            intent.amount * intent.price if intent.side.value == "buy" else intent.amount
        )
        if requested_limit_value > side_limit:
            unit = settings.dst_currency if intent.side.value == "buy" else settings.src_currency
            return _order_result(
                MarginOrderDisposition.REJECT,
                "delegation_limit_exceeded",
                f"Request exceeds the remaining side-specific limit in {unit}.",
            )
        estimated_collateral = MarginCalculator.estimate_initial_collateral(
            intent.amount * intent.price, intent.leverage
        )
        return MarginOrderRiskResult(
            disposition=MarginOrderDisposition.ALLOW,
            reason_code="within_reported_margin_limits",
            explanation=(
                "Market, side, leverage, freshness, active pool capacity, and source-reported "
                "delegation limits passed. Collateral is a planning estimate, not venue authority."
            ),
            estimated_initial_collateral=estimated_collateral,
        )

    def assess_position(
        self,
        position: MarginPosition,
        *,
        now: datetime,
    ) -> MarginRiskAssessment:
        """Classify position health only; this method cannot authorize additional exposure."""
        if not _is_aware(now):
            raise ValueError("Margin risk evaluation time must be timezone-aware.")
        if not _is_aware(position.observed_at):
            return _assessment(
                position, now, MarginHealth.UNKNOWN, "invalid_position_timestamp",
                "Position observation time is not timezone-aware."
            )
        age = now - position.observed_at
        if age < timedelta(0) or age.total_seconds() > self.max_data_age_seconds:
            return _assessment(
                position, now, MarginHealth.UNKNOWN, "stale_margin_position",
                "Position data is stale or from the future; new Margin risk must stop."
            )
        if position.status is MarginPositionStatus.LIQUIDATED:
            return _assessment(
                position, now, MarginHealth.LIQUIDATED, "venue_reports_liquidated",
                "The venue reports this position as liquidated."
            )
        if position.status is MarginPositionStatus.CLOSED:
            return _assessment(
                position, now, MarginHealth.INACTIVE, "position_closed",
                "The venue reports that the position has been closed."
            )
        if position.status is MarginPositionStatus.EXPIRED:
            return _assessment(
                position, now, MarginHealth.UNKNOWN, "expiry_settlement_pending",
                "Expired positions may remain in transition until system settlement completes."
            )
        if position.status is MarginPositionStatus.UNKNOWN:
            return _assessment(
                position, now, MarginHealth.UNKNOWN, "unknown_position_status",
                "The position status is not in the documented state set."
            )
        if position.margin_mode is not MarginMode.ISOLATED and not (
            position.margin_mode is MarginMode.CROSS and self.cross_margin_enabled
        ):
            return _assessment(
                position, now, MarginHealth.UNSUPPORTED_MODE, "unsupported_margin_mode",
                "The observed margin mode is not enabled by the current policy."
            )
        if position.mark_price is None:
            return _assessment(
                position, now, MarginHealth.UNKNOWN, "mark_price_missing",
                "The source did not provide markPrice; liquidation proximity is unknown."
            )
        if position.liquidation_price is not None:
            crossed = (
                position.mark_price <= position.liquidation_price
                if position.side.value == "buy"
                else position.mark_price >= position.liquidation_price
            )
            if crossed:
                return _assessment(
                    position, now, MarginHealth.LIQUIDATION_THRESHOLD_CROSSED,
                    "reported_liquidation_threshold_crossed",
                    "Observed mark price has crossed the venue-reported liquidation threshold."
                )
        if position.margin_ratio is None:
            return _assessment(
                position, now, MarginHealth.UNKNOWN, "margin_ratio_missing",
                "The source did not provide marginRatio; health cannot be verified."
            )
        if position.margin_ratio <= self.maintenance_margin_ratio:
            return _assessment(
                position, now, MarginHealth.AT_RISK,
                "maintenance_margin_ratio_breached",
                "Reported marginRatio is at or below the documented minimum safe ratio."
            )
        if position.liquidation_price is None:
            return _assessment(
                position, now, MarginHealth.UNKNOWN, "liquidation_price_missing",
                "No venue liquidationPrice is available; a local threshold is not fabricated."
            )
        return _assessment(
            position, now, MarginHealth.HEALTHY,
            "within_reported_margin_thresholds",
            "Position is above the reported minimum ratio and has not crossed the venue threshold. "
            "This position-only assessment does not authorize additional exposure.",
        )


class MarginReconciler:
    """Compare material venue position fields; do not silently repair discrepancies."""

    _FIELDS: tuple[str, ...] = (
        "market_symbol", "side", "status", "margin_mode", "collateral", "leverage",
        "liquidation_price", "entry_price", "exit_price", "delegated_amount", "liability",
        "total_asset", "margin_ratio", "liability_in_order", "asset_in_order",
        "unrealized_pnl", "expiration_date", "extension_fee", "mark_price", "realized_pnl",
    )

    @classmethod
    def compare(
        cls, expected: MarginPosition, observed: MarginPosition
    ) -> MarginReconciliationResult:
        """Return all material mismatches; snapshots are never overwritten."""
        differences: list[str] = []
        if expected.position_id != observed.position_id:
            differences.append("position_id_mismatch")
        for field_name in cls._FIELDS:
            if getattr(expected, field_name) != getattr(observed, field_name):
                differences.append(f"{field_name}_mismatch")
        normalized = tuple(sorted(set(differences)))
        return MarginReconciliationResult(
            position_id=expected.position_id,
            matched=not normalized,
            differences=normalized,
        )


def _assessment(
    position: MarginPosition,
    assessed_at: datetime,
    health: MarginHealth,
    code: str,
    explanation: str,
    *,
    new_risk_permitted: bool = False,
) -> MarginRiskAssessment:
    return MarginRiskAssessment(
        position_id=position.position_id,
        health=health,
        assessed_at=assessed_at,
        reason_code=code,
        explanation=explanation,
        new_risk_permitted=new_risk_permitted,
        reported_liquidation_price=position.liquidation_price,
        observed_mark_price=position.mark_price,
        observed_margin_ratio=position.margin_ratio,
    )


def _order_result(
    disposition: MarginOrderDisposition, code: str, explanation: str
) -> MarginOrderRiskResult:
    return MarginOrderRiskResult(
        disposition=disposition, reason_code=code, explanation=explanation
    )


def _positive(value: Decimal, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{label} must be finite and positive.")


def _nonnegative(value: Decimal, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{label} must be finite and non-negative.")


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None
