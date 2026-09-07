"""Streaming Tier 2 — the bidirectional half of the audio engine.

`client.RealtimeClient` handles the menu decision turn-based (a buffered clip in,
one digit out). The **probe** can't work that way: to tell a live person from a
recording you have to *say something* and hear how the line reacts. So this opens
a Realtime session that both speaks into the call and listens to it, concurrently,
over one WebSocket:

    call audio  --(inbound queue)-->  Realtime API
    Realtime API --(output_audio)-->  play() --> the call

The model says a short greeting, listens for a few seconds, then classifies what
it heard as HUMAN / NOT HUMAN. `StreamingProbe` wraps the turn-based client so
`AgentSession` sees one `Tier2`: menu decisions still go to the wrapped client;
`probe()` runs the exchange above on the media event loop.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import Callable
from concurrent.futures import Future

import numpy as np
from websockets.asyncio.client import connect

from ..telephony.audio import ulaw_to_pcm16
from .protocol import MenuDecision, ProbeOutcome, Tier2

log = logging.getLogger("dialpass.realtime")

_URL = "wss://api.openai.com/v1/realtime?model={model}"
_VERDICT = re.compile(r"VERDICT[:\s]+(NOT[\s-]+HUMAN|HUMAN)", re.IGNORECASE)

_PROBE_SYSTEM = (
    "You screen a phone line for a call-routing system. You never hold a "
    "conversation. You do two things only, when told to: (1) say one short "
    "greeting out loud; (2) classify a few seconds of what the line said back. "
    "The classification is exactly one line: 'VERDICT HUMAN' if a live person is "
    "speaking to you (a greeting, a question, asking who is calling), or "
    "'VERDICT NOT HUMAN' for silence, hold music, ringing, tones, beeps, or a "
    "recorded message / IVR menu."
)
_GREETING = (
    "Say just this, calmly, and nothing else: "
    '"Hi, this is an assistant calling — is someone there?"'
)
_JUDGE = (
    "Classify ONLY what you heard since your greeting. Reply with exactly "
    "'VERDICT HUMAN' or 'VERDICT NOT HUMAN' — no other words."
)


def verdict_from_text(text: str) -> ProbeOutcome:
    m = _VERDICT.search(text or "")
    if not m:
        # No clear read — treat as not-human so we don't bridge the user into
        # hold music. A real agent who got no reply will speak again and we
        # re-probe.
        return ProbeOutcome(is_human=False, transcript=text.strip()[:200])
    return ProbeOutcome(is_human=m.group(1).upper() == "HUMAN", transcript=text.strip()[:200])


async def probe_exchange(
    api_key: str,
    model: str,
    *,
    inbound: asyncio.Queue[bytes],
    play: Callable[[np.ndarray], None],
    listen_s: float = 4.0,
    timeout_s: float = 15.0,
) -> ProbeOutcome:
    """Greet the line, listen `listen_s` seconds, return a HUMAN / NOT HUMAN read.

    `inbound` yields 20 ms G.711 (µ-law) frames from the call; `play` takes PCM
    (mono int16, 8 kHz) to send into the call.
    """
    try:
        async with asyncio.timeout(timeout_s):
            async with connect(
                _URL.format(model=model),
                additional_headers={"Authorization": f"Bearer {api_key}"},
                max_size=None,
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "session.update",
                            "session": {
                                "type": "realtime",
                                "instructions": _PROBE_SYSTEM,
                                "output_modalities": ["audio"],
                                "audio": {
                                    "input": {
                                        "format": {"type": "audio/pcmu"},
                                        "turn_detection": None,
                                    },
                                    "output": {
                                        "format": {"type": "audio/pcmu"},
                                        "voice": "alloy",
                                    },
                                },
                            },
                        }
                    )
                )
                await ws.send(
                    json.dumps(
                        {"type": "response.create", "response": {"instructions": _GREETING}}
                    )
                )

                text: list[str] = []
                judging = False

                async def feed() -> None:
                    await asyncio.sleep(2.0)  # let the greeting play first
                    drained = 0.0
                    while drained < listen_s:
                        frame = await inbound.get()
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "input_audio_buffer.append",
                                    "audio": base64.b64encode(frame).decode("ascii"),
                                }
                            )
                        )
                        drained += len(frame) / 8000.0
                    await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    await ws.send(
                        json.dumps(
                            {
                                "type": "response.create",
                                "response": {
                                    "output_modalities": ["text"],
                                    "instructions": _JUDGE,
                                },
                            }
                        )
                    )

                feeder = asyncio.create_task(feed())
                try:
                    async for raw in ws:
                        evt = json.loads(raw)
                        et = evt.get("type", "")
                        if et == "response.output_audio.delta" and not judging:
                            play(ulaw_to_pcm16(base64.b64decode(evt["delta"])))
                        elif et == "response.output_audio_transcript.delta":
                            text.append(evt.get("delta", ""))
                        elif et in ("response.output_text.delta", "response.text.delta"):
                            judging = True
                            text.append(evt.get("delta", ""))
                        elif et == "error":
                            log.warning("probe error event: %s", evt.get("error"))
                            break
                        if _VERDICT.search("".join(text)):
                            break
                finally:
                    feeder.cancel()
                return verdict_from_text("".join(text))
    except (TimeoutError, OSError, ConnectionError) as exc:
        log.warning("probe exchange failed: %s", exc)
        return ProbeOutcome(is_human=False, transcript="probe unavailable")


class StreamingProbe:
    """Wraps a turn-based Tier 2 so the probe streams. Menu decisions and the
    holding line pass straight through to `inner`. Satisfies the `Tier2` protocol."""

    def __init__(self, inner: Tier2, run_probe: Callable[[], Future[ProbeOutcome]]) -> None:
        self._inner = inner
        self._run_probe = run_probe

    def choose_menu_digit(self, audio, sample_rate, goal) -> MenuDecision:
        return self._inner.choose_menu_digit(audio, sample_rate, goal)

    def probe(self, audio, sample_rate) -> ProbeOutcome:
        # Called on the Tier 2 worker thread; the exchange runs on the media loop.
        # `probe_exchange` has its own 15s cap — this is just a backstop.
        return self._run_probe().result(timeout=25)

    def say_to_agent(self, text: str) -> None:
        self._inner.say_to_agent(text)

    def close(self) -> None:
        self._inner.close()
