"""Tier 1 — the always-on classifier.

`Classifier` is the interface the FSM depends on. Two implementations ship:

* `HeuristicClassifier` — cheap DSP features + hand rules. Real but rough; M3
  replaces the rules with VAD, tone templates, local ASR keyword spotting, and
  per-destination priors, and later a learned model behind the same interface.
* `ScriptedClassifier` — a test double that replays a known label schedule, so
  the FSM / session / telemetry pipeline can be exercised end to end without
  depending on M3's signal processing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from .labels import Label


@dataclass(slots=True)
class Frame:
    """One analysis window handed to the classifier."""

    pcm: np.ndarray  # int16 mono
    sample_rate: int
    t_start: float  # seconds since call start


@dataclass(slots=True)
class Classification:
    label: Label
    confidence: float
    features: dict[str, float] = field(default_factory=dict)


class Classifier(Protocol):
    def classify(self, frame: Frame) -> Classification: ...


# --------------------------------------------------------------------------
# Feature extraction
# --------------------------------------------------------------------------
def acoustic_features(pcm: np.ndarray, sample_rate: int) -> dict[str, float]:
    """Cheap frame-level features. Everything here is O(n log n) or better."""
    if pcm.size == 0:
        return {"rms": 0.0, "zcr": 0.0, "flatness": 1.0, "tone_460_ratio": 0.0}

    x = pcm.astype(np.float64) / 32768.0
    rms = float(np.sqrt(np.mean(x**2)))
    zcr = float(np.mean(np.abs(np.diff(np.sign(x))) > 0))

    window = np.hanning(x.size)
    spectrum = np.abs(np.fft.rfft(x * window)) ** 2
    spectrum = np.maximum(spectrum, 1e-12)
    # Spectral flatness: geometric mean / arithmetic mean. ~1 for noise/broadband
    # speech, near 0 for tonal music / pure tones.
    flatness = float(np.exp(np.mean(np.log(spectrum))) / np.mean(spectrum))

    freqs = np.fft.rfftfreq(x.size, 1.0 / sample_rate)
    total = float(np.sum(spectrum)) or 1.0
    # US ringback = 440 + 480 Hz; energy clustered there is a strong ringback cue.
    ring_band = float(np.sum(spectrum[(freqs >= 400) & (freqs <= 520)])) / total

    return {"rms": rms, "zcr": zcr, "flatness": flatness, "tone_460_ratio": ring_band}


class HeuristicClassifier:
    """Rough hand rules over `acoustic_features`. Tuned properly in M3."""

    SILENCE_RMS = 0.005

    def classify(self, frame: Frame) -> Classification:
        f = acoustic_features(frame.pcm, frame.sample_rate)
        rms, zcr, flatness, ring = (
            f["rms"],
            f["zcr"],
            f["flatness"],
            f["tone_460_ratio"],
        )

        if rms < self.SILENCE_RMS:
            return Classification(Label.SILENCE, 0.9, f)
        if ring > 0.6 and flatness < 0.1:
            return Classification(Label.RINGBACK, 0.75, f)
        if flatness < 0.15:
            # tonal + sustained energy -> music bed
            return Classification(Label.HOLD_MUSIC, 0.6, f)
        if zcr > 0.08 and flatness > 0.2:
            # broadband, gappy -> speech; Tier 1 alone can't tell menu from human
            return Classification(Label.LIVE_SPEECH_CANDIDATE, 0.55, f)
        return Classification(Label.UNKNOWN, 0.4, f)


@dataclass(slots=True)
class ScheduledLabel:
    label: Label
    t0: float
    t1: float


class ScriptedClassifier:
    """Replays a fixed schedule. Test double only."""

    def __init__(self, schedule: list[ScheduledLabel], confidence: float = 0.95) -> None:
        self._schedule = sorted(schedule, key=lambda s: s.t0)
        self._confidence = confidence

    def classify(self, frame: Frame) -> Classification:
        for item in self._schedule:
            if item.t0 <= frame.t_start < item.t1:
                return Classification(item.label, self._confidence, {})
        return Classification(Label.UNKNOWN, 0.3, {})
