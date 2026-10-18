"""Strict normalization of the documented Nobitex Margin REST payloads.

This module has no HTTP transport, authentication, or order-writing capability.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from evoryn.margin.models import (
    DelegationLimit,
    MarginMarketSettings,
    MarginMode,
    MarginPosition,
    MarginPositionStatus,
    MarginSide,
)


class NobitexMarginAdapter:
    """Pure payload normalizers for /margin/markets/list and /positions/list."""

    @staticmethod
    def normalize_market_settings(
        market_symbol: str,
        payload: Mapping[str, object],
        *,
        observed_at: datetime,
    ) -> MarginMarketSettings:
        """Normalize one market record without guessing aliases or defaulting fields."""
        return MarginMarketSettings(
            market_symbol=market_symbol,
            src_currency=_required_string(payload, "srcCurrency"),
            dst_currency=_required_string(payload, "dstCurrency"),
            position_fee_rate=_required_decimal(payload, "positionFeeRate"),
            max_leverage=_required_decimal(payload, "maxLeverage"),
            buy_enabled=_required_bool(payload, "buyEnabled"),
            sell_enabled=_required_bool(payload, "sellEnabled"),
            observed_at=observed_at,
        )

    @staticmethod
    def normalize_position(
        payload: Mapping[str, object],
        *,
        observed_at: datetime,
    ) -> MarginPosition:
        """Normalize one documented Position object, preserving absent optional fields."""
        return MarginPosition(
            position_id=_required_int(payload, "id"),
            market_symbol=(
                _required_string(payload, "srcCurrency").upper()
                + _required_string(payload, "dstCurrency").upper()
            ),
            src_currency=_required_string(payload, "srcCurrency").upper(),
            dst_currency=_required_string(payload, "dstCurrency").upper(),
            side=_side(_required_string(payload, "side")),
            status=_status(_required_string(payload, "status")),
            margin_mode=_margin_mode(_required_string(payload, "marginType")),
            collateral=_required_decimal(payload, "collateral"),
            leverage=_required_decimal(payload, "leverage"),
            observed_at=observed_at,
            created_at=_optional_datetime(payload, "createdAt"),
            opened_at=_optional_datetime(payload, "openedAt"),
            closed_at=_optional_datetime(payload, "closedAt"),
            liquidation_price=_optional_decimal(payload, "liquidationPrice"),
            entry_price=_optional_decimal(payload, "entryPrice"),
            exit_price=_optional_decimal(payload, "exitPrice"),
            delegated_amount=_optional_decimal(payload, "delegatedAmount"),
            liability=_optional_decimal(payload, "liability"),
            total_asset=_optional_decimal(payload, "totalAsset"),
            margin_ratio=_optional_decimal(payload, "marginRatio"),
            liability_in_order=_optional_decimal(payload, "liabilityInOrder"),
            asset_in_order=_optional_decimal(payload, "assetInOrder"),
            unrealized_pnl=_optional_decimal(payload, "unrealizedPNL"),
            unrealized_pnl_percent=_optional_decimal(payload, "unrealizedPNLPercent"),
            expiration_date=_optional_date(payload, "expirationDate"),
            extension_fee=_optional_decimal(payload, "extensionFee"),
            mark_price=_optional_decimal(payload, "markPrice"),
            realized_pnl=_optional_decimal(payload, "PNL"),
            realized_pnl_percent=_optional_decimal(payload, "PNLPercent"),
        )

    @staticmethod
    def normalize_delegation_limits(
        payload: Mapping[str, object],
    ) -> tuple[DelegationLimit, ...]:
        """Normalize side-specific limits from /margin/v2/delegation-limit.

        BUY limits are quote/destination currency; SELL limits are base/source currency.
        A missing or malformed side is an error, not an empty/unlimited allowance.
        """
        raw_limits = payload.get("limits")
        if not isinstance(raw_limits, Mapping):
            raise ValueError("Margin response requires a 'limits' mapping.")
        normalized: list[DelegationLimit] = []
        for side, key in ((MarginSide.BUY, "buy"), (MarginSide.SELL, "sell")):
            rows = raw_limits.get(key)
            if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
                raise ValueError(f"Margin delegation limits require a '{key}' list.")
            for row in rows:
                if not isinstance(row, Mapping):
                    raise ValueError(f"Each '{key}' delegation limit must be an object.")
                normalized.append(
                    DelegationLimit(
                        side=side,
                        leverage=_required_decimal(row, "leverage"),
                        limit=_required_decimal(row, "limit"),
                    )
                )
        return tuple(normalized)


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Required Margin field {key!r} is missing or invalid.")
    return value


def _required_bool(payload: Mapping[str, object], key: str) -> bool:
    value = payload.get(key)
    if type(value) is not bool:
        raise ValueError(f"Required Margin field {key!r} must be a boolean.")
    return value


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool):
        raise ValueError(f"Required Margin field {key!r} must be an integer.")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ValueError(f"Required Margin field {key!r} must be an integer.")


def _required_decimal(payload: Mapping[str, object], key: str) -> Decimal:
    value = payload.get(key)
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"Required Margin field {key!r} must not be a float or boolean.")
    if not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"Required Margin field {key!r} is missing or invalid.")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Required Margin field {key!r} is not decimal.") from exc
    if not result.is_finite():
        raise ValueError(f"Required Margin field {key!r} must be finite.")
    return result


def _optional_decimal(payload: Mapping[str, object], key: str) -> Decimal | None:
    if key not in payload or payload[key] is None:
        return None
    return _required_decimal(payload, key)


def _optional_datetime(payload: Mapping[str, object], key: str) -> datetime | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Optional Margin field {key!r} must be an ISO datetime or null.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Margin field {key!r} is not a valid ISO datetime.") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"Margin field {key!r} must include a timezone.")
    return result


def _optional_date(payload: Mapping[str, object], key: str) -> date | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Optional Margin field {key!r} must be an ISO date or null.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Margin field {key!r} is not a valid ISO date.") from exc


def _side(value: str) -> MarginSide:
    try:
        return MarginSide(value.casefold())
    except ValueError as exc:
        raise ValueError(f"Unsupported Nobitex Margin side {value!r}.") from exc


def _status(value: str) -> MarginPositionStatus:
    for status in MarginPositionStatus:
        if value.casefold() == status.value.casefold():
            return status
    return MarginPositionStatus.UNKNOWN


def _margin_mode(value: str) -> MarginMode:
    normalized = value.casefold()
    if normalized == "isolated margin":
        return MarginMode.ISOLATED
    if normalized == "cross margin":
        return MarginMode.CROSS
    return MarginMode.UNKNOWN
