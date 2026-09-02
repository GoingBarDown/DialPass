import numpy as np

from dialpass.telephony.audio import (
    ULAW_SILENCE,
    pcm16_to_ulaw,
    resample,
    ulaw_to_pcm16,
)


def test_ulaw_levels_round_trip_exactly():
    # Every one of the 256 decodable levels must re-encode to itself.
    all_bytes = bytes(range(256))
    pcm = ulaw_to_pcm16(all_bytes)
    re_encoded = pcm16_to_ulaw(pcm)
    assert ulaw_to_pcm16(re_encoded).tolist() == pcm.tolist()


def test_silence_encodes_to_silence_byte():
    out = pcm16_to_ulaw(np.zeros(10, dtype=np.int16))
    assert set(out) == {ULAW_SILENCE}


def test_pcm_round_trip_is_close():
    rng = np.random.default_rng(0)
    pcm = (rng.standard_normal(4000) * 8000).astype(np.int16)
    back = ulaw_to_pcm16(pcm16_to_ulaw(pcm))
    # mu-law is lossy but log-companded; error stays small relative to amplitude
    assert np.mean(np.abs(back.astype(int) - pcm.astype(int))) < 200


def test_resample_changes_length_proportionally():
    pcm = np.zeros(8000, dtype=np.int16)
    assert resample(pcm, 8000, 24000).size == 24000
    assert resample(pcm, 8000, 8000).size == 8000
