"""The telemetry worker: pull off SQS, store, delete. Store failures must leave
messages on the queue; unparseable messages must not wedge the worker."""

from __future__ import annotations

import json
import threading

from tests.fakes import FakeSqsClient

from dialpass.telemetry.store import InMemoryEventStore
from dialpass.workers.telemetry_worker import run


def _run_until_drained(client: FakeSqsClient, store) -> None:
    stop = threading.Event()
    client.stop_when_empty = stop
    t = threading.Thread(target=run, args=(client, "q", store, stop), daemon=True)
    t.start()
    t.join(timeout=3.0)
    assert not t.is_alive()


def test_worker_stores_events_and_deletes_the_messages():
    client = FakeSqsClient()
    client.queue_messages(
        [
            json.dumps({"kind": "state_changed", "call_id": "CA1", "t": 1.0}),
            json.dumps({"kind": "dtmf_sent", "call_id": "CA1", "t": 2.0, "digits": "1"}),
        ]
    )
    store = InMemoryEventStore()

    _run_until_drained(client, store)

    assert [r["kind"] for r in store.rows] == ["state_changed", "dtmf_sent"]
    assert len(client.deleted) == 2


def test_poison_message_is_deleted_but_not_stored():
    client = FakeSqsClient()
    client.queue_messages(
        ["not json at all", json.dumps({"kind": "probe_result", "call_id": "CA1", "t": 3.0})]
    )
    store = InMemoryEventStore()

    _run_until_drained(client, store)

    assert [r["kind"] for r in store.rows] == ["probe_result"]
    assert len(client.deleted) == 2  # the good one AND the poison one


def test_store_failure_leaves_messages_on_the_queue():
    class Boom(InMemoryEventStore):
        def write_many(self, events):
            raise RuntimeError("db down")

    client = FakeSqsClient()
    client.queue_messages([json.dumps({"kind": "state_changed", "call_id": "CA1", "t": 1.0})])

    _run_until_drained(client, Boom())

    assert client.deleted == []  # not acked — SQS will redeliver
