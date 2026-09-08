"""M4: the menu-navigation path — Tier 2 picks a digit, the session presses it."""

from __future__ import annotations

import pytest

from dialpass.agent.classifier import ScriptedClassifier
from dialpass.agent.labels import Label
from dialpass.agent.session import AgentSession
from dialpass.config import get_settings
from dialpass.realtime.client import _parse_verdict
from dialpass.realtime.fake import FakeTier2
from dialpass.telemetry.publisher import CollectingSink
from dialpass.testing import Segment, iter_frames, synthesize_call


@pytest.mark.parametrize(
    "reply, digit",
    [
        ('{"digit": "2", "rationale": "refund"}', "2"),
        ('```json\n{"digit":"3","rationale":"x"}\n```', "3"),
        ('<|vq_1|>{"digit":"0","rationale":"operator"}', "0"),
        ('here you go {"digit":"5","rationale":"y"} thanks', "5"),
        ('{"digit": "", "rationale": "no option fits"}', ""),
        ("null", ""),
        ("Sorry, I can't help with that.", ""),
        ("You should press 4 for billing.", "4"),
        ("Say 'representative' to reach an agent.", "0"),
    ],
)
def test_parse_verdict_is_robust_to_chatty_model_output(reply, digit):
    assert _parse_verdict(reply)[0] == digit


class RecordingDtmfSender:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    def __call__(self, digits: str) -> None:
        self.pressed.append(digits)


def _run(dtmf_sender, *, menu_digits="2"):
    settings = get_settings()
    pcm, schedule = synthesize_call()
    sink = CollectingSink()
    session = AgentSession(
        "menu-test",
        ScriptedClassifier(schedule),
        FakeTier2(menu_digits=menu_digits, probe_is_human=False),
        telemetry=sink,
        settings=settings,
        goal="reach a human",
        dtmf_sender=dtmf_sender,
    )
    for frame in iter_frames(pcm, settings.frame_ms):
        session.feed_audio(frame)
        if session.finished:
            break
    return session, sink


def test_menu_digit_is_pressed_on_the_call():
    sender = RecordingDtmfSender()
    _, sink = _run(sender)
    assert sender.pressed == ["2"]
    sent = sink.of_kind("dtmf_sent")
    assert [e.payload()["digits"] for e in sent] == ["2"]


def test_no_digit_when_tier2_returns_none():
    sender = RecordingDtmfSender()
    _run(sender, menu_digits=None)
    assert sender.pressed == []


def test_dtmf_sender_failure_does_not_crash_the_call():
    def boom(digits: str) -> None:
        raise RuntimeError("twilio down")

    session, sink = _run(boom)
    # the call kept going past the menu; the failure was swallowed
    assert sink.of_kind("dtmf_sent")  # telemetry still emitted
    assert session.state.value not in ("FAILED",) or sink.of_kind("frame_classified")


# A real hold queue with NO music: menu, a gap, then a rep talking. The old FSM
# sat in IVR_MENU forever (no HOLD_MUSIC -> no ON_HOLD -> no probe). Now, after a
# couple of "nothing to press" reads, it drops to ON_HOLD and the probe runs.
_MUSICLESS_QUEUE = [
    Segment(Label.RINGBACK, 3.0),
    Segment(Label.MENU_SPEAKING, 6.0),
    Segment(Label.SILENCE, 3.0),
    Segment(Label.MENU_SPEAKING, 5.0),  # a second prompt the model can't act on
    Segment(Label.SILENCE, 4.0),
    Segment(Label.LIVE_SPEECH_CANDIDATE, 10.0),  # a person picks up
]


def test_musicless_queue_still_reaches_the_probe_and_bridges():
    settings = get_settings()
    pcm, schedule = synthesize_call(_MUSICLESS_QUEUE)
    sink = CollectingSink()
    session = AgentSession(
        "musicless",
        ScriptedClassifier(schedule),
        FakeTier2(menu_digits=None, probe_is_human=True),  # never a digit; the human is real
        telemetry=sink,
        settings=settings,
        goal="reach a human",
    )
    for frame in iter_frames(pcm, settings.frame_ms):
        session.feed_audio(frame)
        if session.finished:
            break

    states = [e.payload()["to"] for e in sink.of_kind("state_changed")]
    assert "ON_HOLD" in states and "EVALUATING_SPEECH" in states
    assert [e.payload()["outcome"] for e in sink.of_kind("call_completed")] == ["human_bridged"]
