"""WebSocket endpoint for Twilio Media Streams — the call's audio transport.

Since M5 the call legs connect over a bidirectional `<Connect><Stream>` (not the
one-way `<Start><Stream>` + Conference of M2-M4). Twilio sends JSON text frames:
`connected`, `start`, `media` (base64 G.711, 8 kHz, 20 ms), `mark`, `stop`; we can
send `media` / `dtmf` back the same way. A `CallBridge` (see agent/bridge.py) is
the hub — this module is just the socket's read and write loops.

`start.customParameters` carries `group` (correlates the call's legs on our side)
and `role` (`agent` = the business call; `user` = Leg B, wired in phase 3).

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
from ..telephony.audio import ulaw_to_pcm16
from ..telephony.recorder import WavRecorder

router = APIRouter()
log = logging.getLogger("dialpass.media")


async def _drain_outbound(ws: WebSocket, bridge: CallBridge) -> None:
    """Ship whatever the bridge has queued toward the call. Polls every 20 ms —
    DTMF goes out at once, queued audio paces itself frame by frame."""
    try:
        while True:
            for msg in bridge.drain_outbound():
                await ws.send_text(json.dumps(msg))
            await asyncio.sleep(0.02)
    except (WebSocketDisconnect, RuntimeError):
        pass


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

                goal = app.state.pending_goals.pop(group, None)
                user_leg_sid = app.state.user_legs.pop(group, None)
                session = app.state.make_session(
                    call_id,
                    goal=goal,
                    conference=group,
                    user_leg_sid=user_leg_sid,
                )
                bridge = CallBridge(group, session)  # wires session.dtmf_sender
                bridge.bind_agent(stream_sid)
                app.state.bridges[group] = bridge
                app.state.sessions[call_id] = session
                writer = asyncio.create_task(_drain_outbound(ws, bridge))

                if settings.record_dir:
                    recorder = WavRecorder(
                        f"{settings.record_dir}/{call_id}.wav", settings.sample_rate
                    )
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
        if recorder is not None:
            recorder.close()
        if bridge is not None:
            bridge.close()
            app.state.bridges.pop(bridge.group_id, None)
            app.state.sessions.pop(bridge.session.call_id, None)
        with contextlib.suppress(RuntimeError):
            await ws.close()
