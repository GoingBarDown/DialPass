"""M5 phase 0: Tier 2 runs off the media loop.

The unit here is `agent/executor.py` plus the `AgentSession` wiring that submits a
Tier 2 call and applies the result on a later tick instead of blocking `feed_audio`.
"""

from __future__ import annotations

import threading
import time

import numpy as np

from dialpass.agent.classifier import ScriptedClassifier
from dialpass.agent.executor import InlineExecutor, ThreadedExecutor
from dialpass.agent.session import AgentSession
from dialpass.config import get_settings
from dialpass.realtime.protocol import MenuDecision, ProbeOutcome
from dialpass.telemetry.publisher import CollectingSink
from dialpass.testing import iter_frames, synthesize_call


# -- the executors in isolation -----------------------------------------------
def test_inline_executor_is_synchronous():
    ex = InlineExecutor()
    ex.submit(lambda: 41 + 1)
    assert ex.poll() == (42, None)
    assert ex.poll() is None  # consumed


def test_inline_executor_captures_the_error():
    ex = InlineExecutor()
    boom = RuntimeError("nope")

    def raise_it():
        raise boom

    ex.submit(raise_it)
    value, error = ex.poll()
    assert value is None and error is boom


def test_threaded_executor_runs_off_thread():
    ex = ThreadedExecutor()
    gate = threading.Event()
    ex.submit(lambda: gate.wait(2) and "done" or "done")
    assert ex.poll() is None  # still running — poll doesn't block
    gate.set()
    for _ in range(200):
        if (r := ex.poll()) is not None:
            assert r == ("done", None)
            break
        time.sleep(0.01)
    else:
        raise AssertionError("threaded job never completed")
    ex.close()


# -- session integration -----------------------------------------------------
class GatedTier2:
    """`choose_menu_digit` blocks until `release` is set, so a test can hold the
    Tier 2 call 'in flight' and watch what the session does meanwhile."""

    def __init__(self, digits: str = "4") -> None:
        self._digits = digits
        self.release = threading.Event()
        self.entered = threading.Event()
        self.menu_calls = 0

    def choose_menu_digit(self, audio, sample_rate, goal) -> MenuDecision:
        self.menu_calls += 1
        self.entered.set()
        self.release.wait(timeout=5)
        return MenuDecision(self._digits, rationale="gated")

    def probe(self, audio, sample_rate) -> ProbeOutcome:
        return ProbeOutcome(is_human=False, transcript="gated")

    def say_to_agent(self, text: str) -> None: ...

    def close(self) -> None: ...


def _session(tier2, executor, sender):
    settings = get_settings()
    pcm, schedule = synthesize_call()
    session = AgentSession(
        "async-test",
        ScriptedClassifier(schedule),
        tier2,
        telemetry=CollectingSink(),
        settings=settings,
        goal="reach a human",
        dtmf_sender=sender,
        tier2_executor=executor,
    )
    return session, iter_frames(pcm, settings.frame_ms)


def test_blocking_tier2_does_not_freeze_ingest_or_double_wake():
    tier2 = GatedTier2(digits="4")
    ex = ThreadedExecutor()
    pressed: list[str] = []
    session, frames = _session(tier2, ex, pressed.append)

    it = iter(frames)
    for frame in it:
        session.feed_audio(frame)
        if tier2.entered.is_set():
            break
    assert session._tier2_kind == "menu"  # a decision is in flight

    # Tier 2 is still gated. Keep feeding audio — ingest must not block, and the
    # session must not wake Tier 2 again while the first call is outstanding.
    ticks = len(session.telemetry.of_kind("frame_classified"))
    for _ in range(150):
        session.feed_audio(next(it, np.zeros(160, dtype=np.int16)))
    assert len(session.telemetry.of_kind("frame_classified")) > ticks  # kept ticking
    assert tier2.menu_calls == 1
    assert pressed == []  # nothing applied while in flight

    # Let the decision finish; a subsequent tick applies it.
    tier2.release.set()
    for _ in range(300):
        session.feed_audio(next(it, np.zeros(160, dtype=np.int16)))
        if pressed:
            break
        time.sleep(0.005)
    assert pressed == ["4"]
    assert session.telemetry.of_kind("dtmf_sent")
    session.close()


def test_inline_executor_keeps_the_menu_press_on_the_same_path():
    """Parity check: with the default InlineExecutor the digit still lands."""
    tier2 = GatedTier2(digits="3")
    tier2.release.set()  # never blocks
    pressed: list[str] = []
    session, frames = _session(tier2, InlineExecutor(), pressed.append)
    for frame in frames:
        session.feed_audio(frame)
        if session.finished:
            break
    assert pressed == ["3"]
