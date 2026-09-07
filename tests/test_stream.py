"""Streaming Tier 2 — verdict parsing and the StreamingProbe wrapper."""

from __future__ import annotations

import numpy as np
import pytest

from dialpass.realtime.protocol import MenuDecision, ProbeOutcome
from dialpass.realtime.stream import StreamingProbe, verdict_from_text


@pytest.mark.parametrize(
    "text, is_human",
    [
        ("VERDICT HUMAN", True),
        ("VERDICT NOT HUMAN", False),
        ("Hi, is someone there? VERDICT HUMAN", True),
        ("...static... verdict not human", False),
        ("Verdict:  Human", True),  # loose spacing / casing
    ],
)
def test_verdict_parsing(text, is_human):
    assert verdict_from_text(text).is_human is is_human


@pytest.mark.parametrize("text", ["", "uh, hello?", "I couldn't tell", "VERDICT MAYBE"])
def test_verdict_defaults_to_not_human_when_unclear(text):
    # never bridge the user into hold music on an ambiguous read
    assert verdict_from_text(text).is_human is False


class _InnerSpy:
    def __init__(self):
        self.calls = []

    def choose_menu_digit(self, audio, sample_rate, goal):
        self.calls.append("menu")
        return MenuDecision("3", "inner")

    def probe(self, audio, sample_rate):
        self.calls.append("inner-probe")
        return ProbeOutcome(is_human=True)

    def say_to_agent(self, text):
        self.calls.append(f"say:{text}")

    def close(self):
        self.calls.append("close")


class _DoneFuture:
    def __init__(self, value):
        self._value = value

    def result(self, timeout=None):
        return self._value


def test_streaming_probe_delegates_menu_and_say_but_streams_probe():
    inner = _InnerSpy()
    ran = []

    def run_probe():
        ran.append(True)
        return _DoneFuture(ProbeOutcome(is_human=False, transcript="music"))

    sp = StreamingProbe(inner, run_probe)

    assert sp.choose_menu_digit(np.zeros(8), 8000, "goal").digits == "3"
    sp.say_to_agent("one moment")
    sp.close()
    assert inner.calls == ["menu", "say:one moment", "close"]
    assert "inner-probe" not in inner.calls

    outcome = sp.probe(np.zeros(8), 8000)
    assert ran == [True]
    assert outcome.is_human is False and outcome.transcript == "music"
