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

# --- key terms -----------------------------------------------------------
# streak       — how many consecutive ticks in a row have produced the same
#                label (at high enough confidence). Resets to 0/1 the moment
#                the label changes. This is the "debounce" counter.
# debounce     — requiring a streak >= N before acting on a label, so one
#                noisy/wrong frame can't flip the state.
# hysteresis   — using DIFFERENT thresholds to enter vs. leave a state (e.g.
#                2 frames to enter EVALUATING_SPEECH, 5 to leave it), so the
#                FSM is quick to get cautious and slow to stand down.
# refractory   — a cooldown window (self._refractory_until) after a failed
#                probe during which we won't re-enter EVALUATING_SPEECH, so
#                the tail of one hold-music interjection can't retrigger us.
# live state   — any state where the call is still actively in progress
#                (as opposed to DONE/FAILED). See LIVE_STATES below.
# non-connect  — a label meaning the call didn't reach a live person and
#                never will (voicemail, error tone) — fail immediately.
# ---------------------------------------------------------------------------


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
    # Block re-EVALUATING after a failed probe. We enter EVALUATING ~2s into an
    # interjection, so ~8s covers the tail of a typical one. A rare long
    # announcement gets re-probed once (cheap). M3: let strong evidence
    # (speech, no music bed, no interjection-fingerprint match) override this.
    refractory_s: float = 8.0
    min_confidence: float = 0.55  # frames below this don't advance a streak


class CallStateMachine:
    def __init__(self, cfg: FsmConfig | None = None) -> None:
        """Fresh FSM, always starting in DIALING (we just placed the call)."""
        self.cfg = cfg or FsmConfig()
        self.state = CallState.DIALING
        self._streak_label: Label | None = None  # label the current streak is counting
        self._streak = 0  # how many consecutive ticks _streak_label has held
        self._refractory_until = 0.0  # timestamp before which we ignore probe-worthy speech
        self._menu_woken = False  # have we already asked Tier 2 to read THIS menu?

    # -- streak bookkeeping -------------------------------------------------
    def _streak_for(self, label: Label, confidence: float) -> int:
        """Update and return the running same-label streak for this tick.

        Low-confidence frames don't count at all (return 0, streak untouched
        conceptually — a confident label must still restart it). A label
        different from the last one resets the streak to 1.
        """
        if confidence < self.cfg.min_confidence:
            return 0
        if label == self._streak_label:
            self._streak += 1
        else:
            self._streak_label = label
            self._streak = 1
        return self._streak

    def _enter(self, state: CallState) -> None:
        """Transition to `state` and reset per-state bookkeeping.

        Every transition wipes the streak counter (a fresh state should not
        inherit progress toward a threshold that belonged to the old state).
        Entering IVR_MENU also resets `_menu_woken` since it's a new menu.
        """
        self.state = state
        self._streak_label = None
        self._streak = 0
        if state == CallState.IVR_MENU:
            self._menu_woken = False

    def _fall_back_to_hold(self, now: float) -> None:
        """Retreat to ON_HOLD after a failed probe, starting the refractory
        cooldown so we don't immediately re-trigger on the same speech."""
        self._refractory_until = now + self.cfg.refractory_s
        self._enter(CallState.ON_HOLD)

    # -- main entry point ------------------------------------------------------
    def observe(self, label: Label, confidence: float, now: float) -> Action:
        """Called once per classifier tick (~every 500ms) with Tier 1's output.

        First checks the universal "give up" condition (non-connect label
        sustained from any live state -> FAIL), then hands off to whichever
        per-state handler matches the current state.
        """
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
        """Tier 2's verdict after a WAKE_TIER2_PROBE ("Hello?" -> reply).

        Only meaningful while EVALUATING_SPEECH (ignored otherwise — e.g. a
        stale/late result after we already moved on). Human confirmed ->
        HUMAN_DETECTED + tell session.py to bridge. Not human -> retreat to
        ON_HOLD and start the refractory cooldown.
        """
        if self.state != CallState.EVALUATING_SPEECH:
            return Action.NONE
        if is_human:
            self._enter(CallState.HUMAN_DETECTED)
            return Action.BRIDGE
        self._fall_back_to_hold(now)
        return Action.NONE

    def bridged(self) -> None:
        """session.py calls this once it has started the handoff (told Tier 2
        to stall the rep). Moves HUMAN_DETECTED -> BRIDGING."""
        if self.state == CallState.HUMAN_DETECTED:
            self._enter(CallState.BRIDGING)

    def completed(self) -> None:
        """session.py calls this once the handoff is fully done. Terminal
        success state — nothing observes past this point."""
        self.state = CallState.DONE

    def failed(self) -> None:
        """Manual override to force FAILED (e.g. an unrecoverable error
        outside the normal label stream, like a dropped connection)."""
        self.state = CallState.FAILED

    # -- per-state handlers -----------------------------------------------------
    # Each handler gets the current label, its streak (from _streak_for), and
    # `now`. It reads self.state implicitly (dispatched by observe()) and
    # returns the Action for session.py to carry out. Called via _HANDLERS.

    def _on_dialing(self, label: Label, streak: int, now: float) -> Action:
        """Waiting for the call to connect (past ringback). Any sustained
        speech-like label (menu prompt or generic speech) means someone/
        something answered -> move to IVR_MENU and immediately wake Tier 2
        to listen to and interpret the greeting/menu."""
        speech = label in MENU_LABELS or label == Label.LIVE_SPEECH_CANDIDATE
        if speech and streak >= self.cfg.enter_menu_frames:
            self._enter(CallState.IVR_MENU)
            self._menu_woken = True  # this transition already wakes Tier 2
            return Action.WAKE_TIER2_MENU
        return Action.NONE

    def _on_ivr_menu(self, label: Label, streak: int, now: float) -> Action:
        """A menu is currently playing/was just read. Two exits: sustained
        hold music means the menu finished and we were placed on hold ->
        ON_HOLD. Otherwise, if menu speech is still going and we haven't
        woken Tier 2 for *this* menu yet, wake it now (handles menus that
        weren't caught by the DIALING->IVR_MENU transition, e.g. a second
        sub-menu)."""
        if label == Label.HOLD_MUSIC and streak >= self.cfg.enter_hold_frames:
            self._enter(CallState.ON_HOLD)
            return Action.NONE
        if label in MENU_LABELS and not self._menu_woken and streak >= 2:
            self._menu_woken = True
            return Action.WAKE_TIER2_MENU
        return Action.NONE

    def _on_on_hold(self, label: Label, streak: int, now: float) -> Action:
        """Sitting on hold, waiting for either another menu (loop back to
        IVR_MENU) or a human to pick up. LIVE_SPEECH_CANDIDATE past the
        refractory cooldown, held for just enter_eval_frames (easy/asymmetric
        threshold — we'd rather over-trigger and confirm with a probe than
        miss a human), moves to EVALUATING_SPEECH and wakes the probe."""
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
        """We've woken Tier 2 to probe; waiting for probe_result(). If Tier 1
        meanwhile sees the audio revert to hold-like labels for a SUSTAINED
        run (leave_eval_frames — hard/asymmetric threshold, so a brief dip
        doesn't bail us out early), give up before the probe even resolves
        and fall back to ON_HOLD."""
        if label in HOLD_LABELS and streak >= self.cfg.leave_eval_frames:
            self._fall_back_to_hold(now)
        return Action.NONE


_HANDLERS = {
    CallState.DIALING: CallStateMachine._on_dialing,
    CallState.IVR_MENU: CallStateMachine._on_ivr_menu,
    CallState.ON_HOLD: CallStateMachine._on_on_hold,
    CallState.EVALUATING_SPEECH: CallStateMachine._on_evaluating_speech,
}
