"""The app shares one telemetry sink across every call, and flushes it on
shutdown. With no queue configured it stays the log sink (no AWS needed)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from tests.fakes import FakeSqsClient

import dialpass.main as main_mod
from dialpass.config import Settings
from dialpass.telemetry.events import StateChanged
from dialpass.telemetry.publisher import LogSink
from dialpass.telemetry.sqs_sink import SqsSink


def test_no_queue_configured_uses_the_log_sink():
    app = main_mod.create_app()
    assert isinstance(app.state.telemetry_sink, LogSink)


def test_sqs_sink_is_shared_and_flushed_on_shutdown(monkeypatch):
    client = FakeSqsClient()
    monkeypatch.setattr(main_mod, "get_settings", lambda: Settings(sqs_queue_url="q"))
    monkeypatch.setattr(main_mod, "build_sqs_client", lambda settings: client)

    app = main_mod.create_app()
    sink = app.state.telemetry_sink
    assert isinstance(sink, SqsSink)
    # every session gets the same sink instance
    assert app.state.make_session("CA1").telemetry is sink

    with TestClient(app):  # enters + exits the lifespan
        sink.emit(StateChanged(call_id="CA1", t=1.0, frm="DIALING", to="IVR_MENU"))

    # lifespan exit called sink.close(), which flushed the queued event
    assert json.loads(client.sent_bodies[0])["to"] == "IVR_MENU"
