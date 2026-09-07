"""EventStore seam for the telemetry worker."""

from __future__ import annotations

from dialpass.config import Settings
from dialpass.telemetry.store import InMemoryEventStore, LoggingEventStore, build_store


def test_in_memory_store_appends():
    store = InMemoryEventStore()
    store.write_many([{"kind": "a"}, {"kind": "b"}])
    store.write_many([{"kind": "c"}])
    assert [r["kind"] for r in store.rows] == ["a", "b", "c"]


def test_build_store_without_a_dsn_falls_back_to_logging():
    store = build_store(Settings(database_url=""))
    assert isinstance(store, LoggingEventStore)
