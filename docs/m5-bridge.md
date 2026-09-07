# M5 — the bidirectional audio engine + the handoff

**Status:** code-complete, branch `feat/m5-bridge` (9 commits, `30713f5..f6955c1`).
82 tests pass, ruff + mypy clean. Transport, menu nav, and DTMF actuation are
**live-proven**; the probe and the handoff have **not** yet run through a full
live call (see Validation).

## Goal

Turn the one-way listener of M2–M4 into a real **bidirectional audio engine**:

1. Stream live Twilio call audio to OpenAI's Realtime API *and stream synthesized
   audio back into the call* — so the AI can greet a line and hear how it reacts.
2. When a human is confirmed, **hand the live human to the user**: patch the
   business call (Leg A) and the user's own phone (Leg B) together through our
   server, play a holding line to the rep, and text the user.

This is the milestone that makes resume bullet 1 literally true, and it is the
reason bullet 2's Tier 2 must stay **episodic** (open-on-wake, close-after) — a
persistent socket would contradict "invokes the full model only on menu prompts
or agent pickup".

---

## The architecture pivot (decided in Phase 2, spike-proven)

M2–M4 forked Leg A's audio to us **one-way** with `<Start><Stream>` while the leg
sat in a Twilio **Conference**. That cannot support the handoff:

- Twilio has **no way to stream audio *into* a Conference**. The AI could never
  speak to the rep — bullet 1's "bidirectional audio engine" was a fiction.
- `<Start><Stream>` is one-way (fork). `<Connect><Stream>` *is* bidirectional but
  **consumes the leg** — a leg on `<Connect><Stream>` cannot also be in a
  Conference.

**Resolution:** drop the Conference. Both legs connect over `<Connect><Stream>`;
**our server is the mixer.** `CallBridge` (`agent/bridge.py`) is the per-call hub
both sockets attach to. The handoff is a state flip (`relay_open`) in that hub.

```
  Leg A (business)  <--Connect/Stream-->  our server  <--Connect/Stream-->  Leg B (user)
                                              |
                                     AgentSession (Tier 1 + FSM)
                                              |
                                  OpenAI Realtime API (episodic)
```

---

## Phase 0 — run Tier 2 off the media loop (`30713f5`)

**Problem (carried from M4):** `choose_menu_digit` / `probe` block for 1–2 s.
Running them inline on the media event loop froze audio ingestion for seconds;
when ingestion caught up it replayed a burst of ticks on near-identical buffer
contents, manufacturing a fake streak and **stampeding the FSM**.

**Fix:** `agent/executor.py` — a `Tier2Executor` seam.
- `InlineExecutor` — synchronous, deterministic; used by the sim and tests.
- `ThreadedExecutor` — `ThreadPoolExecutor(max_workers=1)`, `poll()` is
  non-blocking (`future.done()`); used live.
- `session.py` submits the Tier 2 call and applies the result on a **later tick**
  (`_poll_tier2`). `_tier2_kind` ("menu" | "probe") gates a second wake until the
  first result lands. `feed_audio` skips ahead instead of replaying a backlog if
  it fell >3 intervals behind.

---

## Phase 1 — dial the user's leg (`7115caa`)

`/calls` now also dials **Leg B** (the user's own phone) so the handoff later is
a patch-through, not a cold call at the worst moment. A failed user ring doesn't
sink the call — the agent still navigates; there's just no one to bridge in
(M8 turns that into a fallback notification). Leg correlation on our side is a
`group` id (`dialpass-<uuid12>`), since leg SIDs aren't known until each leg
connects. `tests/fakes.py` = `FakeTwilioClient`.

*(Phase 1 originally joined Leg B to a muted Conference; Phase 3 replaced that.)*

---

## Spike — prove bidirectional Media Streams (`4b625ac`)

Before committing to the rewrite: `api/spike.py` (`/twiml/spike-bidi` +
`/spike-media` WS, throwaway). **Confirmed live 2026-09-07:** server-sent µ-law
`media` frames play into the call (a 1 kHz tone was audible) and inbound echo
works. Bidirectional `<Connect><Stream>` is sound.

---

## Phase 2a — bidirectional media transport (`fa789be`)

- `twiml.connect_stream` — Leg A as `<Connect><Stream>` + `group`/`role`
  `<Parameter>`s, **no Conference**. (`stream_and_conference`,
  `play_digits_then_conference` deleted.)
- `agent/bridge.py` `CallBridge` — decodes Leg A inbound → `AgentSession`
  (Tier 1 + FSM unchanged); owns the reverse path as a queue of ready-to-send
  Twilio messages.
- `api/media.py` reworked — socket read loop + a per-call write/drain task, keyed
  by `group`.
- `tests/test_bridge.py`, `tests/test_media.py` (WS end-to-end).

**Live CRA call 2026-09-07:** Leg A over `<Connect><Stream>`, DIALING→IVR_MENU,
Tier 2 menu wake, model chose "1", DTMF queued, socket stayed up.

---

## Phase 2b — the streaming probe (`37edb6e`)

The menu decision is turn-based (clip in, digit out). The **probe** can't be:
telling a live person from a recording means *saying something* and hearing the
reaction. `realtime/stream.py`:

- `probe_exchange(api_key, model, *, inbound, play, ...)` — opens **one** Realtime
  WS that both speaks and listens. Greets *"Hi, this is an assistant calling — is
  someone there?"*, streams ~4 s of the reply back to the model, asks for a
  one-line verdict → `HUMAN` / `NOT HUMAN` (ambiguous ⇒ `NOT HUMAN`, so we never
  bridge a user into hold music).
- `StreamingProbe` wraps the turn-based client so `AgentSession` still sees one
  `Tier2`; `probe()` bounces to `CallBridge.run_probe()` on the media loop via
  `run_coroutine_threadsafe`.
- `CallBridge` tees inbound call audio to an `asyncio.Queue` while a probe runs.

**Problem P2b.1 — the sync/async boundary.** `probe()` is called on the Tier 2
worker thread but the exchange must run on the media loop (it reads inbound
frames and plays audio). Fix: `bridge.probe_from_thread()` stores `self._loop`
and returns `asyncio.run_coroutine_threadsafe(self.run_probe(), self._loop)`.
This also fixed a `B023` (lambda binding a loop variable) by moving the bounce
into a method.

**Problem P2b.2 — GA Realtime event shapes were guesswork.** Resolved live with
`scripts/check_probe.py` (feeds fixture WAVs, checks the verdict):
`session.update` with `output_modalities:["audio"]`,
`audio.input.format={"type":"audio/pcmu"}`, `audio.input.turn_detection=None`
(manual turn control), `audio.output.format={"type":"audio/pcmu"}`. Per-turn
`response.create` with `response.instructions`. Events:
`response.output_audio.delta` (b64 µ-law), `response.output_audio_transcript.delta`,
`response.done`. `scripts/check_probe.py` confirms human / hold-music / ringback
all classify correctly.

---

## Phase 2b (cont.) — making DTMF actually actuate

**Problem P2b.3 — the stream `dtmf` message is inbound-only.** Twilio's Media
Stream `dtmf` event reports keys the *caller* pressed; there is no "send digit"
message on the socket. First rewrite assumed there was.

**Problem P2b.4 — DTMF-as-audio-tones doesn't work either (`0551b37`).** Next
attempt: synthesize the DTMF tones (`dtmf.py`) and inject them as `media` frames.
Failed live (Zoom: *"you have not entered any numbers"*). **Root cause:** Twilio
only converts *in-band* DTMF to signalling at the **PSTN edge** — tones we inject
into the stream are just audio to the far end's IVR.

**Fix (`2fcc15a`) — redirect + reconnect.** `twilio_client.press_digits` briefly
REST-redirects the leg to `<Play digits="w…"/><Pause/><Redirect>` back to
`/twiml/voice?group=…`. Twilio renders **real telephony DTMF** at the edge, then
the stream reconnects. `media.py` treats a 2nd `start` for a known `group` as a
**RECONNECT** (reuse the session + FSM, new `stream_sid`); `bridge.reconnecting`
tells the closing socket not to tear the call down.

**Problem P2b.5 — the recorder overwrote itself on every reconnect.** Only the
post-reconnect audio survived. Fix: the `WavRecorder` moved onto `bridge.recorder`
so it persists across DTMF reconnects; `media.py` reuses it; `bridge.close()`
closes it.

**Problem P2b.6 — the self-call test is invalid.** Dialing the Twilio number from
itself (`to == from`) collapses to one ambiguous leg (no child call). Abandoned;
used the **Zoom dial-in** (`+16699006833`, "enter meeting ID followed by pound")
as a 24/7 DTMF confirmation target — it echoes the digits it received.
**Confirmed:** sent `8005551234#`, Zoom replied *"…eight zero zero five five five
one two three four. This meeting ID does not exist."*

**Problem P2b.7 — menu model on data-entry prompts.** `_menu_instructions` gained
a data-entry clause (if the line asks you to *enter* a number and that exact
number is in the goal, return those digits; never invent digits).
`choose_menu_digit` now accepts multi-key strings, with guards (valid digits
only, ≤20 length). The model did enter `8005551234#` correctly on Zoom but took
~27 s to decide (deliberated across two menu wakes) — a latency/prompt issue, not
a correctness one. Fast "press 1 for English" menus are much quicker.

---

## Phase 3 — the handoff (`f6955c1`)

**Problem P3.1 — the call failed at the moment of success.** `_bridge()` called
`tier2.say_to_agent(...)`, which still raised `NotImplementedError` → the call
`_fail`ed with `tier2_not_implemented` exactly when a human picked up.

**Fix — `AgentSession.on_bridge` hook.** `_bridge()` now calls `self.on_bridge()`
(injected, no-op offline; `handoff_error` on exception) instead of the Tier 2
speech method. `CallBridge` wires it to `begin_handoff`.

**Leg B is now a real audio leg.** `/twiml/join` returns
`connect_stream(role="user", intro="Stay on the line — I'll connect you the
moment someone picks up.")` — **no more Conference** (`join_conference` deleted).
`media.py` `_serve_user_leg` waits up to 5 s for the group's bridge, binds Leg B,
and pumps its audio. Per-leg drain: `drain_agent()` / `drain_user()`.

**`CallBridge` is now a mixer:**
- `user_stream_sid`, `_outbound_agent`, `_outbound_user`, `relay_open` gate.
- `on_user_audio` — discarded until the handoff (the user is just waiting).
- `on_agent_audio` — after `relay_open`, every Leg A frame is also copied to
  Leg B's queue (and vice versa) — the two people talk directly through us.
- `begin_handoff()` → if there's no event loop or no OpenAI key, open the relay
  and send the SMS **synchronously**; otherwise schedule `_do_handoff()`.
- `_do_handoff()` → `speak_exchange` (new — **one episodic Realtime TTS turn**,
  keeps bullet 2 honest) plays *"connecting my client now"* into Leg A, waits for
  that queue to drain, then flips `relay_open` and calls `notify_user`.

**The SMS.** `twilio_client.send_sms`; `/calls` stashes the user's number in
`app.state.user_numbers[group]`; `media.py` `_wire_notify` builds the callback.

---

## Validation

| target | what it exercises | result |
|---|---|---|
| Spike call (`/twiml/spike-bidi`) | server → call audio + echo | tone audible, echo works ✅ |
| CRA 1-800-959-8281 (2026-09-07) | Leg A over `<Connect><Stream>`, Tier 1, FSM, menu wake, digit choice, DTMF queue, socket stability | DIALING→IVR_MENU, chose "1", DTMF, **socket survived** ✅ (office closed — no human, graceful stop) |
| Zoom dial-in `+16699006833` | DTMF **actuation** on a real far-end IVR | Zoom echoed `8005551234` — **redirect DTMF works** ✅ |
| `scripts/check_probe.py` (live Realtime API) | probe greeting + verdict on fixture audio | human / hold-music / ringback all correct ✅ |
| full live call → `ON_HOLD` → probe → handoff | end-to-end | **not yet done** ❌ |

**The one real gap:** the streaming probe and the handoff have never fired in a
single live call. Reaching `ON_HOLD` on a phone needs a real IVR that actually
queues you — the test-IVR self-call path is invalid (P2b.6) and roleplay audio
(humming) doesn't classify as sustained `HOLD_MUSIC`. The planned clean test is a
**weekday CRA call**. `speak_exchange` also assumes the same GA event shapes as
`probe_exchange` — plausible but unverified.

---

## Known limitations (carried into M8)

- **Bridge leak.** If a DTMF redirect succeeds but the stream never reconnects,
  the bridge stays in `app.state.bridges` forever — no timeout.
- **Leg B drop.** If the user hangs up before the handoff, there's no re-dial or
  fallback yet; the agent leg's own disconnect eventually tears down.
- **Leg B hears silence** between the intro line and the handoff. Fine for a
  short hold; a long queue with a clear upfront message is acceptable for the
  MVP, but comfort audio would be better.
- **Menu decision latency** — seconds, worst-case ~27 s on a data-entry prompt.
  The persistent-socket fix is `future-optimizations.md` #1.
- `/dev-call`, `/spike-call`, `/twiml/test-ivr*` are **unauthenticated** dev
  routes — fine behind a temporary ngrok tunnel, remove before any real deploy.

---

## Is M5 done?

**Yes — code-complete. There is no Phase 4.** Every planned piece (async Tier 2
dispatch, both legs on bidirectional streams, the streaming probe, DTMF
actuation, the handoff + notification) is built, unit-tested, and — for
everything except the probe/handoff composition — proven on a live call.

What's left is **not an M5 phase**:
1. One clean weekday live call to confirm probe → handoff end-to-end.
2. **M7** — Amazon SQS telemetry pipeline (resume bullet 3, part 1).
3. **M8** — circuit breaker around Tier 2 + dropped-call re-dial + fallback
   notification, plus the two carried-over items above (bridge-leak timeout,
   Leg-B-drop recovery). Resume bullet 3, part 2.

M6 (per-destination IVR database) is skipped — not on any resume bullet.

---

## Files

```
src/dialpass/agent/executor.py          Tier2Executor: Inline / Threaded (new)
src/dialpass/agent/bridge.py             CallBridge — per-call hub + handoff mixer (new)
src/dialpass/agent/session.py            off-loop Tier 2 dispatch; on_bridge hook
src/dialpass/realtime/stream.py          probe_exchange, speak_exchange, StreamingProbe (new)
src/dialpass/realtime/client.py          menu prompt: English + data-entry clause
src/dialpass/api/media.py                dual-leg socket, group keying, reconnect, relay drain
src/dialpass/api/calls.py                dial Leg B; stash group goal + user number
src/dialpass/api/voice.py                /twiml/voice + /twiml/join over connect_stream
src/dialpass/api/spike.py                bidi spike + /dev-call (throwaway, remove with M5)
src/dialpass/telephony/twiml.py          connect_stream (+ intro); conference verbs deleted
src/dialpass/telephony/twilio_client.py  press_digits (redirect), send_sms, hang_up
src/dialpass/main.py                     app.state.bridges / user_numbers; ThreadedExecutor
src/dialpass/config.py                   menu_collect_s, realtime_menu_model
scripts/check_probe.py                   live Realtime probe check (new)
tests/test_executor.py, test_bridge.py, test_media.py, test_stream.py,
tests/test_calls.py, test_twiml.py, tests/fakes.py
```

## Dev-only, remove before deployment

- `api/spike.py` + its `main.py` include line + `/dev-call`
- `/twiml/test-ivr`, `/twiml/test-ivr-branch` (voice.py)
- `/twiml/holdmusic-test` (M3), `DIALPASS_RECORD_DIR` recording wiring
