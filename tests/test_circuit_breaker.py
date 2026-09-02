import pytest

from dialpass.resilience.circuit_breaker import (
    BreakerState,
    CircuitBreaker,
    CircuitOpenError,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def boom():
    raise RuntimeError("tier 2 down")


def test_opens_after_threshold_then_rejects_fast():
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=3, reset_timeout=30, clock=clock)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            cb.call(boom)
    assert cb.state == BreakerState.OPEN
    with pytest.raises(CircuitOpenError):
        cb.call(lambda: 1)


def test_half_opens_after_timeout_and_closes_on_success():
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=1, reset_timeout=30, clock=clock)
    with pytest.raises(RuntimeError):
        cb.call(boom)
    assert cb.state == BreakerState.OPEN

    clock.now = 31
    assert cb.state == BreakerState.HALF_OPEN
    assert cb.call(lambda: 42) == 42
    assert cb.state == BreakerState.CLOSED


def test_failure_in_half_open_reopens():
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=1, reset_timeout=10, clock=clock)
    with pytest.raises(RuntimeError):
        cb.call(boom)
    clock.now = 11
    assert cb.state == BreakerState.HALF_OPEN
    with pytest.raises(RuntimeError):
        cb.call(boom)
    assert cb.state == BreakerState.OPEN
