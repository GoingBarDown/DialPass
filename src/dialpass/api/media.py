"""WebSocket endpoint for Twilio Media Streams — the call's audio transport.

Since M5 both legs connect over a bidirectional `<Connect><Stream>` (not the
one-way `<Start><Stream>` + Conference of M2-M4). Twilio sends JSON text frames:
`connected`, `start`, `media` (base64 G.711, 8 kHz, 20 ms), `mark`, `stop`; we
send `media` back the same way. A `CallBridge` (see agent/bridge.py) is the hub —
this module is the sockets' read and write loops.

`start.customParameters` carries `group` (correlates a call's legs on our side)
and `role`: `agent` = the business call (drives Tier 1 + the FSM); `user` = Leg B,
the user's own phone, which just attaches to the same bridge and waits for the
handoff to open the relay.

A keypress redirects the agent leg away and back (see CallBridge.press_dtmf), so a
second `start` for a `group` that already has a bridge is a **reconnect** — reuse
the session, just re-bind the new stream.

M8: an agent leg that drops mid-call (no `stop`, call not finished) is re-dialed
up to `_MAX_REDIALS` times; past that — or on any hard failure — the user gets a
fallback SMS and both legs are hung up. `reap_stale_bridges` (run from the app
lifespan) clears bridges whose DTMF reconnect never arrived.

If `DIALPASS_RECORD_DIR` is set, Leg A's decoded audio is also written to a WAV
for offline classifier tuning (M3 dev aid).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..agent.bridge import CallBridge
from ..realtime.stream import StreamingProbe
from ..resilience.fallback import fallback_message
from ..telephony.audio import ulaw_to_pcm16
from ..telephony.recorder import WavRecorder

router = APIRouter()
log = logging.getLogger("dialpass.media")

# How long a user leg waits for its agent leg's bridge to exist before giving up.
# Leg A is an already-answered outbound call; Leg B is a human answering a cell,
# so Leg A's bridge is almost always up first. 100 * 50 ms = 5 s of headroom.
_USER_ATTACH_TRIES = 100
_USER_ATTACH_INTERVAL = 0.05

_MAX_REDIALS = 2  # per call, on an unexpected agent-leg drop
_RECONNECT_GRACE_S = 20.0  # a DTMF reconnect that hasn't landed by now is dead


# ---- Twilio side effects (all at call end / failure, never mid-audio) ----
def _hang_up(app, call_sid: str | None) -> None:
    tw = app.state.twilio_client
    if tw is None or not call_sid or call_sid == "unknown":
        return
    try:
        tw.hang_up(call_sid)
    except Exception:
        log.exception("media: hang up %s failed", call_sid)


def _sms(app, group: str, body: str) -> None:
    tw = app.state.twilio_client
    number = app.state.user_numbers.get(group)
    if tw is None or not number:
        return
    try:
        tw.send_sms(number, body)
    except Exception:
        log.exception("media: SMS to %s failed", group)


def _clear_context(app, group: str) -> None:
    for d in (
        app.state.pending_goals,
        app.state.user_legs,
        app.state.user_numbers,
        app.state.call_meta,
    ):
        d.pop(group, None)


def _teardown_group(app, group: str, *, reason: str | None = None, hang_up: bool = False) -> None:
    """End a call for good: drop the bridge, optionally text the user a fallback
    line and hang up both legs, then clear the per-group context. Idempotent."""
    bridge = app.state.bridges.pop(group, None)
    if bridge is not None:
        app.state.sessions.pop(bridge.session.call_id, None)
        if reason is not None:
            _sms(app, group, fallback_message(reason))
        if hang_up:
            _hang_up(app, bridge.session.call_id)
            _hang_up(app, bridge.session.user_leg_sid)
        bridge.close()
    _clear_context(app, group)


def reap_stale_bridges(app, *, now: float | None = None) -> int:
    """Clear any bridge stuck mid-reconnect past the grace window (the DTMF
    redirect fired but the stream never came back). Returns how many it reaped."""
    now = time.monotonic() if now is None else now
    reaped = 0
    for group, bridge in list(app.state.bridges.items()):
        since = bridge.reconnect_since
        if bridge.reconnecting and since is not None and now - since > _RECONNECT_GRACE_S:
            log.warning("media: reaping bridge %s — DTMF reconnect never arrived", group)
            _teardown_group(app, group, reason="dropped", hang_up=True)
            reaped += 1
    return reaped


# ---- socket plumbing ----------------------------------------------------
async def _drain_loop(ws: WebSocket, drain) -> None:
    """Ship whatever the bridge has queued toward this leg. Polls every 20 ms —
    queued audio (AI speech, relayed frames) paces itself frame by frame."""
    try:
        while True:
            for msg in drain():
                await ws.send_text(json.dumps(msg))
            await asyncio.sleep(0.02)
    except (WebSocketDisconnect, RuntimeError):
        pass


def _wire_dtmf(app, bridge: CallBridge, call_id: str, group: str) -> None:
    """Give the bridge a way to press a real telephony DTMF key: redirect the
    leg to `<Play digits>` then back to the stream TwiML."""
    twilio_client = app.state.twilio_client
    base = app.state.settings.public_base_url
    if twilio_client is None or not base:
        return
    reconnect_url = f"{base}/twiml/voice?group={group}"

    def press(digits: str) -> None:
        try:
            twilio_client.press_digits(call_id, digits, reconnect_url)
        except Exception:
            log.exception("media: DTMF redirect failed for %s", group)
            bridge.reconnecting = False

    bridge.dtmf_redirect = press


def _wire_bridge_hooks(app, bridge: CallBridge, group: str) -> None:
    """Text the user (handoff line or fallback) and tear the call down when the
    session gives up — both without blocking the media loop."""
    bridge.notify_user = lambda body: _sms(app, group, body)
    bridge.on_teardown = lambda reason: _teardown_group(app, group, reason=reason, hang_up=True)


async def _serve_user_leg(ws: WebSocket, app, group: str, stream_sid: str) -> None:
    """Attach a user leg to its group's bridge and pump its audio until it drops.

    If the user hangs up before the handoff, tell the session to give up — no
    point navigating a menu for someone who's gone (the fallback SMS follows)."""
    bridge = None
    for _ in range(_USER_ATTACH_TRIES):
        bridge = app.state.bridges.get(group)
        if bridge is not None:
            break
        await asyncio.sleep(_USER_ATTACH_INTERVAL)
    if bridge is None:
        log.warning("media: user leg for group %s but no agent bridge — closing", group)
        return

    bridge.bind_user(stream_sid)
    writer = asyncio.create_task(_drain_loop(ws, bridge.drain_user))
    log.info("media: user leg connected, group %s", group)
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            event = msg.get("event")
            if event == "media":
                bridge.on_user_audio(base64.b64decode(msg["media"]["payload"]))
            elif event == "stop":
                break
    except WebSocketDisconnect:
        log.info("media: user leg disconnected for group %s", group)
    finally:
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer
        bridge.user_stream_sid = None
        if not bridge.relay_open and not bridge.session.finished:
            log.warning("media: user hung up before the handoff — abandoning %s", group)
            bridge.session.abort("user_left")


def _redial_agent_leg(app, group: str) -> bool:
    """Re-place the business call for `group`. Returns False if we can't (no
    Twilio, no base URL, no stored number, or attempts exhausted)."""
    meta = app.state.call_meta.get(group)
    tw = app.state.twilio_client
    base = app.state.settings.public_base_url
    if not meta or tw is None or not base or meta.get("redials", 0) >= _MAX_REDIALS:
        return False
    meta["redials"] = meta.get("redials", 0) + 1
    log.warning(
        "media: agent leg %s dropped mid-call — redial %d/%d", group, meta["redials"], _MAX_REDIALS
    )
    try:
        tw.place_outbound_call(meta["business_number"], f"{base}/twiml/voice?group={group}", group)
    except Exception:
        log.exception("media: redial failed for %s", group)
        return False
    return True


@router.websocket("/media")
async def media_stream(ws: WebSocket) -> None:
    await ws.accept()
    app = ws.app
    settings = app.state.settings
    bridge: CallBridge | None = None
    writer: asyncio.Task | None = None
    recorder: WavRecorder | None = None
    group = "unknown"
    saw_stop = False
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            event = msg.get("event")

            if event == "start":
                start = msg.get("start", {})
                params = start.get("customParameters") or {}
                group = params.get("group") or start.get("streamSid") or "unknown"
                role = params.get("role", "agent")
                call_id = start.get("callSid") or "unknown"
                stream_sid = start.get("streamSid") or ""

                if role == "user":
                    await _serve_user_leg(ws, app, group, stream_sid)
                    break  # bridge stays None here — user leg never tears down the call

                existing = app.state.bridges.get(group)
                if existing is not None:
                    # Reconnect after a DTMF redirect — keep the session & FSM.
                    bridge = existing
                    recorder = bridge.recorder
                    bridge.bind_agent(stream_sid, asyncio.get_running_loop())
                    bridge.reconnecting = False
                    bridge.reconnect_since = None
                    writer = asyncio.create_task(_drain_loop(ws, bridge.drain_agent))
                    log.info("media: agent leg reconnected, group %s", group)
                    continue

                goal = app.state.pending_goals.get(group)
                user_leg_sid = app.state.user_legs.get(group)
                session = app.state.make_session(
                    call_id, goal=goal, conference=group, user_leg_sid=user_leg_sid
                )
                bridge = CallBridge(group, session)  # wires session hooks
                bridge.bind_agent(stream_sid, asyncio.get_running_loop())
                _wire_dtmf(app, bridge, call_id, group)
                _wire_bridge_hooks(app, bridge, group)
                if settings.openai_api_key:
                    # Swap the turn-based probe for the streaming one — it greets
                    # the line and listens over a live Realtime socket.
                    session.tier2 = StreamingProbe(session.tier2, bridge.probe_from_thread)
                app.state.bridges[group] = bridge
                app.state.sessions[call_id] = session
                writer = asyncio.create_task(_drain_loop(ws, bridge.drain_agent))

                if settings.record_dir:
                    recorder = WavRecorder(
                        f"{settings.record_dir}/{call_id}.wav", settings.sample_rate
                    )
                    bridge.recorder = recorder  # survives DTMF reconnects
                    log.info("recording call %s to %s", call_id, recorder.path)
                log.info("media: agent leg connected, group %s call %s", group, call_id)

            elif event == "media" and bridge is not None:
                payload = base64.b64decode(msg["media"]["payload"])
                if recorder is not None:
                    recorder.write(ulaw_to_pcm16(payload))
                bridge.on_agent_audio(payload)

            elif event == "stop":
                saw_stop = True
                break
    except WebSocketDisconnect:
        log.info("media: stream disconnected for group %s", group)
    finally:
        if writer is not None:
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer
        _finish_agent_leg(app, bridge, group, saw_stop=saw_stop, recorder=recorder)
        with contextlib.suppress(RuntimeError):
            await ws.close()


def _finish_agent_leg(app, bridge, group, *, saw_stop: bool, recorder) -> None:
    if bridge is None:
        if recorder is not None:
            recorder.close()
        return
    if bridge.reconnecting:
        # DTMF redirect in flight — leave the bridge for the reconnecting socket.
        # reap_stale_bridges cleans it up if that reconnect never comes.
        return

    dropped_mid_call = not saw_stop and not bridge.session.finished
    if dropped_mid_call and _redial_agent_leg(app, group):
        # Re-dial placed. Drop this bridge but keep the per-group context — the
        # fresh agent leg builds a new session for the same group.
        if app.state.bridges.get(group) is bridge:
            app.state.bridges.pop(group, None)
        app.state.sessions.pop(bridge.session.call_id, None)
        bridge.close()
        return

    _teardown_group(
        app, group, reason="dropped" if dropped_mid_call else None, hang_up=dropped_mid_call
    )
