"""Real-time field signals: open formats, corridor priority (GPS + camera), drivers and sources."""
import http.server
import json
import threading
import time
from pathlib import Path

import pytest

from app.field.formats import build_spat, normalize_state, parse_sensorthings_observation, parse_spat
from app.field.io import HttpDriver, JsonSource, SensorThingsSource, SerialDriver, serial_line
from app.field.junction import ApproachCfg, FieldJunction, JunctionConfig
from app.field.manager import FieldManager, offset_latlon
from app.traffic_control.signal import Stage

LAT, LON = 19.03, 73.024
APPROACHES = [ApproachCfg("N", "", 0), ApproachCfg("E", "", 90), ApproachCfg("S", "", 180), ApproachCfg("W", "", 270)]


def junction(**kw) -> FieldJunction:
    return FieldJunction(JunctionConfig(id="J1", name="test", lat=LAT, lon=LON, approaches=list(APPROACHES), **kw))


def run(j: FieldJunction, seconds: float, dt: float = 0.25):
    seen = []
    for _ in range(int(seconds / dt)):
        j.tick(dt)
        seen.append(dict(j.states()))
    return seen


# ---------------------------------------------------------------- formats
def test_state_normalisation():
    assert normalize_state("protected-Movement-Allowed") == "G"
    assert normalize_state(6) == "G" and normalize_state(3) == "R" and normalize_state(8) == "Y"
    assert normalize_state("Amber") == "Y" and normalize_state("rot") == "R" and normalize_state("red-amber") == "R"
    assert normalize_state(7, {"7": "red"}) == "R"
    assert normalize_state("weird") == "X"


def test_spat_round_trip():
    doc = build_spat("101", "x", {"N": "G", "E": "R"}, {"N": 2, "E": 4}, {"N": 12, "E": 30})
    parsed = parse_spat(json.loads(json.dumps(doc)), group_map={"2": "N", "4": "E"})
    assert parsed["N"]["state"] == "G" and parsed["E"]["state"] == "R"
    assert 10 <= parsed["N"]["min_end_s"] <= 13
    numeric = {"intersections": [{"id": {"id": 7}, "states": [{"signalGroup": 1, "state-time-speed": [{"eventState": 5}]}]}]}
    assert parse_spat(numeric)["sg1"]["state"] == "G"
    obs = parse_sensorthings_observation({"value": [{"result": "green", "phenomenonTime": "2026-01-01T00:00:00Z"}]})
    assert obs["state"] == "G"


# ---------------------------------------------------------------- junction
def test_priority_is_safe_and_returns_to_normal():
    j = junction()
    run(j, 3)
    group_of_E = j.group_of("E")
    j.request("E", "test", "manual")
    seen = run(j, 15)
    assert j.active.status == "GRANTED"
    assert j.states()["E"] == "G" and j.states()["W"] == "G" and j.states()["N"] == "R"
    for st in seen:  # never N/S and E/W active together
        ns = any(st[a] in "GY" for a in "NS")
        ew = any(st[a] in "GY" for a in "EW")
        assert not (ns and ew)
    run(j, 10)
    assert j.states()["E"] == "G", "held while the ambulance has not passed"
    j.release("test", "PASSED")
    assert j.compensation, "held approaches are owed extra green"
    seen = run(j, 12)
    assert any(st["N"] == "G" for st in seen), "held approaches are served right after the ambulance"
    run(j, 60)
    assert j.mode == "NORMAL" and j.ctrl.violations == 0
    assert group_of_E == j.group_of("W")


def test_second_request_is_queued():
    j = junction()
    j.request("E", "a", "manual")
    r2 = j.request("N", "b", "manual")
    assert r2.status == "QUEUED"
    run(j, 12)
    j.release("a")
    assert j.active.owner == "b"
    run(j, 15)
    assert j.states()["N"] == "G"


def test_mirror_mode_is_advisory(tmp_path):
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"approaches": {"N": "green", "E": "red", "S": "green", "W": "red"}}))
    j = junction(mode="mirror", source={"type": "json", "url": str(f), "poll_s": 0.1})
    for _ in range(30):
        if j.source.latest:
            break
        time.sleep(0.05)
    assert j.states() == {"N": "G", "E": "R", "S": "G", "W": "R"}
    req = j.request("E", "amb", "gps")
    assert req.status == "ADVISORY" and j.events[-1]["type"] == "PRIORITY_ADVISORY"
    j.close()


# ---------------------------------------------------------------- corridor
def test_gps_corridor_green_then_release(tmp_path):
    m = FieldManager(tmp_path / "j.json")
    m.load((LAT, LON))
    j = m.junctions["J1"]
    # drive from 600 m west, through the junction, 300 m east, at 15 m/s
    t_req = t_grant = None
    for k, d in enumerate(range(-600, 320, 15)):
        lat, lon = offset_latlon(LAT, LON, 270 if d < 0 else 90, abs(d))
        m.gps_update("A1", lat, lon, 15.0, 90.0)
        for _ in range(4):
            j.tick(0.25)
        if j.active and t_req is None:
            t_req = d
            assert j.active.approach == "W"
        if j.active and j.active.status == "GRANTED" and t_grant is None:
            t_grant = d
    assert t_req is not None and t_req < -100, "priority requested well before the junction"
    assert t_grant is not None and t_grant < 0, "green before the ambulance arrives"
    assert j.active is None and j.history[-1].status == "RELEASED"
    assert any(e["type"] == "PRIORITY_RELEASED" and e["reason"] == "PASSED" for e in j.events)
    m.shutdown()


def test_camera_triggers(tmp_path):
    m = FieldManager(tmp_path / "j.json")
    m.load((LAT, LON))
    j = m.junctions["J1"]
    att = {"field": "J1", "approach": "S"}
    m.camera_stats(att, 5)
    assert j.camera_queue("S") == 5
    m.camera_event({"type": "AMBULANCE_IN_ZONE", "source": "cam1", "track": 3, "attach": att})
    assert j.active and j.active.approach == "S" and j.active.trigger == "camera"
    m.camera_event({"type": "AMBULANCE_PASSED", "source": "cam1", "track": 3, "attach": att})
    j.corridor.clearance_after_pass_s = 0
    m._lost_timers = {k: (0, r) for k, (_, r) in m._lost_timers.items()}
    m.process_timers()
    assert j.active is None and j.history[-1].status == "RELEASED"
    m.shutdown()


# ---------------------------------------------------------------- drivers / sources
def test_serial_driver_loopback():
    d = SerialDriver({"type": "serial", "port": "loop://"})
    msg = {"id": "J1", "name": "x", "approaches": {"N": "G", "E": "R"}, "mode": "NORMAL", "groups": {}, "remaining": {}}
    d.apply(msg)
    time.sleep(0.3)
    assert d._ser.readline().decode() == serial_line(msg) == "S N=G E=R M=NORMAL\n"
    d.close()


class _Handler(http.server.BaseHTTPRequestHandler):
    posted: list = []

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        _Handler.posted.append(json.loads(self.rfile.read(n)))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):
        body = json.dumps({"value": [{"result": "rot" if "(2)" in self.path else "gruen"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def http_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_http_driver_and_sensorthings_source(http_server):
    d = HttpDriver({"type": "http", "url": http_server + "/state"})
    d.apply({"id": "J1", "name": "x", "approaches": {"N": "G"}, "mode": "PRIORITY", "groups": {"N": 2}, "remaining": {}})
    for _ in range(40):
        if _Handler.posted:
            break
        time.sleep(0.05)
    assert _Handler.posted[-1]["approaches"] == {"N": "G"} and _Handler.posted[-1]["mode"] == "PRIORITY"
    d.close()
    src = SensorThingsSource({"type": "sensorthings", "base_url": http_server + "/v1.1",
                              "datastreams": {"N": 1, "E": 2}, "value_map": {"gruen": "G", "rot": "R"}})
    states = src.poll()
    assert states["N"]["state"] == "G" and states["E"]["state"] == "R"


def test_field_api():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        snap = client.get("/api/field").json()
        assert snap["junctions"]
        jid = snap["junctions"][0]["id"]
        r = client.post(f"/api/field/junctions/{jid}/priority", json={"approach": "E"})
        assert r.status_code == 200 and r.json()["status"] in ("REQUESTED", "GRANTED")
        spat = client.get(f"/api/field/junctions/{jid}/spat").json()
        assert spat["intersections"][0]["states"]
        assert client.post(f"/api/field/junctions/{jid}/release").status_code == 200
        bad = {"name": "x", "lat": 19, "lon": 73, "approaches": [{"id": "N", "bearing": 0}, {"id": "E", "bearing": 90}],
               "groups": [["N"]]}
        assert client.post("/api/field/junctions", json=bad).status_code == 422
        assert client.post("/api/field/junctions", json={**bad, "groups": None, "mode": "mirror"}).status_code == 422
        r = client.post("/api/field/junctions", json={**bad, "groups": None})
        assert r.status_code == 201
        new_id = r.json()["id"]
        r = client.post("/api/field/ambulances/T1/position", json={"lat": 19.03, "lon": 73.02, "speed": 10})
        assert r.status_code == 200 and "junctions" in r.json()
        assert client.delete(f"/api/field/junctions/{new_id}").status_code == 200


def test_mqtt_spat_round_trip():
    amqtt = pytest.importorskip("amqtt.broker")
    pytest.importorskip("paho.mqtt.client")
    import asyncio

    from app.field.io import MqttDriver, MqttSource

    import socket

    with socket.socket() as sk:  # a free port (a fixed one can still be in TIME_WAIT from a previous run)
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    loop = asyncio.new_event_loop()
    cfg = {"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}}, "auth": {"allow-anonymous": True},
           "topic-check": {"enabled": False}}

    def run():
        asyncio.set_event_loop(loop)
        broker = amqtt.Broker(cfg, loop=loop)
        loop.run_until_complete(broker.start())
        loop.run_forever()

    threading.Thread(target=run, daemon=True).start()
    time.sleep(1.5)
    src = MqttSource({"type": "mqtt", "host": "127.0.0.1", "port": port, "topic": "city/signals/J9", "format": "spat",
                      "group_map": {"2": "N", "4": "E"}})
    src.start()
    time.sleep(1.0)
    drv = MqttDriver({"type": "mqtt", "host": "127.0.0.1", "port": port, "topic": "city/signals/J9", "format": "spat"})
    time.sleep(1.0)
    msg = {"id": "J9", "name": "x", "approaches": {"N": "G", "E": "R"}, "mode": "NORMAL",
           "groups": {"N": 2, "E": 4}, "remaining": {"N": 5}}
    for k in range(80):  # republish until the subscriber has it (connection timing varies between machines)
        if k % 10 == 0:
            drv.apply(dict(msg))
        if "N" in (src.latest or {}) and "E" in src.latest:
            break
        time.sleep(0.1)
    src.stop()
    drv.close()
    loop.call_soon_threadsafe(loop.stop)
    assert src.latest["N"]["state"] == "G" and src.latest["E"]["state"] == "R"
