"""Unit tests for the shared exchange sliding-window rate limiter."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from evoryn.infrastructure.exchange.rate_limit import SlidingWindowRateLimiter
from evoryn.ports.exchange_errors import RateLimitExceeded
from evoryn.ports.rate_limits import RateLimitPolicy


@dataclass
class FakeClock:
    """Mutable monotonic clock for deterministic rate-limit tests."""

    now: float = 0.0

    def read(self) -> float:
        """Return the current synthetic monotonic timestamp."""
        return self.now


def test_sliding_window_rejects_excess_requests_and_reports_retry_after() -> None:
    """A full request budget fails immediately and provides a retry delay."""
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(clock=clock.read)
    policy = RateLimitPolicy(max_requests=2, window_seconds=10.0)

    limiter.acquire("nobitex:orderbook", policy)
    clock.now += 1.0
    limiter.acquire("nobitex:orderbook", policy)

    with pytest.raises(RateLimitExceeded) as caught:
        limiter.acquire("nobitex:orderbook", policy)

    assert caught.value.retry_after_seconds == 9.0


def test_sliding_window_releases_slots_after_window_expiry() -> None:
    """Expired timestamps are removed and a new request can proceed."""
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(clock=clock.read)
    policy = RateLimitPolicy(max_requests=1, window_seconds=5.0)
    limiter.acquire("nobitex:stats", policy)

    clock.now = 5.0
    limiter.acquire("nobitex:stats", policy)


def test_sliding_window_budgets_are_isolated_by_key() -> None:
    """Separate provider or endpoint keys have separate request budgets."""
    limiter = SlidingWindowRateLimiter(clock=lambda: 1.0)
    policy = RateLimitPolicy(max_requests=1, window_seconds=60.0)

    limiter.acquire("nobitex:trades", policy)
    limiter.acquire("nobitex:stats", policy)

    with pytest.raises(RateLimitExceeded):
        limiter.acquire("nobitex:trades", policy)


def test_sliding_window_rejects_empty_keys() -> None:
    """An empty key cannot merge unrelated endpoint budgets accidentally."""
    limiter = SlidingWindowRateLimiter(clock=lambda: 1.0)

    with pytest.raises(ValueError, match="must not be empty"):
        limiter.acquire("", RateLimitPolicy(max_requests=1, window_seconds=1.0))
