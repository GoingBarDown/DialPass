"""CallBridge — the per-call audio hub between the Twilio socket and the agent."""

from __future__ import annotations

import base64
import time

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


def _press_and_wait(bridge: CallBridge, digits: str) -> list[str]:
    got: list[str] = []
    bridge.dtmf_redirect = got.append
    bridge.press_dtmf(digits)
    for _ in range(100):
        if got:
            break
        time.sleep(0.005)
    return got


def test_press_dtmf_redirects_with_real_telephony_dtmf_and_flags_reconnect():
    bridge = CallBridge("grp", _session())
    bridge.bind_agent("MZ42")
    assert _press_and_wait(bridge, "2#") == ["2#"]
    assert bridge.reconnecting is True
    assert bridge.drain_agent() == []  # no audio path for DTMF


def test_press_dtmf_strips_non_dtmf_characters():
    bridge = CallBridge("grp", _session())
    bridge.bind_agent("MZ1")
    assert _press_and_wait(bridge, "2; DROP TABLE--1") == ["21"]


def test_press_dtmf_before_bind_is_dropped():
    bridge = CallBridge("grp", _session())
    got: list[str] = []
    bridge.dtmf_redirect = got.append
    bridge.press_dtmf("2")
    time.sleep(0.05)
    assert got == [] and bridge.reconnecting is False


def test_play_to_agent_splits_into_20ms_frames():
    bridge = CallBridge("grp", _session())
    bridge.bind_agent("MZ7")
    bridge.play_to_agent(np.zeros(400, dtype=np.int16))  # 50 ms -> 3 frames (160+160+80)

    msgs = bridge.drain_agent()
    assert [m["event"] for m in msgs] == ["media", "media", "media"]
    assert all(m["streamSid"] == "MZ7" for m in msgs)
    # frames are base64 G.711; first two are full 160-byte frames
    assert len(base64.b64decode(msgs[0]["media"]["payload"])) == 160
    assert len(base64.b64decode(msgs[2]["media"]["payload"])) == 80


def test_run_probe_tees_inbound_audio_and_returns_the_verdict(monkeypatch):
    import asyncio

    import dialpass.agent.bridge as bridge_mod
    from dialpass.realtime.protocol import ProbeOutcome

    heard: list[bytes] = []

    async def fake_exchange(api_key, model, *, inbound, play, **kw):
        for _ in range(3):
            heard.append(await inbound.get())
        play(np.zeros(160, dtype=np.int16))  # "greeting" into the call
        return ProbeOutcome(is_human=True, transcript="hello there")

    monkeypatch.setattr(bridge_mod, "probe_exchange", fake_exchange)
    bridge = CallBridge("grp", _session())
    bridge.bind_agent("MZ1")

    async def drive():
        task = asyncio.create_task(bridge.run_probe())
        for _ in range(5):
            await asyncio.sleep(0)
            bridge.on_agent_audio(b"\xff" * 160)
        return await task

    outcome = asyncio.run(drive())

    assert outcome.is_human is True
    assert len(heard) == 3
    assert any(m["event"] == "media" for m in bridge.drain_agent())  # greeting queued
    assert bridge._probe_inbound is None  # cleaned up


def test_relay_is_closed_before_the_handoff():
    bridge = CallBridge("grp", _session())
    bridge.bind_agent("MZa")
    bridge.bind_user("MZb")
    # user talking while we're still navigating goes nowhere
    bridge.on_user_audio(b"\xff" * 160)
    assert bridge.drain_agent() == []


def test_begin_handoff_opens_a_two_way_relay_and_notifies_the_user():
    bridge = CallBridge("grp", _session())
    bridge.bind_agent("MZa")  # no loop -> begin_handoff runs synchronously
    bridge.bind_user("MZb")
    texts: list[str] = []
    bridge.notify_user = texts.append

    bridge.begin_handoff()
    assert bridge.relay_open is True
    assert len(texts) == 1

    bridge.on_user_audio(b"\x10" * 160)
    bridge.on_agent_audio(b"\x20" * 160)
    to_agent = bridge.drain_agent()
    to_user = bridge.drain_user()
    assert [m["streamSid"] for m in to_agent] == ["MZa"]  # user's voice -> agent leg
    assert [m["streamSid"] for m in to_user] == ["MZb"]  # agent's voice -> user leg


def test_begin_handoff_is_idempotent():
    bridge = CallBridge("grp", _session())
    bridge.bind_agent("MZa")
    texts: list[str] = []
    bridge.notify_user = texts.append
    bridge.begin_handoff()
    bridge.begin_handoff()
    assert len(texts) == 1


def test_session_bridge_hook_is_wired_to_begin_handoff():
    session = _session()
    bridge = CallBridge("grp", session)
    assert session.on_bridge == bridge.begin_handoff


def test_menu_digit_from_tier2_triggers_a_dtmf_redirect():
    session = _session()
    bridge = CallBridge("grp", session)
    bridge.bind_agent("MZ9")
    redirected: list[str] = []
    bridge.dtmf_redirect = redirected.append

    pcm, _ = synthesize_call()
    ulaw = pcm16_to_ulaw(pcm)
    for i in range(0, len(ulaw), 160):
        bridge.on_agent_audio(ulaw[i : i + 160])
        if session.finished:
            break

    for _ in range(100):
        if redirected:
            break
        time.sleep(0.005)
    assert redirected == ["2"]  # FakeTier2's fixed digit, via the redirect path
    assert bridge.reconnecting is True
