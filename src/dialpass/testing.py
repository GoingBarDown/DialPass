"""Synthetic call audio for the offline harness and tests.

`synthesize_call()` builds a full call — ringback, an IVR menu, hold music, a
"please continue to hold" interjection, then a human picking up — as 8 kHz mono
int16 PCM, plus the ground-truth label schedule for `ScriptedClassifier`.

The waveforms are crude but have the right acoustic character (tonal vs.
broadband vs. silent) so `HeuristicClassifier` also produces something sensible.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .agent.classifier import ScheduledLabel
from .agent.labels import Label

SAMPLE_RATE = 8000


@dataclass(slots=True)
class Segment:
    label: Label
    seconds: float


# The call script. Order matters; durations are chosen so the FSM's debounce
# thresholds are comfortably exceeded at a 500 ms tick.
DEFAULT_SCRIPT: list[Segment] = [
    Segment(Label.RINGBACK, 4.0),
    Segment(Label.MENU_SPEAKING, 5.0),
    Segment(Label.MENU_AWAITING_INPUT, 1.5),
    Segment(Label.SILENCE, 1.0),
    Segment(Label.HOLD_MUSIC, 10.0),
    Segment(Label.HOLD_INTERJECTION, 2.5),
    Segment(Label.HOLD_MUSIC, 8.0),
    Segment(Label.LIVE_SPEECH_CANDIDATE, 6.0),
]


def _tone(freqs: list[float], seconds: float, amp: float = 0.2) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    wave = np.zeros_like(t)
    for f in freqs:
        wave += np.sin(2 * np.pi * f * t)
    return (amp / len(freqs)) * wave


def _speech_like(seconds: float, amp: float = 0.25, syllable_hz: float = 4.0) -> np.ndarray:
    n = int(seconds * SAMPLE_RATE)
    rng = np.random.default_rng(len(str(seconds)))
    noise = rng.standard_normal(n)
    # crude bandpass 300-3400 Hz via FFT mask
    spec = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(n, 1 / SAMPLE_RATE)
    spec[(freqs < 300) | (freqs > 3400)] = 0
    band = np.fft.irfft(spec, n)
    band /= np.max(np.abs(band)) or 1.0
    # Syllabic envelope with real gaps: alternating voiced bursts and short
    # near-silent pauses at ~syllable_hz. This is what separates speech from a
    # sustained music bed for the Tier 1 envelope features (quiet_frac, env_cv).
    mean_syl = SAMPLE_RATE / syllable_hz
    envelope = np.zeros(n)
    i = 0
    while i < n:
        burst = int(rng.uniform(0.45, 0.9) * mean_syl)
        gap = int(rng.uniform(0.25, 0.8) * mean_syl)
        j = min(n, i + burst)
        ramp = np.minimum(np.arange(j - i), (j - i) - np.arange(j - i)) / max(1, 0.15 * mean_syl)
        envelope[i:j] = np.clip(ramp, 0.0, 1.0)
        i = j + gap
    return amp * band * envelope


def _segment_wave(seg: Segment) -> np.ndarray:
    if seg.label == Label.RINGBACK:
        # 440+480 Hz, 2 s on / 4 s off (US ringback), scaled to fit the segment
        on = _tone([440, 480], min(2.0, seg.seconds))
        off = np.zeros(int(max(0.0, seg.seconds - 2.0) * SAMPLE_RATE))
        return np.concatenate([on, off])
    if seg.label in (Label.MENU_SPEAKING, Label.LIVE_SPEECH_CANDIDATE):
        return _speech_like(seg.seconds)
    if seg.label == Label.HOLD_INTERJECTION:
        # speech over a faint music bed
        return _speech_like(seg.seconds, amp=0.22) + _tone([220, 277, 330], seg.seconds, amp=0.05)
    if seg.label == Label.HOLD_MUSIC:
        chord = _tone([220, 277, 330, 440], seg.seconds, amp=0.3)
        trem = 0.5 * (1 + np.sin(2 * np.pi * 5.0 * np.arange(chord.size) / SAMPLE_RATE))
        return chord * trem
    if seg.label == Label.MENU_AWAITING_INPUT:
        return _speech_like(seg.seconds, amp=0.02)
    return np.zeros(int(seg.seconds * SAMPLE_RATE))  # SILENCE


def synthesize_call(
    script: list[Segment] | None = None,
) -> tuple[np.ndarray, list[ScheduledLabel]]:
    script = script or DEFAULT_SCRIPT
    chunks: list[np.ndarray] = []
    schedule: list[ScheduledLabel] = []
    t = 0.0
    for seg in script:
        wave = _segment_wave(seg)
        chunks.append(wave)
        schedule.append(ScheduledLabel(seg.label, t, t + seg.seconds))
        t += seg.seconds
    pcm = np.clip(np.concatenate(chunks) * 32767, -32768, 32767).astype(np.int16)
    return pcm, schedule


def iter_frames(pcm: np.ndarray, frame_ms: int = 20) -> list[np.ndarray]:
    n = int(SAMPLE_RATE * frame_ms / 1000)
    return [pcm[i : i + n] for i in range(0, pcm.size, n)]
