"""Unit tests for the exchange boundary, taxonomy, and common rate-limit policy."""

from __future__ import annotations

from inspect import signature

import pytest

from evoryn.domain.models import ExchangeCapabilities
from evoryn.ports.contracts import ExchangePort, ExchangePlugin, RateLimiterPort
from evoryn.ports.exchange_errors import (
    ExchangeErrorCode,
    RateLimitExceeded,
    TradingDisabledError,
)
from evoryn.ports.rate_limits import RateLimitPolicy


def test_exchange_port_declares_read_and_write_boundaries() -> None:
    """The shared interface declares common reads plus future write operations."""
    for method_name in (
        "get_capabilities",
        "get_market_candles",
        "get_order_book",
        "get_recent_trades",
        "get_market_stats",
        "get_balances",
        "get_order",
        "submit_order",
        "cancel_order",
    ):
        assert callable(getattr(ExchangePort, method_name, None))


def test_capabilities_keep_write_operations_explicitly_separate() -> None:
    """A provider can implement reads while explicitly declaring all writes unavailable."""
    capabilities = ExchangeCapabilities(
        venue="fixture",
        market_data=True,
        account_read=True,
        order_read=True,
        order_write=False,
        candles=True,
        order_book=True,
        recent_trades=True,
        market_stats=True,
        order_submit=False,
        order_cancel=False,
    )

    assert capabilities.market_data
    assert capabilities.account_read
    assert not capabilities.order_write
    assert not capabilities.order_submit
    assert not capabilities.order_cancel


def test_common_error_codes_are_provider_independent() -> None:
    """Rate-limit and disabled-write errors expose stable codes for orchestration."""
    error = RateLimitExceeded("budget exceeded", retry_after_seconds=2.5)
    disabled = TradingDisabledError("writes are not enabled")

    assert error.code is ExchangeErrorCode.RATE_LIMIT
    assert error.retryable
    assert error.retry_after_seconds == 2.5
    assert disabled.code is ExchangeErrorCode.TRADING_DISABLED
    assert not disabled.retryable


def test_rate_limit_policy_rejects_invalid_budgets() -> None:
    """Invalid policies cannot enter a shared exchange rate limiter."""
    with pytest.raises(ValueError, match="at least one"):
        RateLimitPolicy(max_requests=0, window_seconds=60)

    with pytest.raises(ValueError, match="positive"):
        RateLimitPolicy(max_requests=10, window_seconds=0)


def test_port_protocols_are_declared_as_structural_contracts() -> None:
    """Plugin and rate-limiter contracts are runtime-checkable protocols."""
    assert getattr(ExchangePlugin, "_is_runtime_protocol", False)
    assert getattr(RateLimiterPort, "_is_runtime_protocol", False)
    assert tuple(signature(ExchangePort.submit_order).parameters) == ("self", "request")
