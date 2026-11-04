"""Headless runs: matched baseline-vs-priority experiments or a single trial.

    python simulation/run_simulation.py experiment                       # low/medium/high x 5 seeds, built-in engine
    python simulation/run_simulation.py experiment --engine sumo --seeds 1 2 3
    python simulation/run_simulation.py trial --demand high --seed 4 --priority on
    python simulation/run_simulation.py trial --engine sumo --gui        # watch it in sumo-gui

Results (JSON + CSV) are written to results/.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from app.analytics.experiment import run_experiment, run_trial  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.network import RoadNetwork  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("experiment", "trial"):
        p = sub.add_parser(name)
        p.add_argument("--network", default=settings.network_file)
        p.add_argument("--engine", default="builtin", choices=["builtin", "sumo"])
        p.add_argument("--warmup", type=float, default=300)
        p.add_argument("--base")
        p.add_argument("--pickup-node")
        p.add_argument("--hospital")
        p.add_argument("--gui", action="store_true", help="SUMO only: open sumo-gui")
    e = sub.choices["experiment"]
    e.add_argument("--demands", nargs="+", default=["low", "medium", "high"])
    e.add_argument("--seeds", nargs="+", type=int, default=[1, 2, 3, 4, 5])
    t = sub.choices["trial"]
    t.add_argument("--demand", default="medium")
    t.add_argument("--seed", type=int, default=1)
    t.add_argument("--priority", choices=["on", "off"], default="on")
    args = ap.parse_args()

    net = RoadNetwork.load(args.network)
    sumo_opts = {"net_file": settings.sumo_net_file, "gui": args.gui}
    if args.cmd == "trial":
        r = run_trial(net, priority=args.priority == "on", demand=args.demand, seed=args.seed, engine=args.engine,
                      warmup_s=args.warmup, base_id=args.base, pickup_node=args.pickup_node,
                      hospital_id=args.hospital, sumo_options=sumo_opts)
        print(json.dumps(r, indent=1))
        return

    def progress(p):
        print(f"\r  progress {p * 100:5.1f}%", end="", flush=True)

    res = run_experiment(net, args.demands, args.seeds, args.engine, args.warmup, 240, args.base, args.pickup_node,
                         args.hospital, progress, sumo_opts, settings.results_dir)
    print()
    print(f"{'demand':8} {'baseline drive (s)':>20} {'priority drive (s)':>20} {'reduction':>10} "
          f"{'signal delay b/p (s)':>22} {'recovery (s)':>13}")
    for d, b in res["summary"]["by_demand"].items():
        bd, pd = b["baseline"]["drive_time_s"], b["priority"]["drive_time_s"]
        bs, ps = b["baseline"]["signal_delay_s"], b["priority"]["signal_delay_s"]
        rec = b["priority"]["mean_recovery_s"]["mean"]
        print(f"{d:8} {bd['mean']:>12} ± {bd['ci95'] or 0:<6} {pd['mean']:>12} ± {pd['ci95'] or 0:<6} "
              f"{b['drive_time_reduction_pct']:>9}% {bs['mean']:>10} / {ps['mean']:<10} {rec if rec is not None else '-':>12}")
    print(f"safety violations: {res['summary']['safety_violations_total']}")
    print("saved:", *res.get("files", []), sep="\n  ")


if __name__ == "__main__":
    main()
