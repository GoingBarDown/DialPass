"""WebSocket endpoint for Twilio Media Streams.

Twilio sends JSON text frames: `connected`, `start`, `media` (base64 mu-law,
8 kHz, 20 ms), `stop`. We decode each media frame to PCM and feed the call's
`AgentSession`. Wired end to end in M2.

If `DIALPASS_RECORD_DIR` is set, every decoded frame is also written to a WAV
file for offline classifier tuning (M3).
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..telephony.audio import ulaw_to_pcm16
from ..telephony.recorder import WavRecorder

router = APIRouter()
log = logging.getLogger("dialpass.media")


def _make_dtmf_sender(app, call_id: str, conference: str | None):
    """Closure the session calls to press a digit. None (no-op) unless Twilio is
    configured and we know the conference to rejoin after the DTMF redirect."""
    twilio_client = app.state.twilio_client
    if twilio_client is None or not conference:
        return None

    def send(digits: str) -> None:
        twilio_client.send_dtmf(call_id, digits, conference)

    return send


@router.websocket("/media")
async def media_stream(ws: WebSocket) -> None:
    await ws.accept()
    app = ws.app
    settings = app.state.settings
    session = None
    recorder: WavRecorder | None = None
    call_id = "unknown"
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "start":
                start = msg.get("start", {})
                call_id = start.get("callSid") or start.get("streamSid") or "unknown"
                goal = app.state.pending_goals.pop(call_id, None)
                conference = (start.get("customParameters") or {}).get("conference")
                user_leg_sid = app.state.user_legs.pop(call_id, None)
                dtmf_sender = _make_dtmf_sender(app, call_id, conference)
                session = app.state.make_session(
                    call_id,
                    goal=goal,
                    dtmf_sender=dtmf_sender,
                    conference=conference,
                    user_leg_sid=user_leg_sid,
                )
                app.state.sessions[call_id] = session
                if settings.record_dir:
                    recorder = WavRecorder(
                        f"{settings.record_dir}/{call_id}.wav", settings.sample_rate
                    )
                    log.info("recording call %s to %s", call_id, recorder.path)
                log.info("media stream started for call %s", call_id)

            elif event == "media" and session is not None:
                payload = base64.b64decode(msg["media"]["payload"])
                pcm = ulaw_to_pcm16(payload)
                if recorder is not None:
                    recorder.write(pcm)
                if not session.finished:
                    session.feed_audio(pcm)
                elif recorder is None:
                    # Production: nothing left to do once the call is finished.
                    # In record mode we keep draining audio to the WAV so M3
                    # capture calls run their full length regardless of the FSM.
                    break

            elif event == "stop":
                break
    except WebSocketDisconnect:
        log.info("media stream disconnected for call %s", call_id)
    finally:
        if recorder is not None:
            recorder.close()
        if session is not None:
            session.close()
        app.state.sessions.pop(call_id, None)
        with contextlib.suppress(RuntimeError):
            await ws.close()
