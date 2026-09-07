"""TwiML generation. Plain strings — no Twilio SDK dependency in this module.

Exercised for real in M2 (media stream) and M5 (conference bridge).
"""

from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr


def connect_stream(stream_url: str, group_id: str, role: str, *, intro: str | None = None) -> str:
    """A call leg as a bidirectional audio pipe to our media WebSocket (M5).

    `<Connect><Stream>` consumes the leg — there is no conference and no verb
    after it; our server is the mixer. `group` correlates a call's legs on our
    side; `role` is `agent` (the business call) or `user` (Leg B). `intro` is an
    optional line spoken to the leg before the stream opens — Leg B hears "stay
    on the line" so the silence before the handoff isn't alarming."""
    say = f"<Say>{escape(intro)}</Say>" if intro else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"{say}"
        "<Connect>"
        f"<Stream url={quoteattr(stream_url)}>"
        f'<Parameter name="group" value={quoteattr(group_id)}/>'
        f'<Parameter name="role" value={quoteattr(role)}/>'
        "</Stream>"
        "</Connect>"
        "</Response>"
    )
