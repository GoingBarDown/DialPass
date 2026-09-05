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
_EMPTY_FEATURES = {
    "rms": 0.0,
    "zcr": 0.0,
    "flatness": 1.0,
    "tone_460_ratio": 0.0,
    "quiet_frac": 1.0,
    "env_cv": 0.0,
    "mod_4hz": 0.0,
}


def acoustic_features(pcm: np.ndarray, sample_rate: int) -> dict[str, float]:
    """Cheap frame-level features. Everything here is O(n log n) or better."""
    if pcm.size == 0:
        return dict(_EMPTY_FEATURES)

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

    # --- temporal envelope --------------------------------------------------
    # Static spectra can't separate phone-band speech from a music bed (both are
    # low-flatness). The amplitude envelope can: speech pulses at the syllable
    # rate with near-silent gaps between words; sustained music runs steadier.
    # Sub-frame RMS at 25 ms, then: fraction of the window that's near-silent
    # (quiet_frac), envelope coefficient of variation (env_cv), and the share of
    # envelope energy in the 3-8 Hz syllabic / beat band (mod_4hz).
    hop = max(1, int(sample_rate * 0.025))
    n_sub = int(x.size // hop)
    if n_sub >= 4:
        sub = np.sqrt(np.mean(x[: n_sub * hop].reshape(n_sub, hop) ** 2, axis=1) + 1e-12)
        peak = float(sub.max())
        quiet_frac = float(np.mean(sub < 0.2 * peak)) if peak > 1e-6 else 1.0
        env_cv = float(sub.std() / (sub.mean() + 1e-9))
        env = sub - sub.mean()
        env_spec = np.abs(np.fft.rfft(env)) ** 2
        env_freqs = np.fft.rfftfreq(env.size, d=hop / sample_rate)
        syllabic = (env_freqs >= 3.0) & (env_freqs <= 8.0)
        mod_4hz = float(np.sum(env_spec[syllabic]) / (np.sum(env_spec) + 1e-12))
    else:
        quiet_frac, env_cv, mod_4hz = 1.0, 0.0, 0.0

    return {
        "rms": rms,
        "zcr": zcr,
        "flatness": flatness,
        "tone_460_ratio": ring_band,
        "quiet_frac": quiet_frac,
        "env_cv": env_cv,
        "mod_4hz": mod_4hz,
    }


class HeuristicClassifier:
    """Hand rules over `acoustic_features`, tuned in M3 against recorded real
    calls (Delta IVR + ringback, live human speech, phone-band hold music).

    Tier 1 only makes the coarse acoustic call — silence / ringback / music /
    speech-candidate. Telling a recorded menu voice from a live human is left to
    the Tier 2 probe; both look the same to these features.
    """

    SILENCE_RMS = 0.01

    def classify(self, frame: Frame) -> Classification:
        f = acoustic_features(frame.pcm, frame.sample_rate)
        rms, flatness, ring = f["rms"], f["flatness"], f["tone_460_ratio"]
        quiet, env_cv, mod = f["quiet_frac"], f["env_cv"], f["mod_4hz"]

        if rms < self.SILENCE_RMS:
            return Classification(Label.SILENCE, 0.9, f)

        # US ringback = 440 + 480 Hz: energy tightly in-band and near-pure tone.
        # The ring-band gate alone excludes music and speech (both well under 0.6).
        if ring > 0.6 and flatness < 0.1:
            return Classification(Label.RINGBACK, 0.8, f)

        # Speech vs music bed — decided on the envelope, not the spectrum.
        # Speech: near-silent gaps between syllables (high quiet_frac / env_cv).
        # Music bed: steady, and this style carries a strong beat (high mod_4hz).
        musicky = mod >= 0.5 and quiet < 0.32 and env_cv < 0.75
        speechy = quiet >= 0.25 or env_cv >= 0.62

        if musicky:
            return Classification(Label.HOLD_MUSIC, 0.65, f)
        if speechy:
            return Classification(Label.LIVE_SPEECH_CANDIDATE, 0.6, f)
        if flatness < 0.08 and env_cv < 0.6:
            # steady + tonal, no syllabic structure -> music bed
            return Classification(Label.HOLD_MUSIC, 0.55, f)
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
