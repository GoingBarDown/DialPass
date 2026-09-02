# DialPass — Design Decisions

Reference doc. Captures the four design decisions locked before any code:

| Decision | Locked as |
| --- | --- |
| **#1 Bridging** | Twilio Conference; user joins muted at call start; handoff = unmute their leg |
| **#2 Detection** | Two-tier: local Tier 1 perception + `gpt-realtime-mini` Tier 2; FSM; hysteresis; active probe |
| **#3 Trigger client** | Web form + Twilio callback (MVP). Voice-trigger later, native app is the end goal |
| **#4 Scope** | M1–M5 = MVP; reassess M6–M8 after M5 works |

The design phase is complete. M1 is the first thing built.

---
d
## Context: what DialPass is

A caller-side AI agent. It places an outbound phone call on the user's behalf,
navigates the company's phone tree (IVR), waits on hold, detects when a real
human agent picks up, then connects the user into the live call and notifies
them. The user never listens to hold music — they get buzzed when a person is on
the line.

The engineering problem breaks into four parts:

1. **Real-time media relay** — bridge two audio streams with incompatible
   formats (Twilio μ-law 8 kHz ↔ the model's PCM) under a latency budget.
2. **Control loop over a partially observable environment** — sense the call
   audio, infer what phase the call is in, decide an action, act. Model the call
   as a finite state machine.
3. **Cost optimization** — cheap always-on perception, expensive cognition only
   at decision points.
4. **Reliability** — the user's time is committed to this call; degrade
   gracefully, never strand the user or the rep.

---

# Decision #1 — Bridging

**How the user gets connected to the human agent once one is reached.**

## The decision

**Use Twilio Conference.** Every leg of the call joins a named conference room:

- **Leg A** — DialPass's outbound call to the company. This leg also forks its
  audio to our server via TwiML `<Connect><Stream>` (Media Streams) so Tier 1
  can listen.
- **Leg B** — the user's phone. Joins the same conference **muted** at call
  start and stays connected passively the whole time. The user's phone can sit
  in their pocket.
- **The AI** — a participant whose audio into the conference we control in code.
  We choose whether to forward the model's synthesized speech into the room.

## Why Conference over the alternatives

| Option | Why not |
| --- | --- |
| **Call redirect** (hang up the AI, `<Dial>` transfer the raw call to the user) | Risks dropping the call during the handoff. Can't have a third party (AI) talking while a fourth (user) joins. No ability to record/observe the bridge. |
| **Dial the user only when a human is detected** | Adds 5–15 s of ring time at the worst possible moment. The rep hears dead air and hangs up. Also fails if the user doesn't pick up. |

Conference is the only option where the AI can keep talking to the rep while the
user's already-connected leg is un-muted. That eliminates handoff latency.

## The handoff sequence (step by step)

Triggered when Tier 1 + the probe confirm a human (see #2):

1. **Human confirmed.**
2. **AI says a holding line to the rep** — e.g. *"Thanks for picking up, I'm
   connecting my client now, one moment."* This buys 2–4 seconds so the rep
   never hears silence.
3. **Notify the user** — push notification + vibrate.
4. **Beat** — 2–4 seconds for the user to raise the phone.
5. **Stop forwarding AI audio** into the conference + **un-mute the user's leg.**
6. **Optional** — a short chime only the user hears, signalling "you're live."

From everyone's perspective the handoff is seamless: the rep was talking to a
voice that said "connecting you," and now the user is there. No ring, no dead
air.

## The one real tradeoff (say this out loud in interviews)

The user's phone line is occupied for the entire hold — on a mobile, 20–40 min
where they can't take another call. Accepted: the product's job is to make one
painful call painless, not to free up the line. A future "callback mode" could
re-dial the user at detection instead (slower handoff, covered by the AI
stalling the rep). Concurrent calls is a separate, larger product.

## Failure handling

- **User's leg drops mid-hold** (carrier, signal, battery on a 40-min call):
  detect fast, re-dial the user, rejoin them to the conference. This is the
  **only** place re-dial lives — an exception handler in `resilience/`, not the
  happy path.
- **User doesn't respond after handoff:** AI tells the rep it can't reach the
  client, offers a callback number or asks the rep to hold briefly, fires the
  notification again.
- **Rep hangs up during the stall:** mark the call failed, notify the user,
  optionally offer an automatic re-dial.

## Interview framing

> "Handoff latency is the hardest UX problem in the project. I solved it by
> having the user join the conference muted at call start and stay connected
> passively — so when a human is detected the handoff is just un-muting their
> leg while the AI covers the two-second gap by talking to the rep. Re-dial
> exists only as a recovery path if the user's leg drops."

---

# Decision #3 — Trigger client

**How the user starts a call and how their leg joins the conference.**

## The decision

**MVP: a one-page web form + Twilio callback.**

1. Web form — user enters the business number, their own number, and an optional
   goal ("get a refund on order #1234"). Clicks Call.
2. The form calls the backend (`POST /calls`).
3. Backend places the outbound call to the business (Leg A, with the media
   stream fork) and **rings the user's phone** ("This is DialPass, connecting
   you — please hold"). The user answers and becomes Leg B, muted.
4. From there the flow is identical to #1 — navigate, hold, detect, unmute.

The user participates *from their phone*; the web form is just the launch
button. Twilio can't sit in the path of a normal cellular call, so some DialPass
client always has to hand the business number to the backend first.

## Options considered

| # | Option | User's audio leg | Verdict |
| --- | --- | --- | --- |
| 1 | **Web form + Twilio callback** | Twilio rings the user's phone | **MVP.** Realistic, minimal build, clickable demo. |
| 2 | Inbound IVR — call a DialPass number, type target on keypad | The inbound call is the muted leg | Rejected — clunky digit entry. |
| 3 | Inbound + voice — call in and *say* "call Delta about a refund" | Same as #2 | **Post-MVP UX upgrade.** Better experience, needs STT + business-number lookup, not on the critical path. |
| 4 | SMS trigger — text "Call Comcast 1-800-…" | Callback mode — phone free during the wait, rung only at detection | Possible later, different (callback) model. |
| 5 | CLI / REST | Twilio rings the user's phone | Not a product surface — this is just `curl` / a `make` target for triggering. Dev tool, not a client. |
| 6 | Browser softphone — browser tab is the audio leg via Twilio Voice SDK (WebRTC) | Laptop / headphones, no second call | Optional polish after M5 — best for a self-contained screen recording. |
| 7 | Native mobile app — dialer UI, app holds a VoIP leg | The app | **End goal.** Weeks of work, adds little to interview outcomes. Not this cycle. |

## Dev workflow (not a client)

- **Iteration:** the offline fake harness (`scripts/simulate_call.py`) streams a
  pre-recorded menu→hold→human WAV through the real pipeline. Instant, free.
  This is 95% of development.
- **Triggering:** `curl` against `POST /calls`, wrapped in `make` targets
  (`make sim`, `make call TO=… FROM=…`). No dedicated CLI framework — add a
  ~20-line `click` wrapper later only if curl becomes painful.
- **Real calls:** for milestone verification, not iteration.

---

# Decision #4 — Build scope

**M1–M5 is the MVP.** Build straight through to a working end-to-end call, then
reassess whether M6–M8 are worth it before applications close.

## Milestones

| Milestone | What it delivers | Design status |
| --- | --- | --- |
| **M1** | Skeleton + offline harness — repo runs, one green test, fake Twilio streaming a WAV. No real phone, no paid APIs. | Partially — module layout sketched; web form scope known. |
| **M2** | Real Twilio media stream in — place an outbound call, receive audio over Media Streams, decode μ-law → PCM. No AI. | Lightly — known shape, just wiring. |
| **M3** | Tier 1 detection loop — the always-on classifier, the 10-label taxonomy, the FSM, hysteresis. No LLM. | **Locked** (see #2). Most thoroughly designed milestone. |
| **M4** | Menu navigation — wake `gpt-realtime-mini` on a menu, understand it, press the right key via DTMF, confirm the tree advanced. | **Mostly** — model + connection model locked. **Open: how to send DTMF mid-call over Media Streams.** |
| **M5** | Human detection + bridge — detect the human, run the probe, join the user's leg, do the handoff, notify. **End of M5 = working demo call.** | **Locked** (see #1). |
| **M6** | Per-destination database — IVR-map caching, interjection fingerprints + intervals, verify-on-use, write-back. | Conceptual — schema not written. |
| **M7** | Telemetry pipeline — SQS publisher off the hot path, consumer worker → Postgres. | Barely — event taxonomy exists. |
| **M8** | Resilience — circuit breaker around Tier 2, drop-recovery re-dial, graceful degradation. | Lightly — standard patterns. |

## Hardest milestones

- **M4** — mid-call DTMF over Media Streams may require synthesizing dual-tone
  PCM and injecting it into the outbound stream (Twilio REST `sendDigits` only
  works at call setup/redirect). Real DSP, real unknown. Budget weeks here.
- **M5** — the false-positive problem (human vs. recording) and the handoff
  choreography. Hard, but it's iteration, not a wall.

Everything else (M1–M2 wiring, M3 threshold tuning, M6–M8 standard patterns) is
work with low uncertainty.

---

# Decision #2 — Detection architecture (Tier 1 / Tier 2)

**How the agent knows what's happening on the call and decides what to do.**

## The core idea: two tiers

| | Tier 1 | Tier 2 |
| --- | --- | --- |
| **Role** | Always-on perception — classify the call audio | On-demand cognition — understand speech, decide, talk |
| **Runs** | Continuously, entire call | Only at decision points (~90 s total per call) |
| **Where** | Local CPU | `gpt-realtime-mini` over WebSocket |
| **Cost** | ~$0 | ~$0.05–0.15 per call |

The thing that runs for 40 minutes straight is nearly free. The thing that costs
money runs for ~90 seconds. This split is the whole economic argument: it takes
per-call inference cost from ~$6 (naive: stream the whole call to the model) to
under $0.15 — roughly a 90% cut.

## Tier 1 — the always-on loop

Runs for the entire call, from dial to handoff. Every **~0.5–1 second** it:

1. Grabs the last audio chunk from a rolling ~10–15 s buffer.
2. Computes cheap **acoustic features**:
   - frame energy
   - zero-crossing rate (ZCR)
   - **spectral flatness** — the main music-vs-speech signal; music is tonal and
     structured, speech is broadband and gappy
   - harmonicity / pitch
   - VAD speech-ratio over the window (silero-VAD or webrtcvad)
   - silence-gap duration
   - **tone templates** — US ringback ≈ 440 + 480 Hz on 2 s-on / 4 s-off; busy
     and reorder tones have their own signatures
3. Runs **light local ASR** (Whisper tiny/base via `whisper.cpp` or
   `faster-whisper`) — **only on detected speech segments**, not continuously.
   Used for keyword spotting ("representative", "press", "still there",
   position-in-queue phrases) and to match hold interjections against stored
   text. Semantic *understanding and decisions* stay in Tier 2.
4. Emits one **acoustic label** (see taxonomy below) with a confidence score.
5. Feeds that label to the FSM (`agent/state.py`), which decides: do nothing
   (still on hold), or wake Tier 2.

### The 10-label taxonomy

Tier 1's per-frame output. This is **not** the same as the FSM state — it's a
fast, noisy, low-level signal.

| Label | Meaning |
| --- | --- |
| `RINGBACK` | The "brring" while the company's line is ringing |
| `MENU_SPEAKING` | An IVR menu is currently listing options |
| `MENU_AWAITING_INPUT` | Menu finished, waiting for a keypress |
| `HOLD_MUSIC` | On hold, music playing |
| `HOLD_INTERJECTION` | The periodic "please continue to hold" recording |
| `LIVE_SPEECH_CANDIDATE` | Speech that might be a real human |
| `VOICEMAIL` | Hit an answering machine / voicemail greeting |
| `ERROR_TONE` | Busy, number-not-in-service, reorder (SIT) tones |
| `SILENCE` | Dead air |
| `UNKNOWN` | Speech or sound present, can't classify yet |

## Tier 2 — `gpt-realtime-mini`

### What it does

Wakes at decision points to: understand a menu and choose which key advances
toward a human; run the "Hello?" probe to confirm a human; handle an interactive
interjection ("are you still there? press 1"); speak the holding line to the rep
at handoff.

### Model choice

`gpt-realtime-mini` — ~3× cheaper than the full model ($10 / $20 per 1M audio
in/out vs $32 / $64). Menu navigation is not latency-critical and the task is
simple ("hear options, pick the one toward a human, press digit"). The
`realtime/` module is written to an interface so a cheaper STT→LLM→TTS pipeline
(Deepgram + a small text model + TTS) can be swapped in later if cost matters.

### Connection model — keep the socket open, gate the audio

The Realtime API has **no per-minute or per-session connection charge** — it
bills purely by audio/text token. An idle open WebSocket with no audio flowing
costs nothing.

So: **keep the WebSocket connected for the whole call**, and simply **stop
forwarding audio into it during hold**. No audio streamed = no tokens = no cost.
Wake = resume forwarding. Zero reconnect latency.

Two caveats handled in build:

1. **Context re-billing** — every time Tier 2 generates a response it re-ingests
   the accumulated session context as input tokens. Mitigate: tight system
   prompt, prompt caching for static parts ($0.30 cached vs $10), and
   trim/summarize prior menu turns (each menu decision is independent).
2. **Idle session timeout** — OpenAI enforces a max session lifetime and idle
   disconnect (~15–30 min historically). On a long hold OpenAI may drop the
   socket. Handle "OpenAI closed the idle socket" as a normal event — reconnect
   on the next wake. The rolling buffer covers anything missed.

### When Tier 2 wakes

On FSM transitions into states that need cognition — triggered by Tier 1 labels
`MENU_SPEAKING`, `LIVE_SPEECH_CANDIDATE`, or an interjection that looks
interactive (contains a question or a "press X"). Everything else — pure hold,
ringback, music — Tier 2 stays dark.

## The FSM — separate from the label stream

`agent/state.py`. Tracks the call phase. Only ever in one state; transitions are
explicit. **The FSM does not follow every Tier 1 label** — it rides through the
per-frame noise and only moves on a sustained signal.

### States

`DIALING` → `IVR_MENU` → `ON_HOLD` → `EVALUATING_SPEECH` → `HUMAN_DETECTED` →
`BRIDGING` → `DONE`, plus `FAILED` from any state.

### Transitions

```
DIALING            --ringback stops, sustained speech--> IVR_MENU
IVR_MENU           --menu answered, hold music starts--> ON_HOLD
ON_HOLD            --another menu detected------------> IVR_MENU   (loops back)
ON_HOLD            --sustained non-interjection speech-> EVALUATING_SPEECH
EVALUATING_SPEECH  --probe gets conversational reply--> HUMAN_DETECTED
EVALUATING_SPEECH  --interjection / no reply / music--> ON_HOLD    (falls back)
HUMAN_DETECTED     --user un-muted--------------------> BRIDGING
BRIDGING           --user in control-----------------> DONE
(any state)        --voicemail / error tone / drop---> FAILED
```

### `EVALUATING_SPEECH` — the key intermediate state

The dangerous ambiguity is **music → speech that might be a human**. In
`EVALUATING_SPEECH` we wake Tier 2 and run the probe, but we have **not**
notified the user and have **not** un-muted. It's a committed "I'm checking"
state, not "it's a human." Most of the time it falls back to `ON_HOLD` and the
user never knew anything happened.

## Hysteresis / debouncing

Tier 1's label stream flickers during any transition (`HOLD_MUSIC → SILENCE →
UNKNOWN → HOLD_INTERJECTION` over two seconds). Without damping, the FSM would
thrash. Four mechanisms:

1. **Debounce (N consecutive frames)** — the FSM won't accept a transition until
   the new label holds for N frames in a row. A one-frame blip is ignored.
2. **Asymmetric thresholds** — easy to *enter* a cautious state, hard to *leave*
   it. `ON_HOLD → EVALUATING_SPEECH` needs ~2 frames of speech;
   `EVALUATING_SPEECH → ON_HOLD` needs ~5 frames of music/silence or an explicit
   probe-failed signal. Rather linger than bail early and miss the human.
3. **Confidence gating** — each label has a confidence score; only
   high-confidence frames count toward the debounce counter. Transition-moment
   low-confidence frames don't push the FSM.
4. **Refractory period** — after a failed probe, block re-entry to
   `EVALUATING_SPEECH` for ~15–20 s so one ambiguous stretch doesn't fire three
   probes.

### Worked example — music → interjection → music

- 20 frames `HOLD_MUSIC` → FSM `ON_HOLD`
- music stops → 3 frames `SILENCE` → below the 5-frame "leave `ON_HOLD`"
  threshold → FSM stays `ON_HOLD`
- 6 frames of the interjection → local ASR matches "please continue to hold"
  **and** the per-destination record says an interjection is due now → labeled
  `HOLD_INTERJECTION` → doesn't arm `EVALUATING_SPEECH`
- music resumes → steady `HOLD_MUSIC`
- **FSM never moved.** Correct.

Same moment, real human: speech matches no interjection pattern, no music bed
underneath, cadence doesn't fit the interval → `LIVE_SPEECH_CANDIDATE` sustained
2 frames → `ON_HOLD → EVALUATING_SPEECH` → probe → conversational reply sustained
2 frames → `HUMAN_DETECTED`.

## The false-positive problem and the probe

Many hold queues do: music → *"We're experiencing higher than normal call
volume"* → music, every 30–60 s. If each trips the handoff, the user gets
notified repeatedly to a recording and trust collapses.

**Rule: never bridge on "music stopped" alone.** That's only a candidate. Then:

- **Interactivity** is the strongest signal — a recording talks at you and
  stops; a human waits for you to respond.
- **Music resumes** within a few seconds → it was an interjection.
- **Repetition** — audio matches something already heard this call → the loop.
- **Cadence** — speech at the known interval mark (e.g. Delta ≈ every 40 s) →
  probably the scheduled message.

Then Tier 2 runs an **active probe**: says "Hello?", listens for a conversational
interactive reply. Human replies and waits; a recording keeps rolling or loops.
Only on a positive → notify + un-mute.

**Bias toward false negatives.** Notifying the user too early (to a recording) is
far more damaging than bridging a few seconds late — and the AI's holding line
covers a late bridge anyway.

## Non-connect handling

`VOICEMAIL`, `ERROR_TONE`, no-answer, busy — Tier 1 catches these in the first
~10 s, before any hold logic runs. Fail the call cleanly with a notification.

## Logging → two consumers

Every frame logs its feature vector + label + (post-call) ground truth. That log
feeds two things:

1. **Per-destination IVR/hold map** (build milestone M6). Keyed by phone number:

   | Contributed by | Data |
   | --- | --- |
   | Tier 2 | menu prompt text → digit pressed → where it led (the IVR tree) |
   | Tier 2 | rep greeting phrasing |
   | Tier 1 | interjection transcripts + timestamps → median gap = the interval |
   | Tier 1 | total hold duration, whether music sits under interjections |
   | FSM | outcome — reached human / voicemail / error / abandoned |

   Next call to that number loads the map: no Tier 2 wake for known menus, Tier 1
   pre-armed with the interjection cadence. **Caveats:** one call is a noisy
   sample — require a couple of calls to agree before trusting a branch; always
   **verify-on-use** (confirm the live prompt still matches before firing a
   cached digit; on drift, fall back to Tier 2 and rewrite the entry).

2. **Offline training set** — feature vectors + ground-truth labels (you know in
   hindsight exactly when the human picked up) to train the eventual learned
   classifier.

## Heuristics now, learned model later

- **M1–M5:** heuristics only. Get the end-to-end call working. Log everything.
- **After M5:** you have real recordings with real labels. Train a small
  classifier. Run it in **shadow mode** — it predicts, you compare to the
  heuristics, it controls nothing.
- **When shadow numbers are good:** promote to hybrid — model decides,
  heuristics act as a sanity floor ("never bridge if the rule is 100% sure it's
  still hold music").

`classifier.py` is written to an interface so the model slots in behind it.

## Open implementation knobs (tune during M3, not architecture)

- Frame interval — 0.5 s vs 1 s
- Debounce N per transition
- Confidence thresholds per label
- Which local ASR — `whisper.cpp` vs `faster-whisper`
- Probe wording and how long to wait for a reply

---

## Status

All four design decisions are locked. **M1 is built** (skeleton, offline
pipeline, detection FSM, codec, CI). Next: **M2** — real Twilio outbound call +
Media Streams in. See the README for milestone status.
