"""Per-destination knowledge — the aggregated understanding of each business's
phone system, keyed by phone number. Populated in M6 from the frame log; the
dataclasses are here now so telemetry and the IVR map can share shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class IvrNode:
    prompt_text: str  # what the menu said
    digit_pressed: str  # what we pressed
    leads_to: str | None  # id of the next node, or None if it reached hold/human


@dataclass(slots=True)
class Destination:
    phone_number: str
    ivr_tree: list[IvrNode] = field(default_factory=list)
    rep_greeting_patterns: list[str] = field(default_factory=list)
    interjection_texts: list[str] = field(default_factory=list)
    interjection_interval_s: float | None = None  # median gap between repeats
    typical_hold_seconds: float | None = None
    music_under_interjections: bool | None = None
    call_count: int = 0


@dataclass(slots=True)
class CallOutcome:
    call_id: str
    phone_number: str
    result: str  # "human" | "voicemail" | "error" | "abandoned"
    hold_seconds: float
