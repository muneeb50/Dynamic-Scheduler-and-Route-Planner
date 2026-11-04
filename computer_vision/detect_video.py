"""Run the YOLO / OpenCV traffic pipeline on a video file, a camera stream or a webcam – no backend needed.

    python computer_vision/detect_video.py --source traffic.mp4 --out annotated.mp4
    python computer_vision/detect_video.py --source 0 --show                          # laptop webcam, live window
    python computer_vision/detect_video.py --source rtsp://user:pass@192.168.1.50/stream1 --show
    python computer_vision/detect_video.py --source http://192.168.1.23:8080/video --show   # phone "IP Webcam" app
    python computer_vision/detect_video.py --source clip.mp4 --detector opencv-dnn --roi 0 0.4 1 0.4 1 1 0 1 --line 0 0.75 1 0.75

Outputs (with --out): annotated video, <out>.csv (per processed frame) and <out>.json (summary).
Keys in the --show window: q = quit, p = pause.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from computer_vision.video.ambulance import AmbulanceConfig, AmbulanceVerifier, ambulance_model_path  # noqa: E402
from computer_vision.video.analytics import TrafficAnalyzer, ZoneConfig  # noqa: E402
from computer_vision.video.annotate import annotate  # noqa: E402
from computer_vision.video.capture import open_capture  # noqa: E402
from computer_vision.video.detectors import VEHICLE_CLASSES, UltralyticsYolo, create_detector, ultralytics_available  # noqa: E402
from computer_vision.video.tracker import Tracker  # noqa: E402


def pairs(vals):
    return [[vals[i], vals[i + 1]] for i in range(0, len(vals), 2)] if vals else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="video file, rtsp/http URL, or webcam index (0, 1 …)")
    ap.add_argument("--detector", default="auto", choices=["auto", "yolo", "opencv-dnn", "motion"])
    ap.add_argument("--weights", help="YOLO .pt (yolo) or .onnx (opencv-dnn); default yolo11n")
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--fps", type=float, default=10, help="frames per second to analyse")
    ap.add_argument("--roi", type=float, nargs="+", help="zone polygon as x y pairs (fractions 0-1)")
    ap.add_argument("--line", type=float, nargs=4, help="counting line x1 y1 x2 y2 (fractions 0-1)")
    ap.add_argument("--mpp", type=float, help="metres per pixel near the stop line (enables km/h and metres)")
    ap.add_argument("--people", action="store_true", help="also count people")
    ap.add_argument("--out", help="write annotated video here (.mp4 / .avi / .webm)")
    ap.add_argument("--show", action="store_true", help="show a live window")
    ap.add_argument("--no-ambulance", action="store_true", help="disable ambulance recognition")
    args = ap.parse_args()

    src = int(args.source) if args.source.isdigit() else args.source
    try:
        cap, backend = open_capture(src)
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    is_file = isinstance(src, str) and Path(src).exists()
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    fps = fps if 1 <= fps <= 240 else 25
    stride = max(1, int(round(fps / args.fps))) if is_file else 1
    det = create_detector(args.detector, args.weights, conf=args.conf)
    keep = VEHICLE_CLASSES | ({"person"} if args.people else set())
    zone = ZoneConfig.from_dict({"roi": pairs(args.roi), "line": pairs(args.line), "meters_per_pixel": args.mpp})
    tracker, analyzer = Tracker(), TrafficAnalyzer(zone)
    amb_model = None
    if not args.no_ambulance and det.name != "motion" and ambulance_model_path() and ultralytics_available() \
            and "yoloe" not in str(getattr(det, "weights", "")):
        amb_model = UltralyticsYolo(str(ambulance_model_path()), conf=0.1, keep_classes=None)
        amb_model.keep = None
    verifier = AmbulanceVerifier(AmbulanceConfig(enabled=not args.no_ambulance), amb_model,
                                 background=str(args.source).isdigit() or '://' in str(args.source))
    print(f"detector: {det.info()}  source fps: {fps:.1f}  analysing every {stride} frame(s)")

    writer, rows, idx, t0, paused = None, [], 0, time.time(), False
    while True:
        if not paused:
            ok, frame = cap.read()
            if not ok:
                break
            idx += 1
            if (idx - 1) % stride:
                continue
            t = idx / fps if is_file else time.time() - t0
            all_dets = det.detect(frame)
            dets = [d for d in all_dets if d.conf >= args.conf and (d.cls in keep or det.name == "motion")]
            tracks = tracker.update(dets, t)
            verifier.update(frame, tracks, t, all_dets)
            for gone in tracker.removed:
                analyzer.track_lost(gone, t)
            stats = analyzer.update(tracks, t, frame.shape)
            for ev in analyzer.events[len(getattr(analyzer, "_printed", [])):]:
                print(f"  !! t={t:6.1f}s {ev['type']} track #{ev.get('track')} {', '.join(ev.get('reasons') or [])}")
            analyzer._printed = list(analyzer.events)
            img = annotate(frame, tracks, stats, analyzer, det.name)
            rows.append({"t": round(t, 2), "vehicles": stats.vehicles, "queue": stats.queue,
                         "crossings": stats.crossings_total, "flow_per_min": round(stats.flow_per_min, 1),
                         "congestion": stats.congestion, "by_class": json.dumps(stats.by_class)})
            if args.out:
                if writer is None:
                    h, w = img.shape[:2]
                    ext = Path(args.out).suffix.lower()
                    fourcc = {".webm": "VP80", ".avi": "MJPG"}.get(ext, "mp4v")
                    writer = cv2.VideoWriter(args.out, cv2.VideoWriter_fourcc(*fourcc), fps / stride if is_file else args.fps, (w, h))
                writer.write(img)
            if idx % (stride * 50) == 1:
                print(f"t={t:6.1f}s vehicles={stats.vehicles:3d} queue={stats.queue:3d} crossed={stats.crossings_total:4d} "
                      f"flow={stats.flow_per_min:5.1f}/min {stats.congestion}")
        if args.show:
            cv2.imshow("ResQRoute traffic analytics", img)
            key = cv2.waitKey(1 if not paused else 50) & 0xFF
            if key == ord("q"):
                break
            if key == ord("p"):
                paused = not paused
    cap.release()
    if writer:
        writer.release()
    if args.show:
        cv2.destroyAllWindows()
    if args.out and rows:
        base = Path(args.out).with_suffix("")
        with open(f"{base}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        summary = {"source": str(args.source), "detector": det.info(), "frames_analysed": len(rows),
                   "line_crossings": analyzer.crossings_total, "crossings_by_class": dict(analyzer.crossings_by_class),
                   "max_queue": max(r["queue"] for r in rows), "events": analyzer.events}
        Path(f"{base}.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        print(f"saved {args.out}, {base}.csv, {base}.json")
    print(f"done: {analyzer.crossings_total} vehicles crossed the line, {tracker.total_created} tracks")


if __name__ == "__main__":
    main()
