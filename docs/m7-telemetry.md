# M7 — the SQS telemetry pipeline

**Status:** done, branch `feat/m7-telemetry`. 94 tests pass, ruff + mypy clean.
Runs end to end in `docker compose up` (localstack SQS + Postgres + worker).

## Goal

Every interesting thing a call does already emits a typed `Event`
(`telemetry/events.py`) — state changes, frame classifications, Tier 2 wakes,
DTMF presses, probe verdicts, completions. Until now the only sink was `LogSink`.

M7 gets that event stream **off the critical audio path** and into a queryable
store, without ever letting a slow database or a network blip touch the call:

```
  AgentSession.emit(event)                        (media loop / Tier 2 thread)
        │  json.dumps + queue.put_nowait   ~5 µs, never blocks, never raises
        ▼
  SqsSink   bounded in-memory queue ── daemon pump thread ──▶  SQS  SendMessageBatch(≤10)
        ▼
  SQS  dialpass-telemetry  (+ dialpass-telemetry-dlq, maxReceiveCount 5)
        ▼
  telemetry_worker   long-poll ReceiveMessage(20s) ──▶ EventStore.write_many ──▶ DeleteMessageBatch
        ▼
  Postgres  call_events (call_id, t, kind, payload jsonb, seen_at)
```

Resume bullet 3, part 1: *"Architected an AWS SQS telemetry pipeline to keep
call-event data off the critical audio path."*

---

## Producer — `telemetry/sqs_sink.py`

`emit()` is called from the media event loop **and** the Tier 2 worker thread, so
it does the least work that could possibly work:

1. `json.dumps(event.payload())` — the payload is already primitives.
2. `queue.Queue.put_nowait` onto a bounded queue (`maxsize=10_000`).
3. On `queue.Full` → `dropped_overflow += 1` and return. **Telemetry is
   diagnostic; the call is not.** Never blocks, never raises.

A single **daemon thread** drains the queue:

- `get(timeout=poll_interval)` for the first item, then `get_nowait` up to 10
  (the `SendMessageBatch` limit) → one batched API call.
- Send failure → retry `send_retries` times with linear backoff, then
  `dropped_send += len(batch)` and move on.
- Partial batch failure (SQS returns `Failed`) → re-ship only the failed entries.
- `close()` sets the stop flag, the pump finishes its final drain, thread joins.
  Wired to FastAPI's `lifespan` shutdown so a clean stop flushes the queue.

Counters (`sent`, `dropped_overflow`, `dropped_send`) are exposed on `/health`.

**Why a thread, not asyncio:** `emit` has two callers on two different execution
contexts (event loop + worker thread), boto3 is synchronous, and a
`queue.Queue` + thread is provably non-blocking for the producer. An async sink
would have to hop back onto the loop to enqueue — the exact coupling we're
removing.

---

## Transport — SQS (localstack in dev)

`scripts/localstack-init.sh` (mounted into the localstack container's
`init/ready.d`) creates:

- `dialpass-telemetry` — the work queue.
- `dialpass-telemetry-dlq` — dead-letter queue, `maxReceiveCount = 5`. A message
  the worker can't store after 5 receives parks here instead of looping forever.

`build_sqs_client(settings)` honours `DIALPASS_AWS_ENDPOINT_URL` so the same code
points at localstack in compose and real SQS in prod.

---

## Consumer — `workers/telemetry_worker.py`

A standalone process (`python -m dialpass.workers.telemetry_worker`, its own
service in docker-compose):

```
loop until SIGTERM/SIGINT:
    ReceiveMessage(MaxNumberOfMessages=10, WaitTimeSeconds=20)   # long poll, no busy-wait
    parse bodies → events           (unparseable → log + delete as poison)
    store.write_many(events)         (raises → do NOT delete; SQS redelivers)
    DeleteMessageBatch(stored + poison receipts)
```

A message is deleted **only after** its event is durably stored, so the pipeline
is at-least-once: a store outage just means redelivery (and eventually the DLQ),
never data loss on the queue side.

---

## Store — `telemetry/store.py`

`EventStore` is a `Protocol` so the worker doesn't hard-depend on Postgres:

| impl | used by |
|---|---|
| `PostgresEventStore` | docker-compose (`DIALPASS_DATABASE_URL` set) — `executemany` INSERT, `id/call_id/t/kind` columns + full `payload` jsonb, indexes on `call_id` and `kind`, `CREATE TABLE IF NOT EXISTS` on connect |
| `LoggingEventStore` | a bare worker with no DSN — logs each event |
| `InMemoryEventStore` | tests |

`build_store(settings)` picks based on `database_url`.

---

## Wiring changes

- `main.py` — `_build_telemetry(settings)`: `SqsSink` if `DIALPASS_SQS_QUEUE_URL`
  is set, else `LogSink`. **One sink per process**, shared by every
  `AgentSession` (was a fresh `LogSink()` per call). Flushed on `lifespan` exit.
- `config.py` — `sqs_queue_url`, `aws_region`, `aws_endpoint_url`,
  `telemetry_queue_maxsize`, `database_url`. All blank by default → `make sim`
  and a bare dev server need no AWS.
- `api/health.py` — `/health` reports the sink counters when SQS is active.
- `docker-compose.yml` — `localstack` (sqs) + `postgres` + `worker`, health-gated.
- deps: `boto3`, `psycopg[binary]`.

---

## Validation

- **Unit** (`test_sqs_sink.py`, `test_telemetry_worker.py`, `test_store.py`,
  `test_telemetry_wiring.py`): emit-never-blocks (60 emits with the consumer
  stalled complete in <0.5 s), overflow drops, batching ≤10, transient +
  partial send-failure retries, close-flushes-pending; worker stores + deletes,
  poison-message handling, store-failure-leaves-messages; the app shares one
  sink and flushes it on shutdown.
- **Integration:** `docker compose up` → `POST /calls` (or `make sim` piped
  through a running app) → rows land in `call_events`. Not part of `pytest`
  (needs Docker).

---

## Known limitations / notes

- **At-least-once, not exactly-once.** A worker crash between
  `write_many` and `DeleteMessageBatch` re-inserts those events on redelivery.
  Acceptable for telemetry; a real dedup would use the event's natural key.
- **Producer drops are silent** beyond the `/health` counter. Fine — the DLQ and
  counters are the observability story; the alternative (blocking the call) is
  unacceptable.
- No schema migration tool — `PostgresEventStore` runs its own idempotent DDL on
  connect. A dozen-line table doesn't warrant Alembic here.
- The `persistence/` package (M6 per-destination IVR map) is unrelated and stays
  a stub — M6 is skipped.

---

## Files

```
src/dialpass/telemetry/sqs_sink.py       SqsSink producer + build_sqs_client (new)
src/dialpass/telemetry/store.py          EventStore: Postgres / Logging / InMemory (new)
src/dialpass/workers/telemetry_worker.py real poll→store→delete loop (was a stub)
src/dialpass/main.py                     _build_telemetry, shared sink, lifespan flush
src/dialpass/config.py                   sqs / aws / database settings
src/dialpass/api/health.py               sink counters on /health
docker-compose.yml                       localstack + postgres + worker
scripts/localstack-init.sh               create the queue + DLQ (new)
tests/test_sqs_sink.py, test_telemetry_worker.py, test_store.py,
tests/test_telemetry_wiring.py, tests/fakes.py (FakeSqsClient)
```
