"""Test doubles for the telephony layer — no network, records what it was asked."""

from __future__ import annotations

from dialpass.telephony.twilio_client import PlacedCall


class FakeTwilioClient:
    """Stands in for `TwilioClient`. Every method records its call so a test can
    assert on it; `place_outbound_call` / `ring_user` hand back deterministic SIDs."""

    def __init__(self, *, ring_user_raises: Exception | None = None) -> None:
        self.outbound: list[dict] = []
        self.user_rings: list[dict] = []
        self.digit_presses: list[dict] = []
        self.hangups: list[str] = []
        self.sms: list[dict] = []
        self._ring_user_raises = ring_user_raises

    def place_outbound_call(
        self, to_number: str, twiml_url: str, conference_name: str
    ) -> PlacedCall:
        self.outbound.append(
            {"to": to_number, "url": twiml_url, "conference": conference_name}
        )
        return PlacedCall(call_sid="CAagent0001", conference_name=conference_name)

    def ring_user(self, user_number: str, twiml_url: str, conference_name: str) -> PlacedCall:
        if self._ring_user_raises is not None:
            raise self._ring_user_raises
        self.user_rings.append(
            {"to": user_number, "url": twiml_url, "conference": conference_name}
        )
        return PlacedCall(call_sid="CAuser0001", conference_name=conference_name)

    def press_digits(self, call_sid: str, digits: str, reconnect_url: str) -> None:
        self.digit_presses.append(
            {"sid": call_sid, "digits": digits, "reconnect_url": reconnect_url}
        )

    def hang_up(self, call_sid: str) -> None:
        self.hangups.append(call_sid)

    def send_sms(self, to_number: str, body: str) -> None:
        self.sms.append({"to": to_number, "body": body})
