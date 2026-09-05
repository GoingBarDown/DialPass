"""POST /calls — the web form's trigger endpoint.

M1 validated and echoed. M2 wires it to `TwilioClient`: places the outbound
call (Leg A) with a `url` pointing at api/voice.py, which Twilio fetches once
the call connects to get the media-stream + conference TwiML.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()


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
    twiml_url = f"{settings.public_base_url}/twiml/voice?conference={conference_name}"
    placed = twilio_client.place_outbound_call(req.business_number, twiml_url, conference_name)

    # media.py looks this up by Twilio's call SID when the media stream starts.
    app.state.pending_goals[placed.call_sid] = req.goal

    return CallAccepted(call_id=placed.call_sid, state="DIALING")
