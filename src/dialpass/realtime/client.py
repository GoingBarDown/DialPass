"""gpt-realtime-mini over WebSocket — Tier 2.

M4 implements `choose_menu_digit` in the simplest useful mode: **turn-based**.
The menu prompt already happened and is sitting in the session's buffer, so we
open a socket, push the whole clip, ask for one JSON verdict, and close. No
streaming loop, no server VAD — that's M5's probe.

Connection model (see docs/design-decisions.md): a later revision keeps one
socket open for the whole call (see docs/future-optimizations.md #1). For now,
one connection per decision — simpler to reason about and test.

The public methods are sync because the agent's dispatch is sync and drives the
offline sim too. We're invoked from inside uvicorn's event loop, so the async
exchange runs to completion on a short-lived worker-thread loop.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import threading

import numpy as np
from websockets.asyncio.client import connect

from ..telephony.audio import pcm16_to_ulaw
from .protocol import MenuDecision, ProbeOutcome, Tier2Unavailable

log = logging.getLogger("dialpass.realtime")

_URL = "wss://api.openai.com/v1/realtime?model={model}"
_VALID_DIGITS = set("0123456789*#")


def _menu_instructions(goal: str | None) -> str:
    goal_line = (
        f"The caller's goal: {goal}." if goal else "The caller wants to reach a human agent."
    )
    return (
        f"You are a DTMF dialer for a phone menu (IVR). {goal_line} "
        "The caller speaks English.\n"
        "You are given a recording of what the line just said. Pick the keypad "
        "key whose spoken option best matches the goal. If an option is phrased "
        "'<description> or press N' and the description fits, use N. Prefer an "
        "agent / representative / operator option when nothing matches better.\n"
        "Language prompts: choose the English option. If English is the default "
        '(e.g. "for French press 2" with no key for English), use "".\n'
        "Data entry: if the line asks you to ENTER a number (account number, "
        "member ID, meeting ID, zip, extension) and that exact number appears in "
        'the goal, return those digits — add "#" only if it says "followed by '
        'pound". Never invent digits that are not in the goal.\n'
        'Otherwise use "": no stated option fits, none were given, it asks you '
        "to speak, or it is hold music / an after-hours message.\n"
        "Output EXACTLY ONE LINE of JSON and NOTHING else — no prose, no code "
        "fences, never ask a question:\n"
        '{"digit": "<one key 0-9 * #, or the digits to enter, or empty>", '
        '"rationale": "<=12 words>"}'
    )


class RealtimeClient:
    def __init__(
        self,
        api_key: str,
        *,
        menu_model: str = "gpt-realtime",
        timeout_s: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self._menu_model = menu_model
        self._timeout_s = timeout_s

    # -- Tier2 protocol (sync) ------------------------------------------
    def choose_menu_digit(
        self, audio: np.ndarray, sample_rate: int, goal: str | None
    ) -> MenuDecision:
        text = self._run(self._menu_turn(audio, sample_rate, goal))
        if text is None:
            # Infra failure (timeout / transport / error event), not an abstain —
            # let the circuit breaker count it.
            raise Tier2Unavailable("menu exchange failed (timeout or error)")
        log.debug("menu model reply: %s", text.replace("\n", " ")[:300])
        digits, rationale = _parse_verdict(text)
        if digits and not set(digits) <= _VALID_DIGITS:
            return MenuDecision(None, rationale=f"model returned non-DTMF keys {digits!r}")
        if len(digits) > 20:  # a menu key or a short ID, never a monologue
            return MenuDecision(None, rationale="model returned too many keys")
        return MenuDecision(digits or None, rationale=rationale or "")

    def probe(self, audio: np.ndarray, sample_rate: int) -> ProbeOutcome:
        raise NotImplementedError("Tier 2 probe lands in M5")

    def say_to_agent(self, text: str) -> None:
        raise NotImplementedError("Tier 2 speech lands in M5")

    def close(self) -> None:
        pass

    # -- internals ----------------------------------------------------
    def _run(self, coro):
        """Run one async exchange to completion on a worker-thread event loop.
        Blocks the caller (the media loop) for the exchange — ~1-2s, and menu
        decisions are rare. Returns None on timeout / any error."""
        box: dict[str, object] = {}

        def target() -> None:
            try:
                box["v"] = asyncio.run(asyncio.wait_for(coro, self._timeout_s))
            except Exception as exc:  # noqa: BLE001 - never let Tier 2 kill the call
                log.warning("realtime exchange failed: %s", exc)
                box["v"] = None

        th = threading.Thread(target=target, daemon=True)
        th.start()
        th.join(self._timeout_s + 5)
        return box.get("v")

    async def _menu_turn(self, audio: np.ndarray, sample_rate: int, goal: str | None) -> str | None:
        """Push the buffered menu clip, get one text response back."""
        ulaw = pcm16_to_ulaw(np.asarray(audio, dtype=np.int16))
        async with connect(
            _URL.format(model=self._menu_model),
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            max_size=None,
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {
                            "type": "realtime",
                            "instructions": _menu_instructions(goal),
                            "output_modalities": ["text"],
                            "audio": {
                                "input": {
                                    "format": {"type": "audio/pcmu"},
                                    "turn_detection": None,
                                }
                            },
                        },
                    }
                )
            )

            step = max(1, sample_rate // 5)  # ~200 ms chunks (8000 mu-law bytes/s)
            for i in range(0, len(ulaw), step):
                await ws.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(ulaw[i : i + step]).decode("ascii"),
                        }
                    )
                )
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            await ws.send(json.dumps({"type": "response.create"}))

            parts: list[str] = []
            async for raw in ws:
                evt = json.loads(raw)
                etype = evt.get("type", "")
                if etype == "error":
                    log.warning("realtime error event: %s", evt.get("error"))
                    return None
                if etype == "response.output_text.delta":
                    parts.append(evt.get("delta", ""))
                elif etype == "response.done":
                    return "".join(parts) or _text_from_done(evt)
        return "".join(parts) or None


_KEYWORD_TO_OPERATOR = re.compile(
    r"\b(operator|representative|agent|customer service|human)\b", re.IGNORECASE
)
_PRESS_DIGIT = re.compile(
    r"(?:press|dial|key|option|choose|select)\D{0,12}?([0-9*#])", re.IGNORECASE
)


def _parse_verdict(text: str) -> tuple[str, str]:
    """Pull digit + rationale out of the model's reply. `gpt-realtime-mini` is a
    speech model and doesn't reliably honour "JSON only", so fall back through:
    JSON object -> "press N" phrasing -> operator keyword => "0"."""
    blob = text.strip()

    start, end = blob.find("{"), blob.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(blob[start : end + 1])
            digit = str(data.get("digit", "")).strip()
            rationale = str(data.get("rationale", "")).strip()
            if digit or rationale:
                return digit, rationale or blob[:200]
        except (json.JSONDecodeError, AttributeError):
            pass

    m = _PRESS_DIGIT.search(blob)
    if m:
        return m.group(1), blob[:200]
    if _KEYWORD_TO_OPERATOR.search(blob):
        return "0", blob[:200]
    return "", blob[:200]


def _text_from_done(evt: dict) -> str | None:
    for item in evt.get("response", {}).get("output", []):
        for part in item.get("content", []):
            if part.get("type") in ("output_text", "text"):
                return part.get("text")
    return None
