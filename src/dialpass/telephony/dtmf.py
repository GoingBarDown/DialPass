"""DTMF (touch-tone) synthesis.

Each key is two simultaneous sine tones. The open question for M4 is *injection*
(Twilio REST `sendDigits` only fires at call setup/redirect, not mid-call) — but
generating the tones is pure DSP and done here so M4 only has to solve delivery.
"""

from __future__ import annotations

import numpy as np

_LOW = {
    "1": 697,
    "2": 697,
    "3": 697,
    "A": 697,
    "4": 770,
    "5": 770,
    "6": 770,
    "B": 770,
    "7": 852,
    "8": 852,
    "9": 852,
    "C": 852,
    "*": 941,
    "0": 941,
    "#": 941,
    "D": 941,
}
_HIGH = {
    "1": 1209,
    "2": 1336,
    "3": 1477,
    "A": 1633,
    "4": 1209,
    "5": 1336,
    "6": 1477,
    "B": 1633,
    "7": 1209,
    "8": 1336,
    "9": 1477,
    "C": 1633,
    "*": 1209,
    "0": 1336,
    "#": 1477,
    "D": 1633,
}


def dtmf_frequencies(digit: str) -> tuple[int, int]:
    d = digit.upper()
    if d not in _LOW:
        raise ValueError(f"not a DTMF key: {digit!r}")
    return _LOW[d], _HIGH[d]


def dtmf_tone(
    digit: str,
    *,
    tone_ms: int = 180,
    gap_ms: int = 60,
    sample_rate: int = 8000,
    amplitude: float = 0.25,
) -> np.ndarray:
    """One key press: tone then an inter-digit gap. int16, mono."""
    lo, hi = dtmf_frequencies(digit)
    n = int(sample_rate * tone_ms / 1000)
    t = np.arange(n) / sample_rate
    wave = amplitude * (np.sin(2 * np.pi * lo * t) + np.sin(2 * np.pi * hi * t)) / 2.0
    # short raised-cosine ramps to avoid click artifacts
    ramp = min(int(sample_rate * 0.005), n // 2)
    if ramp:
        env = np.ones(n)
        env[:ramp] = np.linspace(0, 1, ramp)
        env[-ramp:] = np.linspace(1, 0, ramp)
        wave *= env
    gap = np.zeros(int(sample_rate * gap_ms / 1000))
    return np.concatenate([(wave * 32767).astype(np.int16), gap.astype(np.int16)])


def dtmf_sequence(digits: str, *, sample_rate: int = 8000, **kw) -> np.ndarray:
    parts = [dtmf_tone(d, sample_rate=sample_rate, **kw) for d in digits if not d.isspace()]
    if not parts:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(parts)
