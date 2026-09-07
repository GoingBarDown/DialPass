from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from .. import __version__
from ..resilience.circuit_breaker import BreakerState
from ..telemetry.sqs_sink import SqsSink

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    body: dict[str, Any] = {"status": "ok", "service": "dialpass", "version": __version__}
    sink = getattr(request.app.state, "telemetry_sink", None)
    if isinstance(sink, SqsSink):
        body["telemetry"] = {
            "sent": sink.sent,
            "dropped_overflow": sink.dropped_overflow,
            "dropped_send": sink.dropped_send,
        }
    breaker = getattr(request.app.state, "tier2_breaker", None)
    if breaker is not None:
        body["tier2_breaker"] = breaker.state.value
        if breaker.state != BreakerState.CLOSED:
            body["status"] = "degraded"
    body["active_calls"] = len(getattr(request.app.state, "bridges", {}))
    return body
