"""FastAPI application factory + the live-call session registry."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from .agent.classifier import HeuristicClassifier
from .agent.session import AgentSession
from .api import calls, health, media, voice
from .config import get_settings
from .realtime.client import RealtimeClient
from .realtime.fake import FakeTier2
from .telemetry.publisher import LogSink
from .telephony.twilio_client import TwilioClient


def _build_tier2(settings):
    if settings.openai_api_key:
        return RealtimeClient(settings.openai_api_key, settings.realtime_model)
    return FakeTier2()  # no key -> offline Tier 2, so the server still runs


def _build_twilio_client(settings) -> TwilioClient | None:
    if settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_from_number:
        return TwilioClient(
            settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_from_number
        )
    return None  # not configured -> /calls stays a 501 stub


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(title="DialPass", version="0.1.0")
    app.state.settings = settings
    app.state.sessions = {}
    app.state.pending_goals = {}  # call_sid -> goal, set by /calls, consumed by /media
    app.state.twilio_client = _build_twilio_client(settings)

    def make_session(call_id: str, goal: str | None = None) -> AgentSession:
        return AgentSession(
            call_id,
            HeuristicClassifier(),
            _build_tier2(settings),
            telemetry=LogSink(),
            settings=settings,
            goal=goal,
        )

    app.state.make_session = make_session

    app.include_router(health.router)
    app.include_router(calls.router)
    app.include_router(media.router)
    app.include_router(voice.router)
    return app


app = create_app()
