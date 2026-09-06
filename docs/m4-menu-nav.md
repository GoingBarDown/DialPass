# M4 — IVR menu navigation + DTMF injection

**Status:** done, branch `feat/m4-menu-nav`. 49 tests pass, ruff + mypy clean.
Validated on live calls (Delta, IRS, American Airlines) + a synthetic clean menu.

## Goal

When Tier 1 detects a menu, wake Tier 2 (OpenAI Realtime API), have it read the
prompt and pick a keypad digit, then actually press that digit on the live call.
Handle one or two menu levels.

---

## Phase 1 — de-risk the DTMF injection (spike)

Twilio has **no mid-call send-digits API** — `sendDigits` only fires at call
setup. The plan was to press a key by *redirecting* the live call:

```python
client.calls(sid).update(twiml='<Response><Play digits="ww2w"/><Dial><Conference>NAME</Conference></Dial></Response>')
```

**The risk:** does the `<Start><Stream>` media fork (how Tier 1 hears the call)
survive a redirect, or does Twilio tear it down?

**Spike:** placed a call, let it connect, fired a hardcoded redirect with
`<Play digits="1">`, watched the server. Result: the recording kept growing with
no gap, `frame_classified` events never stopped, no "stream disconnected". **The
fork survives.** Later re-confirmed on the live American Airlines call — Tier 2
re-woke at t=49/64/85s, all after the DTMF redirect at t=21s.

So the redirect approach is sound; no need for the harder bidirectional
`<Connect><Stream>` path (that's M5's problem).

---

## Phase 2 — Tier 2 over the Realtime API

`realtime/client.py`, `choose_menu_digit`, **turn-based**: the menu prompt
already happened and sits in the session buffer, so — connect a WebSocket, push
the whole clip, ask for one verdict, close. No streaming loop, no server VAD.

- Audio: the buffered PCM16 is re-encoded to **G.711 µ-law** (`pcm16_to_ulaw`)
  and sent as `input_audio_format: audio/pcmu` — 8 kHz native, no resample.
- Sync method (`choose_menu_digit`) running an async exchange on a worker-thread
  event loop, because the agent's dispatch is sync (and drives the offline sim).

### Problems hit

**P2.1 — `beta_api_shape_disabled`.** The account is on the **GA** Realtime API;
sending `OpenAI-Beta: realtime=v1` forced the (disabled) beta shape. Fix: drop
the header, use the GA `session.update` shape (`session.type: "realtime"`,
`output_modalities`, `audio.input.format`).

**P2.2 — `insufficient_quota`.** OpenAI account had no credit balance. Not a code
issue — added credits.

**P2.3 — structured output not supported.** GA Realtime rejects
`session.text.format` (json_schema). No forced-JSON mode.

**P2.4 — `gpt-realtime-mini` won't hold a format.** It's a speech model tuned to
*converse* — every reply was "I'm sorry, could you tell me the menu options?"
instead of a decision. The full **`gpt-realtime`** model follows "output only
JSON" reliably. So menu decisions use the full model
(`DIALPASS_REALTIME_MENU_MODEL`, default `gpt-realtime`); the mini model stays
the default for cheaper future uses.

**P2.5 — still chatty at the edges.** Even the full model sometimes wraps the
JSON in a code fence, a `>` , a `<|vq_…|>` token, or a sentence of preamble.
`_parse_verdict` is defensive: take the substring from the first `{` to the last
`}` and JSON-parse it; fall back to a "press N" regex; fall back to an
operator-keyword → `"0"`. Covered by `test_menu_nav.py`.

---

## Phase 3 — FSM changes

**Submenu re-wake (the hard part).** After pressing a digit we stay in
`IVR_MENU`. A submenu is a *new* menu Tier 2 must read — but a long menu read
without pauses must NOT keep re-triggering. Rule: after a press, only re-wake
once we've heard the previous prompt **END** — a sustained non-speech gap
(`menu_gap_frames`) — *and then* speech resumes. Plus `menu_refractory_s` (a
time floor) and `max_menu_presses` (a hard cap).

**`DIALING → ON_HOLD`** now needs a longer streak (`dialing_to_hold_frames`, 6)
than `IVR_MENU → ON_HOLD` — from DIALING, a brief music-like patch is more likely
a smoothly-read greeting mis-scored than a real queue.

**`SPEECHY_LABELS`** — the heuristic Tier 1 only emits `LIVE_SPEECH_CANDIDATE`
(never `MENU_SPEAKING`), so the menu handlers now match a set that includes it.

---

## Phase 4 — classifier tweak

The IRS greeting — a very smooth continuous recorded voice with no pause in a
1.5 s window (`quiet_frac`≈0, `env_cv`≈0.25) — was mis-scored `HOLD_MUSIC` by the
M3 tonal-fallback rule, which then tripped `DIALING → ON_HOLD` before the menu
path could win. Fix: the tonal fallback now also requires `mod_4hz >= 0.45`
(rhythmic energy). A read sentence has weak 3–8 Hz energy; a music bed doesn't.

---

## Phase 5 — validation

| target | what it is | result |
|---|---|---|
| Spike call (own cell) | — | media fork survives the redirect ✅ |
| Delta 1-800-221-1212 | conversational "tell me why you're calling" bot, no DTMF | Tier 2 correctly presses nothing ✅ |
| IRS 1-800-829-1040 | hybrid "say X or press N", after-hours | reaches IVR_MENU, wakes + re-wakes Tier 2; abstains (after-hours) ✅ |
| American 1-800-433-7300 | conversational bot; only DTMF option is "press 9 for Spanish" | live DTMF pressed, **stream survived**; current code abstains (no English option) ✅ |
| synthetic clean menu (`tests/fixtures/menu_prompt.wav`) | "billing→1, flight status→3, reservations→5" | **3/3 correct** digit picks by goal ✅ |

Repeatable check: `uv run python scripts/check_menu_model.py` (hits the real API,
so it's a script not a pytest test).

### Finding: the IVR landscape has shifted

Delta, American, and (partly) the IRS have replaced "press 1 for reservations…"
with **conversational voice assistants** ("how can I help you with your travel
today?"). Classic DTMF trees now live mostly at government, healthcare, SMB, and
enterprise lines. M4's keypress navigation works for those; the conversational
ones need Tier 2 to *speak*, which is exactly M5's bidirectional path. The
two-tier design already covers both — Tier 2 either presses a key or talks.

---

## Known limitations

- **Menu decision blocks the event loop** for the exchange (≤10 s timeout). Menu
  decisions are rare and Twilio buffers the stream, so it's acceptable for the
  MVP. The fix — a persistent socket + fully-async dispatch — is
  future-optimizations.md #1.
- **One connection per decision** (handshake each time). Same fix.
- Digit choice depends on the buffer window catching the "press N" phrasing; a
  prompt that states options before Tier 1 reaches `IVR_MENU` can be missed on
  the first wake (the gap re-wake recovers it on the next prompt).

---

## Files

```
src/dialpass/realtime/client.py       real Realtime API client (rewritten)
src/dialpass/config.py                 realtime_menu_model
src/dialpass/main.py                   wire the menu model
src/dialpass/agent/session.py          dtmf_sender injection; _handle_menu presses
src/dialpass/agent/state.py            submenu gap re-wake, press cap, dialing->hold streak
src/dialpass/agent/labels.py           SPEECHY_LABELS
src/dialpass/agent/classifier.py       tonal fallback needs mod_4hz
src/dialpass/telephony/twilio_client.py send_dtmf (redirect)
src/dialpass/telephony/twiml.py        play_digits_then_conference; conference Parameter
src/dialpass/api/media.py              build dtmf_sender from the start event
scripts/check_menu_model.py            manual model check (new)
tests/test_menu_nav.py, test_state.py, test_twiml.py, tests/fixtures/menu_prompt.wav
```

## Dev-only, remove before deployment

- `/twiml/holdmusic-test` route, `scripts/place_test_call.py` (M3)
- `DIALPASS_RECORD_DIR` recording wiring
