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
        """Dial the user's own phone (Leg B). `twiml_url` connects them to our
        media socket (role=user); they wait there until the handoff opens the
        relay. `conference_name` is the group id, carried for bookkeeping."""
        call = self._client.calls.create(to=user_number, from_=self._from_number, url=twiml_url)
        return PlacedCall(call_sid=call.sid, conference_name=conference_name)

    def send_sms(self, to_number: str, body: str) -> None:
        """Fire-and-forget text — the handoff notification (M5 phase 3) and the
        circuit-breaker fallback (M8)."""
        self._client.messages.create(to=to_number, from_=self._from_number, body=body)

    def press_digits(self, call_sid: str, digits: str, reconnect_url: str) -> None:
        """Send real telephony DTMF, then reconnect the media stream.

        Audio tones injected over a Media Stream aren't recognized by IVRs —
        Twilio only converts in-band DTMF to signalling at the PSTN edge. So we
        briefly redirect the leg to `<Play digits>` (Twilio generates the tones
        at the telephony layer) and then `<Redirect>` back to the TwiML that
        re-opens `<Connect><Stream>`. The stream drops for ~1-2s per keypress.
        """
        safe = "".join(c for c in digits if c in "0123456789*#w")
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Response><Play digits="w{safe}"/><Pause length="1"/>'
            f"<Redirect>{reconnect_url}</Redirect></Response>"
        )
        self._client.calls(call_sid).update(twiml=twiml)

    def hang_up(self, call_sid: str) -> None:
        """End a call leg. Recovery path (M8) — e.g. drop Leg A if the user's
        gone and can't be re-reached."""
        self._client.calls(call_sid).update(status="completed")
