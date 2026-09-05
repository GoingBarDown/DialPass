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


@router.api_route("/twiml/holdmusic-test", methods=["GET", "POST"])
def holdmusic_test_twiml(request: Request) -> Response:
    """M3 dev only: fork audio to /media and play classic hold music into the
    call so we can capture real phone-band music to tune Tier 1 against.
    Not wired into any production path — remove after M3."""
    settings = request.app.state.settings
    stream_url = _media_ws_url(settings.public_base_url)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Start><Stream url="{stream_url}" track="outbound_track"/></Start>'
        '<Play loop="6">http://demo.twilio.com/docs/classic.mp3</Play>'
        "</Response>"
    )
    return Response(content=xml, media_type="application/xml")
