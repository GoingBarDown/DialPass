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

    # `group` correlates this call's legs on our side (media.py keys everything by
    # it — the leg SIDs aren't known until each leg connects).
    group = f"dialpass-{uuid.uuid4().hex[:12]}"
    base = settings.public_base_url
    voice_url = f"{base}/twiml/voice?group={group}"
    placed = twilio_client.place_outbound_call(req.business_number, voice_url, group)

    app.state.pending_goals[group] = req.goal
    # Kept for the whole call so a dropped agent leg can be re-dialed (M8).
    app.state.call_meta[group] = {"business_number": req.business_number, "redials": 0}

    # Leg B: ring the user now; they connect to our media socket (role=user) and
    # wait there until the handoff opens the relay. A failure here doesn't sink
    # the call — the agent can still navigate; the bridge just has no one to
    # patch in (M8 turns that into a fallback notification).
    join_url = f"{base}/twiml/join?group={group}"
    try:
        user_leg = twilio_client.ring_user(req.user_number, join_url, group)
        app.state.user_legs[group] = user_leg.call_sid
        app.state.user_numbers[group] = req.user_number
    except Exception:
        log.exception("call %s: failed to ring user leg %s", placed.call_sid, req.user_number)

    return CallAccepted(call_id=placed.call_sid, state="DIALING")
