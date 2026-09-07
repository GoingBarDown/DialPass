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

import asyncio
import base64
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future

import numpy as np

from ..realtime.protocol import ProbeOutcome
from ..realtime.stream import probe_exchange
from ..telephony.audio import pcm16_to_ulaw, ulaw_to_pcm16
from ..telephony.recorder import WavRecorder
from .session import AgentSession

log = logging.getLogger("dialpass.bridge")

_FRAME = 160  # samples / bytes per 20 ms G.711 frame at 8 kHz


class CallBridge:
    def __init__(self, group_id: str, session: AgentSession) -> None:
        self.group_id = group_id
        self.session = session
        self.agent_stream_sid: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Ready-to-serialize Twilio outbound messages toward Leg A. The media
        # socket's write task drains this. Unbounded: DTMF is rare and AI audio
        # is paced by the model, so it can't run away.
        self._outbound: list[dict] = []
        # Set while a probe is running: inbound call audio is teed here for the
        # streaming Realtime exchange (see realtime/stream.py).
        self._probe_inbound: asyncio.Queue[bytes] | None = None
        # Presses `digits` as real telephony DTMF via a brief REST redirect
        # (media.py wires the Twilio call). True from the moment we ask for that
        # redirect until the stream reconnects — tells the closing socket not to
        # tear the call down.
        self.dtmf_redirect: Callable[[str], None] = lambda digits: None
        self.reconnecting = False
        # Dev-only WAV tee; survives DTMF reconnects (media.py owns the object).
        self.recorder: WavRecorder | None = None
        # Wire the session's keypress path to us.
        session.dtmf_sender = self.press_dtmf

    # -- Leg A inbound -------------------------------------------------------
    def bind_agent(self, stream_sid: str, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.agent_stream_sid = stream_sid
        self._loop = loop

    def probe_from_thread(self) -> Future[ProbeOutcome]:
        """Kick off `run_probe` on the media loop from the Tier 2 worker thread."""
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(self.run_probe(), self._loop)

    def on_agent_audio(self, ulaw: bytes) -> None:
        """One 20 ms G.711 frame from the business call."""
        if self._probe_inbound is not None:
            self._probe_inbound.put_nowait(ulaw)
        if self.session.finished:
            return
        self.session.feed_audio(ulaw_to_pcm16(ulaw))

    # -- Leg A outbound ----------------------------------------------------
    def press_dtmf(self, digits: str) -> None:
        """Press `digits` on the far end. Not as stream audio — IVRs don't
        detect that — but via a REST redirect to `<Play digits>`, which Twilio
        renders as real telephony DTMF. The stream drops and reconnects."""
        safe = "".join(c for c in digits if c in "0123456789*#")
        if not safe or self.agent_stream_sid is None:
            return
        log.info("bridge %s: pressing %s (redirect + reconnect)", self.group_id, safe)
        self.reconnecting = True
        # The Twilio REST call blocks ~100-300ms — keep it off the media loop.
        threading.Thread(
            target=self.dtmf_redirect, args=(safe,), daemon=True
        ).start()

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

    # -- the probe (streaming Realtime exchange) --------------------------
    async def run_probe(self) -> ProbeOutcome:
        """Greet the line and classify the reply. Driven from the Tier 2 worker
        thread via run_coroutine_threadsafe; runs here on the media loop so it
        can both read inbound frames and play audio into the call."""
        settings = self.session.settings
        model = settings.realtime_menu_model or settings.realtime_model
        self._probe_inbound = asyncio.Queue()
        try:
            outcome = await probe_exchange(
                settings.openai_api_key,
                model,
                inbound=self._probe_inbound,
                play=self.play_to_agent,
            )
        finally:
            self._probe_inbound = None
        log.info("bridge %s: probe -> %s", self.group_id, outcome)
        return outcome

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        self.session.close()
        if self.recorder is not None:
            self.recorder.close()
