"""Offline harness: run a synthetic call through the real pipeline.

    uv run python scripts/simulate_call.py
    uv run python scripts/simulate_call.py --classifier heuristic
    uv run python scripts/simulate_call.py --no-human   # probe fails, stays on hold

No phone, no paid APIs. This is ~95% of how M3-M5 get developed.
"""

from __future__ import annotations

import argparse
import sys

from dialpass.agent.classifier import HeuristicClassifier, ScriptedClassifier
from dialpass.agent.session import AgentSession
from dialpass.config import get_settings
from dialpass.realtime.fake import FakeTier2
from dialpass.telemetry.publisher import CollectingSink
from dialpass.testing import iter_frames, synthesize_call


def run(classifier_kind: str, probe_is_human: bool) -> tuple[AgentSession, CollectingSink]:
    settings = get_settings()
    pcm, schedule = synthesize_call()

    if classifier_kind == "scripted":
        classifier = ScriptedClassifier(schedule)
    else:
        classifier = HeuristicClassifier()

    sink = CollectingSink()
    session = AgentSession(
        "sim-1",
        classifier,
        FakeTier2(menu_digits="2", probe_is_human=probe_is_human),
        telemetry=sink,
        settings=settings,
        goal="reach a human",
    )

    for frame in iter_frames(pcm, settings.frame_ms):
        session.feed_audio(frame)
        if session.finished:
            break
    return session, sink


def print_timeline(sink: CollectingSink) -> None:
    interesting = {
        "state_changed",
        "tier2_woken",
        "dtmf_sent",
        "probe_result",
        "human_detected",
        "bridge_started",
        "call_completed",
        "call_failed",
    }
    print(f"\n{'t (s)':>7}  event")
    print(f"{'-' * 7}  {'-' * 40}")
    for e in sink.events:
        if e.kind in interesting:
            detail = {k: v for k, v in e.payload().items() if k not in ("kind", "call_id", "t")}
            extra = f"  {detail}" if detail else ""
            print(f"{e.t:7.1f}  {e.kind}{extra}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--classifier", choices=["scripted", "heuristic"], default="scripted")
    p.add_argument("--no-human", action="store_true", help="probe returns 'not a human'")
    args = p.parse_args(argv)

    session, sink = run(args.classifier, probe_is_human=not args.no_human)
    print_timeline(sink)

    frames = len(sink.of_kind("frame_classified"))
    print(f"\nframes classified: {frames}")
    print(f"final state: {session.state.value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
