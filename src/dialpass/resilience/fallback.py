"""What we text the user when a call can't finish the normal way.

Every graceful-degradation path (circuit open, Tier 2 down, the line never
connected, the call dropped and couldn't be recovered, the user hung up before
the handoff) ends here rather than stranding the user in silence.
"""

from __future__ import annotations

_DEFAULT = "DialPass couldn't finish that call. Sorry about that — give it another try in a bit."

_BY_REASON: dict[str, str] = {
    "tier2_unavailable": (
        "DialPass is having trouble with the part that listens to the menu right "
        "now. The call was ended — please try again shortly."
    ),
    "tier2_circuit_open": (
        "DialPass is having trouble with the part that listens to the menu right "
        "now. The call was ended — please try again shortly."
    ),
    "non_connect": (
        "That number went to voicemail or didn't connect, so DialPass hung up. "
        "Double-check the number and try again."
    ),
    "dropped": (
        "The call to that number dropped and DialPass couldn't get back through. Please try again."
    ),
    "redial_failed": ("The call dropped and DialPass couldn't redial. Please try again."),
    "user_left": (
        "Looks like you hung up before DialPass reached a person. Call back when "
        "you're ready and it'll try again."
    ),
    "handoff_error": (
        "DialPass reached a person but couldn't connect you. Sorry — please try again."
    ),
}


def fallback_message(reason: str) -> str:
    return _BY_REASON.get(reason, _DEFAULT)
