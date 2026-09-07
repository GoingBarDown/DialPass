"""M5 spike — prove bidirectional Twilio Media Streams.

Throwaway. Answers one question before we rebuild the telephony layer: with
`<Connect><Stream>` (not `<Start><Stream>`), can our server send `media` frames
back over the WebSocket and have Twilio play them into the live call?

Flow: place a call whose TwiML is `/twiml/spike-bidi`. Twilio opens a WS to
`/spike-media`. On `start` we (1) play a 1 kHz tone toward the far end for ~1.5s,
then (2) echo the caller's audio back with a short delay. If the callee hears the
tone and then their own voice, bidirectional streaming works.

Delete this module (and its two routes in main.py) once the real bridge lands.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging

import numpy as np
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect

from ..telephony.audio import pcm16_to_ulaw

router = APIRouter()
log = logging.getLogger("dialpass.spike")

_RATE = 8000
_FRAME = 160  # samples per 20 ms frame at 8 kHz


def _tone_frames(freq: float, seconds: float, amp: float = 0.3) -> list[bytes]:
    n = int(seconds * _RATE)
    t = np.arange(n) / _RATE
    pcm = (amp * 32767 * np.sin(2 * np.pi * freq * t)).astype(np.int16)
    return [pcm16_to_ulaw(pcm[i : i + _FRAME]) for i in range(0, n, _FRAME)]


def _media_ws_url(base: str) -> str:
    return base.replace("https://", "wss://").replace("http://", "ws://") + "/spike-media"


@router.post("/spike-call")
async def spike_call(request: Request) -> dict:
    """Dial a number with the bidirectional-stream TwiML. Body: {"to": "+1..."}."""
    app = request.app
    tw = app.state.twilio_client
    settings = app.state.settings
    if tw is None or not settings.public_base_url:
        return {"error": "twilio not configured"}
    body = await request.json()
    call = tw.place_outbound_call(
        body["to"], f"{settings.public_base_url}/twiml/spike-bidi", "spike"
    )
    return {"call_sid": call.call_sid}


@router.post("/dev-call")
async def dev_call(request: Request) -> dict:
    """Place only Leg A (the agent call) — no user leg, no second ring. Body:
    {"to": "+1...", "goal": "..."}. For testing the agent path by hand."""
    import uuid

    app = request.app
    tw = app.state.twilio_client
    settings = app.state.settings
    if tw is None or not settings.public_base_url:
        return {"error": "twilio not configured"}
    body = await request.json()
    group = f"dialpass-{uuid.uuid4().hex[:12]}"
    app.state.pending_goals[group] = body.get("goal")
    call = tw.place_outbound_call(
        body["to"], f"{settings.public_base_url}/twiml/voice?group={group}", group
    )
    return {"call_sid": call.call_sid, "group": group}


@router.api_route("/twiml/spike-bidi", methods=["GET", "POST"])
def spike_twiml(request: Request) -> Response:
    url = _media_ws_url(request.app.state.settings.public_base_url)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Connect><Stream url="{url}"/></Connect>'
        "</Response>"
    )
    return Response(content=xml, media_type="application/xml")


@router.websocket("/spike-media")
async def spike_media(ws: WebSocket) -> None:
    await ws.accept()
    stream_sid = ""
    echo: asyncio.Queue[bytes] = asyncio.Queue()
    delay_frames = 25  # ~500 ms

    async def send_frame(payload_ulaw: bytes) -> None:
        await ws.send_text(
            json.dumps(
                {
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": base64.b64encode(payload_ulaw).decode("ascii")},
                }
            )
        )

    try:
        while True:
            msg = json.loads(await ws.receive_text())
            event = msg.get("event")

            if event == "start":
                stream_sid = msg["start"]["streamSid"]
                log.info("spike: start sid=%s tracks=%s", stream_sid, msg["start"].get("tracks"))
                # 1) tone toward the far end
                for frame in _tone_frames(1000.0, 1.5):
                    await send_frame(frame)
                    await asyncio.sleep(0.02)
                log.info("spike: tone sent, now echoing with ~500ms delay")

            elif event == "media":
                # echo back, delayed, so the caller hears themselves
                await echo.put(base64.b64decode(msg["media"]["payload"]))
                if echo.qsize() > delay_frames:
                    await send_frame(await echo.get())

            elif event == "mark":
                log.info("spike: mark %s", msg.get("mark"))

            elif event == "stop":
                log.info("spike: stop")
                break
    except WebSocketDisconnect:
        log.info("spike: ws disconnected (streamSid=%s)", stream_sid)
    finally:
        with contextlib.suppress(RuntimeError):
            await ws.close()
