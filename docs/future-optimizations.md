# Future optimizations

Things deliberately **not** built for the MVP (M1–M8), kept here because each is a
real improvement with a clear rationale — and because "what would you do next?"
is a standard interview question. For each: what it is, why it matters, rough
effort, and how it connects to the existing design.

Ordered roughly by value-for-effort.

---

## 1. Persistent Tier 2 WebSocket (one socket per call, not per decision)

**Now:** M4 opens a fresh OpenAI Realtime WebSocket for every menu decision —
connect, append audio, commit, get digit, close.

**Better:** open one socket when the call starts, keep it for the whole call,
reuse it for every menu prompt and the probe. Stop feeding it audio during hold
(no audio → no tokens → no cost), resume on wake.

**Why:** removes a ~300–500ms TLS + session-setup handshake from every decision.
On a call that hits 3 menu levels that's ~1.5s saved, and it's the connection
model the design doc already specifies.

**Effort:** medium. The work is state management, not new concepts — idle-timeout
handling (OpenAI drops idle sockets), reconnect-on-next-wake, clearing the input
buffer between uses so stale audio from menu 1 doesn't leak into menu 2's
decision.

**Interview angle:** "I built it stateless-per-decision first because it's easier
to reason about and test; the persistent socket is a caching optimization with a
reconnect state machine on top."

---

## 2. Known-tree fast path (per-destination priors)

**Now:** every menu is navigated live by Tier 2, even for numbers we've called
100 times.

**Better:** keep a map of `phone number → known key sequence` ("press 0, then 0,
then say 'agent'"). If we have a prior for this number, skip Tier 2 entirely and
fire the digits on a timed schedule. Fall back to live Tier 2 navigation only
when (a) there's no prior, or (b) the audio doesn't match the expected prompt
(the tree changed).

**Sources:** user-supplied sequences; crowdsourced DBs (GetHuman and similar);
self-learned — record the successful path from each call and reuse it.

**Why:** the common case ("call the same airline every day") costs ~zero AI and
navigates in one pass. Tier 2 becomes the fallback, not the default. Same
cache-with-fallback pattern as the two-tier design itself.

**Caveat:** IVR trees change by season, region, time of day, and which number you
dialed; menus can be dynamic ("for the account ending in 4…"). So this is a fast
path, never a full replacement — you still need live navigation as ground truth,
and you need to detect when the prior is stale.

**Effort:** small for a self-learned version (persist the digit sequence keyed by
number, replay it, verify the first prompt matches). Larger if integrating an
external DB.

**Interview angle:** hybrid system — cheap deterministic path for the hot case,
expensive general path as fallback, with staleness detection. Also a nice
"how would you cut cost 10x" answer.

---

## 3. Trained VAD / audio model for Tier 1

**Now:** `HeuristicClassifier` — hand-tuned DSP rules (RMS, spectral flatness,
ringback tone band, envelope features). Tuned in M3 against ~3 real recordings /
2 speakers. Per-frame accuracy ~85–90% on speech-vs-music.

**Better:** swap in a trained model behind the unchanged `Classifier` protocol —
e.g. Silero VAD (~2 MB ONNX, CPU-real-time) for speech/non-speech, or a small
learned classifier over the same features trained on a labelled corpus of real
calls.

**Why:** the heuristic is brittle to voices and hold-music styles it wasn't tuned
on. A trained VAD is ~95%+ and generalizes. The interface seam (`Classifier`
Protocol, `HeuristicClassifier` vs `ScriptedClassifier`) was built specifically
so this is a drop-in.

**Effort:** small to wire Silero (it's a published model); the real cost is
building a labelled dataset of real calls to validate against.

**Interview angle:** "the heuristic proved the two-tier architecture cheaply and
gave me labelled data; production swaps in a trained model without touching the
FSM or session layer — that's why Tier 1 is behind an interface."

---

## 4. Bidirectional DTMF injection (drop the redirect)

**Now (M4 plan):** send a digit by redirecting the live call via
`calls(sid).update()` to TwiML with `<Play digits="2"/>`, then rejoin the
conference. ~2–5s per digit, most of it Twilio's redirect mechanism.

**Better:** hold an outbound media stream open and inject synthesized DTMF PCM
(`telephony/dtmf.py` already generates the dual-tone waveform) directly into it.
No redirect, no conference pop-out. Sub-second.

**Why:** cuts menu-navigation latency to roughly the model's decision time.
Removes the "does the redirect kill the media stream?" fragility.

**Effort:** larger — needs Twilio's bidirectional `<Connect><Stream>` (or Media
Streams with an outbound track), which is the same hard path M5's probe needs.
Doing #4 and M5's streaming together amortizes the effort.

**Interview angle:** the MVP's redirect approach is a pragmatic workaround for a
platform limitation (`sendDigits` only fires at call setup); the streaming
version is the "right" fix once the bidirectional audio path exists anyway.

---

## 5. Batch / cheaper model for menu transcription

**Now:** `gpt-realtime-mini` handles menu decisions.

**Better:** for menu prompts specifically (not the probe), a cheaper path could
be Whisper transcription + a small text model, or even keyword spotting against
the goal ("reservations", "billing", "agent"). Menus are read-once announcements
— they don't need a real-time model.

**Why:** menu decisions might be the majority of Tier 2 wakes on a call that
loops through submenus. Making them the cheapest possible path compounds the
two-tier savings.

**Trade-off:** adds a second vendor/model to maintain, and splits the "one Tier 2
interface" cleanliness. Only worth it if menu-decision cost actually dominates in
production metrics.

**Interview angle:** know when *not* to optimize — this one's premature until
telemetry (bullet 3's SQS pipeline) shows menu decisions are the cost driver.

---

## 6. Reliability / resilience

- **Retry + idempotency on Twilio calls.** `calls.create` / `calls.update` can
  fail transiently. Wrap in bounded retry; key call creation by an idempotency
  token so a retry doesn't place two calls.
- **Circuit breaker tuning from real data.** M8 ships a circuit breaker with
  guessed thresholds. Once telemetry is flowing, tune trip/reset thresholds to
  observed failure rates instead of guesses.
- **Graceful Tier 2 degradation.** Already partially done (`session.py` fails the
  call cleanly if Tier 2 is unavailable). Better: on Tier 2 outage, fall back to
  a known-tree prior (#2) or notify the user to take over manually, rather than
  failing.
- **Dead-letter handling for the SQS pipeline.** Telemetry events that fail to
  publish should hit a DLQ, not vanish — otherwise you lose exactly the data you
  need to debug a bad call.

---

## 7. Scale / infra (further out)

- **Horizontal scale of the media-stream workers.** Each call holds an open
  WebSocket + a rolling audio buffer + an FSM in memory. Today that's one
  process. At scale: stateless workers, call state in Redis, a load balancer
  that pins a call's media stream to one worker (sticky by call SID).
- **Backpressure on the audio path.** Twilio pushes ~50 messages/sec/call. If the
  classifier or buffer write stalls, frames queue unbounded. Add a bounded queue
  that drops oldest frames under load (a dropped 20ms frame is survivable; an OOM
  is not).
- **Cost attribution per call.** Tag every Tier 2 token spend with a call SID so
  you can report cost-per-call and catch a call that's burning money in a menu
  loop.
- **Region pinning.** Place the server near the Twilio region handling the call
  to shave media-stream RTT.

---

## 8. Product / capability (not optimizations, but the obvious next asks)

- **Voice trigger** instead of a web form (design doc #3 — the eventual native
  app).
- **Multiple concurrent calls** for one user (call three pharmacies, take
  whichever answers first).
- **Scheduled / retry calls** ("keep trying the DMV every 10 min until a human
  picks up").
- **Call summary** — transcript + outcome sent to the user after handoff.
- **Learned goal → digit mapping** across all users' calls to the same number,
  feeding #2.

---

## What to actually say in an interview

If asked "what would you improve?" — lead with the ones that show judgment, not
just a wishlist:

1. **#3 (trained VAD)** — shows you designed for replaceability and know the
   heuristic's limits.
2. **#2 (known-tree fast path)** — shows hybrid cheap-path/fallback thinking.
3. **#1 (persistent socket)** — shows you shipped the simple version first on
   purpose.
4. **#5's caveat** — shows you know not to optimize before telemetry proves
   where the cost is.

The meta-point: every one of these has a seam already in the codebase
(`Classifier` protocol, `Tier2` protocol, `TelemetrySink` protocol,
`dtmf.py` waveform gen sitting ready). The MVP was built so these are additions,
not rewrites.
