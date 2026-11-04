"""Measure ambulance recognition on your own clips.

Put short clips (5–30 s each) in two folders:
    data/videos/eval/ambulance/   – an ambulance is clearly visible at some point
    data/videos/eval/other/       – traffic without ambulances (vans, trucks, red cars, police …)

    python -m computer_vision.evaluation.ambulance_eval --root data/videos/eval

Reports, per clip and overall: whether an ambulance was recognised, time to first recognition,
the cues that fired, and clip-level recall (ambulance clips) / false-alarm rate (other clips).
Run it with different settings (--no-model, --no-beacon, --detector yoloe) for an ablation table.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from computer_vision.video.ambulance import AmbulanceConfig, AmbulanceVerifier, ambulance_model_path  # noqa: E402
from computer_vision.video.detectors import VEHICLE_CLASSES, UltralyticsYolo, create_detector  # noqa: E402
from computer_vision.video.tracker import Tracker  # noqa: E402

VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v"}


def run_clip(path: Path, det, verifier_factory, fps: float) -> dict:
    cap = cv2.VideoCapture(str(path))
    vfps = cap.get(cv2.CAP_PROP_FPS) or 25
    stride = max(1, int(round(vfps / fps)))
    tracker, verifier = Tracker(), verifier_factory()
    first, reasons, idx = None, set(), 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        idx += 1
        if (idx - 1) % stride:
            continue
        t = idx / vfps
        all_dets = det.detect(frame)
        dets = [d for d in all_dets if d.conf >= 0.3 and (d.cls in VEHICLE_CLASSES or det.name == "motion")]
        tracks = tracker.update(dets, t)
        newly = verifier.update(frame, tracks, t, all_dets)
        for tr in newly:
            reasons.update(r.split(" ")[0] for r in tr.amb_reasons)
            if first is None:
                first = round(t, 1)
    cap.release()
    return {"clip": path.name, "recognised": first is not None, "first_at_s": first, "cues": sorted(reasons)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="data/videos/eval")
    ap.add_argument("--detector", default="auto")
    ap.add_argument("--fps", type=float, default=8)
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--no-beacon", action="store_true")
    ap.add_argument("--no-livery", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args()
    root = Path(args.root)
    det = create_detector(args.detector, conf=0.1)
    det.keep = None
    model = None
    if not args.no_model and ambulance_model_path() and "yoloe" not in str(getattr(det, "weights", "")):
        model = UltralyticsYolo(str(ambulance_model_path()), conf=0.1, keep_classes=None)
        model.keep = None
    cfg = AmbulanceConfig(use_model=not args.no_model, use_beacon=not args.no_beacon, use_livery=not args.no_livery)
    results = {}
    for label in ("ambulance", "other"):
        clips = sorted(p for p in (root / label).glob("*") if p.suffix.lower() in VIDEO_EXT)
        results[label] = [run_clip(p, det, lambda: AmbulanceVerifier(cfg, model, background=False), args.fps) for p in clips]
        for r in results[label]:
            print(f"[{label:9}] {r['clip']:40} recognised={r['recognised']!s:5} first={r['first_at_s']} cues={r['cues']}")
    amb, other = results["ambulance"], results["other"]
    summary = {
        "settings": {"detector": det.name, "model": bool(model) or "yoloe" in str(getattr(det, "weights", "")),
                     "beacon": cfg.use_beacon, "livery": cfg.use_livery},
        "ambulance_clips": len(amb), "recall": round(sum(r["recognised"] for r in amb) / len(amb), 3) if amb else None,
        "other_clips": len(other),
        "false_alarm_rate": round(sum(r["recognised"] for r in other) / len(other), 3) if other else None,
        "mean_time_to_recognise_s": round(sum(r["first_at_s"] for r in amb if r["recognised"]) /
                                          max(1, sum(r["recognised"] for r in amb)), 2) if amb else None,
    }
    print(json.dumps(summary, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": summary, "clips": results}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
