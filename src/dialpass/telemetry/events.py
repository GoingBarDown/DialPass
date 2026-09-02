"""Call-event taxonomy.

Events are emitted off the critical audio path (see `publisher.py`). In M7 the
sink becomes an SQS publisher feeding a worker -> Postgres; for now a log sink.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass
class Event:
    kind: ClassVar[str] = "event"
    call_id: str
    t: float  # seconds since call start

    def payload(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["t"] = round(self.t, 3)
        return {"kind": self.kind, **d}


@dataclass
class FrameClassified(Event):
    kind: ClassVar[str] = "frame_classified"
    label: str
    confidence: float
    features: dict[str, float] = field(default_factory=dict)


@dataclass
class StateChanged(Event):
    kind: ClassVar[str] = "state_changed"
    frm: str
    to: str


@dataclass
class Tier2Woken(Event):
    kind: ClassVar[str] = "tier2_woken"
    reason: str  # "menu" | "probe"


@dataclass
class DtmfSent(Event):
    kind: ClassVar[str] = "dtmf_sent"
    digits: str


@dataclass
class ProbeResult(Event):
    kind: ClassVar[str] = "probe_result"
    is_human: bool


@dataclass
class HumanDetected(Event):
    kind: ClassVar[str] = "human_detected"


@dataclass
class BridgeStarted(Event):
    kind: ClassVar[str] = "bridge_started"


@dataclass
class CallCompleted(Event):
    kind: ClassVar[str] = "call_completed"
    outcome: str


@dataclass
class CallFailed(Event):
    kind: ClassVar[str] = "call_failed"
    reason: str
