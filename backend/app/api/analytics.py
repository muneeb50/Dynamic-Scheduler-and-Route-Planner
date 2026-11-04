"""Journey analytics, run summaries, experiments and computer-vision endpoints."""
from __future__ import annotations

import asyncio
import statistics
import threading
from typing import Optional

import cv2
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..analytics.experiment import run_experiment
from ..config import settings
from ..db import get_db, session_scope
from ..models import Booking, Experiment, JourneyMetrics, RecoveryEventRow, SignalEvent, SimulationRun, utcnow
from ..schemas import ExperimentRequest
from ..services.live import LiveSimulation
from .deps import get_live, require_admin

router = APIRouter(prefix="/api", tags=["analytics"])


@router.get("/analytics/summary")
def summary(run_id: Optional[int] = None, db: Session = Depends(get_db), live: LiveSimulation = Depends(get_live)):
    """Aggregate metrics over all completed journeys, split by whether priority was enabled."""
    q = select(JourneyMetrics)
    if run_id:
        q = q.where(JourneyMetrics.run_id == run_id)
    rows = db.scalars(q).all()
    out = {}
    for label, flag in (("baseline", False), ("priority", True)):
        done = [r for r in rows if r.priority_enabled == flag and r.status == "COMPLETED"]
        tt = [r.travel_time_s for r in done if r.travel_time_s is not None]
        out[label] = {
            "journeys": len(done),
            "mean_travel_time_s": round(statistics.fmean(tt), 1) if tt else None,
            "mean_signal_delay_s": round(statistics.fmean([r.signal_delay_s for r in done]), 1) if done else None,
            "mean_stops": round(statistics.fmean([r.stops for r in done]), 2) if done else None,
        }
    rec = db.execute(select(RecoveryEventRow.status, func.count(), func.avg(RecoveryEventRow.duration_s))
                     .group_by(RecoveryEventRow.status)).all()
    out["recoveries"] = {s: {"count": c, "mean_duration_s": None if a is None else round(a, 1)} for s, c, a in rec}
    out["bookings_by_status"] = dict(db.execute(select(Booking.status, func.count()).group_by(Booking.status)).all())
    out["priority_events"] = dict(db.execute(select(SignalEvent.event_type, func.count())
                                             .group_by(SignalEvent.event_type)).all())
    out["runs"] = [{"id": r.id, "engine": r.engine, "demand": r.demand, "priority_enabled": r.priority_enabled,
                    "started_at": r.started_at} for r in db.scalars(select(SimulationRun).order_by(SimulationRun.id.desc()).limit(20))]
    out["safety_violations_live"] = sum(c.violations for c in live.coord.controllers.values())
    return out


@router.get("/analytics/{booking_id}")
def booking_analytics(booking_id: int, db: Session = Depends(get_db)):
    b = db.get(Booking, booking_id)
    if b is None:
        raise HTTPException(404, "booking not found")
    m = db.scalar(select(JourneyMetrics).where(JourneyMetrics.booking_id == booking_id))
    recs = db.scalars(select(RecoveryEventRow).where(RecoveryEventRow.booking_id == booking_id)).all()
    pri = db.scalars(select(SignalEvent).where(SignalEvent.booking_id == booking_id,
                                               SignalEvent.event_type.in_(["PRIORITY_REQUESTED", "PRIORITY_GRANTED",
                                                                           "PRIORITY_RELEASED", "PRIORITY_TIMEOUT"]))).all()
    return {
        "booking_id": booking_id, "status": b.status,
        "metrics": m.metrics if m else None,
        "priority_enabled": m.priority_enabled if m else None,
        "priority_events": [{"type": e.event_type, "sim_time": e.sim_time, "node": e.intersection_id,
                             **{k: e.payload.get(k) for k in ("eta_s", "distance_m", "queue_ahead", "hold_time_s")}}
                            for e in pri],
        "recoveries": [{"node": r.intersection_id, "status": r.status, "duration_s": r.duration_s,
                        "baseline": r.details.get("baseline"), "peak_backlog": r.details.get("peak_backlog"),
                        "residual": r.details.get("residual")} for r in recs],
    }


# ------------------------------------------------------------------ experiments
def _run_experiment_job(exp_id: int, req: ExperimentRequest, live: LiveSimulation):
    def progress(p: float):
        with session_scope() as s:
            e = s.get(Experiment, exp_id)
            e.progress = round(p, 3)

    with session_scope() as s:
        s.get(Experiment, exp_id).status = "RUNNING"
    try:
        res = run_experiment(live.net, req.demands, req.seeds, req.engine, req.warmup_s, req.recovery_tail_s,
                             req.base_id, req.pickup_node, req.hospital_id, progress,
                             {"net_file": settings.sumo_net_file}, settings.results_dir)
        with session_scope() as s:
            e = s.get(Experiment, exp_id)
            e.status, e.results, e.progress, e.finished_at = "DONE", res, 1.0, utcnow()
    except Exception as exc:  # report the failure instead of hanging in RUNNING
        with session_scope() as s:
            e = s.get(Experiment, exp_id)
            e.status, e.error, e.finished_at = "FAILED", f"{type(exc).__name__}: {exc}", utcnow()


@router.post("/experiments", status_code=202, dependencies=[Depends(require_admin)])
def start_experiment(req: ExperimentRequest, db: Session = Depends(get_db), live: LiveSimulation = Depends(get_live)):
    running = db.scalar(select(func.count()).select_from(Experiment).where(Experiment.status.in_(["QUEUED", "RUNNING"])))
    if running:
        raise HTTPException(409, "an experiment is already running")
    e = Experiment(params=req.model_dump(), status="QUEUED")
    db.add(e)
    db.commit()
    threading.Thread(target=_run_experiment_job, args=(e.id, req, live), daemon=True).start()
    return {"id": e.id, "status": e.status}


@router.get("/experiments")
def list_experiments(db: Session = Depends(get_db)):
    rows = db.scalars(select(Experiment).order_by(Experiment.id.desc()).limit(20)).all()
    return [{"id": e.id, "status": e.status, "progress": e.progress, "params": e.params, "error": e.error,
             "created_at": e.created_at, "finished_at": e.finished_at} for e in rows]


@router.get("/experiments/{exp_id}")
def get_experiment(exp_id: int, db: Session = Depends(get_db)):
    e = db.get(Experiment, exp_id)
    if e is None:
        raise HTTPException(404, "experiment not found")
    return {"id": e.id, "status": e.status, "progress": e.progress, "params": e.params, "error": e.error,
            "results": e.results, "created_at": e.created_at, "finished_at": e.finished_at}


# --------------------------------------------------------------- computer vision
def _provider(live: LiveSimulation):
    if live.cv_provider is None:
        from computer_vision.provider import CVQueueProvider

        live.cv_provider = CVQueueProvider()
    return live.cv_provider


@router.get("/cv/{node_id}/estimate")
async def cv_estimate(node_id: str, live: LiveSimulation = Depends(get_live)):
    coord = live.coord
    if node_id not in coord.controllers:
        raise HTTPException(404, "not a signalised intersection")
    prov = _provider(live)
    out = []
    for a in live.net.nodes[node_id].incoming:
        r = prov.last.get(a)
        if r is None or coord.now - r.t > 2.0:
            prov.estimate(coord, a)
            r = prov.last[a]
        out.append({**r.summary(), "sim_queue_whole_approach": coord.engine.halting(a), "name": live.net.edges[a].name})
    return {"node": node_id, "queue_source": coord.queue_source, "approaches": out}


@router.get("/cv/{node_id}/frame.png")
async def cv_frame(node_id: str, approach: str, truth: bool = True, live: LiveSimulation = Depends(get_live)):
    from computer_vision.provider import annotate

    coord = live.coord
    node = live.net.nodes.get(node_id)
    if node is None or approach not in node.incoming:
        raise HTTPException(404, "approach does not belong to this intersection")
    prov = _provider(live)
    r = prov.last.get(approach)
    if r is None or coord.now - r.t > 1.0:
        prov.estimate(coord, approach)
        r = prov.last[approach]
    ok, png = cv2.imencode(".png", annotate(r, show_truth=truth))
    return Response(content=png.tobytes(), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/cv/evaluate", dependencies=[Depends(require_admin)])
async def cv_evaluate(seconds: float = 300, demand: str = "medium", seed: int = 7):
    from computer_vision.evaluation.evaluate import evaluate

    if seconds > 1800:
        raise HTTPException(422, "seconds must be <= 1800")
    return await asyncio.to_thread(evaluate, settings.network_file, seconds, demand, seed)
