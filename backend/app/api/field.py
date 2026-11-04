"""Real-time signals ("field junctions"): live open-format inputs, outputs to virtual/physical
signals, ambulance corridor priority from cameras and GPS."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from ..field.formats import build_spat
from ..field.io import make_source
from ..services.live import LiveSimulation
from .deps import get_live, require_admin

router = APIRouter(tags=["field signals"])


class PriorityBody(BaseModel):
    approach: str


class PositionBody(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    speed: Optional[float] = Field(default=None, ge=0, le=80, description="m/s")
    heading: Optional[float] = Field(default=None, ge=0, le=360)


class SimulateBody(BaseModel):
    junctions: list[str] = []
    entry_approach: Optional[str] = None
    path: Optional[list[list[float]]] = None
    speed_kmh: float = Field(default=40, gt=3, le=120)


class FollowBody(BaseModel):
    enabled: bool


def _junction(live: LiveSimulation, jid: str):
    j = live.field.junctions.get(jid)
    if j is None:
        raise HTTPException(404, "junction not found")
    return j


@router.get("/api/field")
async def field_snapshot(live: LiveSimulation = Depends(get_live)):
    snap = live.field.snapshot()
    snap["follow_sim"] = live.field_follow_sim
    return snap


@router.post("/api/field/junctions", status_code=201, dependencies=[Depends(require_admin)])
async def upsert_junction(body: dict, live: LiveSimulation = Depends(get_live)):
    try:
        j = live.field.upsert(body)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:  # e.g. serial port cannot be opened, MQTT library missing
        raise HTTPException(422, f"{type(exc).__name__}: {exc}")
    return j.to_dict()


@router.delete("/api/field/junctions/{jid}", dependencies=[Depends(require_admin)])
async def delete_junction(jid: str, live: LiveSimulation = Depends(get_live)):
    _junction(live, jid)
    live.field.delete(jid)
    return {"deleted": jid}


@router.post("/api/field/junctions/{jid}/priority", dependencies=[Depends(require_admin)])
async def manual_priority(jid: str, body: PriorityBody, live: LiveSimulation = Depends(get_live)):
    """Operator override: give green to one approach (held until released, max hold applies)."""
    j = _junction(live, jid)
    try:
        req = j.request(body.approach, "manual:operator", "manual")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return req.to_dict()


@router.post("/api/field/junctions/{jid}/release", dependencies=[Depends(require_admin)])
async def manual_release(jid: str, live: LiveSimulation = Depends(get_live)):
    j = _junction(live, jid)
    if j.active:
        j.release(j.active.owner, "MANUAL")
    return j.to_dict()


@router.get("/api/field/junctions/{jid}/state")
async def junction_state(jid: str, live: LiveSimulation = Depends(get_live)):
    """Simple open JSON: {"approaches": {"N": "G", …}} – readable by another ResQRoute instance ("json" source)."""
    j = _junction(live, jid)
    return {"id": j.cfg.id, "name": j.cfg.name, "mode": j.mode, "approaches": j.states(), "remaining_s": j.remaining()}


@router.get("/api/field/junctions/{jid}/spat")
async def junction_spat(jid: str, live: LiveSimulation = Depends(get_live)):
    """SAE J2735-style SPaT JSON of this junction (signal groups, eventState, minEndTime)."""
    j = _junction(live, jid)
    return build_spat(j.cfg.id, j.cfg.name, j.states(), j.group_numbers(), j.remaining())


@router.post("/api/field/sources/test", dependencies=[Depends(require_admin)])
def test_source(cfg: dict):
    """Poll a live signal source once and return what it reports (for the setup form)."""
    try:
        src = make_source(cfg)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if src is None:
        return {"states": {}}
    if cfg.get("type") == "mqtt":
        raise HTTPException(422, "MQTT sources are push-based: save the junction and watch its live state")
    try:
        return {"states": src.poll()}
    except Exception as exc:
        raise HTTPException(502, f"{type(exc).__name__}: {exc}")


@router.get("/api/field/serial-ports")
def serial_ports():
    try:
        from serial.tools import list_ports
    except ImportError:
        return {"installed": False, "ports": []}
    return {"installed": True, "ports": [{"device": p.device, "description": p.description} for p in list_ports.comports()]}


@router.post("/api/field/ambulances/{amb_id}/position")
async def ambulance_position(amb_id: str, body: PositionBody, live: LiveSimulation = Depends(get_live)):
    """GPS fix from an ambulance (driver page, phone app or vehicle tracker)."""
    return {"junctions": live.field.gps_update(amb_id, body.lat, body.lon, body.speed, body.heading)}


@router.post("/api/field/ambulances/{amb_id}/simulate", dependencies=[Depends(require_admin)])
async def simulate(amb_id: str, body: SimulateBody, live: LiveSimulation = Depends(get_live)):
    try:
        path = body.path or live.field.corridor_path(body.junctions, body.entry_approach)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if len(path) < 2:
        raise HTTPException(422, "path needs at least two points")
    live.field.start_simulation(amb_id, path, body.speed_kmh)
    return {"ambulance": amb_id, "path": path}


@router.delete("/api/field/ambulances/{amb_id}/simulate", dependencies=[Depends(require_admin)])
async def stop_simulation(amb_id: str, live: LiveSimulation = Depends(get_live)):
    t = live.field.sim_tasks.pop(amb_id, None)
    if t:
        t.cancel()
    return {"stopped": bool(t)}


@router.post("/api/field/follow-sim", dependencies=[Depends(require_admin)])
async def follow_sim(body: FollowBody, live: LiveSimulation = Depends(get_live)):
    live.field_follow_sim = body.enabled
    return {"follow_sim": live.field_follow_sim}


@router.websocket("/ws/field")
async def ws_field(ws: WebSocket):
    live = ws.app.state.live
    await live.hub.connect("field", ws)
    try:
        snap = live.field.snapshot()
        snap["follow_sim"] = live.field_follow_sim
        await ws.send_json(snap)
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        live.hub.disconnect("field", ws)
