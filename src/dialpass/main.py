"""FastAPI application factory + the live-call session registry."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from .agent.classifier import HeuristicClassifier
from .agent.session import AgentSession
from .api import calls, health, media
from .config import get_settings
from .realtime.client import RealtimeClient
from .realtime.fake import FakeTier2
from .telemetry.publisher import LogSink


def _build_tier2(settings):
    if settings.openai_api_key:
        return RealtimeClient(settings.openai_api_key, settings.realtime_model)
    return FakeTier2()  # no key -> offline Tier 2, so the server still runs


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(title="DialPass", version="0.1.0")
    app.state.settings = settings
    app.state.sessions = {}

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
    return app


app = create_app()
