"""POST /calls — the web form's trigger endpoint.

M1 validated and echoed. M2 wires it to `TwilioClient`: places the outbound
call (Leg A) with a `url` pointing at api/voice.py, which Twilio fetches once
the call connects to get the media-stream + conference TwiML.

M5 phase 1 also dials the user's own phone (Leg B) and joins it to the same
conference muted, so the handoff later is just an unmute — no ring at the worst
possible moment. See docs/design-decisions.md#decision-1--bridging.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()
log = logging.getLogger("dialpass.calls")


class CallRequest(BaseModel):
    business_number: str = Field(min_length=3)
    user_number: str = Field(min_length=3)
    goal: str | None = None


class CallAccepted(BaseModel):
    call_id: str
    state: str


@router.post("/calls", response_model=CallAccepted)
def create_call(req: CallRequest, request: Request) -> CallAccepted:
    app = request.app
    twilio_client = app.state.twilio_client
    settings = app.state.settings

    if twilio_client is None or not settings.public_base_url:
        raise HTTPException(
            status_code=501,
            detail={
                "message": (
                    "Twilio isn't configured. Set DIALPASS_TWILIO_ACCOUNT_SID/"
                    "AUTH_TOKEN/FROM_NUMBER and DIALPASS_PUBLIC_BASE_URL in .env, "
                    "or use `make sim` for the offline pipeline."
                ),
                "received": req.model_dump(),
            },
        )

    conference_name = f"dialpass-{uuid.uuid4().hex[:12]}"
    base = settings.public_base_url
    voice_url = f"{base}/twiml/voice?conference={conference_name}"
    placed = twilio_client.place_outbound_call(req.business_number, voice_url, conference_name)

    # media.py looks this up by Twilio's call SID when the media stream starts.
    app.state.pending_goals[placed.call_sid] = req.goal

    # Leg B: ring the user now, join them muted. A failure here doesn't sink the
    # call — the agent can still navigate; the bridge just has no one to unmute
    # (M8 turns that into a fallback notification).
    join_url = f"{base}/twiml/join?conference={conference_name}"
    try:
        user_leg = twilio_client.ring_user(req.user_number, join_url, conference_name)
        app.state.user_legs[placed.call_sid] = user_leg.call_sid
    except Exception:
        log.exception("call %s: failed to ring user leg %s", placed.call_sid, req.user_number)

    return CallAccepted(call_id=placed.call_sid, state="DIALING")
