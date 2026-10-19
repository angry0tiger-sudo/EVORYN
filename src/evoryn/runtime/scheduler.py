"""Cooperative fixed-cadence scheduling for the Paper-only runtime.

This is a process-local supervisor, not an OS service manager. It serializes cycles, does not
run catch-up bursts after delays, cooperates with shutdown requests, and exits on a latched HALT.
It never clears halts or talks to an exchange.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event
from time import monotonic
from typing import Protocol

from evoryn.runtime.paper import PaperRuntimeResult, RuntimeStatus


class CycleSupervisor(Protocol):
    """Minimal cycle boundary needed by the process-local scheduler."""

    def step(self, *, now: datetime | None = None) -> PaperRuntimeResult:
        """Execute a single bounded runtime cycle."""
        ...


@dataclass(frozen=True, slots=True)
class SchedulerReport:
    """Immutable stop report suitable for console logs and service health checks."""

    cycles_completed: int
    last_status: str | None
    exit_reason: str
    started_at: datetime
    ended_at: datetime

    def __post_init__(self) -> None:
        if self.cycles_completed < 0:
            raise ValueError("cycles_completed must be non-negative.")
        for name in ("started_at", "ended_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware.")
        if self.ended_at < self.started_at:
            raise ValueError("Scheduler end time cannot precede start time.")
        if not self.exit_reason.strip():
            raise ValueError("exit_reason must not be empty.")


class PaperRuntimeScheduler:
    """One-cycle-at-a-time scheduler with explicit stop, cadence, and terminal-HALT handling."""

    def __init__(
        self,
        supervisor: CycleSupervisor,
        *,
        interval_seconds: float = 60.0,
        stop_event: Event | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        on_result: Callable[[PaperRuntimeResult], None] | None = None,
    ) -> None:
        if not 0.1 <= interval_seconds <= 86_400:
            raise ValueError("interval_seconds must be in [0.1, 86400].")
        self._supervisor = supervisor
        self._interval = interval_seconds
        self._stop = stop_event or Event()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic_clock
        self._on_result = on_result

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        """Signal the loop to stop before its next cycle or interrupt its interval wait."""
        self._stop.set()

    def run(self, *, max_cycles: int | None = None) -> SchedulerReport:
        """Run until an explicit stop, HALT, safe cycle limit, or sanitized supervisor exception."""
        if max_cycles is not None and (
            type(max_cycles) is not int or max_cycles <= 0
        ):
            raise ValueError("max_cycles must be None or a positive integer.")
        started = self._aware_utc(self._clock(), "scheduler start time")
        cycles = 0
        last_status: str | None = None
        reason = "stop_requested"
        while not self._stop.is_set():
            cycle_started = self._monotonic()
            try:
                result = self._supervisor.step(now=self._aware_utc(self._clock(), "cycle time"))
            except Exception as exc:
                reason = f"scheduler_exception:{type(exc).__name__}"
                self._stop.set()
                break
            cycles += 1
            last_status = result.status.value
            try:
                if self._on_result is not None:
                    self._on_result(result)
            except Exception as exc:
                reason = f"result_observer_exception:{type(exc).__name__}"
                self._stop.set()
                break
            if result.status is RuntimeStatus.HALTED:
                reason = f"runtime_halted:{result.reason_code}"
                self._stop.set()
                break
            if max_cycles is not None and cycles >= max_cycles:
                reason = "cycle_limit_reached"
                self._stop.set()
                break
            remaining = max(
                0.0,
                cycle_started + self._interval - self._monotonic(),
            )
            if remaining > 0 and self._stop.wait(remaining):
                reason = "stop_requested"
                break
        ended = self._aware_utc(self._clock(), "scheduler end time")
        if self._stop.is_set() and reason == "stop_requested":
            reason = "stop_requested"
        return SchedulerReport(
            cycles_completed=cycles,
            last_status=last_status,
            exit_reason=reason,
            started_at=started,
            ended_at=ended,
        )

    @staticmethod
    def _aware_utc(value: datetime, name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware.")
        offset = value.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError(f"{name} must use UTC.")
        return value.astimezone(UTC)
