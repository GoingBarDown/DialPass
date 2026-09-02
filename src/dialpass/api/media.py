"""WebSocket endpoint for Twilio Media Streams.

Twilio sends JSON text frames: `connected`, `start`, `media` (base64 mu-law,
8 kHz, 20 ms), `stop`. We decode each media frame to PCM and feed the call's
`AgentSession`. Wired end to end in M2; the handler exists now so the offline
`--ws` harness and tests have a target.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..telephony.audio import ulaw_to_pcm16

router = APIRouter()
log = logging.getLogger("dialpass.media")


@router.websocket("/media")
async def media_stream(ws: WebSocket) -> None:
    await ws.accept()
    app = ws.app
    session = None
    call_id = "unknown"
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "start":
                start = msg.get("start", {})
                call_id = start.get("callSid") or start.get("streamSid") or "unknown"
                session = app.state.make_session(call_id)
                app.state.sessions[call_id] = session
                log.info("media stream started for call %s", call_id)

            elif event == "media" and session is not None:
                payload = base64.b64decode(msg["media"]["payload"])
                session.feed_audio(ulaw_to_pcm16(payload))
                if session.finished:
                    break

            elif event == "stop":
                break
    except WebSocketDisconnect:
        log.info("media stream disconnected for call %s", call_id)
    finally:
        app.state.sessions.pop(call_id, None)
        with contextlib.suppress(RuntimeError):
            await ws.close()
