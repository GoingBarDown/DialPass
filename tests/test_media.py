"""/media — the bidirectional Twilio Media Streams socket (M5).

Drives the socket end to end with the FakeTier2: audio in over the WS runs Tier 1
+ the FSM, and the menu digit comes back through the bridge as a `dtmf` event.
We assert on the session's telemetry rather than reading the socket — the test
transport has no non-blocking receive.
"""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient
from tests.fakes import FakeTwilioClient

from dialpass.agent.classifier import ScriptedClassifier
from dialpass.agent.session import AgentSession
from dialpass.config import get_settings
from dialpass.main import create_app
from dialpass.realtime.fake import FakeTier2
from dialpass.telemetry.publisher import CollectingSink
from dialpass.telephony.audio import pcm16_to_ulaw
from dialpass.testing import synthesize_call

GROUP = "dialpass-testgroup1"


def _app_with_capturing_session():
    app = create_app()
    app.state.twilio_client = FakeTwilioClient()
    app.state.settings.public_base_url = "https://dialpass.test"
    app.state.pending_goals[GROUP] = "reach a human"

    sink = CollectingSink()
    _, schedule = synthesize_call()

    def make_session(call_id, goal=None, conference=None, user_leg_sid=None):
        return AgentSession(
            call_id,
            ScriptedClassifier(schedule),
            FakeTier2(menu_digits="2", probe_is_human=False),
            telemetry=sink,
            settings=get_settings(),
            goal=goal,
            conference=conference,
            user_leg_sid=user_leg_sid,
        )

    app.state.make_session = make_session
    return app, sink


def _start(role: str = "agent") -> dict:
    return {
        "event": "start",
        "start": {
            "streamSid": "MZtest",
            "callSid": "CAtest",
            "customParameters": {"group": GROUP, "role": role},
        },
    }


def _frame(ulaw: bytes) -> dict:
    return {"event": "media", "media": {"payload": base64.b64encode(ulaw).decode("ascii")}}


def test_media_socket_navigates_a_menu_over_the_stream():
    app, sink = _app_with_capturing_session()
    pcm, _ = synthesize_call()
    ulaw = pcm16_to_ulaw(pcm)

    with TestClient(app).websocket_connect("/media") as ws:
        ws.send_json(_start())
        for i in range(0, len(ulaw), 160):
            ws.send_json(_frame(ulaw[i : i + 160]))
            if i == 160 * 10:
                assert GROUP in app.state.bridges
        ws.send_json({"event": "stop"})

    states = [(e.payload()["frm"], e.payload()["to"]) for e in sink.of_kind("state_changed")]
    assert ("DIALING", "IVR_MENU") in states
    assert [e.payload()["digits"] for e in sink.of_kind("dtmf_sent")] == ["2"]
    assert GROUP not in app.state.bridges  # cleaned up on stop


def test_media_socket_ignores_a_user_leg_for_now():
    app, _ = _app_with_capturing_session()
    with TestClient(app).websocket_connect("/media") as ws:
        ws.send_json(_start(role="user"))
        ws.send_json({"event": "stop"})
    assert GROUP not in app.state.bridges  # phase 3 wires the user leg
