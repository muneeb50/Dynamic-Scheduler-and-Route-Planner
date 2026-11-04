"""Scale benchmark: how much traffic the simulator handles on a given map, and how fast.

    python simulation/benchmark.py                                  # the current map, medium + high demand, 20 min
    python simulation/benchmark.py --demand low medium high peak --minutes 30
    python simulation/benchmark.py --network data/maps/navi-mumbai.network.json
    python simulation/benchmark.py --synthetic 14 12                # synthetic 14×12 km test city (offline)

Writes results/benchmark.json and prints, per demand level: vehicles inserted per hour,
vehicles on the road at the end, trips completed, mean time loss, signals, priority
safety violations, and the real-time factor (simulated seconds per wall-clock second).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "simulation" / "tools"))

from app.config import settings  # noqa: E402
from app.core.network import RoadNetwork  # noqa: E402
from app.sim.base import DEMAND_LEVELS, DemandConfig  # noqa: E402
from app.sim.coordinator import Coordinator, build_engine  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--network", default=None, help="network.json (default: the map the backend would load)")
    ap.add_argument("--synthetic", nargs=2, type=float, metavar=("WIDTH_KM", "HEIGHT_KM"))
    ap.add_argument("--demand", nargs="+", default=["medium", "high"], choices=list(DEMAND_LEVELS))
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--emergencies", type=int, default=3, help="test ambulances sent through random signals")
    ap.add_argument("--out", default=str(ROOT / "results" / "benchmark.json"))
    a = ap.parse_args()

    if a.synthetic:
        from app.mapdata.osm import BuildOptions, build_network, parse_overpass_json
        from synthetic_city import make_city

        net = RoadNetwork(build_network(parse_overpass_json(make_city(*a.synthetic)), BuildOptions(name="Synthetic city")))
    else:
        net = RoadNetwork.load(a.network or settings.network_file)
    s = net.summary()
    print(f"map: {s['name']} – {s['nodes']:,} junctions, {s['edges']:,} segments, {s['lane_km']} lane-km, {s['signals']} signals")
    rows = []
    for level in a.demand:
        eng = build_engine(net, "builtin", 0.5, DemandConfig(level, DEMAND_LEVELS[level], seed=a.seed))
        c = Coordinator(net, eng)
        sigs = sorted(c.controllers)
        steps = int(a.minutes * 60 / 0.5)
        t0 = time.perf_counter()
        step_times, peak = [], 0
        for k in range(steps):
            if a.emergencies and sigs and k in {int(steps * (i + 1) / (a.emergencies + 1)) for i in range(a.emergencies)}:
                try:
                    c.spawn_test_ambulance(sigs[(k * 7919) % len(sigs)])
                except ValueError:
                    pass
            ts = time.perf_counter()
            c.step()
            step_times.append(time.perf_counter() - ts)
            peak = max(peak, len(eng.vehicles))
        wall = time.perf_counter() - t0
        loss = [t.time_loss for t in eng.trips]
        sessions = c.priority.history
        row = {
            "demand": level, "vehicles_per_hour": round(eng.effective_vehicles_per_hour), "demand_scale": round(eng.demand_scale, 2),
            "sim_minutes": a.minutes, "vehicles_end": len(eng.vehicles), "vehicles_peak": peak, "trips_completed": len(eng.trips),
            "mean_time_loss_s": round(statistics.mean(loss), 1) if loss else None, "teleports": eng.teleports,
            "priority_sessions": len(sessions), "priority_granted": sum(1 for x in sessions if x.granted_at is not None),
            "recoveries": len(c.recovery.all_events()), "safety_violations": sum(x.violations for x in c.controllers.values()),
            "wall_s": round(wall, 1), "realtime_factor": round(a.minutes * 60 / wall, 1),
            "step_ms_p95": round(sorted(step_times)[int(len(step_times) * 0.95)] * 1000, 1),
        }
        rows.append(row)
        print(f"  {level:7s} {row['vehicles_per_hour']:>7,} veh/h  peak {peak:>6,} on road  {row['trips_completed']:>6,} trips  "
              f"loss {row['mean_time_loss_s']}s  priority {row['priority_granted']}/{row['priority_sessions']}  "
              f"violations {row['safety_violations']}  {row['realtime_factor']}× real time")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps({"network": {k: v for k, v in s.items() if k != "meta"}, "runs": rows}, indent=1),
                           encoding="utf-8")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
