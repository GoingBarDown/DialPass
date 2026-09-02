"""Offline Tier 2. Deterministic, no network — used by the harness and tests."""

from __future__ import annotations

import numpy as np

from .protocol import MenuDecision, ProbeOutcome


class FakeTier2:
    def __init__(
        self,
        *,
        menu_digits: str | None = "2",
        probe_is_human: bool = True,
    ) -> None:
        self._menu_digits = menu_digits
        self._probe_is_human = probe_is_human
        self.calls: list[str] = []

    def choose_menu_digit(
        self, audio: np.ndarray, sample_rate: int, goal: str | None
    ) -> MenuDecision:
        self.calls.append("choose_menu_digit")
        return MenuDecision(self._menu_digits, rationale="fake: fixed digit")

    def probe(self, audio: np.ndarray, sample_rate: int) -> ProbeOutcome:
        self.calls.append("probe")
        return ProbeOutcome(self._probe_is_human, transcript="fake")

    def say_to_agent(self, text: str) -> None:
        self.calls.append(f"say:{text}")

    def close(self) -> None:
        self.calls.append("close")
