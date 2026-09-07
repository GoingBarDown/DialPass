"""WebSocket endpoint for Twilio Media Streams — the call's audio transport.

Since M5 the business call (Leg A) connects over a bidirectional `<Connect><Stream>`
(not the one-way `<Start><Stream>` + Conference of M2-M4). Twilio sends JSON text
frames: `connected`, `start`, `media` (base64 G.711, 8 kHz, 20 ms), `mark`, `stop`;
we send `media` back the same way. A `CallBridge` (see agent/bridge.py) is the hub
— this module is the socket's read and write loops.

`start.customParameters` carries `group` (correlates a call's legs on our side)
and `role` (`agent` = the business call; `user` = Leg B, wired in phase 3).

A keypress redirects the leg away and back (see CallBridge.press_dtmf), so a
second `start` for a `group` that already has a bridge is a **reconnect** — reuse
the session, just re-bind the new stream.

If `DIALPASS_RECORD_DIR` is set, Leg A's decoded audio is also written to a WAV
for offline classifier tuning (M3 dev aid).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..agent.bridge import CallBridge
from ..realtime.stream import StreamingProbe
from ..telephony.audio import ulaw_to_pcm16
from ..telephony.recorder import WavRecorder

router = APIRouter()
log = logging.getLogger("dialpass.media")


async def _drain_outbound(ws: WebSocket, bridge: CallBridge) -> None:
    """Ship whatever the bridge has queued toward the call. Polls every 20 ms —
    queued audio (AI speech, tones) paces itself frame by frame."""
    try:
        while True:
            for msg in bridge.drain_outbound():
                await ws.send_text(json.dumps(msg))
            await asyncio.sleep(0.02)
    except (WebSocketDisconnect, RuntimeError):
        pass


def _wire_dtmf(app, bridge: CallBridge, call_id: str, group: str) -> None:
    """Give the bridge a way to press a real telephony DTMF key: redirect the
    leg to `<Play digits>` then back to the stream TwiML."""
    twilio_client = app.state.twilio_client
    base = app.state.settings.public_base_url
    if twilio_client is None or not base:
        return
    reconnect_url = f"{base}/twiml/voice?group={group}"

    def press(digits: str) -> None:
        try:
            twilio_client.press_digits(call_id, digits, reconnect_url)
        except Exception:
            log.exception("media: DTMF redirect failed for %s", group)
            bridge.reconnecting = False

    bridge.dtmf_redirect = press


@router.websocket("/media")
async def media_stream(ws: WebSocket) -> None:
    await ws.accept()
    app = ws.app
    settings = app.state.settings
    bridge: CallBridge | None = None
    writer: asyncio.Task | None = None
    recorder: WavRecorder | None = None
    group = "unknown"
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            event = msg.get("event")

            if event == "start":
                start = msg.get("start", {})
                params = start.get("customParameters") or {}
                group = params.get("group") or start.get("streamSid") or "unknown"
                role = params.get("role", "agent")
                call_id = start.get("callSid") or "unknown"
                stream_sid = start.get("streamSid") or ""

                if role != "agent":
                    # Leg B (the user) lands here in phase 3; nothing to do yet.
                    log.info("media: %s leg for group %s — ignored pre-phase-3", role, group)
                    continue

                existing = app.state.bridges.get(group)
                if existing is not None:
                    # Reconnect after a DTMF redirect — keep the session & FSM.
                    bridge = existing
                    recorder = bridge.recorder
                    bridge.bind_agent(stream_sid, asyncio.get_running_loop())
                    bridge.reconnecting = False
                    writer = asyncio.create_task(_drain_outbound(ws, bridge))
                    log.info("media: agent leg reconnected, group %s", group)
                    continue

                goal = app.state.pending_goals.pop(group, None)
                user_leg_sid = app.state.user_legs.pop(group, None)
                session = app.state.make_session(
                    call_id, goal=goal, conference=group, user_leg_sid=user_leg_sid
                )
                bridge = CallBridge(group, session)  # wires session.dtmf_sender
                bridge.bind_agent(stream_sid, asyncio.get_running_loop())
                _wire_dtmf(app, bridge, call_id, group)
                if settings.openai_api_key:
                    # Swap the turn-based probe for the streaming one — it greets
                    # the line and listens over a live Realtime socket.
                    session.tier2 = StreamingProbe(session.tier2, bridge.probe_from_thread)
                app.state.bridges[group] = bridge
                app.state.sessions[call_id] = session
                writer = asyncio.create_task(_drain_outbound(ws, bridge))

                if settings.record_dir:
                    recorder = WavRecorder(
                        f"{settings.record_dir}/{call_id}.wav", settings.sample_rate
                    )
                    bridge.recorder = recorder  # survives DTMF reconnects
                    log.info("recording call %s to %s", call_id, recorder.path)
                log.info("media: agent leg connected, group %s call %s", group, call_id)

            elif event == "media" and bridge is not None:
                payload = base64.b64decode(msg["media"]["payload"])
                if recorder is not None:
                    recorder.write(ulaw_to_pcm16(payload))
                bridge.on_agent_audio(payload)

            elif event == "stop":
                break
    except WebSocketDisconnect:
        log.info("media: stream disconnected for group %s", group)
    finally:
        if writer is not None:
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer
        # A reconnect is imminent (DTMF redirect) — leave the bridge, its session
        # and its recorder in place for the next socket. Otherwise it's really over.
        if bridge is not None and not bridge.reconnecting:
            bridge.close()  # also closes bridge.recorder
            app.state.bridges.pop(bridge.group_id, None)
            app.state.sessions.pop(bridge.session.call_id, None)
        elif bridge is None and recorder is not None:
            recorder.close()
        with contextlib.suppress(RuntimeError):
            await ws.close()
