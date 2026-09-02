"""TwiML generation. Plain strings — no Twilio SDK dependency in this module.

Exercised for real in M2 (media stream) and M5 (conference bridge).
"""

from __future__ import annotations

from xml.sax.saxutils import escape


def stream_and_conference(stream_url: str, conference_name: str) -> str:
    """Leg A (outbound to the business): fork audio to our media WebSocket, then
    drop into the shared conference."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Start>"
        f'<Stream url="{escape(stream_url)}" track="inbound_track"/>'
        "</Start>"
        "<Dial>"
        f'<Conference startConferenceOnEnter="true" endConferenceOnExit="true">'
        f"{escape(conference_name)}</Conference>"
        "</Dial>"
        "</Response>"
    )


def join_conference(conference_name: str, *, muted: bool, end_on_exit: bool = False) -> str:
    """Leg B (the user's phone): join muted, stay connected passively."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Say>This is DialPass. Connecting you now — please stay on the line.</Say>"
        "<Dial>"
        f'<Conference muted="{str(muted).lower()}" '
        f'startConferenceOnEnter="false" '
        f'endConferenceOnExit="{str(end_on_exit).lower()}" '
        'beep="false">'
        f"{escape(conference_name)}</Conference>"
        "</Dial>"
        "</Response>"
    )
