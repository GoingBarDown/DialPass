"""FSM behaviour: debounce, asymmetric thresholds, refractory period, probe flow."""

from dialpass.agent.labels import Label
from dialpass.agent.state import Action, CallState, CallStateMachine, FsmConfig


def feed(sm: CallStateMachine, label: Label, n: int, *, t0: float = 0.0, conf: float = 0.95):
    """Feed n frames; return the first non-NONE action (the transition action)."""
    result = Action.NONE
    for i in range(n):
        action = sm.observe(label, conf, t0 + i)
        if action != Action.NONE and result == Action.NONE:
            result = action
    return result


def test_ringback_then_speech_enters_menu():
    sm = CallStateMachine()
    feed(sm, Label.RINGBACK, 5)
    assert sm.state == CallState.DIALING
    action = feed(sm, Label.MENU_SPEAKING, 3, t0=5)
    assert sm.state == CallState.IVR_MENU
    assert action == Action.WAKE_TIER2_MENU


def test_single_frame_blip_does_not_transition():
    sm = CallStateMachine()
    feed(sm, Label.MENU_SPEAKING, 3)  # -> IVR_MENU
    feed(sm, Label.HOLD_MUSIC, 3, t0=10)  # -> ON_HOLD
    assert sm.state == CallState.ON_HOLD
    # one stray speech frame must not arm EVALUATING_SPEECH
    sm.observe(Label.LIVE_SPEECH_CANDIDATE, 0.95, 20)
    assert sm.state == CallState.ON_HOLD


def test_low_confidence_frames_do_not_advance_streak():
    sm = CallStateMachine()
    feed(sm, Label.MENU_SPEAKING, 3)
    feed(sm, Label.HOLD_MUSIC, 3, t0=10)
    for i in range(10):
        sm.observe(Label.LIVE_SPEECH_CANDIDATE, 0.3, 20 + i)  # below min_confidence
    assert sm.state == CallState.ON_HOLD


def test_speech_on_hold_triggers_probe_then_bridge():
    sm = CallStateMachine()
    feed(sm, Label.MENU_SPEAKING, 3)
    feed(sm, Label.HOLD_MUSIC, 3, t0=10)
    action = feed(sm, Label.LIVE_SPEECH_CANDIDATE, 2, t0=20)
    assert sm.state == CallState.EVALUATING_SPEECH
    assert action == Action.WAKE_TIER2_PROBE

    assert sm.probe_result(is_human=True, now=25) == Action.BRIDGE
    assert sm.state == CallState.HUMAN_DETECTED


def test_failed_probe_falls_back_and_refractory_blocks_immediate_retry():
    cfg = FsmConfig(refractory_s=18.0)
    sm = CallStateMachine(cfg)
    feed(sm, Label.MENU_SPEAKING, 3)
    feed(sm, Label.HOLD_MUSIC, 3, t0=10)
    feed(sm, Label.LIVE_SPEECH_CANDIDATE, 2, t0=20)
    sm.probe_result(is_human=False, now=22)
    assert sm.state == CallState.ON_HOLD

    # within the refractory window: no re-entry even on sustained speech
    feed(sm, Label.LIVE_SPEECH_CANDIDATE, 5, t0=25)
    assert sm.state == CallState.ON_HOLD

    # after it: allowed again
    action = feed(sm, Label.LIVE_SPEECH_CANDIDATE, 3, t0=45)
    assert sm.state == CallState.EVALUATING_SPEECH
    assert action == Action.WAKE_TIER2_PROBE


def test_evaluating_speech_leaves_only_after_sustained_hold():
    cfg = FsmConfig(leave_eval_frames=5)
    sm = CallStateMachine(cfg)
    feed(sm, Label.MENU_SPEAKING, 3)
    feed(sm, Label.HOLD_MUSIC, 3, t0=10)
    feed(sm, Label.LIVE_SPEECH_CANDIDATE, 2, t0=20)
    assert sm.state == CallState.EVALUATING_SPEECH
    feed(sm, Label.HOLD_MUSIC, 4, t0=22)  # not enough to leave
    assert sm.state == CallState.EVALUATING_SPEECH
    feed(sm, Label.HOLD_MUSIC, 5, t0=30)  # now it leaves
    assert sm.state == CallState.ON_HOLD


def test_voicemail_fails_from_any_state():
    sm = CallStateMachine()
    feed(sm, Label.MENU_SPEAKING, 3)
    action = feed(sm, Label.VOICEMAIL, 2, t0=5)
    assert sm.state == CallState.FAILED
    assert action == Action.FAIL


def test_hold_can_loop_back_to_menu():
    sm = CallStateMachine()
    feed(sm, Label.MENU_SPEAKING, 3)
    feed(sm, Label.HOLD_MUSIC, 3, t0=10)
    action = feed(sm, Label.MENU_SPEAKING, 3, t0=20)
    assert sm.state == CallState.IVR_MENU
    assert action == Action.WAKE_TIER2_MENU
