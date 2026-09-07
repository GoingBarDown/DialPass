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

    group = leg_a["conference"]  # the fake stores the group arg under this key
    assert leg_b["conference"] == group  # both legs share the group id
    assert group.startswith("dialpass-")
    assert leg_a["to"] == BODY["business_number"]
    assert leg_b["to"] == BODY["user_number"]
    assert leg_a["url"] == f"https://dialpass.test/twiml/voice?group={group}"
    assert leg_b["url"] == f"https://dialpass.test/twiml/join?group={group}"


def test_calls_records_the_user_leg_for_the_handoff():
    app = _app_with_twilio()
    TestClient(app).post("/calls", json=BODY)
    # keyed by the group id, so either leg's media stream can find it on connect
    assert list(app.state.user_legs.values()) == ["CAuser0001"]
    (group,) = app.state.user_legs
    assert group.startswith("dialpass-")
    # the user's number is stashed for the handoff SMS
    assert app.state.user_numbers[group] == BODY["user_number"]
    # and the business number, so a dropped agent leg can be re-dialed (M8)
    assert app.state.call_meta[group] == {
        "business_number": BODY["business_number"],
        "redials": 0,
    }


def test_calls_survives_a_failed_user_ring():
    app = _app_with_twilio(ring_user_raises=RuntimeError("twilio 500"))
    r = TestClient(app).post("/calls", json=BODY)
    # Leg A still placed, call still accepted; no user leg recorded
    assert r.status_code == 200
    assert app.state.twilio_client.outbound
    assert app.state.user_legs == {}


@pytest.mark.parametrize("path", ["/twiml/join?group=dialpass-xyz"])
def test_join_twiml_route_connects_the_user_leg_to_the_media_stream(path):
    app = create_app()
    app.state.settings.public_base_url = "https://dialpass.test"
    r = TestClient(app).get(path)
    assert r.status_code == 200
    assert "<Connect><Stream" in r.text.replace("\n", "")
    assert "wss://dialpass.test/media" in r.text
    assert 'value="dialpass-xyz"' in r.text and 'value="user"' in r.text
    assert "<Conference" not in r.text
    assert "Stay on the line" in r.text  # the intro is spoken before the stream
