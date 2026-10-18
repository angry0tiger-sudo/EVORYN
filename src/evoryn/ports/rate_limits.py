"""Portable rate-limit policy records shared by exchange adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """Sliding-window request budget for one exchange endpoint category."""

    max_requests: int
    window_seconds: float

    def __post_init__(self) -> None:
        """Reject impossible rate policies at construction time."""
        if self.max_requests < 1:
            raise ValueError("max_requests must be at least one.")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive.")


