"""AgentSession — one live call.

Owns the rolling buffer, runs Tier 1 on a fixed cadence (driven by the audio
clock, not wall-clock, so it's deterministic and testable), drives the FSM, and
dispatches the FSM's actions to Tier 2 / telephony / telemetry.

M1 stubs the outward effects: WAKE_TIER2_* and BRIDGE emit telemetry and call the
fake Tier 2, but no DTMF is injected and no conference is manipulated. M4/M5 wire
those to `telephony/`.
"""

from __future__ import annotations

import logging

import numpy as np

from ..config import Settings, get_settings
from ..telemetry.events import (
    BridgeStarted,
    CallCompleted,
    CallFailed,
    DtmfSent,
    FrameClassified,
    HumanDetected,
    ProbeResult,
    StateChanged,
    Tier2Woken,
)
from ..telemetry.publisher import NullSink, TelemetrySink
from .buffer import RollingBuffer
from .classifier import Classifier, Frame
from .state import Action, CallState, CallStateMachine, FsmConfig

log = logging.getLogger("dialpass.agent")


class AgentSession:
    def __init__(
        self,
        call_id: str,
        classifier: Classifier,
        tier2,
        *,
        telemetry: TelemetrySink | None = None,
        settings: Settings | None = None,
        fsm_config: FsmConfig | None = None,
        goal: str | None = None,
    ) -> None:
        self.call_id = call_id
        self.settings = settings or get_settings()
        self.classifier = classifier
        self.tier2 = tier2
        self.telemetry = telemetry or NullSink()
        self.goal = goal

        self.fsm = CallStateMachine(fsm_config)
        self.buffer = RollingBuffer(self.settings.buffer_seconds, self.settings.sample_rate)

        self._samples_seen = 0
        self._next_classify_at = 0.0
        self.finished = False

    # -- properties -------------------------------------------------------
    @property
    def state(self) -> CallState:
        return self.fsm.state

    @property
    def audio_seconds(self) -> float:
        return self._samples_seen / self.settings.sample_rate

    # -- audio ingest ----------------------------------------------------
    def feed_audio(self, pcm: np.ndarray) -> None:
        """Called with each decoded PCM chunk (mono int16, 8 kHz)."""
        if self.finished:
            return
        self.buffer.write(pcm)
        self._samples_seen += int(pcm.size)

        interval = self.settings.classifier_interval_ms / 1000.0
        while self.audio_seconds >= self._next_classify_at:
            self._run_tick(self._next_classify_at)
            self._next_classify_at += interval
            if self.finished:
                return

    def _run_tick(self, now: float) -> None:
        frame = Frame(
            pcm=self.buffer.tail(self.settings.classify_window_ms),
            sample_rate=self.settings.sample_rate,
            t_start=now,
        )
        c = self.classifier.classify(frame)
        self.telemetry.emit(
            FrameClassified(
                call_id=self.call_id,
                t=now,
                label=c.label.value,
                confidence=round(c.confidence, 3),
                features={k: round(v, 5) for k, v in c.features.items()},
            )
        )

        before = self.fsm.state
        action = self.fsm.observe(c.label, c.confidence, now)
        self._emit_state_change(before, now)
        self._dispatch(action, now)

    # -- FSM action dispatch -------------------------------------------
    def _dispatch(self, action: Action, now: float) -> None:
        if action == Action.NONE:
            return
        if action == Action.WAKE_TIER2_MENU:
            self._handle_menu(now)
        elif action == Action.WAKE_TIER2_PROBE:
            self._handle_probe(now)
        elif action == Action.BRIDGE:
            self._bridge(now)
        elif action == Action.FAIL:
            self._fail(now, reason="non_connect")

    def _handle_menu(self, now: float) -> None:
        self.telemetry.emit(Tier2Woken(call_id=self.call_id, t=now, reason="menu"))
        try:
            decision = self.tier2.choose_menu_digit(
                self.buffer.snapshot(), self.settings.sample_rate, self.goal
            )
        except NotImplementedError:
            # Real Tier 2 (M4) isn't wired yet — don't crash a live call over it.
            self._fail(now, reason="tier2_not_implemented")
            return
        if decision.digits:
            self.telemetry.emit(DtmfSent(call_id=self.call_id, t=now, digits=decision.digits))
            # M4: synthesize dual-tone PCM and inject into the outbound stream.

    def _handle_probe(self, now: float) -> None:
        self.telemetry.emit(Tier2Woken(call_id=self.call_id, t=now, reason="probe"))
        try:
            outcome = self.tier2.probe(self.buffer.snapshot(), self.settings.sample_rate)
        except NotImplementedError:
            self._fail(now, reason="tier2_not_implemented")
            return
        self.telemetry.emit(ProbeResult(call_id=self.call_id, t=now, is_human=outcome.is_human))
        before = self.fsm.state
        follow_up = self.fsm.probe_result(outcome.is_human, now)
        self._emit_state_change(before, now)
        self._dispatch(follow_up, now)

    def _bridge(self, now: float) -> None:
        self.telemetry.emit(HumanDetected(call_id=self.call_id, t=now))
        try:
            self.tier2.say_to_agent("Thanks for picking up — connecting my client now, one moment.")
        except NotImplementedError:
            self._fail(now, reason="tier2_not_implemented")
            return
        before = self.fsm.state
        self.fsm.bridged()
        self._emit_state_change(before, now)
        self.telemetry.emit(BridgeStarted(call_id=self.call_id, t=now))
        # M5: notify the user, wait a beat, stop forwarding AI audio, unmute the
        # user's conference leg.
        before = self.fsm.state
        self.fsm.completed()
        self._emit_state_change(before, now)
        self.telemetry.emit(CallCompleted(call_id=self.call_id, t=now, outcome="human_bridged"))
        self.finished = True

    def _fail(self, now: float, reason: str) -> None:
        self.telemetry.emit(CallFailed(call_id=self.call_id, t=now, reason=reason))
        self.finished = True

    def _emit_state_change(self, before: CallState, now: float) -> None:
        if self.fsm.state != before:
            self.telemetry.emit(
                StateChanged(call_id=self.call_id, t=now, frm=before.value, to=self.fsm.state.value)
            )
            log.info("call %s: %s -> %s", self.call_id, before.value, self.fsm.state.value)
