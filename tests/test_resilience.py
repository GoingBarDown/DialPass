"""M8 — circuit breaker around Tier 2, fallback routing, external abort."""

from __future__ import annotations

import contextlib
import threading

from dialpass.agent.classifier import ScriptedClassifier
from dialpass.agent.session import AgentSession
from dialpass.config import get_settings
from dialpass.realtime.fake import FakeTier2
from dialpass.realtime.protocol import MenuDecision, ProbeOutcome, Tier2Unavailable
from dialpass.resilience.circuit_breaker import BreakerState, CircuitBreaker
from dialpass.resilience.fallback import fallback_message
from dialpass.telemetry.publisher import CollectingSink
from dialpass.testing import iter_frames, synthesize_call


# ---- fallback messages -------------------------------------------------
def test_fallback_message_maps_known_reasons_and_defaults():
    assert "menu" in fallback_message("tier2_unavailable")
    assert "voicemail" in fallback_message("non_connect")
    assert fallback_message("something_new") == fallback_message("")  # default


# ---- circuit breaker thread-safety ----------------------------------
def test_breaker_is_thread_safe_under_concurrent_failures():
    cb = CircuitBreaker(failure_threshold=50, reset_timeout=99)

    def boom() -> None:
        raise RuntimeError("x")

    def hammer() -> None:
        for _ in range(50):
            with contextlib.suppress(RuntimeError):
                cb.call(boom)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 200 failures against a threshold of 50 — must be open, counter not corrupted
    assert cb.state == BreakerState.OPEN


# ---- session + breaker -------------------------------------------------
class _FlakyTier2(FakeTier2):
    """Realtime API is down — every turn fails as infra. Counts real hits so a
    test can prove the open breaker never even called it."""

    def __init__(self) -> None:
        super().__init__(menu_digits=None, probe_is_human=False)
        self.hits = 0

    def choose_menu_digit(self, audio, sample_rate, goal) -> MenuDecision:
        self.hits += 1
        raise Tier2Unavailable("realtime down")

    def probe(self, audio, sample_rate) -> ProbeOutcome:
        self.hits += 1
        raise Tier2Unavailable("realtime down")


def _run_to_probe(tier2, breaker) -> tuple[AgentSession, CollectingSink]:
    settings = get_settings()
    pcm, schedule = synthesize_call()
    sink = CollectingSink()
    fails: list[str] = []
    session = AgentSession(
        "res-test",
        ScriptedClassifier(schedule),
        tier2,
        telemetry=sink,
        settings=settings,
        goal="reach a human",
        tier2_breaker=breaker,
    )
    session.on_fail = fails.append
    for frame in iter_frames(pcm, settings.frame_ms):
        session.feed_audio(frame)
        if session.finished:
            break
    sink.fails = fails  # type: ignore[attr-defined]
    return session, sink


def test_probe_infra_failure_fails_the_call_with_the_fallback_reason():
    tier2 = _FlakyTier2()
    session, sink = _run_to_probe(tier2, CircuitBreaker(failure_threshold=9, reset_timeout=99))

    assert session.finished
    # menu failure is survivable (Tier 1 keeps listening); the probe failure is
    # what ends the call — we can't confirm a human, so we must not bridge.
    assert [e.payload()["reason"] for e in sink.of_kind("call_failed")] == ["tier2_unavailable"]
    assert sink.fails == ["tier2_unavailable"]  # type: ignore[attr-defined]


def test_menu_infra_failure_is_survivable_but_still_counts_toward_the_breaker():
    class MenuDown(FakeTier2):
        def __init__(self) -> None:
            super().__init__(menu_digits="1", probe_is_human=True)
            self.menu_hits = 0

        def choose_menu_digit(self, audio, sample_rate, goal) -> MenuDecision:
            self.menu_hits += 1
            raise Tier2Unavailable("menu exchange failed")

    tier2 = MenuDown()
    breaker = CircuitBreaker(failure_threshold=99, reset_timeout=99)
    session, sink = _run_to_probe(tier2, breaker)

    assert tier2.menu_hits >= 1
    # the call still reached the human-bridged outcome via the probe
    assert [e.payload()["outcome"] for e in sink.of_kind("call_completed")] == ["human_bridged"]
    assert not sink.of_kind("call_failed")


def test_open_breaker_fails_fast_without_calling_tier2():
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout=99)
    _run_to_probe(_FlakyTier2(), breaker)
    assert breaker.state == BreakerState.OPEN  # one call, menu + probe = 2 failures

    third = _FlakyTier2()
    session, sink = _run_to_probe(third, breaker)
    assert session.finished
    assert third.hits == 0  # breaker rejected it before Tier 2 was touched
    assert [e.payload()["reason"] for e in sink.of_kind("call_failed")] == ["tier2_unavailable"]


# ---- external abort ---------------------------------------------------
def test_abort_routes_through_the_failure_path_once():
    settings = get_settings()
    sink = CollectingSink()
    reasons: list[str] = []
    session = AgentSession(
        "abort-test",
        ScriptedClassifier([]),
        FakeTier2(),
        telemetry=sink,
        settings=settings,
    )
    session.on_fail = reasons.append

    session.abort("user_left")
    session.abort("user_left")  # idempotent — already finished

    assert session.finished
    assert reasons == ["user_left"]
    assert [e.payload()["reason"] for e in sink.of_kind("call_failed")] == ["user_left"]
