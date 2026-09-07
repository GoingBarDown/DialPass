"""Consumes telemetry events off SQS and lands them in a store (Postgres in
docker-compose). Runs as its own process so a slow database never touches the
audio path — the API just does `queue.put_nowait` (see telemetry/sqs_sink.py).

    python -m dialpass.workers.telemetry_worker
"""

from __future__ import annotations

import json
import logging
import signal
import threading
from typing import Any

from ..config import get_settings
from ..telemetry.sqs_sink import build_sqs_client
from ..telemetry.store import EventStore, build_store

log = logging.getLogger("dialpass.telemetry.worker")

_LONG_POLL_S = 20
_BATCH = 10


def run(sqs, queue_url: str, store: EventStore, stop: threading.Event) -> None:
    """Poll `queue_url` until `stop` is set. A message is deleted only after its
    event is durably stored, so a store failure just means SQS redelivers it
    (and, past the queue's redrive policy, parks it in the DLQ)."""
    while not stop.is_set():
        resp = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=_BATCH,
            WaitTimeSeconds=_LONG_POLL_S,
        )
        messages = resp.get("Messages", [])
        if not messages:
            continue

        events: list[dict[str, Any]] = []
        stored_receipts: list[dict[str, str]] = []
        poison_receipts: list[dict[str, str]] = []
        for m in messages:
            entry = {"Id": m["MessageId"], "ReceiptHandle": m["ReceiptHandle"]}
            try:
                events.append(json.loads(m["Body"]))
                stored_receipts.append(entry)
            except (json.JSONDecodeError, KeyError):
                log.warning("telemetry worker: dropping unparseable message %.180s", m.get("Body"))
                poison_receipts.append(entry)

        if events:
            try:
                store.write_many(events)
            except Exception:
                log.exception(
                    "telemetry worker: store write failed — %d events will redeliver", len(events)
                )
                stored_receipts = []  # leave them on the queue

        to_delete = stored_receipts + poison_receipts
        if to_delete:
            sqs.delete_message_batch(QueueUrl=queue_url, Entries=to_delete)
            log.debug(
                "telemetry worker: stored %d, dropped %d",
                len(stored_receipts),
                len(poison_receipts),
            )


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    if not settings.sqs_queue_url:
        raise SystemExit("DIALPASS_SQS_QUEUE_URL is not set")

    sqs = build_sqs_client(settings)
    store = build_store(settings)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    log.info("telemetry worker: polling %s", settings.sqs_queue_url)
    try:
        run(sqs, settings.sqs_queue_url, store, stop)
    finally:
        store.close()
        log.info("telemetry worker: stopped")


if __name__ == "__main__":
    main()
