"""Telemetry sinks. `emit` must never raise into the caller and never block the
audio path — real sinks (SQS, M7) push onto a bounded queue and drop on overflow.
"""

from __future__ import annotations

import logging
from typing import Protocol

from .events import Event

log = logging.getLogger("dialpass.telemetry")


class TelemetrySink(Protocol):
    def emit(self, event: Event) -> None: ...


class NullSink:
    def emit(self, event: Event) -> None:  # noqa: D102
        pass


class LogSink:
    def __init__(self, level: int = logging.INFO) -> None:
        self._level = level

    def emit(self, event: Event) -> None:
        log.log(self._level, "%s", event.payload())


class CollectingSink:
    """Keeps every event in memory. For the offline harness and tests."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def of_kind(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.kind == kind]
