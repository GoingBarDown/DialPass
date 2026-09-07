"""Where the telemetry worker lands events after it pulls them off SQS.

`EventStore` is the seam so the worker doesn't hard-depend on Postgres: tests use
`InMemoryEventStore`, `make sim` / a bare worker uses `LoggingEventStore`, and
docker-compose wires `PostgresEventStore`.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

log = logging.getLogger("dialpass.telemetry.worker")


class EventStore(Protocol):
    def write_many(self, events: list[dict[str, Any]]) -> None: ...
    def close(self) -> None: ...


class InMemoryEventStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def write_many(self, events: list[dict[str, Any]]) -> None:
        self.rows.extend(events)

    def close(self) -> None:
        pass


class LoggingEventStore:
    def write_many(self, events: list[dict[str, Any]]) -> None:
        for e in events:
            log.info("call_event %s", e)

    def close(self) -> None:
        pass


class PostgresEventStore:
    """One row per event: the three query columns lifted out, plus the whole
    payload as jsonb so nothing is lost as the taxonomy grows."""

    _DDL = (
        "CREATE TABLE IF NOT EXISTS call_events ("
        " id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
        " call_id TEXT NOT NULL,"
        " t DOUBLE PRECISION NOT NULL,"
        " kind TEXT NOT NULL,"
        " payload JSONB NOT NULL,"
        " seen_at TIMESTAMPTZ NOT NULL DEFAULT now());"
        "CREATE INDEX IF NOT EXISTS call_events_call_id_idx ON call_events (call_id);"
        "CREATE INDEX IF NOT EXISTS call_events_kind_idx ON call_events (kind);"
    )

    def __init__(self, dsn: str) -> None:
        import psycopg

        self._psycopg = psycopg
        self._conn = psycopg.connect(dsn, autocommit=True)
        self._conn.execute(self._DDL)

    def write_many(self, events: list[dict[str, Any]]) -> None:
        from psycopg.types.json import Json

        rows = [
            (str(e.get("call_id", "")), float(e.get("t", 0.0)), str(e.get("kind", "")), Json(e))
            for e in events
        ]
        with self._conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO call_events (call_id, t, kind, payload) VALUES (%s, %s, %s, %s)",
                rows,
            )

    def close(self) -> None:
        self._conn.close()


def build_store(settings) -> EventStore:
    if settings.database_url:
        return PostgresEventStore(settings.database_url)
    log.warning("telemetry worker: no DIALPASS_DATABASE_URL — logging events instead of storing")
    return LoggingEventStore()
