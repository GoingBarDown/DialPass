"""Tier 1's per-frame output taxonomy.

This is a fast, noisy, low-level signal — NOT the call's FSM state. The FSM
(`state.py`) rides through per-frame flicker and only moves on a sustained label.
"""

from __future__ import annotations

from enum import StrEnum


class Label(StrEnum):
    RINGBACK = "RINGBACK"  # the company's line is ringing
    MENU_SPEAKING = "MENU_SPEAKING"  # an IVR menu is listing options
    MENU_AWAITING_INPUT = "MENU_AWAITING_INPUT"  # menu done, waiting for a keypress
    HOLD_MUSIC = "HOLD_MUSIC"  # on hold, music playing
    HOLD_INTERJECTION = "HOLD_INTERJECTION"  # periodic "please continue to hold"
    LIVE_SPEECH_CANDIDATE = "LIVE_SPEECH_CANDIDATE"  # speech that might be a human
    VOICEMAIL = "VOICEMAIL"  # answering machine / voicemail greeting
    ERROR_TONE = "ERROR_TONE"  # busy / not-in-service / reorder (SIT)
    SILENCE = "SILENCE"  # dead air
    UNKNOWN = "UNKNOWN"  # sound present, can't classify yet


# A menu is being spoken or is waiting for input.
MENU_LABELS = frozenset({Label.MENU_SPEAKING, Label.MENU_AWAITING_INPUT})

# The call did not connect — fail fast, no hold logic.
NON_CONNECT_LABELS = frozenset({Label.VOICEMAIL, Label.ERROR_TONE})

# "The line went quiet / stayed on hold" — used to leave EVALUATING_SPEECH.
HOLD_LABELS = frozenset({Label.HOLD_MUSIC, Label.HOLD_INTERJECTION, Label.SILENCE})
