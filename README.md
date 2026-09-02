# DialPass

**A caller-side AI agent that sits through phone trees and hold music for you, then hands you a live human.**

You give DialPass a business number and your number. It places the call, navigates
the IVR menu, waits on hold, detects when a real agent picks up, bridges you into
the live call, and buzzes your phone. You never listen to hold music.

> Status: **M1 — skeleton + offline pipeline.** No real calls yet. See [Milestones](#milestones).

---

## The hard part

Streaming a 25-minute call to a speech-to-speech model the whole time costs real
money and is mostly wasted — 20+ of those minutes are hold music that needs no
cognition. DialPass splits perception from cognition:

| | Tier 1 — perception | Tier 2 — cognition |
| --- | --- | --- |
| Runs | continuously, whole call | only at decision points (~90s total) |
| Where | local CPU, cheap DSP + tiny ASR | `gpt-realtime-mini` over WebSocket |
| Job | classify the call audio every ~0.5s | understand a menu, press a key, confirm a human |
| Cost | ~$0 | ~$0.05–0.15 / call |

A finite state machine consumes Tier 1's noisy per-frame labels through
[hysteresis](docs/design-decisions.md#hysteresis--debouncing) and only wakes Tier 2
when the call actually needs a decision. On hold, the model's socket stays open
but no audio flows — so no tokens, no cost.

### Cost model

Rough per-call estimate for a 25-minute call (5 min of menus, 20 min of hold):

| Approach | AI inference | Notes |
| --- | --- | --- |
| Naive (stream whole call to a realtime model) | ~$5–12 | continuous audio tokens + context re-billing on every turn |
| DialPass two-tier | **< $0.15** | Tier 2 active ~90s; Tier 1 is local |

≈ **90% reduction in active inference cost.** Telephony (~$0.90/call) then dominates,
which is the point — you can't optimize the phone company away, but you can stop
paying a model to listen to Muzak. Assumptions are in
[`docs/design-decisions.md`](docs/design-decisions.md#decision-2--detection-architecture-tier-1--tier-2).

---

## Architecture

```
  Twilio outbound call ──<Connect><Stream>──▶  /media (WS)  ──▶  AgentSession
        │                                                          │
        │  (all legs in a named Twilio Conference)                 ├─ RollingBuffer
        │                                                          ├─ Tier 1 Classifier ──▶ Label + confidence
  User's phone ── joins muted, stays connected ──┐                 ├─ CallStateMachine (FSM + hysteresis)
        │                                        │                 └─ Tier 2 (gpt-realtime-mini)  ◀─ woken on demand
        ▼                                        │                        │
  unmuted at handoff ◀───────────────────────────┴────────────────────────┘  presses DTMF / runs "Hello?" probe
```

Ports-and-adapters: `agent/` is pure logic and imports no vendor SDK. Twilio and
OpenAI live behind interfaces in `telephony/` and `realtime/`, each with a fake
for offline runs and tests.

```
src/dialpass/
  config.py            settings (pydantic-settings)
  main.py              FastAPI app + session registry
  api/                 health · calls · media (Twilio media WebSocket)
  agent/               session · state (FSM) · classifier (Tier 1) · labels
  telephony/           twilio_client · twiml · dtmf (tone synthesis) · audio (mu-law <-> PCM)
  realtime/            client (gpt-realtime-mini) · fake · protocol
  telemetry/           events · publisher (log sink now, SQS in M7)
  resilience/          circuit_breaker
  persistence/         db · models          (M6/M7)
  workers/             telemetry_worker      (M7)
scripts/simulate_call.py   offline harness
```

---

## Quickstart

```bash
uv sync --extra dev          # or: make install
make sim                     # run a synthetic call through the real pipeline
make test
```

`make sim` synthesizes ringback → menu → hold → interjection → human, feeds it
through the codec, Tier 1, and the FSM, and prints the state timeline ending in a
bridge. No API keys required.

Run the server:

```bash
make dev                     # http://localhost:8000/health
```

---

## Milestones

| | | Status |
| --- | --- | --- |
| **M1** | Skeleton + offline harness | **done** |
| **M2** | Real Twilio media stream in (outbound call, mu-law decode) | next |
| **M3** | Tier 1 detection loop (real features, VAD, tone templates, local ASR) | — |
| **M4** | Menu navigation (wake Tier 2, understand menu, press key via DTMF) | — |
| **M5** | Human detection + bridge (probe, join user's leg, handoff, notify) | — |
| **M6** | Per-destination database (cached IVR maps, interjection cadence) | — |
| **M7** | Telemetry pipeline (SQS off the hot path → Postgres) | — |
| **M8** | Resilience (circuit breaker around Tier 2, drop-recovery) | — |

Design rationale for every decision: [`docs/design-decisions.md`](docs/design-decisions.md).
