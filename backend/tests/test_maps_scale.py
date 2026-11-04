"""Real-map import (OpenStreetMap structure), city-scale simulation, simulated signal control from
cameras / operators / test ambulances, and the webcam 'all objects' detection mode."""
import json
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "simulation" / "tools"))
sys.path.insert(0, str(Path(__file__).parent))

from synthetic_city import make_city  # noqa: E402

from app.core.network import RoadNetwork  # noqa: E402
from app.mapdata.osm import BuildOptions, build_network, parse_overpass_json  # noqa: E402
from app.sim.base import DemandConfig  # noqa: E402
from app.sim.coordinator import Coordinator, build_engine  # noqa: E402
from computer_vision.video.detectors import BaseDetector, Det  # noqa: E402
from computer_vision.video.manager import VideoManager  # noqa: E402


@pytest.fixture(scope="module")
def city():
    raw = make_city(6.0, 5.0)
    data = build_network(parse_overpass_json(raw), BuildOptions(name="Synthetic city"))
    return raw, data, RoadNetwork(data)


def test_osm_import_consolidates_junctions_and_maps_signals(city):
    raw, data, net = city
    st = raw["_stats"]
    # dual carriageways (two one-way ways 20 m apart) become single junctions: no tiny connector edges left
    assert min(e.length for e in net.edges.values()) > 25
    # every small roundabout is one junction
    assert sum(1 for n in data["nodes"] if n.get("roundabout")) == st["roundabouts"]
    # signals tagged 20 m before the junction are mapped onto it; untagged main-road crossings are inferred
    assert data["meta"]["osm_signals"] >= 0.6 * st["signals_tagged"] / 2
    assert data["meta"]["inferred_signals"] > 0
    assert all(len(net.signal_groups(s.id)) == 2 for s in net.signal_nodes)
    assert not any(n.signal and n.roundabout for n in net.nodes.values())
    # hospitals mapped as nodes and as building outlines; bases at hospitals / fire stations
    assert len(net.hospitals) == 5 and all(h.node in net.nodes for h in net.hospitals.values())
    assert len(net.bases) >= 3 and any("Fire" in b.name for b in net.bases)
    # junction names come from the crossing roads; curved roads keep their shape points
    assert any(" × " in n.name for n in net.signal_nodes)
    assert max(len(e.geometry) for e in net.edges.values()) > 5
    # strongly connected: every node reachable
    tree = net.shortest_path_tree(next(iter(net.nodes)))
    assert len(tree) == len(net.nodes) - 1


def test_demand_scales_with_the_map_and_runs_city_scale(city, net):
    _, _, big = city
    eng = build_engine(big, "builtin", 0.5, DemandConfig("high", 2700, seed=3))
    assert eng.demand_scale > 4 and eng.internal_share > 0
    assert build_engine(net, "builtin", 0.5, DemandConfig("high", 2700)).demand_scale == 1.0  # demo unchanged
    c = Coordinator(big, eng)
    t0 = time.perf_counter()
    c.run_for(600)
    wall = time.perf_counter() - t0
    assert len(eng.vehicles) > 1000, "city-scale traffic"
    assert wall < 120, f"10 simulated minutes took {wall:.0f} s"
    assert sum(x.violations for x in c.controllers.values()) == 0
    # trips start and end inside the city, not only at its edges
    on_local = sum(1 for v in eng.vehicles.values() if big.edges[v.edge].road_class == "residential")
    assert on_local > 20


def test_external_priority_green_release_and_recovery(net):
    eng = build_engine(net, "builtin", 0.5, DemandConfig("medium", 1800, seed=5))
    c = Coordinator(net, eng)
    c.run_for(200)
    node = next(iter(c.controllers))
    ctrl = c.controllers[node]
    approach = ctrl.groups[1 - ctrl.current][0] if ctrl.stage.value == "GREEN" else ctrl.groups[0][0]
    evs = c.external_priority(node, approach, "cam:test:x", "camera", detail={"camera": "test"})
    assert evs[0]["type"] == "PRIORITY_REQUESTED" and evs[0]["source"] == "camera"
    for _ in range(40):
        c.step()
    assert ctrl.is_green_for(ctrl.group_of[approach]) and c.priority.external["cam:test:x"].status == "GRANTED"
    c.run_for(20)
    assert ctrl.is_green_for(ctrl.group_of[approach]), "green is held until release"
    c.release_external("cam:test:x")
    assert node in c.recovery.active, "recovery starts after the ambulance passed"
    assert any(e["type"] == "RECOVERY_STARTED" for e in c.events)
    # a second request from another owner at a busy junction waits; max hold ends a forgotten request
    c.external_priority(node, approach, "manual:a", "manual", max_hold_s=10)
    evs = c.external_priority(node, approach, "manual:b", "manual")
    assert evs and evs[0]["type"] == "PRIORITY_DEFERRED"
    c.run_for(15)
    assert "manual:a" not in c.priority.external
    assert any(s.status == "TIMEOUT" and s.vehicle_id == "manual:a" for s in c.priority.history)
    assert sum(x.violations for x in c.controllers.values()) == 0


def test_test_ambulance_drives_through_with_priority(city):
    _, _, big = city
    eng = build_engine(big, "builtin", 0.5, DemandConfig("medium", 1800, seed=9))
    c = Coordinator(big, eng)
    c.run_for(120)
    node = sorted(c.controllers)[len(c.controllers) // 2]
    info = c.spawn_test_ambulance(node)
    assert big.edges[info["route"][-1]].from_node != node and info["approach"] in big.nodes[node].incoming
    for _ in range(int(600 / 0.5)):
        c.step()
        if info["id"] not in c.test_ambulances:
            break
    assert info["id"] not in c.test_ambulances, "test ambulance finished its run"
    sess = [s for s in c.priority.history if s.node == node and s.journey_id == info["id"]]
    assert sess and sess[0].status == "RELEASED" and sess[0].granted_at is not None


class _Fake(BaseDetector):
    name = "fake"
    keep = None

    def __init__(self, dets):
        super().__init__(0.1)
        self.dets = dets

    def detect(self, frame):
        return list(self.dets)


def test_all_objects_mode_draws_everything_but_counts_road_users(tmp_path):
    dets = [Det((50, 200, 150, 300), "car", 0.9), Det((300, 100, 360, 300), "person", 0.8),
            Det((500, 250, 540, 300), "cup", 0.6)]
    vm = VideoManager(tmp_path / "o", tmp_path / "u")
    vm._detector_for = lambda cfg: _Fake(dets)
    src = vm.create(kind="push", detector="fake", detect="all", ambulance={"enabled": False},
                    zone={"roi": [[0, 0], [1, 0], [1, 1], [0, 1]]})
    img = np.full((360, 640, 3), 90, np.uint8)
    for k in range(4):
        src.started_at = time.time() - k * 0.2
        out = src.push_frame(img)
    by = {t["cls"]: t for t in out["tracks"]}
    assert set(by) == {"car", "person", "cup"}
    assert by["car"]["road"] and not by["cup"]["road"] and not by["person"]["road"]
    assert out["stats"]["vehicles"] == 1, "traffic numbers use road vehicles only"
    src.cfg.detect = "vehicles"
    for k in range(4, 8):
        src.started_at = time.time() - k * 0.2
        out = src.push_frame(img)
    assert {t["cls"] for t in out["tracks"]} == {"car"}


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_map_upload_switches_network_and_camera_drives_sim_signal(client, tmp_path):
    live = client.app.state.live
    # validation
    assert client.post("/api/map/load", json={"bbox": [18.0, 72.0, 19.5, 73.5]}).status_code == 422  # too large
    assert client.post("/api/map/load", json={"area": "nowhere"}).status_code == 422
    assert any(a["id"] == "nerul-seawoods" for a in client.get("/api/map/areas").json()["areas"])
    # upload an Overpass JSON file (same path as an .osm export) -> background build -> live switch
    f = tmp_path / "city.json"
    f.write_text(json.dumps(make_city(3.0, 2.5, seed=4)))
    v0 = live.network_version
    with open(f, "rb") as fh:
        r = client.post("/api/map/upload", files={"file": ("city.json", fh, "application/json")}, data={"name": "Test city"})
    assert r.status_code == 200, r.text
    for _ in range(100):
        if live.maps.ready or live.maps.status["state"] in ("ready", "error"):
            break
        time.sleep(0.1)
    assert live.maps.status["state"] == "ready", live.maps.status
    live._check_map_ready()
    assert live.network_version == v0 + 1 and live.net.source == "osm" and live.net.name == "Test city"
    net = client.get("/api/network").json()
    assert net["network_version"] == live.network_version and net["signals"] > 5
    assert all(len(next(iter(i["approaches"].values()))) == 2 for i in net["intersections"])
    # a booking on the new map
    h = next(iter(live.net.hospitals.values()))
    n = sorted(live.net.nodes.values(), key=lambda x: x.id)[len(live.net.nodes) // 3]
    r = client.post("/api/bookings", json={"patient_name": "Ravi", "phone": "+919000000001", "pickup_lat": n.lat,
                                           "pickup_lon": n.lon, "hospital_id": h.id})
    assert r.status_code == 201, r.text
    # operator trigger + test ambulance through the API
    node = next(iter(live.coord.controllers))
    app_ = live.net.nodes[node].incoming[0]
    r = client.post(f"/api/intersections/{node}/priority", json={"approach": app_, "action": "request"})
    assert r.status_code == 200 and r.json()["events"][0]["source"] == "manual"
    assert client.post(f"/api/intersections/{node}/priority", json={"approach": app_, "action": "release"}).status_code == 200
    assert client.post(f"/api/intersections/{node}/test-ambulance", json={}).status_code == 200
    # a camera attached to a simulated junction approach: ambulance in zone -> green; passed -> release + recovery
    live.coord.run_for(30)
    node2 = sorted(live.coord.controllers)[-1]
    app2 = live.net.nodes[node2].incoming[0]
    ev = {"type": "AMBULANCE_IN_ZONE", "track": 7, "source": "cam9", "source_name": "Test cam",
          "attach": {"node": node2, "approach": app2}, "reasons": ["beacon"]}
    live._camera_sim_event(ev)
    owner = f"cam:cam9:{app2}"
    assert owner in live.coord.priority.external
    live.coord.run_for(20)
    assert live.coord.controllers[node2].is_green_for(live.coord.controllers[node2].group_of[app2])
    live._camera_sim_event({**ev, "type": "AMBULANCE_PASSED"})
    live._cam_release = {k: (0, s) for k, (_, s) in live._cam_release.items()}
    live._process_camera_releases()
    assert owner not in live.coord.priority.external and node2 in live.coord.recovery.active
    # back to the demo grid for the other tests
    assert client.post("/api/map/load", json={"area": "demo"}).status_code == 200
    assert live.net.source == "demo"


def test_detector_selftest_endpoint(client):
    r = client.get("/api/video/selftest", params={"detector": "motion"}).json()
    assert r["ok"] is False and "motion" in r["error"].lower()
    r = client.get("/api/video/selftest", params={"detector": "opencv-dnn"}).json()
    assert r["ok"] and any(d["cls"] == "bus" for d in r["detections"])


def test_compact_live_snapshot(city):
    _, _, big = city
    c = Coordinator(big, build_engine(big, "builtin", 0.5, DemandConfig("high", 2700, seed=2)))
    c.run_for(120)
    full, small = c.snapshot(), c.snapshot(compact=True)
    q = small["vehicles_q"]
    assert len(q["d"]) == q["n"] * full["vehicle_count"]
    lat = q["o"][0] + q["d"][0] / 1e5
    assert abs(lat - full["vehicles"][0][0]) < 2e-5
    assert set(small["signals"]) == set(full["signals"]) and "groups" not in next(iter(small["signals"].values()))
    assert len(json.dumps(small)) < 0.7 * len(json.dumps(full))
