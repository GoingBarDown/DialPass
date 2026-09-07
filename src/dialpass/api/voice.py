"""Webhook Twilio hits once a call we placed is answered.

We already told Twilio to dial with `url=<this endpoint>`. Twilio POSTs here
and expects TwiML back describing what to do — fork audio to our media
WebSocket and drop into the shared conference (Leg A). See
telephony/twiml.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response

from ..telephony.twiml import connect_stream

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
def join_twiml(request: Request, group: str = Query(...)) -> Response:
    """Leg B (the user's phone). Twilio fetches this when the user answers the
    call DialPass placed to them; they connect to our media socket (role=user)
    and wait — hearing only the intro line — until the handoff opens the relay
    between them and the business call."""
    settings = request.app.state.settings
    xml = connect_stream(
        _media_ws_url(settings.public_base_url),
        group,
        "user",
        intro=(
            "This is DialPass. Stay on the line — I'll connect you the moment someone picks up."
        ),
    )
    return Response(content=xml, media_type="application/xml")


_MUSIC = "http://demo.twilio.com/docs/classic.mp3"


@router.api_route("/twiml/test-ivr", methods=["GET", "POST"])
def test_ivr_twiml(request: Request) -> Response:
    """Dev only: a reproducible phone tree to point a Twilio number at, so
    DialPass has something with a real DTMF menu + a scripted agent pickup to
    navigate end to end. Not a production path."""
    base = request.app.state.settings.public_base_url
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Gather input="dtmf" numDigits="1" timeout="12" action="{base}/twiml/test-ivr-branch">'
        "<Say>Thank you for calling Acme Corporation. "
        "For billing, press 1. For technical support, press 2. "
        "To speak with an agent, press 0.</Say>"
        "</Gather>"
        "<Say>We did not receive a selection. Goodbye.</Say>"
        "</Response>"
    )
    return Response(content=xml, media_type="application/xml")


@router.api_route("/twiml/test-ivr-branch", methods=["GET", "POST"])
async def test_ivr_branch_twiml(request: Request) -> Response:
    form = await request.form()
    digit = str(form.get("Digits", ""))
    if digit in ("0", "2"):
        # Hold music long enough to reach ON_HOLD, then an "agent" who keeps
        # talking for ~20s so the probe catches him mid-sentence and the relay
        # opens while he's still on the line (a one-line greeting then silence
        # classifies as NOT HUMAN — the probe hears nothing back).
        v = 'voice="Polly.Matthew"'
        body = (
            "<Say>Please hold while we connect you to the next available agent.</Say>"
            f'<Play loop="1">{_MUSIC}</Play>'
            f"<Say {v}>Hi there, thanks for holding. This is Mark on the support desk.</Say>"
            '<Pause length="1"/>'
            f"<Say {v}>Can you hear me okay? Who am I speaking with today?</Say>"
            '<Pause length="2"/>'
            f"<Say {v}>Hello? This is Mark, I'm still here. How can I help you?</Say>"
            '<Pause length="2"/>'
            f"<Say {v}>Take your time — I'll stay on the line with you.</Say>"
            '<Pause length="3"/>'
            f"<Say {v}>Okay, still holding for you here. Let me know when you're ready.</Say>"
            '<Pause length="8"/>'
            "<Say>I can't hear anyone. I'll try back later. Goodbye.</Say>"
        )
    else:
        body = (
            "<Say>You selected billing. All of our representatives are currently busy. "
            "Please continue to hold.</Say>"
            f'<Play loop="10">{_MUSIC}</Play>'
        )
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'
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
