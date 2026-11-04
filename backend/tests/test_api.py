"""End-to-end API tests (the live loop is not auto-started; tests advance time explicitly)."""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def live(client):
    return client.app.state.live


def advance(client, seconds):
    live(client).coord.run_for(seconds)


def book(client, **kw):
    body = {"patient_name": "Asha", "phone": "+91 98765 43210", "pickup_lat": 19.0300, "pickup_lon": 73.0310,
            "hospital_id": "H2"}
    body.update(kw)
    return client.post("/api/bookings", json=body)


def test_health_and_network(client):
    assert client.get("/api/health").json()["ok"]
    net = client.get("/api/network").json()
    assert net["signals"] == 9 and len(net["hospitals"]) == 2
    assert len(client.get("/api/intersections").json()) == 9


def test_booking_lifecycle(client):
    r = book(client)
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["status"] == "EN_ROUTE_TO_PICKUP" and b["ambulance_id"]
    route = client.get(f"/api/bookings/{b['id']}/route").json()
    assert route["legs"] and route["legs"][0]["geometry"]["type"] == "LineString"
    for _ in range(40):
        advance(client, 30)
        if client.get(f"/api/bookings/{b['id']}").json()["status"] == "COMPLETED":
            break
    final = client.get(f"/api/bookings/{b['id']}").json()
    assert final["status"] == "COMPLETED"
    events = client.get(f"/api/bookings/{b['id']}/events").json()
    types = [e["type"] for e in events]
    for t in ("BOOKING_CREATED", "AMBULANCE_ASSIGNED", "ARRIVED_AT_PICKUP", "ARRIVED_AT_HOSPITAL"):
        assert t in types
    a = client.get(f"/api/analytics/{b['id']}").json()
    assert a["metrics"]["status"] == "COMPLETED" and a["metrics"]["total_time_s"] > 0


def test_validation_errors(client):
    assert book(client, hospital_id="NOPE").status_code == 422
    assert book(client, pickup_lat=18.5, pickup_lon=72.5).status_code == 422
    assert book(client, phone="abc").status_code == 422
    assert client.get("/api/bookings/99999").status_code == 404


def test_no_ambulance_then_cancel(client):
    ids = [book(client).json()["id"] for _ in range(4)]
    statuses = [client.get(f"/api/bookings/{i}").json()["status"] for i in ids]
    assert "WAITING_FOR_AMBULANCE" in statuses
    for i in ids:
        r = client.post(f"/api/bookings/{i}/cancel")
        assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    advance(client, 600)
    ambs = client.get("/api/ambulances").json()
    assert all(a["status"] in ("AVAILABLE", "RETURNING") for a in ambs)


def test_simulation_control_and_websocket(client):
    r = client.post("/api/simulation/control", json={"action": "reset", "demand": "low", "priority_enabled": False})
    assert r.status_code == 200 and r.json()["params"]["demand"] == "low" and r.json()["params"]["priority"] is False
    r = client.post("/api/simulation/control", json={"priority_enabled": True, "queue_source": "cv"})
    assert r.json()["params"]["priority"] is True and r.json()["params"]["queue_source"] == "cv"
    advance(client, 30)
    assert client.post("/api/simulation/control", json={"demand": "extreme"}).status_code == 422
    b = book(client).json()
    with client.websocket_connect(f"/ws/bookings/{b['id']}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "booking" and msg["booking"]["id"] == f"B{b['id']}"
    client.post("/api/simulation/control", json={"queue_source": "sim", "action": "pause"})


def test_cv_endpoints(client):
    advance(client, 120)
    est = client.get("/api/cv/n22/estimate").json()
    assert len(est["approaches"]) == 4
    r = client.get("/api/cv/n22/frame.png", params={"approach": "n21_n22"})
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert client.get("/api/cv/n22/frame.png", params={"approach": "n00_n01"}).status_code == 404


def test_intersection_status(client):
    s = client.get("/api/intersections/n22/status").json()
    assert s["stage"] in ("GREEN", "YELLOW", "ALL_RED") and "queue_history" in s
    assert client.get("/api/intersections/n00/status").status_code == 404
