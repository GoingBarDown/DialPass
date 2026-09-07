"""Test doubles for the telephony layer — no network, records what it was asked."""

from __future__ import annotations

from dialpass.telephony.twilio_client import PlacedCall


class FakeTwilioClient:
    """Stands in for `TwilioClient`. Every method records its call so a test can
    assert on it; `place_outbound_call` / `ring_user` hand back deterministic SIDs."""

    def __init__(self, *, ring_user_raises: Exception | None = None) -> None:
        self.outbound: list[dict] = []
        self.user_rings: list[dict] = []
        self.dtmf: list[dict] = []
        self.mutes: list[dict] = []
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

    def send_dtmf(self, call_sid: str, digits: str, conference_name: str) -> None:
        self.dtmf.append({"sid": call_sid, "digits": digits, "conference": conference_name})

    def set_participant_muted(
        self, conference: str, call_sid: str, *, muted: bool
    ) -> None:
        self.mutes.append({"conference": conference, "sid": call_sid, "muted": muted})
