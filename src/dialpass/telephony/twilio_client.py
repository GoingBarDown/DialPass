"""Thin wrapper over Twilio's REST API for placing/controlling calls. Lands in M2.

Kept behind this interface so `agent/` never imports the Twilio SDK and the
offline harness can substitute a fake.
"""

from __future__ import annotations

from dataclasses import dataclass

from twilio.rest import Client


@dataclass(slots=True)
class PlacedCall:
    call_sid: str
    conference_name: str


class TwilioClient:
    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        self._client = Client(account_sid, auth_token)
        self._from_number = from_number

    def place_outbound_call(
        self, to_number: str, twiml_url: str, conference_name: str
    ) -> PlacedCall:
        """Dial `to_number` (Leg A). Twilio will POST/GET `twiml_url` once the
        call is answered to fetch what to do next (see api/voice.py)."""
        call = self._client.calls.create(to=to_number, from_=self._from_number, url=twiml_url)
        return PlacedCall(call_sid=call.sid, conference_name=conference_name)

    def ring_user(self, user_number: str, twiml_url: str, conference_name: str) -> PlacedCall:
        """Dial the user's own phone (Leg B), joining the same conference muted."""
        call = self._client.calls.create(to=user_number, from_=self._from_number, url=twiml_url)
        return PlacedCall(call_sid=call.sid, conference_name=conference_name)

    def set_participant_muted(self, conference: str, call_sid: str, *, muted: bool) -> None:
        raise NotImplementedError("Conference control lands in M5")
