"""AgentSession — one live call.

Owns the rolling buffer, runs Tier 1 on a fixed cadence (driven by the audio
clock, not wall-clock, so it's deterministic and testable), drives the FSM, and
dispatches the FSM's actions to Tier 2 / telephony / telemetry.

M1 stubs the outward effects: WAKE_TIER2_* and BRIDGE emit telemetry and call the
fake Tier 2, but no DTMF is injected and no conference is manipulated. M4/M5 wire
those to `telephony/`.

Tier 2 calls (menu decision, probe) block for 1-2s, so they don't run on the
media loop — `_tier2_exec` runs them off to the side and `_poll_tier2` applies
the result on a later tick. See `agent/executor.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

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
from .executor import InlineExecutor, Tier2Executor
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
        dtmf_sender: Callable[[str], None] | None = None,
        tier2_executor: Tier2Executor | None = None,
        conference: str | None = None,
        user_leg_sid: str | None = None,
    ) -> None:
        self.call_id = call_id
        self.settings = settings or get_settings()
        self.classifier = classifier
        self.tier2 = tier2
        self.telemetry = telemetry or NullSink()
        self.goal = goal
        # Conference room this call is in, and the user's (Leg B) call SID. Set
        # on live calls; the handoff (M5 phase 3) unmutes user_leg_sid here.
        self.conference = conference
        self.user_leg_sid = user_leg_sid
        # Presses digits on the live call. Injected so agent/ stays vendor-free;
        # the media handler wires the Twilio-backed one. No-op offline.
        self.dtmf_sender: Callable[[str], None] = dtmf_sender or (lambda digits: None)
        # Runs the blocking Tier 2 calls. InlineExecutor (default) keeps the sim
        # and tests deterministic; the live handler passes a ThreadedExecutor.
        self._tier2_exec: Tier2Executor = tier2_executor or InlineExecutor()
        # "menu" | "probe" while a Tier 2 call is outstanding, else None. Blocks
        # a second wake until the first result lands.
        self._tier2_kind: str | None = None

        self.fsm = CallStateMachine(fsm_config)
        self.buffer = RollingBuffer(self.settings.buffer_seconds, self.settings.sample_rate)

        self._samples_seen = 0
        self._next_classify_at = 0.0
        self.finished = False
        # A menu wake is deferred by `menu_collect_s` so Tier 2 hears the whole
        # prompt — fast-answering IVRs start talking before the FSM even reaches
        # IVR_MENU. None = nothing pending.
        self._menu_wake_due: float | None = None

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
        # If we fell far behind — e.g. a blocking Tier 2 call froze ingestion for
        # seconds — don't replay a burst of ticks on near-identical buffer
        # contents (that manufactures a fake streak and stampedes the FSM). Skip
        # to the latest window and carry on.
        if self.audio_seconds - self._next_classify_at > 3 * interval:
            skipped = self.audio_seconds - interval - self._next_classify_at
            self._next_classify_at = self.audio_seconds - interval
            log.warning(
                "call %s: classifier fell %.1fs behind, skipping ahead", self.call_id, skipped
            )
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

        # fire a deferred menu wake once its collect window has elapsed (and
        # we're still in a menu, and no other Tier 2 call is in flight — a
        # hold/hangup or a still-running decision in the meantime cancels it)
        if self._menu_wake_due is not None and now >= self._menu_wake_due:
            self._menu_wake_due = None
            if self.fsm.state == CallState.IVR_MENU and self._tier2_kind is None:
                self._start_menu(now)

        self._poll_tier2(now)

    # -- FSM action dispatch -------------------------------------------
    def _dispatch(self, action: Action, now: float) -> None:
        if action == Action.NONE:
            return
        if action == Action.WAKE_TIER2_MENU:
            self._menu_wake_due = now + self.settings.menu_collect_s
        elif action == Action.WAKE_TIER2_PROBE:
            if self._tier2_kind is None:
                self._start_probe(now)
        elif action == Action.BRIDGE:
            self._bridge(now)
        elif action == Action.FAIL:
            self._fail(now, reason="non_connect")

    # -- Tier 2: start off the media loop, apply the result on a later tick --
    def _start_menu(self, now: float) -> None:
        self.telemetry.emit(Tier2Woken(call_id=self.call_id, t=now, reason="menu"))
        self._tier2_kind = "menu"
        snap = self.buffer.snapshot()
        sr, goal = self.settings.sample_rate, self.goal
        self._tier2_exec.submit(lambda: self.tier2.choose_menu_digit(snap, sr, goal))

    def _start_probe(self, now: float) -> None:
        self.telemetry.emit(Tier2Woken(call_id=self.call_id, t=now, reason="probe"))
        self._tier2_kind = "probe"
        snap = self.buffer.snapshot()
        sr = self.settings.sample_rate
        self._tier2_exec.submit(lambda: self.tier2.probe(snap, sr))

    def _poll_tier2(self, now: float) -> None:
        if self._tier2_kind is None:
            return
        result = self._tier2_exec.poll()
        if result is None:
            return
        value, error = result
        kind, self._tier2_kind = self._tier2_kind, None
        if error is not None:
            if isinstance(error, NotImplementedError):
                # A stubbed Tier 2 (probe lands in M5) — don't crash a live call.
                self._fail(now, reason="tier2_not_implemented")
            else:
                log.error("call %s: tier2 %s failed", self.call_id, kind, exc_info=error)
                if kind == "probe":
                    self._fail(now, reason="tier2_error")
            return
        if kind == "menu":
            self._apply_menu(value, now)
        elif kind == "probe":
            self._apply_probe(value, now)

    def _apply_menu(self, decision, now: float) -> None:
        if not decision.digits:
            return
        try:
            self.dtmf_sender(decision.digits)
        except Exception:
            # A failed keypress shouldn't kill the call — Tier 1 keeps listening,
            # and the menu will re-prompt on no input.
            log.exception("call %s: DTMF send failed", self.call_id)
        self.telemetry.emit(DtmfSent(call_id=self.call_id, t=now, digits=decision.digits))
        self.fsm.note_menu_action(now)  # re-arm the wake for a submenu

    def _apply_probe(self, outcome, now: float) -> None:
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

    def close(self) -> None:
        """Release the Tier 2 executor's thread. Called when the media stream
        ends. Idempotent."""
        self._tier2_exec.close()

    def _emit_state_change(self, before: CallState, now: float) -> None:
        if self.fsm.state != before:
            self.telemetry.emit(
                StateChanged(call_id=self.call_id, t=now, frm=before.value, to=self.fsm.state.value)
            )
            log.info("call %s: %s -> %s", self.call_id, before.value, self.fsm.state.value)
