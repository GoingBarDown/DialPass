"""TwiML generation. Plain strings — no Twilio SDK dependency in this module.

Exercised for real in M2 (media stream) and M5 (conference bridge).
"""

from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr


def connect_stream(stream_url: str, group_id: str, role: str) -> str:
    """A call leg as a bidirectional audio pipe to our media WebSocket (M5).

    `<Connect><Stream>` consumes the leg — there is no conference and no verb
    after it; our server is the mixer. `group` correlates a call's legs on our
    side; `role` is `agent` (the business call) or `user` (Leg B)."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Connect>"
        f"<Stream url={quoteattr(stream_url)}>"
        f"<Parameter name=\"group\" value={quoteattr(group_id)}/>"
        f"<Parameter name=\"role\" value={quoteattr(role)}/>"
        "</Stream>"
        "</Connect>"
        "</Response>"
    )


def join_conference(conference_name: str, *, muted: bool, end_on_exit: bool = False) -> str:
    """Leg B (the user's phone): join the conference and stay connected passively
    for the whole call. Joined muted at call start; the handoff unmutes this leg.
    `startConferenceOnEnter=false` so the room doesn't start (and the hold-music
    timer doesn't run) until Leg A — the outbound call — is actually in it."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Say>This is DialPass. Stay on the line — I'll connect you as soon as "
        "someone picks up.</Say>"
        "<Dial>"
        f'<Conference muted="{str(muted).lower()}" '
        f'startConferenceOnEnter="false" '
        f'endConferenceOnExit="{str(end_on_exit).lower()}" '
        'beep="false">'
        f"{escape(conference_name)}</Conference>"
        "</Dial>"
        "</Response>"
    )
