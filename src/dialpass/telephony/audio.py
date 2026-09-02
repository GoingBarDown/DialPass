"""G.711 mu-law <-> PCM16 and sample-rate conversion.

Pure functions, no I/O. Implemented on numpy (no stdlib `audioop`, which was
removed in Python 3.13). Twilio Media Streams carry 8 kHz mono mu-law in 20 ms
frames; gpt-realtime works in 24 kHz PCM, hence `resample`.
"""

from __future__ import annotations

import numpy as np


def _build_ulaw_decode_table() -> np.ndarray:
    table = np.zeros(256, dtype=np.int16)
    for i in range(256):
        u = (~i) & 0xFF
        sign = u & 0x80
        exponent = (u >> 4) & 0x07
        mantissa = u & 0x0F
        magnitude = (((mantissa << 3) + 0x84) << exponent) - 0x84
        table[i] = -magnitude if sign else magnitude
    return table


_ULAW_DECODE = _build_ulaw_decode_table()
# Sorted decode levels + the byte that produces each — lets us encode by nearest
# quantization level, which is provably correct and simple.
_LEVELS = np.sort(_ULAW_DECODE.astype(np.int32))
_LEVEL_TO_BYTE = np.argsort(_ULAW_DECODE.astype(np.int32)).astype(np.uint8)

ULAW_SILENCE = 0xFF  # mu-law byte for PCM 0


def ulaw_to_pcm16(data: bytes | bytearray) -> np.ndarray:
    """mu-law bytes -> int16 PCM samples."""
    u = np.frombuffer(bytes(data), dtype=np.uint8)
    return _ULAW_DECODE[u].astype(np.int16)


def pcm16_to_ulaw(samples: np.ndarray) -> bytes:
    """int16 PCM samples -> mu-law bytes (nearest-level quantization)."""
    s = np.clip(np.asarray(samples), -32768, 32767).astype(np.int32)
    idx = np.searchsorted(_LEVELS, s)
    idx = np.clip(idx, 0, len(_LEVELS) - 1)
    lo = np.clip(idx - 1, 0, len(_LEVELS) - 1)
    take_lo = np.abs(s - _LEVELS[lo]) <= np.abs(s - _LEVELS[idx])
    chosen = np.where(take_lo, lo, idx)
    out = _LEVEL_TO_BYTE[chosen]
    # Bytes 0x7F and 0xFF both decode to PCM 0; collapse to 0xFF, the
    # telephony-standard idle byte.
    out = np.where(out == 0x7F, np.uint8(ULAW_SILENCE), out)
    return out.tobytes()


def resample(pcm: np.ndarray, src_hz: int, dst_hz: int) -> np.ndarray:
    """Linear-interpolation resample. Fine for M1/M2; swap for polyphase if the
    quality shows up in ASR accuracy."""
    if src_hz == dst_hz or pcm.size == 0:
        return pcm.astype(np.int16)
    n_out = int(round(pcm.size * dst_hz / src_hz))
    x_old = np.arange(pcm.size)
    x_new = np.linspace(0.0, pcm.size - 1, n_out)
    return np.interp(x_new, x_old, pcm).astype(np.int16)
