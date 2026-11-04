"""Network, hospitals, ambulances, intersections and simulation control."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import SignalEvent, TrafficSnapshot
from ..schemas import ManualPriority, SimulationControl, TestAmbulance
from ..services.live import LiveSimulation
from .deps import get_live, require_admin

router = APIRouter(prefix="/api", tags=["system"])


def _approach_tail(net, edge_id: str, m: float = 50.0) -> list:
    """The last ~m metres of an approach (for drawing the signal colour on the map)."""
    e = net.edges[edge_id]
    pts = []
    for d in (max(0.0, e.length - m), e.length):
        lat, lon, _ = net.latlon_at(edge_id, d)
        pts.append([round(lat, 6), round(lon, 6)])
    return pts


@router.get("/health")
async def health(live: LiveSimulation = Depends(get_live)):
    return {"ok": True, "running": live.running, "run_id": live.run_id, "error": live.error}


@router.get("/network")
async def network(live: LiveSimulation = Depends(get_live)):
    net = live.net
    return {
        **net.summary(),
        "roads": net.to_geojson(),
        "intersections": [
            {"id": n.id, "name": n.name, "lat": n.lat, "lon": n.lon, "groups": net.signal_groups(n.id),
             "approaches": {a: _approach_tail(net, a) for a in n.incoming}, "signal_source": n.signal_source,
             "approach_names": {a: net.edges[a].name for a in n.incoming}}
            for n in net.signal_nodes
        ],
        "network_version": live.network_version,
        "hospitals": [vars(h) for h in net.hospitals.values()],
        "bases": [{"id": b.id, "name": b.name, "node": b.node, "lat": net.nodes[b.node].lat,
                   "lon": net.nodes[b.node].lon} for b in net.bases],
    }


@router.get("/hospitals")
async def hospitals(live: LiveSimulation = Depends(get_live)):
    return [vars(h) for h in live.net.hospitals.values()]


@router.get("/ambulances")
async def ambulances(live: LiveSimulation = Depends(get_live)):
    return [a.to_dict() for a in live.coord.ambulances.values()]


@router.get("/intersections")
async def intersections(live: LiveSimulation = Depends(get_live)):
    snap = live.coord.signal_snapshot()
    return [{"id": n.id, "name": n.name, "lat": n.lat, "lon": n.lon, **snap.get(n.id, {})} for n in live.net.signal_nodes]


@router.get("/intersections/{node_id}/status")
async def intersection_status(node_id: str, since_s: float = 300, live: LiveSimulation = Depends(get_live)):
    coord = live.coord
    if node_id not in coord.controllers:
        raise HTTPException(404, "not a signalised intersection")
    node = live.net.nodes[node_id]
    snap = coord.signal_snapshot()[node_id]
    rec = coord.recovery.active.get(node_id)
    recent = [r.to_dict() for r in coord.recovery.finished if r.node == node_id][-5:]
    return {
        "id": node_id, "name": node.name, "lat": node.lat, "lon": node.lon, **snap,
        "approach_names": {a: live.net.edges[a].name for a in node.incoming},
        "queue_history": coord.history.series(node_id, coord.now - since_s),
        "priority_sessions": [s.to_dict() for s in coord.priority.history if s.node == node_id][-10:],
        "active_recovery": rec.to_dict(include_timeline=True) if rec else None,
        "recent_recoveries": recent,
        "queue_source": coord.queue_source,
        "signal_source": node.signal_source, "roundabout": node.roundabout,
        "external_priority": [x.to_dict() for x in coord.priority.external.values() if x.node == node_id],
    }


@router.post("/intersections/{node_id}/priority", dependencies=[Depends(require_admin)])
async def manual_priority(node_id: str, req: ManualPriority, live: LiveSimulation = Depends(get_live)):
    """Operator trigger on a simulated signal: green for one approach (as if an ambulance were reported there)."""
    coord = live.coord
    owner = f"manual:{node_id}:{req.approach}"
    try:
        if req.action == "request":
            evs = coord.external_priority(node_id, req.approach, owner, "manual", detail={"operator": True},
                                          max_hold_s=req.hold_s or 45.0)
        elif req.action == "release":
            evs = coord.release_external(owner)
        else:
            raise ValueError("action must be request or release")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"events": evs, "signal": coord.signal_snapshot().get(node_id)}


@router.post("/intersections/{node_id}/test-ambulance", dependencies=[Depends(require_admin)])
async def test_ambulance(node_id: str, req: TestAmbulance, live: LiveSimulation = Depends(get_live)):
    """Send a simulated ambulance through this junction (full route-aware priority + recovery cycle)."""
    try:
        info = live.coord.spawn_test_ambulance(node_id, req.approach, req.upstream_m)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {k: v for k, v in info.items() if k != "route"}


@router.get("/intersections/{node_id}/events")
def intersection_events(node_id: str, limit: int = 100, db: Session = Depends(get_db),
                        live: LiveSimulation = Depends(get_live)):
    rows = db.scalars(select(SignalEvent).where(SignalEvent.intersection_id == node_id, SignalEvent.run_id == live.run_id)
                      .order_by(SignalEvent.id.desc()).limit(limit)).all()
    return [{"type": r.event_type, "sim_time": r.sim_time, "booking_id": r.booking_id, "payload": r.payload} for r in rows]


@router.get("/intersections/{node_id}/snapshots")
def intersection_snapshots(node_id: str, limit: int = 200, db: Session = Depends(get_db),
                           live: LiveSimulation = Depends(get_live)):
    rows = db.scalars(select(TrafficSnapshot).where(TrafficSnapshot.intersection_id == node_id,
                                                    TrafficSnapshot.run_id == live.run_id)
                      .order_by(TrafficSnapshot.id.desc()).limit(limit)).all()
    return [{"sim_time": r.sim_time, "queues": r.queue_lengths, "source": r.source} for r in reversed(rows)]


@router.get("/simulation/status")
async def sim_status(live: LiveSimulation = Depends(get_live)):
    return live.status()


@router.get("/simulation/snapshot")
async def sim_snapshot(vehicles: bool = True, live: LiveSimulation = Depends(get_live)):
    snap = live.coord.snapshot(include_vehicles=vehicles)
    snap.update(running=live.running, speed=live.speed, run_id=live.run_id)
    return snap


@router.get("/events")
async def recent_events(limit: int = 100, live: LiveSimulation = Depends(get_live)):
    return live.coord.events[-limit:][::-1]


@router.post("/simulation/control", dependencies=[Depends(require_admin)])
async def control(req: SimulationControl, live: LiveSimulation = Depends(get_live)):
    try:
        if req.action == "reset" or req.demand or req.seed is not None or req.engine or req.recovery_enabled is not None:
            live.start_run(demand=req.demand, priority=req.priority_enabled, recovery=req.recovery_enabled,
                           queue_source=req.queue_source, seed=req.seed, engine=req.engine)
        else:
            if req.priority_enabled is not None:
                live.set_priority(req.priority_enabled)
            if req.queue_source:
                live.set_queue_source(req.queue_source)
        if req.auto_emergencies_per_hour is not None:
            live.auto_emergencies_per_hour = req.auto_emergencies_per_hour
        if req.action == "pause":
            live.running = False
        elif req.action in ("resume", "reset"):
            live.running = True
            live.error = None
        if req.speed:
            live.speed = req.speed
    except (ValueError, FileNotFoundError, ImportError) as exc:
        raise HTTPException(422, str(exc))
    return live.status()
