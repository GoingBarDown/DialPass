"""TwiML generation. Plain strings — no Twilio SDK dependency in this module.

Exercised for real in M2 (media stream) and M5 (conference bridge).
"""

from __future__ import annotations

from xml.sax.saxutils import escape


def stream_and_conference(stream_url: str, conference_name: str) -> str:
    """Leg A (outbound to the business): fork audio to our media WebSocket, then
    drop into the shared conference. The conference name rides along as a Stream
    <Parameter> so the media handler gets it in the `start` event (it needs it to
    redirect the call for DTMF in M4)."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Start>"
        f'<Stream url="{escape(stream_url)}" track="inbound_track">'
        f'<Parameter name="conference" value="{escape(conference_name)}"/>'
        "</Stream>"
        "</Start>"
        "<Dial>"
        f'<Conference startConferenceOnEnter="true" endConferenceOnExit="true">'
        f"{escape(conference_name)}</Conference>"
        "</Dial>"
        "</Response>"
    )


def play_digits_then_conference(digits: str, conference_name: str) -> str:
    """M4 DTMF injection. Redirect target for `calls(sid).update()`: play the
    touch-tones toward the far end, then drop back into the conference.

    The `w`s are Twilio's 0.5s pauses — a lead-in so the first tone isn't clipped
    by the redirect, and a tail so tones aren't rushed. The `<Start><Stream>`
    from the original TwiML survives this redirect (verified), so it isn't
    re-added here.
    """
    safe_digits = "".join(c for c in digits if c in "0123456789*#w")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Play digits="ww{escape(safe_digits)}w"/>'
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
