"""Score the CV pipeline against simulation ground truth.

    python -m computer_vision.evaluation.evaluate --seconds 600 --demand medium

Reports
  * detection precision / recall (IoU >= 0.5 against true vehicle boxes)
  * vehicle-count MAE per frame
  * queue MAE and bias against the true number of halting vehicles *in view*
  * queue MAE against halting vehicles on the *whole* approach (shows the effect
    of the camera's limited field of view on long queues)
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)

from computer_vision.provider import CVQueueProvider  # noqa: E402


def iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter == 0:
        return 0.0
    area = lambda r: (r[2] - r[0]) * (r[3] - r[1])  # noqa: E731
    return inter / float(area(a) + area(b) - inter)


def match(dets, gts, thr=0.5) -> tuple[int, int, int]:
    used, tp = set(), 0
    for d in dets:
        best, bi = 0.0, None
        for i, g in enumerate(gts):
            if i in used:
                continue
            v = iou(d.box, g.box)
            if v > best:
                best, bi = v, i
        if bi is not None and best >= thr:
            used.add(bi)
            tp += 1
    return tp, len(dets) - tp, len(gts) - tp


def evaluate(network_file: str | None = None, seconds: float = 600, demand: str = "medium", seed: int = 7,
             warmup: float = 180, sample_every: float = 5.0, max_nodes: int = 6) -> dict:
    from app.config import settings
    from app.core.network import RoadNetwork
    from app.sim.base import DEMAND_LEVELS, DemandConfig
    from app.sim.coordinator import Coordinator, build_engine

    net = RoadNetwork.load(network_file or settings.network_file)
    eng = build_engine(net, "builtin", 0.5, DemandConfig(level=demand, vehicles_per_hour=DEMAND_LEVELS[demand], seed=seed))
    coord = Coordinator(net, eng)
    provider = CVQueueProvider()
    nodes = list(coord.controllers)[:max_nodes]
    approaches = [a for n in nodes for a in net.nodes[n].incoming]
    coord.run_for(warmup)
    tp = fp = fn = 0
    count_err, q_err, q_bias, q_err_full, conf = [], [], [], [], []
    frames = 0
    t_end = coord.now + seconds
    next_score = coord.now
    while coord.now < t_end:
        coord.run_for(1.0)
        score = coord.now >= next_score
        if score:
            next_score = coord.now + sample_every
        for a in approaches:
            est = provider.estimate(coord, a)
            if not score:
                continue
            r = provider.last[a]
            frames += 1
            t, f, n = match(est.detections, r.ground_truth)
            tp, fp, fn = tp + t, fp + f, fn + n
            count_err.append(abs(est.vehicles - len(r.ground_truth)))
            q_err.append(abs(est.stopped - r.gt_stopped))
            q_bias.append(est.stopped - r.gt_stopped)
            q_err_full.append(abs(est.queue_used - eng.halting(a)))
            conf.append(est.confidence)
    eng.close()
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return {
        "frames": frames, "approaches": len(approaches), "demand": demand, "seed": seed,
        "precision": None if precision is None else round(precision, 3),
        "recall": None if recall is None else round(recall, 3),
        "f1": None if not precision or not recall else round(2 * precision * recall / (precision + recall), 3),
        "count_mae": round(statistics.fmean(count_err), 3) if count_err else None,
        "queue_mae_in_view": round(statistics.fmean(q_err), 3) if q_err else None,
        "queue_bias_in_view": round(statistics.fmean(q_bias), 3) if q_bias else None,
        "queue_mae_whole_approach": round(statistics.fmean(q_err_full), 3) if q_err_full else None,
        "mean_confidence": round(statistics.fmean(conf), 3) if conf else None,
        "note": "Scored on rendered simulation frames, not real CCTV footage.",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--network")
    ap.add_argument("--seconds", type=float, default=600)
    ap.add_argument("--demand", default="medium")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out")
    args = ap.parse_args()
    res = evaluate(args.network, args.seconds, args.demand, args.seed)
    print(json.dumps(res, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps(res, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
