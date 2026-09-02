"""Tier 2 interface — what the agent asks of the realtime model, vendor-neutral.

`agent/` depends only on `Tier2` here. `client.RealtimeClient` (gpt-realtime-mini)
and `fake.FakeTier2` implement it. A Deepgram/STT->LLM->TTS pipeline could slot in
behind the same interface if cost ever matters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(slots=True)
class MenuDecision:
    digits: str | None  # DTMF to press, or None if nothing sensible
    rationale: str = ""


@dataclass(slots=True)
class ProbeOutcome:
    is_human: bool
    transcript: str = ""


class Tier2(Protocol):
    def choose_menu_digit(
        self, audio: np.ndarray, sample_rate: int, goal: str | None
    ) -> MenuDecision:
        """Listen to a menu prompt, decide which key advances toward a human."""
        ...

    def probe(self, audio: np.ndarray, sample_rate: int) -> ProbeOutcome:
        """Say 'Hello?' and decide whether the reply is a live human."""
        ...

    def say_to_agent(self, text: str) -> None:
        """Speak a holding line into the call (e.g. at handoff)."""
        ...

    def close(self) -> None: ...
