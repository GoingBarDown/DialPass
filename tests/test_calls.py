"""POST /calls — M5 phase 1: dial the business (Leg A) AND the user (Leg B)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from tests.fakes import FakeTwilioClient

from dialpass.main import create_app

BODY = {
    "business_number": "+18005550100",
    "user_number": "+15145550123",
    "goal": "check my refund status",
}


def _app_with_twilio(**fake_kwargs):
    app = create_app()
    app.state.twilio_client = FakeTwilioClient(**fake_kwargs)
    app.state.settings.public_base_url = "https://dialpass.test"
    return app


def test_calls_dials_both_legs_into_the_same_conference():
    app = _app_with_twilio()
    fake = app.state.twilio_client
    r = TestClient(app).post("/calls", json=BODY)

    assert r.status_code == 200
    assert r.json()["state"] == "DIALING"

    assert len(fake.outbound) == 1
    assert len(fake.user_rings) == 1
    leg_a, leg_b = fake.outbound[0], fake.user_rings[0]

    conf = leg_a["conference"]
    assert leg_b["conference"] == conf  # same room
    assert leg_a["to"] == BODY["business_number"]
    assert leg_b["to"] == BODY["user_number"]
    assert leg_a["url"] == f"https://dialpass.test/twiml/voice?conference={conf}"
    assert leg_b["url"] == f"https://dialpass.test/twiml/join?conference={conf}"


def test_calls_records_the_user_leg_for_the_handoff():
    app = _app_with_twilio()
    TestClient(app).post("/calls", json=BODY)
    # keyed by the agent (Leg A) SID, so media.py can find it on stream start
    assert app.state.user_legs == {"CAagent0001": "CAuser0001"}


def test_calls_survives_a_failed_user_ring():
    app = _app_with_twilio(ring_user_raises=RuntimeError("twilio 500"))
    r = TestClient(app).post("/calls", json=BODY)
    # Leg A still placed, call still accepted; no user leg recorded
    assert r.status_code == 200
    assert app.state.twilio_client.outbound
    assert app.state.user_legs == {}


@pytest.mark.parametrize("path", ["/twiml/join?conference=room-xyz"])
def test_join_twiml_route_returns_a_muted_conference(path):
    r = TestClient(create_app()).get(path)
    assert r.status_code == 200
    assert 'muted="true"' in r.text
    assert "room-xyz" in r.text
    assert 'startConferenceOnEnter="false"' in r.text
