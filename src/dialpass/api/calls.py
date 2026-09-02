"""POST /calls — the web form's trigger endpoint.

M1: validates and echoes. M2 wires it to `TwilioClient` (place the outbound call
with the media-stream fork, ring the user's phone to join the conference muted).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
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
def create_call(req: CallRequest) -> CallAccepted:
    raise HTTPException(
        status_code=501,
        detail={
            "message": "Outbound calling lands in M2. Use `make sim` for the offline pipeline.",
            "received": req.model_dump(),
        },
    )
