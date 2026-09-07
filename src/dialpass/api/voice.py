"""Webhook Twilio hits once a call we placed is answered.

We already told Twilio to dial with `url=<this endpoint>`. Twilio POSTs here
and expects TwiML back describing what to do — fork audio to our media
WebSocket and drop into the shared conference (Leg A). See
telephony/twiml.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response

from ..telephony.twiml import connect_stream, join_conference

router = APIRouter()


def _media_ws_url(public_base_url: str) -> str:
    # Twilio Media Streams require wss:// (or ws:// for plain http, only in dev
    # with non-TLS — ngrok gives us https, so wss).
    return public_base_url.replace("https://", "wss://").replace("http://", "ws://") + "/media"


@router.api_route("/twiml/voice", methods=["GET", "POST"])
def voice_twiml(request: Request, group: str = Query(...)) -> Response:
    """Leg A (the business call). Connect it to our media socket as a
    bidirectional audio pipe — our server is the mixer from here on."""
    settings = request.app.state.settings
    xml = connect_stream(_media_ws_url(settings.public_base_url), group, "agent")
    return Response(content=xml, media_type="application/xml")


@router.api_route("/twiml/join", methods=["GET", "POST"])
def join_twiml(conference: str = Query(...)) -> Response:
    """Leg B (the user's phone). Twilio fetches this when the user answers the
    call DialPass placed to them; they join the conference muted and wait there
    passively until the handoff unmutes them (M5 phase 3 switches this to a
    stream + software relay)."""
    xml = join_conference(conference, muted=True)
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
