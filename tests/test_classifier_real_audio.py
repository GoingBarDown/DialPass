"""M3 regression: the heuristic classifier on real recorded phone audio.

Fixtures are short clips pulled from real Twilio calls placed during M3 tuning:
US ringback + IVR (Delta), phone-band hold music, and live human speech. These
guard the speech-vs-music separation that M3 added the envelope features for —
before M3 every one of these clips came back HOLD_MUSIC.
"""

from __future__ import annotations

import wave
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from dialpass.agent.classifier import Frame, HeuristicClassifier
from dialpass.agent.labels import Label
from dialpass.config import get_settings

FIXTURES = Path(__file__).parent / "fixtures"


def _labels_over(path: Path) -> Counter[Label]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    s = get_settings()
    win = int(sr * s.classify_window_ms / 1000)
    hop = int(sr * s.classifier_interval_ms / 1000)
    clf = HeuristicClassifier()
    counts: Counter[Label] = Counter()
    for start in range(0, max(1, pcm.size - win + 1), hop):
        seg = pcm[start : start + win]
        counts[clf.classify(Frame(pcm=seg, sample_rate=sr, t_start=start / sr)).label] += 1
    return counts


def _dominant_share(counts: Counter[Label], label: Label) -> float:
    return counts[label] / sum(counts.values())


def test_ringback_fixture_detects_ringback_not_music():
    counts = _labels_over(FIXTURES / "ringback.wav")
    assert counts[Label.RINGBACK] >= 1
    assert counts[Label.HOLD_MUSIC] == 0  # a pure tone must not read as a music bed


def test_hold_music_fixture_reads_as_music_not_speech():
    counts = _labels_over(FIXTURES / "hold_music.wav")
    assert _dominant_share(counts, Label.HOLD_MUSIC) >= 0.7
    assert _dominant_share(counts, Label.LIVE_SPEECH_CANDIDATE) <= 0.2


def test_human_speech_fixture_reads_as_speech_not_music():
    counts = _labels_over(FIXTURES / "human_speech.wav")
    assert _dominant_share(counts, Label.LIVE_SPEECH_CANDIDATE) >= 0.75
    assert _dominant_share(counts, Label.HOLD_MUSIC) <= 0.2


@pytest.mark.parametrize("name", ["ringback.wav", "hold_music.wav", "human_speech.wav"])
def test_fixture_present(name):
    assert (FIXTURES / name).is_file()
