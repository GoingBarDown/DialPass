"""/media — the bidirectional Twilio Media Streams socket (M5).

Drives the socket end to end with the FakeTier2: audio in over the WS runs Tier 1
+ the FSM, and the menu digit comes back through the bridge as a `dtmf` event.
We assert on the session's telemetry rather than reading the socket — the test
transport has no non-blocking receive.
"""

from __future__ import annotations

import base64
import time

from fastapi.testclient import TestClient
from tests.fakes import FakeTwilioClient

from dialpass.agent.bridge import CallBridge
from dialpass.agent.classifier import ScriptedClassifier
from dialpass.agent.session import AgentSession
from dialpass.api.media import _MAX_REDIALS, reap_stale_bridges
from dialpass.config import get_settings
from dialpass.main import create_app
from dialpass.realtime.fake import FakeTier2
from dialpass.telemetry.publisher import CollectingSink
from dialpass.telephony.audio import pcm16_to_ulaw
from dialpass.testing import synthesize_call

GROUP = "dialpass-testgroup1"


def _app_with_capturing_session(*, probe_is_human: bool = False):
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
            FakeTier2(menu_digits="2", probe_is_human=probe_is_human),
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
    # the digit went out as a real telephony DTMF redirect, not stream audio
    presses = app.state.twilio_client.digit_presses
    assert [p["digits"] for p in presses] == ["2"]
    assert presses[0]["reconnect_url"].endswith(f"/twiml/voice?group={GROUP}")
    # bridge is held for the imminent reconnect (redirect stub never completes it)
    assert app.state.bridges[GROUP].reconnecting is True


def test_media_socket_hands_off_to_the_user_when_a_human_is_detected():
    app, sink = _app_with_capturing_session(probe_is_human=True)
    app.state.user_numbers[GROUP] = "+15145550123"
    pcm, _ = synthesize_call()
    ulaw = pcm16_to_ulaw(pcm)

    captured: list = []
    with TestClient(app).websocket_connect("/media") as ws:
        ws.send_json(_start())
        for i in range(0, len(ulaw), 160):
            ws.send_json(_frame(ulaw[i : i + 160]))
            if not captured and GROUP in app.state.bridges:
                bridge = app.state.bridges[GROUP]
                bridge.bind_user("MZuser")  # user leg is on the line for the handoff
                captured.append(bridge)
        ws.send_json({"event": "stop"})

    kinds = [e.payload()["kind"] for e in sink.events]
    assert "human_detected" in kinds and "bridge_started" in kinds
    assert [e.payload()["outcome"] for e in sink.of_kind("call_completed")] == ["human_bridged"]
    # relay opened and the user got a text
    assert captured[0].relay_open is True
    assert [s["to"] for s in app.state.twilio_client.sms] == ["+15145550123"]


def test_media_socket_attaches_a_user_leg_to_the_existing_bridge():
    app, _ = _app_with_capturing_session()
    session = app.state.make_session("CAtest")
    bridge = CallBridge(GROUP, session)
    bridge.bind_agent("MZagent")
    bridge.relay_open = True  # pretend the handoff already happened
    app.state.bridges[GROUP] = bridge

    with TestClient(app).websocket_connect("/media") as ws:
        ws.send_json(_start(role="user"))
        for _ in range(5):
            ws.send_json(_frame(b"\x30" * 160))
        ws.send_json({"event": "stop"})

    # the user's voice was relayed toward the agent leg
    relayed = bridge.drain_agent()
    assert relayed and all(m["streamSid"] == "MZagent" for m in relayed)
    session.close()


# ---- M8: drop recovery -------------------------------------------------
def _drop_agent_leg_mid_call(app):
    """Connect the agent leg, feed a little audio, then vanish (no `stop`)."""
    pcm, _ = synthesize_call()
    ulaw = pcm16_to_ulaw(pcm)
    with TestClient(app).websocket_connect("/media") as ws:
        ws.send_json(_start())
        for i in range(0, 160 * 6, 160):  # a few ringback frames — call not finished
            ws.send_json(_frame(ulaw[i : i + 160]))


def test_agent_leg_drop_redials_and_keeps_the_call_context():
    app, _ = _app_with_capturing_session()
    app.state.call_meta[GROUP] = {"business_number": "+18005550100", "redials": 0}

    _drop_agent_leg_mid_call(app)

    fake = app.state.twilio_client
    assert [c["to"] for c in fake.outbound] == ["+18005550100"]  # re-dialed once
    assert app.state.call_meta[GROUP]["redials"] == 1
    assert GROUP in app.state.pending_goals  # context kept for the fresh leg
    assert GROUP not in app.state.bridges  # old bridge dropped


def test_agent_leg_drop_gives_up_after_max_redials():
    app, _ = _app_with_capturing_session()
    app.state.user_numbers[GROUP] = "+15145550123"
    app.state.user_legs[GROUP] = "CAuser0001"
    app.state.call_meta[GROUP] = {"business_number": "+18005550100", "redials": _MAX_REDIALS}

    _drop_agent_leg_mid_call(app)

    fake = app.state.twilio_client
    assert fake.outbound == []  # no more redials
    assert [s["to"] for s in fake.sms] == ["+15145550123"]  # fallback text sent
    assert "CAuser0001" in fake.hangups  # user leg hung up
    assert GROUP not in app.state.pending_goals  # context cleared
    assert GROUP not in app.state.call_meta


def test_clean_stop_does_not_redial():
    app, _ = _app_with_capturing_session()
    app.state.call_meta[GROUP] = {"business_number": "+18005550100", "redials": 0}
    pcm, _ = synthesize_call()
    ulaw = pcm16_to_ulaw(pcm)

    with TestClient(app).websocket_connect("/media") as ws:
        ws.send_json(_start())
        for i in range(0, 160 * 6, 160):
            ws.send_json(_frame(ulaw[i : i + 160]))
        ws.send_json({"event": "stop"})

    assert app.state.twilio_client.outbound == []
    assert GROUP not in app.state.call_meta


def test_reap_stale_bridges_clears_a_stuck_reconnect():
    app, _ = _app_with_capturing_session()
    app.state.user_numbers[GROUP] = "+15145550123"
    session = app.state.make_session("CAagent", user_leg_sid="CAuser")
    bridge = CallBridge(GROUP, session)
    bridge.reconnecting = True
    bridge.reconnect_since = time.monotonic() - 999
    app.state.bridges[GROUP] = bridge
    app.state.sessions["CAagent"] = session

    reaped = reap_stale_bridges(app)

    assert reaped == 1
    assert GROUP not in app.state.bridges
    fake = app.state.twilio_client
    assert "CAagent" in fake.hangups and "CAuser" in fake.hangups
    assert [s["to"] for s in fake.sms] == ["+15145550123"]


def test_reap_leaves_a_healthy_reconnect_alone():
    app, _ = _app_with_capturing_session()
    session = app.state.make_session("CAagent")
    bridge = CallBridge(GROUP, session)
    bridge.reconnecting = True
    bridge.reconnect_since = time.monotonic()  # just started — still within grace
    app.state.bridges[GROUP] = bridge

    assert reap_stale_bridges(app) == 0
    assert GROUP in app.state.bridges
    session.close()
