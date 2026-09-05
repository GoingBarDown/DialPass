# M3 — Tier 1 classifier tuning against real calls

**Status:** done, committed `e11d819` (2026-09-05). 32 tests pass, ruff + mypy clean.

## Goal

Tier 1 is the always-on local classifier (`agent/classifier.py`,
`HeuristicClassifier`). It runs on every ~0.5s audio window with **no API call**
and emits one coarse acoustic label:

```
SILENCE  RINGBACK  HOLD_MUSIC  LIVE_SPEECH_CANDIDATE  (UNKNOWN)
```

The FSM (`agent/state.py`) consumes that label stream and decides when to wake
the expensive Tier 2 model. Tier 1 deliberately does **not** try to tell a
recorded IVR/menu voice from a live human — both look identical to these
features; that call is Tier 2's job (the "Hello?" probe).

Coming into M3 the classifier had only ever seen synthetic test tones. M3 = make
it work on real phone audio.

---

## Phase 1 — Recording infrastructure

**Built:**
- `telephony/recorder.py` — `WavRecorder`, an incremental WAV writer held open
  for the call's lifetime.
- `api/media.py` — when `DIALPASS_RECORD_DIR` is set, every decoded PCM frame is
  tee'd to `recordings/<call_id>.wav` alongside being fed to the session.
- `config.py` — `record_dir` setting (blank in production).
- `scripts/analyze_audio.py` — slides the **exact production analysis window**
  (`classify_window_ms` / `classifier_interval_ms`) over a recording and prints,
  per window: the `HeuristicClassifier` verdict + raw features. With
  `--segments "a:b=LABEL,..."` it also prints per-label feature distributions
  (min/median/max) and a confusion matrix. This is the tuning instrument.
- `recordings/` gitignored.

**Learned:** nothing surprising yet — this is plumbing. The design choice that
paid off: analyze offline against a saved WAV, so tuning is a fast edit/run loop
with zero phone calls.

---

## Phase 2 — Data capture

Real Twilio calls. Claude can't place outbound calls from a tool (auto-mode
classifier blocks it), so every call was placed by the user with `!curl ...` /
`!uv run python scripts/place_test_call.py ...` in the Claude Code prompt.

| # | target | intent | result |
|---|---|---|---|
| 1 | Delta Air Lines `+18002211212` | ringback + real IVR menu | **kept** — 59.5s |
| 2 | own cell, `/twiml/voice` | Twilio conference hold music | **failed** — see below |
| 3 | own cell, `/twiml/voice`, talk 85s | live human speech | **kept** — 85.5s |
| 4 | own cell, `/twiml/holdmusic-test` | phone-band hold music | **kept after 2 retries** — 41s |

**Kept recordings** (all 8 kHz mono int16, gitignored):
- `CA09928f79585b226cfdfe59d8a4937b77.wav` — Delta: ringback (~0–1s), then a
  recorded IVR voice re-prompting into dead air, then "goodbye". Two silence
  types: noise-floor (rms ~3e-4, flatness ~0.1) and digital-zero (rms 0,
  flatness 1.0). No hold music (no menu selection → never queued).
- `CA7ffd6c9f3db6d612ae90779280fdbc15.wav` — 85s of the user reading aloud, with
  natural pauses.
- `CA3a0f34378b3388ca36483827eb5264bb.wav` — Twilio's classic hold-music clip
  played into the call, phone-band.

### Problems hit in Phase 2

**P2.1 — Call 2 captured silence, not hold music.**
`/twiml/voice` puts Leg A into the conference with `startConferenceOnEnter="true"`,
so the conference starts immediately and Twilio never plays wait music. We
recorded ~19s of line noise (rms ~0.009, zcr ~0.8).
*Fix:* dedicated `/twiml/holdmusic-test` route — `<Start><Stream>` +
`<Play loop>http://demo.twilio.com/docs/classic.mp3</Play>` — and
`scripts/place_test_call.py` to place a call pointed at an arbitrary TwiML path
(the `/calls` endpoint is hard-wired to the conference path).

**P2.2 — Recordings were being cut off at ~19s.**
`api/media.py` did `if session.finished: break`. Because Tier 2 isn't built,
any call that reached `IVR_MENU` failed immediately (`tier2_not_implemented`),
the session went `finished`, the loop broke, and the recorder closed — mid-call.
*Fix:* decouple recording from session lifecycle. Always write to the recorder;
only feed the session while it's live; in record mode keep draining audio until
Twilio sends `stop` / disconnects. Production behavior (no recorder → break when
finished) unchanged.

**P2.3 — First hold-music take: silence again.**
`track="inbound_track"` captures only what the far end *sends* us. The `<Play>`
music goes the *other* direction (Twilio → phone). Inbound track = the user's
silent mic.
*Fix:* `track="outbound_track"` — capture what Twilio plays toward the phone.

**P2.4 — Second hold-music take: contaminated.**
User talked over the music while it recorded.
*Fix:* re-run, stay silent. (Deleted the bad take.)

**P2.5 — The hold-music clip is Rick Astley** (`classic.mp3` has vocals).
Not fixed — accepted. Sung vocals have syllabic structure too, so this is a
*harder* (adversarial) music sample than an instrumental bed. If it separates,
an instrumental bed separates more easily.

---

## Phase 3 — Analysis: why the old classifier failed

Old speech/music rule:

```python
if flatness < 0.15:
    return HOLD_MUSIC          # "tonal + sustained -> music"
if zcr > 0.08 and flatness > 0.2:
    return LIVE_SPEECH_CANDIDATE
```

Feature distributions from the recordings (non-silent windows):

| feature | hold music | human speech | Delta IVR speech |
|---|---|---|---|
| rms | 0.07–0.19 | 0.02–0.19 | 0.03–0.24 |
| zcr | 0.13–0.31 | 0.11–0.59 | 0.05–0.34 |
| flatness | 0.04–0.13 | **0.001–0.41 (median 0.03)** | 0.015–0.17 |

**What we learned:**

1. **Ringback and silence detection already worked** and transfer perfectly to
   real audio. US ringback = 440+480 Hz; `tone_460_ratio` was ~0.99 with
   `flatness` ~0.001 on the real Delta ringback. That rule is untouched.
2. **The speech/music rule was backwards for phone audio.** Real phone-band
   speech is *low*-flatness — formants are tonal and the codec band-limits
   everything to 300–3400 Hz, so speech looks "tonal + sustained" too. Median
   speech flatness was **0.03**, well under the 0.15 music threshold. ~85% of
   real human speech (and all of Delta's IVR voice) was labeled `HOLD_MUSIC`.
3. **Static spectral features cannot separate speech from a music bed.** rms,
   zcr, flatness all overlap almost completely. No threshold on any of them
   works.
4. **The separable dimension is time.** Speech energy pulses at the syllable
   rate (~3–7 Hz) with near-silent gaps between words. A music bed runs steady
   and (this clip) carries a strong beat. Single-window spectra throw that away.

---

## Phase 4 — Temporal (envelope) features

The **envelope** = the shape of loudness over time (tenths of a second),
ignoring the fast waveform oscillation that carries pitch/timbre.

Computed cheaply: chop the analysis window into 25 ms sub-frames, take each
sub-frame's RMS → that sequence is the envelope. Then:

| feature | definition | speech | music bed |
|---|---|---|---|
| `quiet_frac` | fraction of sub-frames below 20% of the window's peak | **high** (inter-syllable gaps) | low |
| `env_cv` | std / mean of the sub-frame RMS sequence | **high** | low |
| `mod_4hz` | share of envelope-spectrum energy in the 3–8 Hz band | — | **high** (beat) |

Verified on the recordings before wiring in (non-silent windows, p25–p75):

| | `quiet_frac` | `env_cv` | `mod_4hz` |
|---|---|---|---|
| hold music | 0.10–0.25 | 0.47–0.60 | 0.53–0.63 |
| human speech | 0.29–0.61 | 0.68–1.20 | 0.12–0.32 |

`quiet_frac` is the cleanest and most speaker/genre-independent signal — every
speaker pauses between syllables; a sustained bed doesn't. `mod_4hz` being
*higher* for music was the opposite of the first guess (expected speech to peak
at ~4 Hz) but it's still discriminative for this clip — it just reflects the
song's beat, so it's used only as a supporting vote, not the primary rule.

Added all three to `acoustic_features()`; `_EMPTY_FEATURES` covers the
zero-length / <4-sub-frame guard.

---

## Phase 5 — Retuned decision tree

```python
if rms < SILENCE_RMS (0.01):            -> SILENCE
if ring > 0.6 and flatness < 0.1:       -> RINGBACK
musicky = mod >= 0.5 and quiet < 0.32 and env_cv < 0.75
speechy = quiet >= 0.25 or env_cv >= 0.62
if musicky:                             -> HOLD_MUSIC
if speechy:                             -> LIVE_SPEECH_CANDIDATE
if flatness < 0.08 and env_cv < 0.6:    -> HOLD_MUSIC   (steady + tonal fallback)
otherwise:                              -> UNKNOWN
```

Thresholds were iterated ~3× against the confusion matrices. Key tension:
tightening the `musicky` rule to recover speech pushed borderline windows into
`UNKNOWN` instead; the `env_cv < 0.75` guard on `musicky` (music envelope tops
out ~0.70, speech runs higher) was the useful lever.

### Results on the recordings (per 0.5s window)

| input | before M3 | after M3 |
|---|---|---|
| ringback | 100% | 100% |
| silence | 100% | 100% |
| hold music | (100% only because *everything* was HOLD_MUSIC) | ~90% |
| human speech | **9%** | **85%** |
| Delta IVR speech | ~0% | ~92% |

**Stopped tuning here.** Further hand-fitting 6 thresholds to 3 recordings
overfits. The residual ~10–15% per-frame error is isolated single windows; the
FSM requires a label to hold 2–5 consecutive frames before it acts, so scattered
misclassifications never flip a state.

---

## Phase 6 — Downstream fixes

**FSM gap (found on the M2 test call).** `_on_dialing` only exited on speech-like
labels → `IVR_MENU`. A line that answers straight into a music queue ("all
agents are busy...") had no exit from `DIALING`. Added: sustained `HOLD_MUSIC`
from `DIALING` → `ON_HOLD` directly (`enter_hold_frames` streak). The `ON_HOLD`
handler still catches a later menu.

**Synthetic audio too clean.** `testing._speech_like` used a pure 4 Hz sine
envelope — never actually silent, all envelope energy at exactly 4 Hz. The new
features (correctly) read that as "too steady = music", so the offline
`heuristic` sim stopped reaching `IVR_MENU` and a test failed. Rewrote
`_speech_like` to emit alternating voiced bursts and real near-silent gaps at
~syllable rate. The `heuristic` sim now runs the full call
(`DIALING → IVR_MENU → DTMF → ON_HOLD → EVALUATING_SPEECH → human → DONE`).

**Regression test.** `tests/test_classifier_real_audio.py` + three committed WAV
fixtures trimmed from the real recordings (`tests/fixtures/`,
ringback / hold_music / human_speech, ~2–10s each). Asserts: ringback fixture
detects `RINGBACK` and never `HOLD_MUSIC`; hold-music fixture ≥70% `HOLD_MUSIC`
and ≤20% speech; human-speech fixture ≥75% speech and ≤20% `HOLD_MUSIC`. These
guard against a regression to "everything reads as music".

---

## What's real vs still stubbed after M3

**Real:** FSM, hysteresis/debounce, μ-law codec, DTMF tone synthesis, circuit
breaker, Twilio outbound + Media Streams ingest, **Tier 1 classifier**.

**Stubbed (raise `NotImplementedError`):** Tier 2 (`realtime/client.py`),
conference mute/bridge, persistence, telemetry worker. `session.py` degrades
gracefully — a call that needs Tier 2 fails with reason `tier2_not_implemented`
rather than crashing.

---

## Known limitations (state these first in an interview)

- Heuristic, not learned. Tuned against ~3 real calls / 2 distinct speakers
  (the user + Delta's IVR voice actor).
- Per-frame accuracy ~85–90%; end-to-end reliability comes from the FSM's
  multi-frame debounce, not from the classifier alone.
- Not yet validated against: multiple live human voices, real corporate
  (instrumental) hold music, a real agent pickup mid-hold. Plan: capture these
  together on the M5 probe/bridge test call.
- **Production path:** swap `HeuristicClassifier` for a trained VAD
  (e.g. Silero VAD, ~2 MB ONNX) behind the unchanged `Classifier` protocol. The
  seam is already there — that was the point of keeping Tier 1 behind an
  interface.

---

## Files touched

```
src/dialpass/agent/classifier.py   envelope features + retuned rules
src/dialpass/agent/state.py        DIALING -> ON_HOLD transition
src/dialpass/api/media.py          recording tee; drain-past-finished
src/dialpass/api/voice.py          /twiml/holdmusic-test (dev)
src/dialpass/config.py             record_dir setting
src/dialpass/telephony/recorder.py WavRecorder (new)
src/dialpass/testing.py            gappy _speech_like
scripts/analyze_audio.py           windowed feature analysis (new)
scripts/place_test_call.py         place call at arbitrary TwiML path (new)
tests/test_classifier_real_audio.py + tests/fixtures/*.wav   regression (new)
.env.example / .gitignore          DIALPASS_RECORD_DIR, recordings/
```

## Dev-only, remove before any real deployment

- `/twiml/holdmusic-test` route in `api/voice.py`
- `scripts/place_test_call.py`
- `DIALPASS_RECORD_DIR` / `recorder.py` wiring (or gate behind an explicit debug flag)
