from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Booking, Hospital, Route, SignalEvent, SystemEvent, User
from ..schemas import AssignRequest, BookingCreate, BookingOut
from ..services.live import LiveSimulation
from .deps import get_live

router = APIRouter(prefix="/api/bookings", tags=["bookings"])


def to_out(b: Booking, db: Session, live: LiveSimulation) -> BookingOut:
    user = db.get(User, b.user_id)
    hosp = db.get(Hospital, b.hospital_id)
    j = live.coord.journeys.get(b.journey_id) if live.coord and b.run_id == live.run_id else None
    return BookingOut(
        id=b.id, status=j.status if j else b.status, patient_name=user.name if user else "",
        pickup_lat=b.pickup_lat, pickup_lon=b.pickup_lon, pickup_node=b.pickup_node, hospital_id=b.hospital_id,
        hospital_name=hosp.name if hosp else None, ambulance_id=j.ambulance_id if j else b.ambulance_id,
        eta_s=(round(j.eta_s, 1) if j and j.eta_s is not None else b.eta_s),
        failure_reason=j.failure_reason if j else b.failure_reason,
        created_at=b.created_at, completed_at=b.completed_at, run_id=b.run_id,
        live=live.coord.journey_dict(j) if j else None,
    )


@router.post("", response_model=BookingOut, status_code=201)
async def create_booking(req: BookingCreate, db: Session = Depends(get_db), live: LiveSimulation = Depends(get_live)):
    if req.hospital_id not in live.net.hospitals:
        raise HTTPException(422, f"unknown hospital '{req.hospital_id}'")
    lat0, lon0, lat1, lon1 = live.net.bbox()
    pad = 0.015
    if not (lat0 - pad <= req.pickup_lat <= lat1 + pad and lon0 - pad <= req.pickup_lon <= lon1 + pad):
        raise HTTPException(422, "pickup location is outside the simulated service area")
    user = db.scalar(select(User).where(User.phone == req.phone))
    if user is None:
        user = User(name=req.patient_name, phone=req.phone)
        db.add(user)
        db.flush()
    else:
        user.name = req.patient_name
    b = Booking(user_id=user.id, run_id=live.run_id, hospital_id=req.hospital_id, pickup_lat=req.pickup_lat,
                pickup_lon=req.pickup_lon, notes=req.notes, status="REQUESTED")
    db.add(b)
    db.commit()
    try:
        j = live.coord.create_journey(req.pickup_lat, req.pickup_lon, req.hospital_id, journey_id=b.journey_id,
                                      auto_assign=req.auto_assign)
    except ValueError as exc:
        b.status = "REJECTED"
        b.failure_reason = str(exc)
        db.commit()
        raise HTTPException(422, str(exc))
    b.pickup_node = j.pickup_node
    b.status = j.status
    b.ambulance_id = j.ambulance_id
    db.commit()
    return to_out(b, db, live)


@router.get("", response_model=list[BookingOut])
async def list_bookings(limit: int = 50, active_only: bool = False, db: Session = Depends(get_db),
                  live: LiveSimulation = Depends(get_live)):
    q = select(Booking).order_by(Booking.id.desc()).limit(min(limit, 500))
    rows = db.scalars(q).all()
    out = [to_out(b, db, live) for b in rows]
    if active_only:
        out = [o for o in out if o.status not in ("COMPLETED", "CANCELLED", "FAILED", "REJECTED")]
    return out


def _get(db: Session, booking_id: int) -> Booking:
    b = db.get(Booking, booking_id)
    if b is None:
        raise HTTPException(404, "booking not found")
    return b


@router.get("/{booking_id}", response_model=BookingOut)
async def get_booking(booking_id: int, db: Session = Depends(get_db), live: LiveSimulation = Depends(get_live)):
    return to_out(_get(db, booking_id), db, live)


@router.post("/{booking_id}/assign", response_model=BookingOut)
async def assign(booking_id: int, req: Optional[AssignRequest] = None, db: Session = Depends(get_db),
                 live: LiveSimulation = Depends(get_live)):
    b = _get(db, booking_id)
    j = live.coord.journeys.get(b.journey_id) if b.run_id == live.run_id else None
    if j is None:
        raise HTTPException(409, "booking belongs to a previous simulation run")
    if j.status != "WAITING_FOR_AMBULANCE":
        raise HTTPException(409, f"booking is already {j.status}")
    amb = live.coord.assign(j.id, req.ambulance_id if req else None)
    if amb is None:
        raise HTTPException(409, "no ambulance available")
    return to_out(b, db, live)


@router.post("/{booking_id}/cancel", response_model=BookingOut)
async def cancel(booking_id: int, db: Session = Depends(get_db), live: LiveSimulation = Depends(get_live)):
    b = _get(db, booking_id)
    j = live.coord.journeys.get(b.journey_id) if b.run_id == live.run_id else None
    if j is None:
        raise HTTPException(409, "booking belongs to a previous simulation run")
    live.coord.cancel(j.id)
    db.refresh(b)
    return to_out(b, db, live)


@router.get("/{booking_id}/route")
async def get_route(booking_id: int, db: Session = Depends(get_db), live: LiveSimulation = Depends(get_live)):
    b = _get(db, booking_id)
    routes = db.scalars(select(Route).where(Route.booking_id == booking_id).order_by(Route.id)).all()
    j = live.coord.journeys.get(b.journey_id) if b.run_id == live.run_id else None
    return {
        "booking_id": booking_id,
        "eta_s": j.eta_s if j else b.eta_s,
        "remaining": live.coord.journey_dict(j)["route"] if j else [],
        "legs": [{"leg": r.leg, "edges": r.edges, "geometry": json.loads(r.geometry_geojson),
                  "length_m": r.length_m, "estimated_time_s": r.estimated_time_s} for r in routes],
    }


@router.get("/{booking_id}/events")
def get_events(booking_id: int, db: Session = Depends(get_db)):
    _get(db, booking_id)
    sys_ev = db.scalars(select(SystemEvent).where(SystemEvent.booking_id == booking_id)).all()
    sig_ev = db.scalars(select(SignalEvent).where(SignalEvent.booking_id == booking_id)).all()
    events = [{"source": "journey", "type": e.event_type, "sim_time": e.sim_time, "payload": e.payload,
               "created_at": e.created_at} for e in sys_ev]
    events += [{"source": "signal", "type": e.event_type, "sim_time": e.sim_time, "intersection": e.intersection_id,
                "payload": e.payload, "created_at": e.created_at} for e in sig_ev]
    return sorted(events, key=lambda e: e["sim_time"])
