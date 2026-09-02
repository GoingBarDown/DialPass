"""Rolling PCM buffer — the last N seconds of call audio, mono int16."""

from __future__ import annotations

import numpy as np


class RollingBuffer:
    def __init__(self, seconds: float, sample_rate: int) -> None:
        self._max = int(seconds * sample_rate)
        self._rate = sample_rate
        self._data = np.zeros(0, dtype=np.int16)

    def write(self, pcm: np.ndarray) -> None:
        if pcm.dtype != np.int16:
            pcm = pcm.astype(np.int16)
        self._data = np.concatenate([self._data, pcm])[-self._max :]

    def tail(self, ms: int) -> np.ndarray:
        n = int(self._rate * ms / 1000)
        return self._data[-n:].copy()

    def snapshot(self) -> np.ndarray:
        return self._data.copy()

    def __len__(self) -> int:
        return int(self._data.size)
