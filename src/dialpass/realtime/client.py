"""gpt-realtime-mini over WebSocket. Lands in M4.

Connection model (see docs/design-decisions.md): keep the socket open for the
whole call, stop forwarding audio into it during hold (no audio -> no tokens ->
no cost), resume on wake. Handle OpenAI's idle-socket disconnect as a normal
event and reconnect on the next wake.
"""

from __future__ import annotations

import numpy as np

from .protocol import MenuDecision, ProbeOutcome


class RealtimeClient:
    def __init__(self, api_key: str, model: str = "gpt-realtime-mini") -> None:
        self._api_key = api_key
        self._model = model

    def choose_menu_digit(
        self, audio: np.ndarray, sample_rate: int, goal: str | None
    ) -> MenuDecision:
        raise NotImplementedError("Tier 2 menu navigation lands in M4")

    def probe(self, audio: np.ndarray, sample_rate: int) -> ProbeOutcome:
        raise NotImplementedError("Tier 2 probe lands in M4/M5")

    def say_to_agent(self, text: str) -> None:
        raise NotImplementedError("Tier 2 speech lands in M5")

    def close(self) -> None:
        pass
