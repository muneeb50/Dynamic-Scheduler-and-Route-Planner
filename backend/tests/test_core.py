"""Routing, signal safety, priority, recovery and journey lifecycle (headless)."""
import itertools

import pytest

from app.recovery.engine import RecoveryConfig
from app.sim.base import DemandConfig
from app.sim.coordinator import Coordinator, build_engine
from app.traffic_control.priority import PriorityConfig
from app.traffic_control.signal import SignalController, SignalPlan, Stage


def make(net, demand="medium", vph=1800, seed=3, priority=True, recovery=True):
    eng = build_engine(net, "builtin", 0.5, DemandConfig(level=demand, vehicles_per_hour=vph, seed=seed))
    return Coordinator(net, eng, PriorityConfig(enabled=priority), RecoveryConfig(enabled=recovery))


def only(coord, amb_id):
    for a in coord.ambulances.values():
        if a.id != amb_id:
            a.status = "RESERVED"


# ----------------------------------------------------------------------- routing
def test_route_follows_road_network(net):
    route = net.shortest_path("n00", "n44")
    assert route, "route must exist"
    for a, b in zip(route, route[1:]):
        assert net.edges[a].to_node == net.edges[b].from_node, "consecutive edges must share a node"
    assert net.edges[route[0]].from_node == "n00" and net.edges[route[-1]].to_node == "n44"
    geom = net.route_geometry(route)
    assert geom[0] == (net.nodes["n00"].lat, net.nodes["n00"].lon)


def test_signal_groups_do_not_mix_crossing_approaches(net):
    groups = net.signal_groups("n22")
    assert len(groups) == 2
    assert sorted(groups[0] + groups[1]) == sorted(net.nodes["n22"].incoming)


# ------------------------------------------------------------------ signal safety
def test_signal_controller_never_runs_conflicting_greens():
    ctrl = SignalController("x", [["a", "b"], ["c", "d"]], SignalPlan())
    owners = itertools.cycle(["p1", None, "p2", None])
    seen = set()
    for i in range(20000):
        if i % 97 == 0:
            o = next(owners)
            if o:
                ctrl.request_priority(i % 2, o)
            elif ctrl.priority_owner:
                ctrl.release_priority(ctrl.priority_owner)
        prev = ctrl.stage
        ctrl.tick(0.5)
        seen.add((prev, ctrl.stage))
        states = ctrl.approach_states()
        green_groups = {0 if a in "ab" else 1 for a, s in states.items() if s in "GY"}
        assert len(green_groups) <= 1
    assert ctrl.violations == 0
    assert seen <= {(Stage.GREEN, Stage.GREEN), (Stage.YELLOW, Stage.YELLOW), (Stage.ALL_RED, Stage.ALL_RED),
                    (Stage.GREEN, Stage.YELLOW), (Stage.YELLOW, Stage.ALL_RED), (Stage.ALL_RED, Stage.GREEN)}


def test_priority_respects_min_green_and_amber():
    plan = SignalPlan()
    ctrl = SignalController("y", [["a"], ["b"]], plan)
    # force a fresh green on group 0
    while not (ctrl.stage == Stage.GREEN and ctrl.current == 0 and ctrl.elapsed < 1):
        ctrl.tick(0.5)
    ctrl.request_priority(1, "amb")
    t, stages = 0.0, []
    while not ctrl.is_green_for(1):
        ctrl.tick(0.5)
        t += 0.5
        stages.append(ctrl.stage)
    assert Stage.YELLOW in stages and Stage.ALL_RED in stages
    assert t >= plan.min_green - 1 + plan.yellow + plan.all_red - 0.5


# -------------------------------------------------------- priority & recovery
def run_journey(coord, pickup="n23", hospital="H2", limit=2400):
    p = coord.net.nodes[pickup]
    j = coord.create_journey(p.lat, p.lon, hospital)
    t0 = coord.now
    while j.status not in ("COMPLETED", "FAILED", "CANCELLED") and coord.now - t0 < limit:
        coord.step()
    return j


def test_priority_only_at_intersections_on_route(net):
    coord = make(net)
    coord.run_for(200)
    only(coord, "A1")
    events = []
    coord.listeners.append(events.append)
    j = run_journey(coord)
    assert j.status == "COMPLETED"
    on_route = {net.edges[e].to_node for leg in j.legs for e in leg.route[:-1]}
    requested = [e for e in events if e["type"] == "PRIORITY_REQUESTED"]
    assert requested, "the ambulance should request priority at least once"
    assert {e["node"] for e in requested} <= on_route
    released = {e["id"] for e in events if e["type"] == "PRIORITY_RELEASED"}
    assert {e["id"] for e in requested} <= released | {e["id"] for e in events if e["type"] == "PRIORITY_TIMEOUT"}
    assert sum(c.violations for c in coord.controllers.values()) == 0


def test_release_happens_after_passing_the_junction(net):
    coord = make(net)
    coord.run_for(200)
    only(coord, "A1")
    checks = []

    def on(ev):
        if ev["type"] == "PRIORITY_RELEASED":
            amb = coord.ambulances["A1"]
            st = coord.engine.vehicle_state(amb.leg.vehicle_id) if amb.leg else None
            if st:
                checks.append(net.edges[st.edge].from_node == ev["node"] or st.route_index > 0)
    coord.listeners.append(on)
    run_journey(coord)
    assert checks and all(checks)


def test_priority_reduces_signal_delay_on_average(net):
    base, pri = [], []
    for seed in (1, 2, 3):
        for enabled, bucket in ((False, base), (True, pri)):
            c = make(net, "high", 2700, seed=seed, priority=enabled, recovery=enabled)
            c.run_for(300)
            only(c, "A1")
            j = run_journey(c)
            assert j.status == "COMPLETED"
            bucket.append(j.metrics["signal_delay_s"])
    assert sum(pri) / 3 < sum(base) / 3


def test_recovery_terminates_and_is_bounded(net):
    coord = make(net, "high", 2700, seed=5, recovery=True)
    coord.recovery.cfg.max_duration_s = 60
    coord.run_for(200)
    only(coord, "A1")
    run_journey(coord)
    coord.run_for(120)
    evs = coord.recovery.all_events()
    assert evs
    for r in evs:
        assert r.status in ("COMPLETED", "TIMEOUT", "SUPERSEDED")
        assert r.duration is not None and r.duration <= 60 + 1
    assert all(c.green_time_provider is None for c in coord.controllers.values())


def test_baseline_has_no_priority_or_recovery(net):
    coord = make(net, priority=False, recovery=False)
    coord.run_for(100)
    only(coord, "A1")
    run_journey(coord)
    assert coord.priority.history == [] and coord.recovery.all_events() == []


# --------------------------------------------------------------------- lifecycle
def test_no_ambulance_available_then_assigned_when_free(net):
    coord = make(net, "low", 600)
    p = net.nodes["n12"]
    js = [coord.create_journey(p.lat, p.lon, "H1") for _ in range(4)]
    assert [j.status for j in js[:3]] == ["EN_ROUTE_TO_PICKUP"] * 3
    assert js[3].status == "WAITING_FOR_AMBULANCE"
    for _ in range(int(3600 / 0.5)):
        coord.step()
        if js[3].status not in ("WAITING_FOR_AMBULANCE",):
            break
    assert js[3].status != "WAITING_FOR_AMBULANCE"


def test_cancel_releases_ambulance(net):
    coord = make(net, "low", 600)
    p = net.nodes["n23"]
    j = coord.create_journey(p.lat, p.lon, "H1")
    coord.run_for(20)
    amb = coord.ambulances[j.ambulance_id]
    coord.cancel(j.id)
    assert j.status == "CANCELLED"
    assert amb.status in ("RETURNING", "AVAILABLE")
    assert not coord.priority.active
    for _ in range(int(1200 / 0.5)):
        coord.step()
        if amb.status == "AVAILABLE":
            break
    assert amb.status == "AVAILABLE"


def test_blocked_ambulance_times_out_instead_of_completing(net):
    coord = make(net, "low", 600, priority=False)
    coord.cfg.no_progress_timeout_s = 60
    # sabotage every signal: permanently red for everyone (simulates a broken junction)
    for ctrl in coord.controllers.values():
        ctrl.approach_states = (lambda c=ctrl: {a: "R" for g in c.groups for a in g})
    only(coord, "A1")
    j = run_journey(coord, limit=900)
    assert j.status == "FAILED"
    assert "progress" in j.failure_reason or "time limit" in j.failure_reason


def test_pickup_outside_service_area_rejected(net):
    coord = make(net, "off", 0)
    with pytest.raises(ValueError):
        coord.create_journey(18.90, 72.80, "H1")
