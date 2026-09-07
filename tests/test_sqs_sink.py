"""SqsSink — the telemetry producer. Must never block the caller, batches to SQS,
drops on overflow, retries transient send failures."""

from __future__ import annotations

import json
import threading
import time

from tests.fakes import FakeSqsClient

from dialpass.telemetry.events import FrameClassified
from dialpass.telemetry.sqs_sink import SqsSink


def _event(i: int) -> FrameClassified:
    return FrameClassified(call_id="CA1", t=float(i), label="hold_music", confidence=0.9)


def _drain(sink: SqsSink, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and not sink._q.empty():
        time.sleep(0.01)
    sink.close()


def test_emit_batches_every_event_to_sqs():
    client = FakeSqsClient()
    sink = SqsSink("q", client=client, poll_interval=0.02)
    for i in range(25):
        sink.emit(_event(i))
    _drain(sink)

    assert all(len(b) <= 10 for b in client.sent_batches)
    bodies = client.sent_bodies
    assert len(bodies) == 25
    assert json.loads(bodies[0])["kind"] == "frame_classified"
    assert sink.sent == 25


def test_emit_never_blocks_and_drops_on_overflow():
    client = FakeSqsClient()
    client.block_send = threading.Event()  # pump will stall inside send
    sink = SqsSink("q", client=client, max_queue=5, poll_interval=0.02)

    start = time.perf_counter()
    for i in range(60):
        sink.emit(_event(i))
    elapsed = time.perf_counter() - start

    assert elapsed < 0.5  # 60 emits with the consumer stalled — still instant
    assert sink.dropped_overflow > 0
    client.block_send.set()
    sink.close()


def test_transient_send_failure_is_retried():
    client = FakeSqsClient()
    client.fail_times = 2
    sink = SqsSink("q", client=client, poll_interval=0.02)
    for i in range(3):
        sink.emit(_event(i))
    _drain(sink, timeout=3.0)

    assert sink.sent == 3
    assert sink.dropped_send == 0


def test_partial_batch_failure_reships_only_the_failed_entries():
    client = FakeSqsClient()
    client.partial_fail_ids = {"0"}
    sink = SqsSink("q", client=client, poll_interval=0.02)
    for i in range(3):
        sink.emit(_event(i))
    _drain(sink, timeout=3.0)

    # first batch: 3 entries (1 fails); second batch: just the retried one
    assert len(client.sent_batches) == 2
    assert len(client.sent_batches[1]) == 1
    assert sink.sent == 3


def test_close_flushes_queued_events():
    client = FakeSqsClient()
    sink = SqsSink("q", client=client, poll_interval=5.0)  # pump would sleep
    for i in range(4):
        sink.emit(_event(i))
    sink.close()
    assert len(client.sent_bodies) == 4
