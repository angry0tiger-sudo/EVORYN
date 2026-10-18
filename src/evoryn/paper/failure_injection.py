"""Deterministic failure injection for paper execution and recovery tests."""

from __future__ import annotations

from collections import defaultdict, deque
from enum import StrEnum
from threading import Lock


class FailureMode(StrEnum):
    """Fault classes the paper environment can inject without network access."""

    NETWORK_DISCONNECT = "network_disconnect"
    TIMEOUT = "timeout"
    INVALID_DATA = "invalid_data"


class FailureInjector:
    """Thread-safe, operation-scoped queue of one-shot synthetic faults."""

    def __init__(self) -> None:
        """Create an empty injector; no fault is active unless a test adds one."""
        self._lock = Lock()
        self._faults: dict[str, deque[FailureMode]] = defaultdict(deque)

    def inject_next(self, operation: str, mode: FailureMode) -> None:
        """Schedule one fault for the next matching named operation."""
        normalized = operation.strip()
        if not normalized:
            raise ValueError("Failure-injection operation must not be empty.")
        if not isinstance(mode, FailureMode):
            raise ValueError("Failure injection mode must be a supported FailureMode.")
        with self._lock:
            self._faults[normalized].append(mode)

    def consume(self, operation: str) -> FailureMode | None:
        """Return and remove the oldest fault scheduled for this operation."""
        with self._lock:
            pending = self._faults.get(operation)
            if not pending:
                return None
            result = pending.popleft()
            if not pending:
                self._faults.pop(operation, None)
            return result
