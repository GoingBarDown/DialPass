"""The M1 acceptance test: a synthetic call runs end to end through the real
pipeline and reaches a bridged human."""

from scripts.simulate_call import run

from dialpass.agent.state import CallState


def test_scripted_call_reaches_bridge():
    session, sink = run("scripted", probe_is_human=True)

    assert session.state == CallState.DONE
    kinds = [e.kind for e in sink.events]
    assert "tier2_woken" in kinds
    assert "human_detected" in kinds
    assert "call_completed" in kinds

    woken_reasons = {e.payload()["reason"] for e in sink.of_kind("tier2_woken")}
    assert {"menu", "probe"} <= woken_reasons


def test_scripted_call_stays_on_hold_when_probe_says_not_human():
    session, sink = run("scripted", probe_is_human=False)

    # never bridged; ends still on hold (or evaluating), never DONE via human
    assert session.state in (CallState.ON_HOLD, CallState.EVALUATING_SPEECH)
    assert "human_detected" not in [e.kind for e in sink.events]


def test_heuristic_classifier_runs_without_error():
    # not asserting the outcome — the heuristic rules are rough until M3 — just
    # that the real feature pipeline processes a whole call without blowing up.
    session, sink = run("heuristic", probe_is_human=True)
    assert len(sink.of_kind("frame_classified")) > 50
