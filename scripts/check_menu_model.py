"""Manual check: does Tier 2 pick sane digits from a menu clip?

Hits the real OpenAI Realtime API (costs a few cents, non-deterministic) so it's
a script, not a pytest test. Run it after touching realtime/client.py or the
menu prompt.

    uv run python scripts/check_menu_model.py                  # bundled fixture
    uv run python scripts/check_menu_model.py recordings/x.wav "my goal"
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

from dialpass.config import get_settings
from dialpass.realtime.client import RealtimeClient
from dialpass.telephony.audio import resample

FIXTURE = Path(__file__).parent.parent / "tests" / "fixtures" / "menu_prompt.wav"
# menu_prompt.wav says: billing -> 1, existing flight status -> 3, reservations -> 5, repeat -> 9
DEFAULT_CASES = [
    ("check the status of an existing flight", "3"),
    ("make a new reservation", "5"),
    ("a billing problem", "1"),
]


def _load_8k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return resample(pcm, sr, 8000)


def main(argv: list[str]) -> int:
    s = get_settings()
    if not s.openai_api_key:
        print("no DIALPASS_OPENAI_API_KEY set")
        return 1
    client = RealtimeClient(s.openai_api_key, menu_model=s.realtime_menu_model)

    if len(argv) >= 3:
        pcm = _load_8k(Path(argv[1]))
        d = client.choose_menu_digit(pcm, 8000, goal=argv[2])
        print(f"digit={d.digits!r}  rationale={d.rationale}")
        return 0

    pcm = _load_8k(FIXTURE)
    ok = 0
    for goal, want in DEFAULT_CASES:
        d = client.choose_menu_digit(pcm, 8000, goal=goal)
        hit = d.digits == want
        ok += hit
        mark = "ok " if hit else "MISS"
        print(f"{mark} goal={goal!r:45} got={d.digits!r} want={want!r}")
        print(f"     {d.rationale}")
    print(f"\n{ok}/{len(DEFAULT_CASES)} correct")
    return 0 if ok == len(DEFAULT_CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
