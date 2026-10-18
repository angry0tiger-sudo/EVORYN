"""Thread-safe, deterministic sliding-window rate-limit implementation."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock

from evoryn.ports.exchange_errors import RateLimitExceeded
from evoryn.ports.rate_limits import RateLimitPolicy


@dataclass(slots=True)
class SlidingWindowRateLimiter:
    """Enforce per-key budgets without sleeping or retrying requests implicitly."""

    clock: Callable[[], float] = time.monotonic
    _events: dict[str, deque[float]] = field(default_factory=dict, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def acquire(self, key: str, policy: RateLimitPolicy) -> None:
        """Consume one request slot or raise a typed rate-limit exception."""
        if not key.strip():
            raise ValueError("Rate-limit key must not be empty.")
        now = self.clock()
        cutoff = now - policy.window_seconds
        with self._lock:
            events = self._events.setdefault(key, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= policy.max_requests:
                retry_after = max(0.0, events[0] + policy.window_seconds - now)
                raise RateLimitExceeded(
                    f"Local rate budget exceeded for key '{key}'.",
                    retry_after_seconds=retry_after,
                )
            events.append(now)
