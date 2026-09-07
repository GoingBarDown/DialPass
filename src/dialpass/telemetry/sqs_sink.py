"""`SqsSink` — the telemetry producer, kept strictly off the critical audio path.

`emit()` is called from the media event loop and from the Tier 2 worker thread.
It must never block and never raise, so it does the cheapest possible thing:
`json.dumps` + `queue.put_nowait` onto a bounded in-memory queue, then returns.
If the queue is full (the network is slow or SQS is down) events are **dropped** —
telemetry is diagnostic, the call is not.

A single daemon thread drains that queue in batches of up to 10 (the SQS
`SendMessageBatch` limit) and ships them to the queue. Send failures are retried
a few times, then the batch is dropped. `close()` flushes what's left.

The consumer side is `workers/telemetry_worker.py`.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time

import boto3

from .events import Event

log = logging.getLogger("dialpass.telemetry")

_SQS_BATCH_MAX = 10


def build_sqs_client(settings):
    """An SQS client honouring `DIALPASS_AWS_ENDPOINT_URL` (localstack in dev)."""
    kwargs = {"region_name": settings.aws_region}
    if settings.aws_endpoint_url:
        kwargs["endpoint_url"] = settings.aws_endpoint_url
    return boto3.client("sqs", **kwargs)


class SqsSink:
    def __init__(
        self,
        queue_url: str,
        *,
        client,
        max_queue: int = 10_000,
        batch_size: int = _SQS_BATCH_MAX,
        poll_interval: float = 0.5,
        send_retries: int = 3,
    ) -> None:
        self._queue_url = queue_url
        self._client = client
        self._batch_size = min(batch_size, _SQS_BATCH_MAX)
        self._poll_interval = poll_interval
        self._send_retries = send_retries
        self._q: queue.Queue[str] = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        # counters — surfaced for a /health check and asserted in tests
        self.dropped_overflow = 0
        self.dropped_send = 0
        self.sent = 0
        self._pump = threading.Thread(target=self._run, name="dialpass-telemetry", daemon=True)
        self._pump.start()

    # -- producer side (hot path) --------------------------------------
    def emit(self, event: Event) -> None:
        try:
            body = json.dumps(event.payload(), separators=(",", ":"))
        except (TypeError, ValueError):
            log.exception("telemetry: un-serializable event %r", getattr(event, "kind", "?"))
            return
        try:
            self._q.put_nowait(body)
        except queue.Full:
            self.dropped_overflow += 1

    # -- consumer side (pump thread) ----------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            batch = self._next_batch(block=True)
            if batch:
                self._send(batch)
        # final drain on shutdown
        while True:
            batch = self._next_batch(block=False)
            if not batch:
                break
            self._send(batch)

    def _next_batch(self, *, block: bool) -> list[str]:
        out: list[str] = []
        try:
            out.append(self._q.get(timeout=self._poll_interval) if block else self._q.get_nowait())
        except queue.Empty:
            return out
        while len(out) < self._batch_size:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out

    def _send(self, bodies: list[str]) -> None:
        entries = [{"Id": str(i), "MessageBody": b} for i, b in enumerate(bodies)]
        for attempt in range(1, self._send_retries + 1):
            try:
                resp = self._client.send_message_batch(QueueUrl=self._queue_url, Entries=entries)
            except Exception:
                log.warning(
                    "telemetry: batch send failed (attempt %d/%d)", attempt, self._send_retries
                )
                time.sleep(min(2.0, 0.25 * attempt))
                continue
            failed_ids = {f["Id"] for f in resp.get("Failed", [])}
            self.sent += len(entries) - len(failed_ids)
            if not failed_ids:
                return
            entries = [e for e in entries if e["Id"] in failed_ids]
            time.sleep(min(2.0, 0.25 * attempt))
        self.dropped_send += len(entries)
        log.error("telemetry: dropped %d events after %d retries", len(entries), self._send_retries)

    # -- lifecycle ---------------------------------------------------
    def close(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._pump.join(timeout=timeout)
