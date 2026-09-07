"""FastAPI application factory + the live-call session registry."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI

from .agent.classifier import HeuristicClassifier
from .agent.executor import ThreadedExecutor
from .agent.session import AgentSession
from .api import calls, health, media, spike, voice
from .config import get_settings
from .realtime.client import RealtimeClient
from .realtime.fake import FakeTier2
from .telemetry.publisher import LogSink
from .telemetry.sqs_sink import SqsSink, build_sqs_client
from .telephony.twilio_client import TwilioClient

log = logging.getLogger("dialpass.main")


def _build_telemetry(settings):
    """SQS producer if a queue is configured, else the log sink. The sink is
    shared across every call's `AgentSession` — its `emit` is non-blocking."""
    if settings.sqs_queue_url:
        log.info("telemetry -> SQS %s", settings.sqs_queue_url)
        return SqsSink(
            settings.sqs_queue_url,
            client=build_sqs_client(settings),
            max_queue=settings.telemetry_queue_maxsize,
        )
    return LogSink()


def _build_tier2(settings):
    if settings.openai_api_key:
        return RealtimeClient(
            settings.openai_api_key,
            menu_model=settings.realtime_menu_model or settings.realtime_model,
        )
    return FakeTier2()  # no key -> offline Tier 2, so the server still runs


def _build_twilio_client(settings) -> TwilioClient | None:
    if settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_from_number:
        return TwilioClient(
            settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_from_number
        )
    return None  # not configured -> /calls stays a 501 stub


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    sink = getattr(app.state, "telemetry_sink", None)
    if isinstance(sink, SqsSink):
        sink.close()  # flush queued events before the process exits


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(title="DialPass", version="0.1.0", lifespan=_lifespan)
    app.state.settings = settings
    app.state.sessions = {}
    app.state.bridges = {}  # group_id -> CallBridge, for the lifetime of each call
    app.state.pending_goals = {}  # group_id -> goal, set by /calls, consumed by /media
    app.state.user_legs = {}  # group_id -> user (Leg B) call_sid, for the handoff
    app.state.user_numbers = {}  # group_id -> user phone, for the handoff SMS
    app.state.twilio_client = _build_twilio_client(settings)
    # One telemetry sink for the whole process, shared by every call.
    app.state.telemetry_sink = _build_telemetry(settings)

    def make_session(
        call_id: str,
        goal: str | None = None,
        dtmf_sender=None,
        conference: str | None = None,
        user_leg_sid: str | None = None,
    ) -> AgentSession:
        return AgentSession(
            call_id,
            HeuristicClassifier(),
            _build_tier2(settings),
            telemetry=app.state.telemetry_sink,
            settings=settings,
            goal=goal,
            dtmf_sender=dtmf_sender,
            tier2_executor=ThreadedExecutor(),
            conference=conference,
            user_leg_sid=user_leg_sid,
        )

    app.state.make_session = make_session

    app.include_router(health.router)
    app.include_router(calls.router)
    app.include_router(media.router)
    app.include_router(voice.router)
    app.include_router(spike.router)  # M5 spike — remove with api/spike.py
    return app


app = create_app()
