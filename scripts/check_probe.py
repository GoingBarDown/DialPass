"""Dev probe: learn the GA Realtime API's audio-in / audio-out event shapes.

Opens a realtime session configured for audio output, tells the model to say
"Hello?", streams a fixture WAV in as the "response from the line", and asks for
a human/not verdict. Logs every event type seen and writes the model's audio to
`scratch_probe_out.wav` so we can confirm it actually spoke.

    uv run python scripts/check_probe.py human_speech
    uv run python scripts/check_probe.py hold_music
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import wave
from collections import Counter
from pathlib import Path

from websockets.asyncio.client import connect

from dialpass.config import get_settings
from dialpass.telephony.audio import pcm16_to_ulaw, resample, ulaw_to_pcm16

FIXTURES = Path(__file__).parent.parent / "tests" / "fixtures"
URL = "wss://api.openai.com/v1/realtime?model={model}"

PROBE_INSTRUCTIONS = (
    "You screen a phone line for a call-routing system. You never hold a "
    "conversation. You do two things only, when told to: (1) say the single word "
    "'Hello?' out loud; (2) classify a short recording of what the line said "
    "back. Classification output is exactly one of these lines, spoken aloud, "
    "nothing else: 'VERDICT HUMAN' if a live person is talking (a greeting, a "
    "question, asking who's calling); 'VERDICT NOT HUMAN' for silence, hold "
    "music, ringing, beeps, or a recorded message / IVR menu."
)


def _load_ulaw(name: str) -> bytes:
    with wave.open(str(FIXTURES / f"{name}.wav"), "rb") as w:
        rate = w.getframerate()
        pcm = w.readframes(w.getnframes())
    import numpy as np

    samples = np.frombuffer(pcm, dtype=np.int16)
    return pcm16_to_ulaw(resample(samples, rate, 8000))


async def main(fixture: str) -> None:
    settings = get_settings()
    model = settings.realtime_menu_model or "gpt-realtime"
    ulaw = _load_ulaw(fixture)
    seen: Counter[str] = Counter()
    audio_out = bytearray()
    text_out = ""

    async with connect(
        URL.format(model=model),
        additional_headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        max_size=None,
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "instructions": PROBE_INSTRUCTIONS,
                        "output_modalities": ["audio"],
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcmu"},
                                "turn_detection": None,
                            },
                            "output": {"format": {"type": "audio/pcmu"}, "voice": "alloy"},
                        },
                    },
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "instructions": (
                            "Say just this and nothing more, in a calm voice: "
                            "\"Hi, this is an assistant calling — is someone there?\""
                        )
                    },
                }
            )
        )

        async def feed_after_hello() -> None:
            # let the model say "Hello?", then stream the fixture in as the reply
            await asyncio.sleep(2.0)
            step = 8000 // 5
            for i in range(0, len(ulaw), step):
                await ws.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(ulaw[i : i + step]).decode(),
                        }
                    )
                )
                await asyncio.sleep(0.2)
            # manual turn: judge what was just heard
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            await ws.send(
                json.dumps(
                    {
                        "type": "response.create",
                        "response": {
                            "output_modalities": ["text"],
                            "instructions": (
                                "Classify ONLY the audio heard since your greeting. Reply with "
                                "exactly 'VERDICT HUMAN' (a live person spoke to you) or "
                                "'VERDICT NOT HUMAN' (silence / music / ringing / tones / beeps / "
                                "a recording). No other words."
                            ),
                        },
                    }
                )
            )

        feeder = asyncio.create_task(feed_after_hello())
        try:
            async with asyncio.timeout(25):
                async for raw in ws:
                    evt = json.loads(raw)
                    et = evt.get("type", "?")
                    seen[et] += 1
                    if "audio" in et and "delta" in et and "transcript" not in et:
                        audio_out += base64.b64decode(evt["delta"])
                    elif "delta" in et and ("text" in et or "transcript" in et):
                        text_out += evt.get("delta", "")
                    elif et == "error":
                        print("ERROR EVENT:", json.dumps(evt, indent=2)[:500])
                    import re

                    m = re.search(r"VERDICT\s+(NOT\s+HUMAN|HUMAN)", text_out.upper())
                    if m:
                        print(f"--- verdict: {m.group(1)}  (transcript {text_out!r}) ---")
                        break
        except TimeoutError:
            pass
        feeder.cancel()

    print("\nevent types seen:")
    for name, n in seen.most_common():
        print(f"  {n:4d}  {name}")
    print(f"\ntext_out: {text_out!r}")
    print(f"audio_out: {len(audio_out)} ulaw bytes (~{len(audio_out) / 8000:.1f}s)")
    if audio_out:
        pcm = ulaw_to_pcm16(bytes(audio_out))
        with wave.open("scratch_probe_out.wav", "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(pcm.tobytes())
        print("wrote scratch_probe_out.wav")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "human_speech"))
