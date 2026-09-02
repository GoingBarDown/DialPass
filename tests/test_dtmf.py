import numpy as np

from dialpass.telephony.dtmf import dtmf_frequencies, dtmf_sequence, dtmf_tone


def _dominant_freqs(sig: np.ndarray, sample_rate: int, n: int = 2) -> list[int]:
    spec = np.abs(np.fft.rfft(sig.astype(float)))
    freqs = np.fft.rfftfreq(sig.size, 1 / sample_rate)
    idx = np.argsort(spec)[-n:]
    return sorted(int(round(freqs[i])) for i in idx)


def test_tone_contains_both_dtmf_frequencies():
    lo, hi = dtmf_frequencies("5")
    tone = dtmf_tone("5", gap_ms=0, sample_rate=8000)
    found = _dominant_freqs(tone, 8000)
    assert min(abs(found[0] - lo), abs(found[0] - hi)) < 15
    assert min(abs(found[1] - lo), abs(found[1] - hi)) < 15


def test_sequence_length_scales_with_digits():
    one = dtmf_tone("1", sample_rate=8000)
    three = dtmf_sequence("123", sample_rate=8000)
    assert abs(three.size - 3 * one.size) <= 3


def test_rejects_non_dtmf():
    try:
        dtmf_frequencies("Z")
    except ValueError:
        return
    raise AssertionError("expected ValueError")
