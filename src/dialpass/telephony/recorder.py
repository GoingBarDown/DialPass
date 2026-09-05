"""Incremental WAV writer for capturing a live call's audio.

Dev aid only (M3 classifier tuning): when `DIALPASS_RECORD_DIR` is set, the
media handler writes every decoded PCM frame here so we have real phone-band
audio to analyse and tune against. Not used in production.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np


class WavRecorder:
    def __init__(self, path: str | Path, sample_rate: int) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Held open for the call's lifetime and closed in close(); a context
        # manager doesn't fit the streaming write pattern here.
        self._wav = wave.open(str(self.path), "wb")  # noqa: SIM115
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)  # int16
        self._wav.setframerate(sample_rate)
        self._closed = False

    def write(self, pcm: np.ndarray) -> None:
        if self._closed:
            return
        self._wav.writeframes(np.asarray(pcm, dtype=np.int16).tobytes())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._wav.close()
