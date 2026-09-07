"""CallBridge — one outbound call's audio hub.

Since M2 the business call (Leg A) forked its audio to us one-way via
`<Start><Stream>` and sat in a Twilio Conference. That can't work for the handoff:
Twilio has no way to stream audio *into* a conference, so the AI could never
speak to the rep and the bullet-1 "bidirectional audio engine" was a fiction.

M5 replaces it. Leg A (and, in phase 3, the user's Leg B) connect over a
bidirectional `<Connect><Stream>` WebSocket. This object is the hub both sockets
attach to: it decodes Leg A's inbound audio into the `AgentSession` (Tier 1 + FSM
unchanged) and owns the reverse path — DTMF and, later, the AI's synthesized
speech — as a queue of ready-to-send Twilio messages the socket task drains.

The asyncio plumbing (read loop, write loop) lives in `api/media.py`; this stays
a plain object with sync methods so it's trivial to unit-test.
"""

from __future__ import annotations

import base64
import logging

import numpy as np

from ..telephony.audio import pcm16_to_ulaw, ulaw_to_pcm16
from .session import AgentSession

log = logging.getLogger("dialpass.bridge")

_FRAME = 160  # samples / bytes per 20 ms G.711 frame at 8 kHz


class CallBridge:
    def __init__(self, group_id: str, session: AgentSession) -> None:
        self.group_id = group_id
        self.session = session
        self.agent_stream_sid: str | None = None
        # Ready-to-serialize Twilio outbound messages toward Leg A. The media
        # socket's write task drains this. Unbounded: DTMF is rare and AI audio
        # is paced by the model, so it can't run away.
        self._outbound: list[dict] = []
        # Wire the session's keypress path to us.
        session.dtmf_sender = self.press_dtmf

    # -- Leg A inbound -------------------------------------------------------
    def bind_agent(self, stream_sid: str) -> None:
        self.agent_stream_sid = stream_sid

    def on_agent_audio(self, ulaw: bytes) -> None:
        """One 20 ms G.711 frame from the business call."""
        if self.session.finished:
            return
        self.session.feed_audio(ulaw_to_pcm16(ulaw))

    # -- Leg A outbound ----------------------------------------------------
    def press_dtmf(self, digits: str) -> None:
        """Send touch-tones to the far end over the stream — no REST redirect,
        so the socket never drops (Twilio bidirectional `dtmf` event)."""
        safe = "".join(c for c in digits if c in "0123456789*#w")
        if not safe or self.agent_stream_sid is None:
            return
        self._outbound.append(
            {"event": "dtmf", "streamSid": self.agent_stream_sid, "dtmf": {"digits": safe}}
        )
        log.info("bridge %s: queued DTMF %s", self.group_id, safe)

    def play_to_agent(self, pcm16: np.ndarray) -> None:
        """Queue PCM (mono int16, 8 kHz) to play into the business call, split
        into 20 ms G.711 frames. Used by the probe / handoff line in phase 2b+."""
        if self.agent_stream_sid is None:
            return
        ulaw = pcm16_to_ulaw(np.asarray(pcm16, dtype=np.int16))
        for i in range(0, len(ulaw), _FRAME):
            self._outbound.append(
                {
                    "event": "media",
                    "streamSid": self.agent_stream_sid,
                    "media": {"payload": base64.b64encode(ulaw[i : i + _FRAME]).decode("ascii")},
                }
            )

    def drain_outbound(self) -> list[dict]:
        """Hand the socket task everything queued since the last call."""
        out, self._outbound = self._outbound, []
        return out

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        self.session.close()
