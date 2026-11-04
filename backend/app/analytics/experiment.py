"""Matched baseline-vs-priority experiments.

For every (demand level, seed) pair the same scenario is simulated twice – once
with fixed-time signals only (baseline) and once with emergency priority +
adaptive recovery. Background demand uses the same random seed in both runs,
so differences come from the control strategy, not from different traffic.
Results are reported as means, standard deviations, 95% confidence intervals
and paired differences – never from a single run.
"""
from __future__ import annotations

import csv
import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from ..core.network import RoadNetwork
from ..recovery.engine import RecoveryConfig
from ..sim.base import DEMAND_LEVELS, DemandConfig
from ..sim.coordinator import J_FINAL, Coordinator, build_engine
from ..traffic_control.priority import PriorityConfig

T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23,
       15: 2.13, 20: 2.09, 30: 2.04}

METRICS = [
    "drive_time_s", "total_time_s", "to_pickup_s", "to_hospital_s", "stops", "waiting_s", "signal_delay_s",
    "bg_mean_time_loss_s", "max_cross_queue", "mean_recovery_s", "recoveries_timeout", "mean_hold_s",
]


def t95(n: int) -> float:
    if n <= 1:
        return float("nan")
    df = n - 1
    keys = sorted(T95)
    for k in keys:
        if df <= k:
            return T95[k]
    return 1.96


def default_scenario(net: RoadNetwork) -> tuple[str, str, str]:
    sc = getattr(net, "scenario", None) or {}
    base = sc.get("experiment_base") or (net.bases[0].id if net.bases else None)
    pickup = sc.get("experiment_pickup_node") or next(iter(net.nodes))
    hospital = sc.get("experiment_hospital") or next(iter(net.hospitals))
    return base, pickup, hospital


def run_trial(net: RoadNetwork, *, priority: bool, demand: str, seed: int, engine: str = "builtin",
              warmup_s: float = 300, recovery_tail_s: float = 240, base_id: Optional[str] = None,
              pickup_node: Optional[str] = None, hospital_id: Optional[str] = None, max_journey_s: float = 2400,
              measure_horizon_s: float = 900, sumo_options: Optional[dict] = None) -> dict:
    """One simulated emergency trip.

    Other traffic is measured over a FIXED window after dispatch (identical in both arms
    of a matched pair): trips departing in [dispatch, dispatch + horizon - 300 s] and
    finishing by dispatch + horizon. Measuring only until the ambulance arrives would bias
    the faster arm towards short trips."""
    d_base, d_pickup, d_hosp = default_scenario(net)
    base_id, pickup_node, hospital_id = base_id or d_base, pickup_node or d_pickup, hospital_id or d_hosp
    eng = build_engine(net, engine, 0.5, DemandConfig(level=demand, vehicles_per_hour=DEMAND_LEVELS[demand], seed=seed),
                       sumo_options)
    try:
        coord = Coordinator(net, eng, PriorityConfig(enabled=priority), RecoveryConfig(enabled=priority))
        coord.run_for(warmup_s)
        for a in coord.ambulances.values():
            if a.id != base_id:
                a.status = "RESERVED_FOR_EXPERIMENT"
        p = net.nodes[pickup_node]
        j = coord.create_journey(p.lat, p.lon, hospital_id, journey_id="EXP")
        t_dispatch = coord.now
        max_cross = 0.0
        next_sample = coord.now
        while j.status not in J_FINAL and coord.now < t_dispatch + max_journey_s:
            coord.step()
            if coord.now >= next_sample:
                next_sample = coord.now + 1.0
                for s in coord.priority.active.values():
                    ctrl = coord.controllers[s.node]
                    others = [a for g, approaches in enumerate(ctrl.groups) if g != s.group for a in approaches]
                    if others:
                        max_cross = max(max_cross, max(coord.queue_for(a) for a in others))
        if j.status not in J_FINAL:
            coord.cancel(j.id, reason="experiment journey time limit")
        tail_end = coord.now + recovery_tail_s
        while coord.now < tail_end and coord.recovery.active:
            coord.step()
        while coord.now < t_dispatch + measure_horizon_s:
            coord.step()
        m = j.metrics
        window_end = t_dispatch + max(measure_horizon_s - 300.0, 60.0)
        trips = [t for t in eng.trips if t_dispatch <= t.depart <= window_end and t.arrival <= coord.now]
        losses = sorted(t.time_loss for t in trips)
        drive = (m.get("to_pickup_s") or 0) + (m.get("to_hospital_s") or 0) if j.status == "COMPLETED" else None
        return {
            "demand": demand, "seed": seed, "priority": priority, "engine": engine, "status": j.status,
            "drive_time_s": drive,
            "total_time_s": m.get("total_time_s"),
            "to_pickup_s": m.get("to_pickup_s"), "to_hospital_s": m.get("to_hospital_s"),
            "free_flow_s": m.get("free_flow_s"), "distance_m": m.get("distance_m"),
            "stops": m.get("stops"), "waiting_s": m.get("waiting_s"), "signal_delay_s": m.get("signal_delay_s"),
            "priority_requests": m.get("priority_requests"), "priority_timeouts": m.get("priority_timeouts"),
            "mean_hold_s": m.get("mean_hold_s"),
            "recoveries": m.get("recoveries"), "recoveries_completed": m.get("recoveries_completed"),
            "recoveries_timeout": m.get("recoveries_timeout"), "mean_recovery_s": m.get("mean_recovery_s"),
            "max_cross_queue": round(max_cross, 1),
            "bg_trips": len(trips),
            "bg_mean_time_loss_s": round(sum(losses) / len(losses), 2) if losses else None,
            "bg_p95_time_loss_s": round(losses[int(0.95 * (len(losses) - 1))], 2) if losses else None,
            "safety_violations": sum(c.violations for c in coord.controllers.values()),
            "teleports": getattr(eng, "teleports", 0),
            "intersections": m.get("intersections"),
            "failure_reason": m.get("failure_reason"),
        }
    finally:
        eng.close()


def _stats(values: list[float]) -> dict:
    vals = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not vals:
        return {"n": 0, "mean": None, "sd": None, "ci95": None}
    mean = statistics.fmean(vals)
    sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
    ci = t95(len(vals)) * sd / math.sqrt(len(vals)) if len(vals) > 1 else None
    return {"n": len(vals), "mean": round(mean, 2), "sd": round(sd, 2), "ci95": None if ci is None else round(ci, 2)}


def summarize(trials: list[dict]) -> dict:
    out: dict = {"by_demand": {}}
    demands = sorted({t["demand"] for t in trials}, key=lambda d: list(DEMAND_LEVELS).index(d))
    for d in demands:
        block = {}
        for label, flag in (("baseline", False), ("priority", True)):
            rows = [t for t in trials if t["demand"] == d and t["priority"] == flag]
            block[label] = {k: _stats([r.get(k) for r in rows]) for k in METRICS}
            block[label]["completed"] = sum(1 for r in rows if r["status"] == "COMPLETED")
            block[label]["runs"] = len(rows)
        # paired differences (same seed): priority - baseline
        pairs = []
        for t in trials:
            if t["demand"] == d and t["priority"]:
                b = next((x for x in trials if x["demand"] == d and not x["priority"] and x["seed"] == t["seed"]), None)
                if b and t["drive_time_s"] is not None and b["drive_time_s"] is not None:
                    pairs.append((b, t))
        paired = {}
        for k in ("drive_time_s", "signal_delay_s", "stops", "bg_mean_time_loss_s"):
            diffs = [p[k] - b[k] for b, p in pairs if p.get(k) is not None and b.get(k) is not None]
            paired[k] = _stats(diffs)
        bm, pm = block["baseline"]["drive_time_s"]["mean"], block["priority"]["drive_time_s"]["mean"]
        block["paired_difference"] = paired
        block["drive_time_reduction_pct"] = round(100 * (bm - pm) / bm, 1) if bm and pm is not None else None
        out["by_demand"][d] = block
    out["safety_violations_total"] = sum(t.get("safety_violations", 0) for t in trials)
    return out


def run_experiment(net: RoadNetwork, demands: list[str], seeds: list[int], engine: str = "builtin",
                   warmup_s: float = 300, recovery_tail_s: float = 240, base_id: Optional[str] = None,
                   pickup_node: Optional[str] = None, hospital_id: Optional[str] = None,
                   progress: Optional[Callable[[float], None]] = None, sumo_options: Optional[dict] = None,
                   results_dir: Optional[str] = None) -> dict:
    for d in demands:
        if d not in DEMAND_LEVELS or d == "off":
            raise ValueError(f"unknown demand level {d}")
    d_base, d_pickup, d_hosp = default_scenario(net)
    scenario = {"base_id": base_id or d_base, "pickup_node": pickup_node or d_pickup,
                "hospital_id": hospital_id or d_hosp}
    total = len(demands) * len(seeds) * 2
    trials, done = [], 0
    t0 = time.time()
    for d in demands:
        for seed in seeds:
            for priority in (False, True):
                trials.append(run_trial(net, priority=priority, demand=d, seed=seed, engine=engine, warmup_s=warmup_s,
                                        recovery_tail_s=recovery_tail_s, sumo_options=sumo_options, **scenario))
                done += 1
                if progress:
                    progress(done / total)
    result = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "network": net.name, "engine": engine, "demands": demands, "seeds": seeds, "scenario": scenario,
        "warmup_s": warmup_s, "wall_time_s": round(time.time() - t0, 1),
        "summary": summarize(trials), "trials": trials,
    }
    if results_dir:
        out = Path(results_dir)
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        (out / f"experiment_{stamp}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        cols = [k for k in trials[0] if k != "intersections"]
        with open(out / f"experiment_{stamp}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(trials)
        result["files"] = [str(out / f"experiment_{stamp}.json"), str(out / f"experiment_{stamp}.csv")]
    return result
