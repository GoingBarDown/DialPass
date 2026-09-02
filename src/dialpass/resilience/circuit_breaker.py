"""A small circuit breaker.

Wraps the Tier 2 connection (M8): if gpt-realtime-mini starts failing or timing
out, stop hammering it, fail fast, and let the agent fall back to notifying the
user instead of stranding the call.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum
from typing import TypeVar

T = TypeVar("T")


class BreakerState(StrEnum):
    CLOSED = "CLOSED"  # healthy, calls pass through
    OPEN = "OPEN"  # failing, calls rejected immediately
    HALF_OPEN = "HALF_OPEN"  # probing — allow one trial call


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._clock = clock
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0

    @property
    def state(self) -> BreakerState:
        if self._state == BreakerState.OPEN and (
            self._clock() - self._opened_at >= self._reset_timeout
        ):
            self._state = BreakerState.HALF_OPEN
        return self._state

    def call(self, fn: Callable[..., T], *args, **kwargs) -> T:
        state = self.state
        if state == BreakerState.OPEN:
            raise CircuitOpenError("circuit is open")
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self._on_failure()
            raise
        self._on_success()
        return result

    def _on_success(self) -> None:
        self._failures = 0
        self._state = BreakerState.CLOSED

    def _on_failure(self) -> None:
        self._failures += 1
        if self._state == BreakerState.HALF_OPEN or self._failures >= self._threshold:
            self._state = BreakerState.OPEN
            self._opened_at = self._clock()
