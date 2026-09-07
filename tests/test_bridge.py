"""CallBridge — the per-call audio hub between the Twilio socket and the agent."""

from __future__ import annotations

import base64

import numpy as np

from dialpass.agent.bridge import CallBridge
from dialpass.agent.classifier import ScriptedClassifier
from dialpass.agent.session import AgentSession
from dialpass.config import get_settings
from dialpass.realtime.fake import FakeTier2
from dialpass.telemetry.publisher import CollectingSink
from dialpass.telephony.audio import pcm16_to_ulaw
from dialpass.testing import synthesize_call


def _session(**kw) -> AgentSession:
    pcm, schedule = synthesize_call()
    return AgentSession(
        "bridge-test",
        ScriptedClassifier(schedule),
        FakeTier2(menu_digits="2", probe_is_human=False),
        telemetry=CollectingSink(),
        settings=get_settings(),
        goal="reach a human",
        **kw,
    )


def test_bridge_wires_the_dtmf_path_on_construction():
    session = _session()
    bridge = CallBridge("grp", session)
    assert session.dtmf_sender == bridge.press_dtmf


def test_inbound_ulaw_reaches_the_session_as_pcm():
    session = _session()
    bridge = CallBridge("grp", session)
    bridge.bind_agent("MZ1")

    tone = (0.2 * 32767 * np.sin(2 * np.pi * 300 * np.arange(1600) / 8000)).astype(np.int16)
    for i in range(0, len(tone), 160):
        bridge.on_agent_audio(pcm16_to_ulaw(tone[i : i + 160]))

    assert session.audio_seconds > 0
    assert session.telemetry.of_kind("frame_classified")


def test_press_dtmf_queues_a_twilio_dtmf_event():
    bridge = CallBridge("grp", _session())
    bridge.bind_agent("MZ42")
    bridge.press_dtmf("2w3")

    (msg,) = bridge.drain_outbound()
    assert msg == {"event": "dtmf", "streamSid": "MZ42", "dtmf": {"digits": "2w3"}}
    assert bridge.drain_outbound() == []  # consumed


def test_press_dtmf_before_bind_is_dropped():
    bridge = CallBridge("grp", _session())
    bridge.press_dtmf("2")
    assert bridge.drain_outbound() == []


def test_play_to_agent_splits_into_20ms_frames():
    bridge = CallBridge("grp", _session())
    bridge.bind_agent("MZ7")
    bridge.play_to_agent(np.zeros(400, dtype=np.int16))  # 50 ms -> 3 frames (160+160+80)

    msgs = bridge.drain_outbound()
    assert [m["event"] for m in msgs] == ["media", "media", "media"]
    assert all(m["streamSid"] == "MZ7" for m in msgs)
    # frames are base64 G.711; first two are full 160-byte frames
    assert len(base64.b64decode(msgs[0]["media"]["payload"])) == 160
    assert len(base64.b64decode(msgs[2]["media"]["payload"])) == 80


def test_menu_digit_flows_from_tier2_through_the_bridge_to_a_dtmf_event():
    session = _session()
    bridge = CallBridge("grp", session)
    bridge.bind_agent("MZ9")

    pcm, _ = synthesize_call()
    ulaw = pcm16_to_ulaw(pcm)
    for i in range(0, len(ulaw), 160):
        bridge.on_agent_audio(ulaw[i : i + 160])
        if session.finished:
            break

    dtmf = [m for m in bridge.drain_outbound() if m["event"] == "dtmf"]
    assert [m["dtmf"]["digits"] for m in dtmf] == ["2"]
