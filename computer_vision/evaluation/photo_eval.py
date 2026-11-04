"""Measure photo detection on full scenes: vehicles found and ambulances recognised.

    python -m computer_vision.evaluation.photo_eval                       # Indian traffic test scenes (default)
    python -m computer_vision.evaluation.photo_eval --images my/images --labels my/labels --ambulance-id 0

Default test set: the *test* split of the Indian ambulance/traffic dataset (HariPrashanna-07/Ambulance_dataset),
minus every image that looks like a training image (perceptual hash), so nothing here was seen in training.

Two configurations are compared on the same photos:
  previous – what the app did before: YOLO11n, one pass, open-vocabulary YOLOE + light bar / livery checks
  new      – YOLO11m scale-aware (640 px + 1280 px + 2×2 tiles), the trained vehicle classifier, 'AMBULANCE' text (OCR),
             all fused (computer_vision/video/ambulance_fusion.py)

Metrics
  vehicle recall     – labelled vehicles (car, bus, auto, bike, ambulance) with a detected vehicle at IoU >= 0.5
  ambulance recall   – labelled ambulances recognised as ambulance (IoU >= 0.3)
  ambulance precision – reported ambulances that match a labelled ambulance (IoU >= 0.3). The dataset often labels
                       only the siren of an ambulance, so 'precision (siren-aware)' also accepts a reported
                       ambulance that contains a labelled siren. Some ambulances are not labelled at all, so both
                       numbers are lower bounds.
  false alarms       – photos with *no* ambulance or siren label in which an ambulance was reported
Writes results/photo_eval.json.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DEFAULT = ROOT.parent / "datasets" / "HariPrashanna-07_Ambulance_dataset" / "Ambulance.v1i.yolov11"


def iou(a, b):
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    i = max(0, x2 - x1) * max(0, y2 - y1)
    return i / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i + 1e-9) if i else 0.0


def dhash(img) -> int:
    g = cv2.cvtColor(cv2.resize(img, (9, 8), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
    return int("".join("1" if b else "0" for b in (g[:, 1:] > g[:, :-1]).flatten()), 2)


def load_scenes(images: Path, labels: Path, amb_id: int, siren_id: int | None, vehicle_ids: set, exclude: list[int]):
    arr = np.array(exclude, dtype=np.uint64) if exclude else None
    out, skipped = [], 0
    for f in sorted(images.glob("*")):
        img = cv2.imread(str(f))
        if img is None:
            continue
        if arr is not None:
            x = np.bitwise_xor(arr, np.uint64(dhash(img)))
            if (np.unpackbits(x.view(np.uint8).reshape(-1, 8), axis=1).sum(1) <= 6).any():
                skipped += 1
                continue
        h, w = img.shape[:2]
        gt_v, gt_a, siren = [], [], []
        lab = labels / (f.stem + ".txt")
        for line in (lab.read_text().splitlines() if lab.exists() else []):
            p = line.split()
            if len(p) < 5:
                continue
            c, x, y, bw, bh = int(float(p[0])), *map(float, p[1:5])
            box = ((x - bw / 2) * w, (y - bh / 2) * h, (x + bw / 2) * w, (y + bh / 2) * h)
            if c == amb_id:
                gt_a.append(box)
                gt_v.append(box)
            elif c in vehicle_ids:
                gt_v.append(box)
            elif siren_id is not None and c == siren_id:
                siren.append(box)
        out.append((f, img, gt_v, gt_a, siren))
    return out, skipped


def run(config: str, scenes, args) -> dict:
    from computer_vision.video.detectors import MODELS_DIR, UltralyticsYolo, create_detector
    from computer_vision.video.ensemble import AmbulanceText, EnsembleDetector
    from computer_vision.video.photo import analyze_photo
    from computer_vision.video.vehicle_classifier import VehicleClassifier, classifier_path

    if config == "previous":
        det = create_detector("yolo", str(MODELS_DIR / "yolo11n.pt"), conf=0.1)
        det.keep = None
        yoloe = MODELS_DIR / "yoloe-11s-traffic.pt"
        amb = UltralyticsYolo(str(yoloe), conf=0.1) if yoloe.exists() else None
        if amb:
            amb.keep = None
        text, clf, imgsz = None, None, 960
    else:
        big = UltralyticsYolo(str(MODELS_DIR / "yolo11m.pt"), conf=0.1)
        big.keep = None
        det = EnsembleDetector([(big, 1.0, 640, "all"), (big, 1.0, 1280, "small")], tile_grid=2, conf=0.15)
        amb = None
        text = AmbulanceText() if not args.no_ocr else None
        clf = VehicleClassifier() if classifier_path() else None
        imgsz = 1280
    tv = fv = 0
    ta = fa_amb = 0
    false_alarm_imgs = clean_imgs = 0
    pred_amb_total = correct_amb = correct_siren = 0
    times = []
    examples = []
    for f, img, gt_v, gt_a, siren in scenes:
        t0 = time.perf_counter()
        _, r = analyze_photo(img, det, conf=args.conf, detect="vehicles", amb_model=amb, ambulance=True, imgsz=imgsz,
                             text_reader=text, classifier=clf)
        times.append(time.perf_counter() - t0)
        veh = [d["box"] for d in r["detections"] if d.get("road_user") and d["cls"] != "person"]
        ambs = [d["box"] for d in r["detections"] if d.get("ambulance")]
        tv += sum(1 for g in gt_v if any(iou(g, p) >= 0.5 for p in veh))
        fv += len(gt_v)
        hit = sum(1 for g in gt_a if any(iou(g, p) >= 0.3 for p in ambs))
        ta += hit
        fa_amb += len(gt_a)
        pred_amb_total += len(ambs)
        correct_amb += sum(1 for p in ambs if any(iou(g, p) >= 0.3 for g in gt_a))
        correct_siren += sum(1 for p in ambs if any(iou(g, p) >= 0.3 for g in gt_a) or any(
            p[0] <= (s[0] + s[2]) / 2 <= p[2] and p[1] <= (s[1] + s[3]) / 2 <= p[3] for s in siren))
        if not gt_a and not siren:
            clean_imgs += 1
            if ambs:
                false_alarm_imgs += 1
                if len(examples) < 10:
                    examples.append(f.name)
    return {
        "photos": len(scenes), "seconds_per_photo": round(float(np.mean(times)), 2),
        "vehicle_recall": round(tv / max(1, fv), 3), "labelled_vehicles": fv,
        "ambulance_recall": round(ta / max(1, fa_amb), 3), "labelled_ambulances": fa_amb,
        "ambulance_precision": round(correct_amb / max(1, pred_amb_total), 3) if pred_amb_total else None,
        "ambulance_precision_siren_aware": round(correct_siren / max(1, pred_amb_total), 3) if pred_amb_total else None,
        "reported_ambulances": pred_amb_total,
        "false_alarm_photos": f"{false_alarm_imgs}/{clean_imgs}",
        "false_alarm_rate": round(false_alarm_imgs / max(1, clean_imgs), 3), "false_alarm_examples": examples,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--images", default=str(DEFAULT / "test" / "images"))
    ap.add_argument("--labels", default=str(DEFAULT / "test" / "labels"))
    ap.add_argument("--train-images", default=str(DEFAULT / "train" / "images"), help="near-duplicates of these are skipped")
    ap.add_argument("--ambulance-id", type=int, default=1)
    ap.add_argument("--siren-id", type=int, default=0)
    ap.add_argument("--vehicle-ids", default="2,3,4,5")
    ap.add_argument("--conf", type=float, default=0.3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--configs", default="previous,new")
    ap.add_argument("--no-ocr", action="store_true")
    a = ap.parse_args()
    exclude = []
    tr = Path(a.train_images)
    if tr.exists():
        exclude = sorted({dhash(im) for p in tr.glob("*") if (im := cv2.imread(str(p))) is not None})
    scenes, skipped = load_scenes(Path(a.images), Path(a.labels), a.ambulance_id, a.siren_id,
                                  {int(x) for x in a.vehicle_ids.split(",") if x}, exclude)
    if a.limit:
        # keep every scene with an ambulance, fill up with the others
        amb = [s for s in scenes if s[3]]
        rest = [s for s in scenes if not s[3]]
        scenes = amb[: a.limit // 2] + rest[: a.limit - min(len(amb), a.limit // 2)]
    print(f"{len(scenes)} test photos ({sum(1 for s in scenes if s[3])} with ambulances), {skipped} near-duplicates of training images skipped")
    res = {"test_set": str(a.images), "photos": len(scenes), "skipped_near_duplicates": skipped}
    for c in a.configs.split(","):
        res[c] = run(c, scenes, a)
        print(c, json.dumps(res[c]))
    out = ROOT / "results" / "photo_eval.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(res, indent=1))
    print("wrote", out)


if __name__ == "__main__":
    main()
