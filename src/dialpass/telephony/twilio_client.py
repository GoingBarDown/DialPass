"""Thin wrapper over Twilio's REST API for placing/controlling calls. Lands in M2.

Kept behind this interface so `agent/` never imports the Twilio SDK and the
offline harness can substitute a fake.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class PlacedCall:
    call_sid: str
    conference_name: str


class TwilioClient:
    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from_number = from_number

    def place_outbound_call(self, to_number: str, twiml_url: str) -> PlacedCall:
        raise NotImplementedError("Outbound calling lands in M2")

    def ring_user(self, user_number: str, twiml_url: str) -> PlacedCall:
        raise NotImplementedError("User callback lands in M2")

    def set_participant_muted(self, conference: str, call_sid: str, *, muted: bool) -> None:
        raise NotImplementedError("Conference control lands in M5")
