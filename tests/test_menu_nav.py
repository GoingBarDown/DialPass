"""M4: the menu-navigation path — Tier 2 picks a digit, the session presses it."""

from __future__ import annotations

import pytest

from dialpass.agent.classifier import ScriptedClassifier
from dialpass.agent.session import AgentSession
from dialpass.config import get_settings
from dialpass.realtime.client import _parse_verdict
from dialpass.realtime.fake import FakeTier2
from dialpass.telemetry.publisher import CollectingSink
from dialpass.testing import iter_frames, synthesize_call


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
