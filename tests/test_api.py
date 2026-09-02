from fastapi.testclient import TestClient

from dialpass.main import create_app

client = TestClient(create_app())


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_calls_validates_body():
    r = client.post("/calls", json={"business_number": "x"})
    assert r.status_code == 422  # missing user_number


def test_calls_not_implemented_until_m2():
    r = client.post(
        "/calls", json={"business_number": "+18005550100", "user_number": "+15145550123"}
    )
    assert r.status_code == 501
