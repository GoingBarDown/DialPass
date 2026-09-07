"""CallBridge — one outbound call's audio hub and, at the handoff, its mixer.

Since M2 the business call (Leg A) forked its audio to us one-way via
`<Start><Stream>` and sat in a Twilio Conference. That can't work for the handoff:
Twilio has no way to stream audio *into* a conference, so the AI could never
speak to the rep and the bullet-1 "bidirectional audio engine" was a fiction.

M5 replaces it. Leg A (the business call) and Leg B (the user's own phone) each
connect over a bidirectional `<Connect><Stream>` WebSocket. This object is the
hub both sockets attach to:

  * Leg A inbound audio -> `AgentSession` (Tier 1 + FSM unchanged), and it is
    teed to the probe while one is running.
  * Leg A / Leg B outbound audio -> per-leg queues the socket write tasks drain.
  * On `Action.BRIDGE` (human confirmed) `begin_handoff` speaks a holding line
    into Leg A, texts the user, and flips `relay_open` — from then on every Leg A
    frame is copied to Leg B and vice versa, so the two people talk directly.

The asyncio plumbing (read loops, write loops) lives in `api/media.py`; this
stays a plain object with sync methods so it's trivial to unit-test.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future

import numpy as np

from ..realtime.protocol import ProbeOutcome
from ..realtime.stream import probe_exchange, speak_exchange
from ..telephony.audio import pcm16_to_ulaw, ulaw_to_pcm16
from ..telephony.recorder import WavRecorder
from .session import AgentSession, HandoffUnavailable

log = logging.getLogger("dialpass.bridge")

_FRAME = 160  # samples / bytes per 20 ms G.711 frame at 8 kHz

_AGENT_HOLD_LINE = "Thanks for holding — connecting my client now, one moment."
_USER_SMS = "DialPass reached a live agent — you're connected now. Go ahead and talk."


class CallBridge:
    def __init__(self, group_id: str, session: AgentSession) -> None:
        self.group_id = group_id
        self.session = session
        self.agent_stream_sid: str | None = None
        self.user_stream_sid: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Ready-to-serialize Twilio outbound messages, one queue per leg. The
        # media sockets' write tasks drain them. Unbounded: relayed audio is
        # paced 1:1 by the far end and AI audio is paced by the model.
        self._outbound_agent: list[dict] = []
        self._outbound_user: list[dict] = []
        # Set while a probe is running: inbound call audio is teed here for the
        # streaming Realtime exchange (see realtime/stream.py).
        self._probe_inbound: asyncio.Queue[bytes] | None = None
        # Presses `digits` as real telephony DTMF via a brief REST redirect
        # (media.py wires the Twilio call). True from the moment we ask for that
        # redirect until the stream reconnects — tells the closing socket not to
        # tear the call down.
        self.dtmf_redirect: Callable[[str], None] = lambda digits: None
        self.reconnecting = False
        # monotonic time the current DTMF redirect started, so a sweeper can reap
        # a bridge whose stream never came back (M8).
        self.reconnect_since: float | None = None
        # Texts the user (media.py wires the Twilio-backed one). No-op offline.
        self.notify_user: Callable[[str], None] = lambda body: None
        # Ends the call for good — fallback SMS + hang up both legs + drop the
        # bridge. media.py wires this; no-op offline. `reason` picks the SMS.
        self.on_teardown: Callable[[str | None], None] = lambda reason: None
        # False until the handoff: Leg A <-> Leg B audio is not relayed, Leg B
        # hears only silence (its <Say> intro told it to wait).
        self.relay_open = False
        self._handoff_started = False
        # Dev-only WAV tee; survives DTMF reconnects (media.py owns the object).
        self.recorder: WavRecorder | None = None
        self._closed = False
        # Wire the session's keypress, handoff and failure hooks to us.
        session.dtmf_sender = self.press_dtmf
        session.on_bridge = self.begin_handoff
        session.on_fail = self.on_session_fail

    # -- leg binding -------------------------------------------------------
    def bind_agent(self, stream_sid: str, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.agent_stream_sid = stream_sid
        self._loop = loop

    def bind_user(self, stream_sid: str) -> None:
        self.user_stream_sid = stream_sid

    def probe_from_thread(self) -> Future[ProbeOutcome]:
        """Kick off `run_probe` on the media loop from the Tier 2 worker thread."""
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(self.run_probe(), self._loop)

    # -- inbound audio ---------------------------------------------------
    def on_agent_audio(self, ulaw: bytes) -> None:
        """One 20 ms G.711 frame from the business call."""
        if self._probe_inbound is not None:
            self._probe_inbound.put_nowait(ulaw)
        if self.relay_open and self.user_stream_sid is not None:
            self._outbound_user.append(self._frame(self.user_stream_sid, ulaw))
        if not self.session.finished:
            self.session.feed_audio(ulaw_to_pcm16(ulaw))

    def on_user_audio(self, ulaw: bytes) -> None:
        """One 20 ms G.711 frame from the user's phone. Discarded until the
        handoff — before that the user is just waiting on our line."""
        if self.relay_open and self.agent_stream_sid is not None:
            self._outbound_agent.append(self._frame(self.agent_stream_sid, ulaw))

    # -- outbound audio -------------------------------------------------
    def press_dtmf(self, digits: str) -> None:
        """Press `digits` on the far end. Not as stream audio — IVRs don't
        detect that — but via a REST redirect to `<Play digits>`, which Twilio
        renders as real telephony DTMF. The stream drops and reconnects."""
        safe = "".join(c for c in digits if c in "0123456789*#")
        if not safe or self.agent_stream_sid is None:
            return
        log.info("bridge %s: pressing %s (redirect + reconnect)", self.group_id, safe)
        self.reconnecting = True
        self.reconnect_since = time.monotonic()
        # The Twilio REST call blocks ~100-300ms — keep it off the media loop.
        threading.Thread(target=self.dtmf_redirect, args=(safe,), daemon=True).start()

    def play_to_agent(self, pcm16: np.ndarray) -> None:
        """Queue PCM (mono int16, 8 kHz) to play into the business call, split
        into 20 ms G.711 frames. Used by the probe greeting and the handoff line."""
        if self.agent_stream_sid is None:
            return
        ulaw = pcm16_to_ulaw(np.asarray(pcm16, dtype=np.int16))
        for i in range(0, len(ulaw), _FRAME):
            self._outbound_agent.append(self._frame(self.agent_stream_sid, ulaw[i : i + _FRAME]))

    def _frame(self, stream_sid: str, ulaw: bytes) -> dict:
        return {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(ulaw).decode("ascii")},
        }

    def drain_agent(self) -> list[dict]:
        out, self._outbound_agent = self._outbound_agent, []
        return out

    def drain_user(self) -> list[dict]:
        out, self._outbound_user = self._outbound_user, []
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

    # -- the handoff ---------------------------------------------------
    def begin_handoff(self) -> None:
        """`AgentSession` calls this (on the media loop) the moment a human is
        confirmed. Schedules the async handoff and returns immediately so the
        FSM can finish its tick."""
        if self._handoff_started:
            return
        self._handoff_started = True
        settings = self.session.settings
        if self.user_stream_sid is None:
            # The user hung up before we found a human — nobody to hand off to.
            raise HandoffUnavailable("user_left")
        if self._loop is None or not settings.openai_api_key:
            # Nothing async to do — no holding line to synthesize (offline / no
            # key) or no loop to run it on (unit tests). Open the relay now.
            self.relay_open = True
            self._safe_notify(_USER_SMS)
            return
        self._loop.create_task(self._do_handoff())

    async def _do_handoff(self) -> None:
        settings = self.session.settings
        model = settings.realtime_menu_model or settings.realtime_model
        try:
            await speak_exchange(
                settings.openai_api_key, model, _AGENT_HOLD_LINE, play=self.play_to_agent
            )
        except Exception:
            log.exception("bridge %s: handoff holding line failed", self.group_id)
        # Wait for the queued holding line to actually drain into the call before
        # patching the two parties together, so they don't talk over our voice.
        for _ in range(250):  # 5 s cap
            if not self._outbound_agent:
                break
            await asyncio.sleep(0.02)
        self.relay_open = True
        self._safe_notify(_USER_SMS)
        log.info("bridge %s: relay open — user and agent connected", self.group_id)

    # -- failure -------------------------------------------------------
    def on_session_fail(self, reason: str) -> None:
        """`AgentSession.on_fail`: the call couldn't finish. Hand off to
        `on_teardown`, which texts the user a reason-appropriate line and hangs
        up both legs instead of leaving them on a dead line."""
        log.info("bridge %s: call failed (%s)", self.group_id, reason)
        self.on_teardown(reason)

    def _safe_notify(self, body: str) -> None:
        try:
            self.notify_user(body)
        except Exception:
            log.exception("bridge %s: user notification failed", self.group_id)

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.session.close()
        if self.recorder is not None:
            self.recorder.close()
