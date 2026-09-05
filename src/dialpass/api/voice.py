"""Webhook Twilio hits once a call we placed is answered.

We already told Twilio to dial with `url=<this endpoint>`. Twilio POSTs here
and expects TwiML back describing what to do — fork audio to our media
WebSocket and drop into the shared conference (Leg A). See
telephony/twiml.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response

from ..telephony.twiml import stream_and_conference

router = APIRouter()


def _media_ws_url(public_base_url: str) -> str:
    # Twilio Media Streams require wss:// (or ws:// for plain http, only in dev
    # with non-TLS — ngrok gives us https, so wss).
    return public_base_url.replace("https://", "wss://").replace("http://", "ws://") + "/media"


@router.api_route("/twiml/voice", methods=["GET", "POST"])
def voice_twiml(request: Request, conference: str = Query(...)) -> Response:
    settings = request.app.state.settings
    stream_url = _media_ws_url(settings.public_base_url)
    xml = stream_and_conference(stream_url, conference)
    return Response(content=xml, media_type="application/xml")
