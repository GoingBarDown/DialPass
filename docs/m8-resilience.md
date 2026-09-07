# M8 — resilience: circuit breaker, drop recovery, fallback notification

**Status:** done, branch `feat/m8-resilience`. 107 tests pass, ruff + mypy clean.

## Goal

Everything so far assumes the happy path. M8 makes the failure paths graceful —
resume bullet 3, part 2: *"a circuit breaker that reroutes dropped calls and
timeouts to a fallback notification."*

Three things, plus the two loose ends M5 left:

1. **Circuit breaker around Tier 2** — when the Realtime API starts failing or
   timing out, stop hammering it, fail fast, and route the call to a fallback SMS.
2. **Drop-recovery re-dial** — an agent leg that drops mid-call is re-dialed.
3. **Fallback notification** — every dead end texts the user a reason-appropriate
   line instead of leaving them on a silent call.
4. Bridge-leak reaper (M5 carry-over).
5. Leg-B-drop handling (M5 carry-over).

---

## 1. Circuit breaker around Tier 2

`resilience/circuit_breaker.py` already existed (unit-tested since M1) but was
wired to nothing. M8 wires it.

- **One breaker per process**, created in `main.py`, injected into every
  `AgentSession` (like the telemetry sink). One call's Realtime outage protects
  the next — a tripped breaker fails new calls fast rather than making each one
  wait out a 10 s timeout. Added a `threading.RLock` since it's now shared across
  every call's Tier 2 executor thread.
- `AgentSession._start_menu` / `_start_probe` wrap the Tier 2 call:
  `self._breaker.call(self.tier2.probe, ...)`. The breaker counts an exception
  as a failure; a plain abstain (`MenuDecision(None)`, a not-human verdict) is a
  normal return and doesn't count.
- For that distinction to hold, "the exchange failed" now **raises** instead of
  returning a sentinel:
  - `ProbeOutcome` gained `ok: bool`. `probe_exchange` returns `ok=False` on
    timeout / transport error / error-event. `StreamingProbe.probe` raises
    `Tier2Unavailable` when `not ok`.
  - `RealtimeClient.choose_menu_digit` raises `Tier2Unavailable` when the
    exchange returns nothing (was `MenuDecision(None, "tier2 unavailable")`).
- `_poll_tier2` reacts:

  | error | menu | probe |
  |---|---|---|
  | `Tier2Unavailable` (one blip) | **survivable** — log, keep listening, next prompt re-wakes | `_fail("tier2_unavailable")` — can't confirm a human, must not bridge |
  | `CircuitOpenError` (breaker tripped) | `_fail("tier2_unavailable")` | `_fail("tier2_unavailable")` |
  | `NotImplementedError` | `_fail("tier2_not_implemented")` | same |

  Defaults: `tier2_failure_threshold=4`, `tier2_reset_timeout_s=45`.

`/health` reports `tier2_breaker` state and flips `status` to `"degraded"` when
it's not closed.

---

## 2. Drop-recovery re-dial

`/calls` now also stashes `app.state.call_meta[group] = {business_number, redials}`
and the per-group context (`pending_goals`, `user_legs`, `user_numbers`) is
**read, not popped**, on agent-leg connect — so it survives a re-dial.

`media.py`'s socket `finally` → `_finish_agent_leg`:

```
if bridge.reconnecting:            leave it — DTMF redirect in flight
elif dropped mid-call (no `stop`, session not finished) and redials < 2:
        drop the old bridge, re-place the business call for the same group
else:   terminal — _teardown_group (fallback SMS if it was a drop, hang up both legs)
```

A re-dialed leg gets a **fresh session** (a half-navigated FSM and a stale buffer
aren't worth carrying across a reconnect); the goal and context come back from
the retained per-group state.

---

## 3. Fallback notification

`resilience/fallback.py` `fallback_message(reason)` maps a failure reason to a
user-facing SMS line (with a sane default). Wired two ways:

- `AgentSession.on_fail(reason)` — called from `_fail`. The bridge routes it to
  `on_teardown(reason)`, which media.py wires to `_teardown_group(reason=…,
  hang_up=True)`: text the user, hang up both legs, drop the bridge.
- media.py's own drop / reap paths call `_teardown_group(reason="dropped", …)`
  directly (no session failure happened there).

`AgentSession.abort(reason)` is the external kill switch — the transport layer
calls it when something outside the label stream ends the call; it routes through
the same `_fail` path.

Reasons → messages: `tier2_unavailable`, `non_connect`, `dropped`,
`redial_failed`, `user_left`, `handoff_error`.

---

## 4. Bridge-leak reaper (M5 carry-over)

A DTMF keypress redirects the agent leg and expects the stream to reconnect. If
it never does, the bridge used to sit in `app.state.bridges` forever.

`CallBridge.reconnect_since` is stamped when the redirect fires.
`media.reap_stale_bridges(app)` clears any bridge still `reconnecting` past
`_RECONNECT_GRACE_S` (20 s) — fallback SMS, hang up both legs, drop it. Run every
`bridge_reap_interval_s` (10 s) by a task started in the app lifespan.

---

## 5. Leg-B-drop handling (M5 carry-over)

- **Before the handoff:** `_serve_user_leg`'s `finally` calls
  `bridge.session.abort("user_left")` — no point navigating a menu for someone
  who hung up. Fallback SMS ("call back when you're ready") follows.
- **At the handoff:** `begin_handoff` raises `HandoffUnavailable("user_left")` if
  the user leg is already gone, so `_bridge` fails cleanly instead of reporting a
  bridge that didn't happen.

---

## Validation

`tests/test_resilience.py` (new), plus additions to `test_media.py`,
`test_bridge.py`, `test_calls.py`:

- `fallback_message` mapping + default.
- breaker is thread-safe under 4 threads × 50 concurrent failures.
- a probe infra failure fails the call with `tier2_unavailable` + `on_fail` fires;
  a menu infra failure is survivable but still counts toward the breaker.
- an open breaker fails a new call fast **without touching Tier 2**.
- `abort()` routes through `_fail` exactly once (idempotent).
- an agent-leg drop re-dials and keeps the call context; a clean `stop` doesn't;
  the drop gives up after `_MAX_REDIALS` with a fallback SMS + hang-ups.
- `reap_stale_bridges` clears a stuck reconnect and leaves a fresh one alone.

Not exercised in `pytest`: the breaker against the real Realtime API, and re-dial
against real Twilio (both need live credentials / a running call).

---

## Files

```
src/dialpass/resilience/circuit_breaker.py  + RLock (now process-shared)
src/dialpass/resilience/fallback.py          fallback_message (new)
src/dialpass/realtime/protocol.py            ProbeOutcome.ok, Tier2Unavailable
src/dialpass/realtime/stream.py              probe_exchange ok=False; StreamingProbe raises
src/dialpass/realtime/client.py              choose_menu_digit raises on infra failure
src/dialpass/agent/session.py                breaker wiring, on_fail hook, abort(), HandoffUnavailable
src/dialpass/agent/bridge.py                 on_teardown hook, on_session_fail, reconnect_since
src/dialpass/api/media.py                    _teardown_group, reap_stale_bridges, re-dial, leg-B abort
src/dialpass/api/calls.py                    call_meta (business number + redial count)
src/dialpass/api/health.py                   breaker state + active_calls
src/dialpass/main.py                          shared breaker, bridge-reaper lifespan task
src/dialpass/config.py                        tier2_failure_threshold / reset / reap interval
tests/test_resilience.py (new), test_media.py, test_bridge.py, test_calls.py
```
