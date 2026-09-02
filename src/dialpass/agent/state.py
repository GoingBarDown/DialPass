"""The call-phase finite state machine.

Consumes Tier 1's per-frame `Label` stream and decides when the call needs a
decision (wake Tier 2) or an action (bridge, fail). It deliberately lags the
label stream: transitions require a label to *hold* for N frames, entry into
cautious states is easy while leaving them is hard, and low-confidence frames
don't count. See docs/design-decisions.md#hysteresis--debouncing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .labels import HOLD_LABELS, MENU_LABELS, NON_CONNECT_LABELS, Label


class CallState(StrEnum):
    DIALING = "DIALING"
    IVR_MENU = "IVR_MENU"
    ON_HOLD = "ON_HOLD"
    EVALUATING_SPEECH = "EVALUATING_SPEECH"
    HUMAN_DETECTED = "HUMAN_DETECTED"
    BRIDGING = "BRIDGING"
    DONE = "DONE"
    FAILED = "FAILED"


LIVE_STATES = frozenset(
    {
        CallState.DIALING,
        CallState.IVR_MENU,
        CallState.ON_HOLD,
        CallState.EVALUATING_SPEECH,
    }
)


class Action(StrEnum):
    """What the FSM asks `AgentSession` to do as a result of an observation."""

    NONE = "NONE"
    WAKE_TIER2_MENU = "WAKE_TIER2_MENU"  # understand the menu, choose a digit
    WAKE_TIER2_PROBE = "WAKE_TIER2_PROBE"  # run the "Hello?" probe
    BRIDGE = "BRIDGE"  # human confirmed — hand off to the user
    FAIL = "FAIL"  # non-connect / fatal


@dataclass(slots=True)
class FsmConfig:
    enter_menu_frames: int = 3  # DIALING -> IVR_MENU
    enter_hold_frames: int = 3  # IVR_MENU -> ON_HOLD
    reenter_menu_frames: int = 3  # ON_HOLD -> IVR_MENU (menu loop)
    enter_eval_frames: int = 2  # ON_HOLD -> EVALUATING_SPEECH (easy, asymmetric)
    leave_eval_frames: int = 5  # EVALUATING_SPEECH -> ON_HOLD (hard, asymmetric)
    non_connect_frames: int = 2  # any live state -> FAILED
    refractory_s: float = 18.0  # block re-EVALUATING after a failed probe
    min_confidence: float = 0.55  # frames below this don't advance a streak


class CallStateMachine:
    def __init__(self, cfg: FsmConfig | None = None) -> None:
        self.cfg = cfg or FsmConfig()
        self.state = CallState.DIALING
        self._streak_label: Label | None = None
        self._streak = 0
        self._refractory_until = 0.0
        self._menu_woken = False

    # -- streak bookkeeping -------------------------------------------------
    def _streak_for(self, label: Label, confidence: float) -> int:
        if confidence < self.cfg.min_confidence:
            return 0
        if label == self._streak_label:
            self._streak += 1
        else:
            self._streak_label = label
            self._streak = 1
        return self._streak

    def _enter(self, state: CallState) -> None:
        self.state = state
        self._streak_label = None
        self._streak = 0
        if state == CallState.IVR_MENU:
            self._menu_woken = False

    def _fall_back_to_hold(self, now: float) -> None:
        self._refractory_until = now + self.cfg.refractory_s
        self._enter(CallState.ON_HOLD)

    # -- main entry point ------------------------------------------------------
    def observe(self, label: Label, confidence: float, now: float) -> Action:
        streak = self._streak_for(label, confidence)

        if (
            self.state in LIVE_STATES
            and label in NON_CONNECT_LABELS
            and streak >= self.cfg.non_connect_frames
        ):
            self._enter(CallState.FAILED)
            return Action.FAIL

        handler = _HANDLERS.get(self.state)
        if handler is None:
            return Action.NONE
        return handler(self, label, streak, now)

    # -- external signals ---------------------------------------------------
    def probe_result(self, is_human: bool, now: float) -> Action:
        """Tier 2's verdict after a WAKE_TIER2_PROBE."""
        if self.state != CallState.EVALUATING_SPEECH:
            return Action.NONE
        if is_human:
            self._enter(CallState.HUMAN_DETECTED)
            return Action.BRIDGE
        self._fall_back_to_hold(now)
        return Action.NONE

    def bridged(self) -> None:
        if self.state == CallState.HUMAN_DETECTED:
            self._enter(CallState.BRIDGING)

    def completed(self) -> None:
        self.state = CallState.DONE

    def failed(self) -> None:
        self.state = CallState.FAILED

    # -- per-state handlers -----------------------------------------------------
    def _on_dialing(self, label: Label, streak: int, now: float) -> Action:
        speech = label in MENU_LABELS or label == Label.LIVE_SPEECH_CANDIDATE
        if speech and streak >= self.cfg.enter_menu_frames:
            self._enter(CallState.IVR_MENU)
            self._menu_woken = True  # this transition already wakes Tier 2
            return Action.WAKE_TIER2_MENU
        return Action.NONE

    def _on_ivr_menu(self, label: Label, streak: int, now: float) -> Action:
        if label == Label.HOLD_MUSIC and streak >= self.cfg.enter_hold_frames:
            self._enter(CallState.ON_HOLD)
            return Action.NONE
        if label in MENU_LABELS and not self._menu_woken and streak >= 2:
            self._menu_woken = True
            return Action.WAKE_TIER2_MENU
        return Action.NONE

    def _on_on_hold(self, label: Label, streak: int, now: float) -> Action:
        if label in MENU_LABELS and streak >= self.cfg.reenter_menu_frames:
            self._enter(CallState.IVR_MENU)
            return Action.WAKE_TIER2_MENU
        if (
            label == Label.LIVE_SPEECH_CANDIDATE
            and now >= self._refractory_until
            and streak >= self.cfg.enter_eval_frames
        ):
            self._enter(CallState.EVALUATING_SPEECH)
            return Action.WAKE_TIER2_PROBE
        return Action.NONE

    def _on_evaluating_speech(self, label: Label, streak: int, now: float) -> Action:
        if label in HOLD_LABELS and streak >= self.cfg.leave_eval_frames:
            self._fall_back_to_hold(now)
        return Action.NONE


_HANDLERS = {
    CallState.DIALING: CallStateMachine._on_dialing,
    CallState.IVR_MENU: CallStateMachine._on_ivr_menu,
    CallState.ON_HOLD: CallStateMachine._on_on_hold,
    CallState.EVALUATING_SPEECH: CallStateMachine._on_evaluating_speech,
}
