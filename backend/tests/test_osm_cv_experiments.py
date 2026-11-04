import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "simulation" / "tools"))
sys.path.insert(0, str(Path(__file__).parent))

from osm_fixture import write_fixture  # noqa: E402

from app.analytics.experiment import run_experiment  # noqa: E402
from app.core.network import RoadNetwork  # noqa: E402


def test_osm_import(tmp_path):
    import osm_to_network

    osm = write_fixture(tmp_path / "t.osm")
    data = osm_to_network.convert(osm, guess_signals=0)
    net = RoadNetwork(data)
    # the disconnected stub is removed, oneway road has a single direction
    assert len(net.nodes) == 9
    oneway = [e for e in net.edges.values() if e.name == "Cross 2"]
    assert oneway and all(net.nodes[e.to_node].lat > net.nodes[e.from_node].lat for e in oneway)
    # the signal tagged 15 m before the centre junction is mapped onto the junction
    assert [n.lat for n in net.signal_nodes] == [pytest.approx(19.034)]
    # curved roads keep their shape points
    assert max(len(e.geometry) for e in net.edges.values()) >= 3
    primary = [e for e in net.edges.values() if e.road_class == "primary"]
    assert all(e.lanes == 2 and abs(e.speed - 13.89) < 0.01 for e in primary)
    assert data["hospitals"][0]["name"] == "Test Hospital"
    # every node is reachable from every other node
    ids = list(net.nodes)
    assert all(net.shortest_path(ids[0], n) is not None for n in ids[1:])


def test_cv_detects_and_flags_stopped_vehicles():
    from computer_vision.detection.opencv_detector import OpenCVDetector
    from computer_vision.estimation.queue import QueueEstimator
    from computer_vision.rendering.camera import empty_road, render_frame

    V = lambda i, pos, speed, lane=0: SimpleNamespace(id=f"v{i}", pos=pos, speed=speed, lane=lane, vtype="car")  # noqa
    L = 400.0
    queue = [V(i, L - 1 - i * 7.0, 0.0) for i in range(4)]
    mover = V(9, 300.0, 10.0, lane=1)
    det, est = OpenCVDetector(), QueueEstimator()
    plate = empty_road("e", 2)
    f1, gt1 = render_frame("e", 2, L, queue + [mover], 100.0)
    d1 = det.detect(f1, plate, 2)
    mover2 = V(9, 310.0, 10.0, lane=1)
    f2, gt2 = render_frame("e", 2, L, queue + [mover2], 101.0)
    d2 = det.detect(f2, plate, 2)
    e = est.estimate(d2, d1, None)
    assert len(gt2) == 5
    assert abs(e.vehicles - 5) <= 1
    assert abs(e.stopped - 4) <= 1
    assert e.queue_m > 20


def test_small_experiment_is_matched_and_safe(net, tmp_path):
    res = run_experiment(net, ["medium"], [11], warmup_s=120, recovery_tail_s=60, results_dir=str(tmp_path))
    trials = res["trials"]
    assert len(trials) == 2 and {t["priority"] for t in trials} == {False, True}
    assert all(t["status"] == "COMPLETED" for t in trials)
    assert res["summary"]["safety_violations_total"] == 0
    assert Path(res["files"][0]).exists() and Path(res["files"][1]).exists()
    json.loads(Path(res["files"][0]).read_text())


@pytest.mark.skipif(importlib.util.find_spec("traci") is None, reason="SUMO/TraCI not installed")
def test_sumo_engine_trial(net, tmp_path):
    from build_sumo_net import build

    from app.analytics.experiment import run_trial

    net_file = build(ROOT / "data" / "network.demo.json", tmp_path)
    r = run_trial(net, priority=True, demand="low", seed=1, engine="sumo", warmup_s=60, recovery_tail_s=30,
                  sumo_options={"net_file": str(net_file)})
    assert r["status"] == "COMPLETED" and r["safety_violations"] == 0 and r["priority_requests"] >= 1
