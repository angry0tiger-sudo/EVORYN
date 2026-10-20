"""Tests for safe process-local Paper scheduler behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from threading import Event

from evoryn.runtime.paper import PaperRuntimeResult, RuntimeStatus
from evoryn.runtime.scheduler import PaperRuntimeScheduler

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class FakeSupervisor:
    def __init__(self, statuses: list[RuntimeStatus]) -> None:
        self.statuses = statuses
        self.calls = 0

    def step(self, *, now: datetime | None = None) -> PaperRuntimeResult:
        assert now is not None and now.tzinfo is not None
        status = self.statuses[min(self.calls, len(self.statuses) - 1)]
        self.calls += 1
        return PaperRuntimeResult(
            status=status,
            correlation_id=f"cycle:{self.calls}",
            reason_code="fixture",
        )


def test_scheduler_runs_serially_and_stops_on_requested_event() -> None:
    stop = Event()
    supervisor = FakeSupervisor([RuntimeStatus.NO_NEW_CANDLE] * 3)
    observations: list[PaperRuntimeResult] = []

    def observe(result: PaperRuntimeResult) -> None:
        observations.append(result)
        stop.set()

    scheduler = PaperRuntimeScheduler(
        supervisor,
        interval_seconds=5,
        stop_event=stop,
        clock=lambda: NOW,
        monotonic_clock=lambda: 10.0,
        on_result=observe,
    )
    report = scheduler.run()

    assert report.cycles_completed == 1
    assert report.last_status == RuntimeStatus.NO_NEW_CANDLE.value
    assert report.exit_reason == "stop_requested"
    assert supervisor.calls == 1
    assert len(observations) == 1


def test_scheduler_terminates_immediately_on_latched_halt() -> None:
    supervisor = FakeSupervisor([RuntimeStatus.HALTED, RuntimeStatus.PROCESSED])
    scheduler = PaperRuntimeScheduler(
        supervisor,
        interval_seconds=1,
        clock=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    )

    report = scheduler.run()

    assert report.cycles_completed == 1
    assert report.last_status == RuntimeStatus.HALTED.value
    assert report.exit_reason == "runtime_halted:fixture"
    assert supervisor.calls == 1
    assert scheduler.stop_requested


def test_scheduler_cycle_limit_is_explicit_for_supervised_tools() -> None:
    supervisor = FakeSupervisor([RuntimeStatus.NO_NEW_CANDLE])
    scheduler = PaperRuntimeScheduler(
        supervisor,
        interval_seconds=1,
        clock=lambda: NOW,
        monotonic_clock=lambda: 0.0,
    )

    report = scheduler.run(max_cycles=2)

    assert report.cycles_completed == 2
    assert report.exit_reason == "cycle_limit_reached"
