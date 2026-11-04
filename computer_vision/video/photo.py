"""Single-photo traffic analysis: the same detectors as the video pipeline, applied to still images.

For each photo: every detected object, road vehicles by class, pedestrians, how much of the road area
vehicles cover (occupancy) and a congestion level, and an ambulance check per vehicle. A still image
cannot show a *flashing* beacon, so the photo ambulance check uses the open-vocabulary model (if
installed), a lit red/blue light bar and the livery – it is less certain than the video check.
"""
from __future__ import annotations

import time
from typing import Optional

import cv2
import numpy as np

from .ambulance import AMBULANCE_NAMES, CANDIDATE_CLASSES, beacon_sample, livery_score
from .ambulance_fusion import ambulance_probability
from .ambulance_fusion import threshold as fusion_threshold
from .annotate import CONGESTION_COLORS, _color
from .detectors import VEHICLE_CLASSES, BaseDetector, Det
from .ensemble import remove_wrapping_boxes
from .tracker import iou

MAX_SIDE = 1920  # larger photos are scaled down (detection runs at 640–960 px anyway)


def _congestion(occupancy: float, vehicles: int) -> str:
    if vehicles < 3:  # one big vehicle close to the camera is not congestion
        return "free" if vehicles < 2 or occupancy < 0.5 else "moderate"
    if occupancy > 0.45 or vehicles >= 25:
        return "jam"
    if occupancy > 0.25 or vehicles >= 14:
        return "heavy"
    if occupancy > 0.10 or vehicles >= 6:
        return "moderate"
    return "free"


def static_beacon_score(img: np.ndarray, box) -> float:
    """A lit light bar on the roof: bright saturated red and/or blue in the top band of the vehicle,
    but not so much that it is simply red or blue paint."""
    smp = beacon_sample(img, box)
    if not smp:
        return 0.0
    r, b = smp
    if r > 0.25 or b > 0.25:
        return 0.0
    score = float(np.clip(max(r, b) / 0.025, 0, 1)) * 0.6
    if r > 0.004 and b > 0.004:  # red AND blue lights: the typical Indian ambulance light bar
        score += 0.35
    return float(min(1.0, score))


def analyze_photo(img: np.ndarray, detector: BaseDetector, conf: float = 0.3, detect: str = "all",
                  amb_model: Optional[BaseDetector] = None, ambulance: bool = True, imgsz: int = 960,
                  roi: Optional[list] = None, text_reader=None, classifier=None) -> tuple[np.ndarray, dict]:
    t0 = time.perf_counter()
    h, w = img.shape[:2]
    if max(h, w) > MAX_SIDE:
        s = MAX_SIDE / max(h, w)
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
    try:
        dets: list[Det] = detector.detect(img, imgsz=imgsz)
    except TypeError:
        dets = detector.detect(img)
    dets = [d for d in dets if d.conf >= conf]
    # one box per vehicle: at high resolution the model sometimes reports the same bus/truck twice
    # (two classes or two overlapping boxes) – keep the most confident
    kept: list[Det] = []
    for d in sorted(dets, key=lambda x: -(x.conf + (0.3 if x.cls == "ambulance" else 0.0))):
        if d.cls in VEHICLE_CLASSES and any(k.cls in VEHICLE_CLASSES and iou(k.box, d.box) > 0.5 for k in kept):
            continue
        kept.append(d)
    dets = remove_wrapping_boxes(kept)
    motion = detector.name == "motion"
    vehicles = [d for d in dets if d.cls in VEHICLE_CLASSES]
    people = [d for d in dets if d.cls == "person"]
    others = [d for d in dets if d.cls not in VEHICLE_CLASSES and d.cls != "person"]

    # region of interest (fractions) for occupancy – default: whole photo
    if roi and len(roi) >= 3:
        poly = np.array([[x * w, y * h] for x, y in roi], np.float32)
    else:
        poly = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32)
    mask = np.zeros((h, w), np.uint8)
    cv2.fillPoly(mask, [poly.astype(np.int32)], 1)
    roi_area = max(1.0, float(mask.sum()))
    cover = np.zeros((h, w), np.uint8)
    for d in vehicles:
        x1, y1, x2, y2 = (int(round(v)) for v in d.box)
        cover[max(0, y1):max(0, y2), max(0, x1):max(0, x2)] = 1
    occupancy = float((cover & mask).sum()) / roi_area

    # ambulance check per vehicle
    model_dets: list[Det] = [d for d in dets if d.cls in AMBULANCE_NAMES]
    model_used = False
    if ambulance and amb_model is not None and not motion and any(d.cls in CANDIDATE_CLASSES for d in vehicles):
        try:
            model_dets += [d for d in amb_model.detect(img) if d.cls in AMBULANCE_NAMES and d.conf >= 0.25]
            model_used = True
        except Exception:
            pass
    # text on vehicles ("AMBULANCE", mirrored on bonnets, "108" …) – the strongest cue for Indian ambulances
    text_ev: dict[int, tuple[float, str]] = {}
    text_used = False
    if ambulance and text_reader is not None and not motion:
        cands = [d for d in vehicles if d.cls not in ("motorcycle", "bicycle")]
        if cands:
            try:
                for d, ev in zip(cands, text_reader.score_vehicles(img, [d.box for d in cands])):
                    text_ev[id(d)] = ev
                text_used = bool(getattr(text_reader, "available", True))
            except Exception:
                pass
    # trained vehicle classifier: ambulance probability and Indian vehicle types for every vehicle box
    type_probs: dict[int, dict] = {}
    if classifier is not None and not motion:
        cv_ = [d for d in vehicles if d.cls != "bicycle"]
        try:
            for d, p in zip(cv_, classifier.classify_boxes(img, [d.box for d in cv_])):
                if p:
                    type_probs[id(d)] = p
                    top = max(p, key=p.get)
                    if top == "auto_rickshaw" and p[top] > 0.55 and d.cls in ("car", "motorcycle", "truck", "van"):
                        d.cls = "auto_rickshaw"  # COCO has no auto-rickshaw class
                    elif top == "emergency_other" and p[top] > 0.7 and d.cls in ("car", "truck", "van", "bus"):
                        d.cls = "fire_or_police"
        except Exception:
            type_probs = {}
    # lit light bar: a property of one vehicle, not of the scene. In rain, at night or in a jam the brake lights of
    # the vehicle ahead put red into many boxes, so with 3+ vehicles the scene's median score is subtracted.
    beacons: dict[int, float] = {}
    if ambulance:
        cand = [d for d in vehicles if d.cls in CANDIDATE_CLASSES or d.cls in AMBULANCE_NAMES]
        raw = {id(d): static_beacon_score(img, d.box) for d in cand}
        base = float(np.median(list(raw.values()))) if len(raw) >= 3 else 0.0
        beacons = {k: max(0.0, v - base) for k, v in raw.items()}
    items = []
    ambulances = []
    for d in dets:
        item = {"cls": d.cls, "conf": round(float(d.conf), 3), "box": [round(float(v), 1) for v in d.box],
                "road_user": d.cls in VEHICLE_CLASSES or d.cls == "person"}
        if ambulance and d.cls in VEHICLE_CLASSES and (d.cls in CANDIDATE_CLASSES or d.cls in AMBULANCE_NAMES):
            if d.cls == "ambulance":  # the trained traffic model names ambulances itself
                model_dets.append(d)
            m = max([md.conf for md in model_dets if md is d or iou(md.box, d.box) >= 0.3] or [0.0])
            beacon = beacons.get(id(d), static_beacon_score(img, d.box))
            livery = livery_score(img, d.box)
            txt, word = text_ev.get(id(d), (0.0, ""))
            probs = type_probs.get(id(d))
            p_cls = probs.get("ambulance", 0.0) if probs else None
            score, reasons = ambulance_probability(p_cls, txt, beacon, livery, m)
            if word and txt > 0.3:
                reasons = [f"text '{word}'" if r == "'AMBULANCE' text" else r for r in reasons]
            thr = fusion_threshold() if probs else 0.5
            item.update(ambulance_score=round(float(score), 2), ambulance=bool(score >= thr), reasons=reasons)
            if probs:
                item["type_probs"] = {k: round(v, 3) for k, v in sorted(probs.items(), key=lambda kv: -kv[1])[:3]}
            if score >= thr:
                ambulances.append(item)
        items.append(item)

    by_class: dict[str, int] = {}
    for d in vehicles:
        k = "ambulance" if any(a["box"] == [round(float(v), 1) for v in d.box] for a in ambulances) else d.cls
        by_class[k] = by_class.get(k, 0) + 1
    objects: dict[str, int] = {}
    for d in dets:
        objects[d.cls] = objects.get(d.cls, 0) + 1
    level = _congestion(occupancy, len(vehicles))
    ms = (time.perf_counter() - t0) * 1000

    # ---- annotated image
    out = img.copy()
    thick = max(2, int(round(max(w, h) / 900)))
    fs = max(0.45, max(w, h) / 2000)
    if roi and len(roi) >= 3:
        cv2.polylines(out, [poly.astype(np.int32)], True, (255, 160, 40), thick)
    for it in sorted(items, key=lambda x: x.get("ambulance", False)):
        x1, y1, x2, y2 = (int(v) for v in it["box"])
        if it.get("ambulance"):
            col, label, t = (0, 0, 255), f"AMBULANCE {it['ambulance_score']:.2f}", thick * 2
        elif it["cls"] in VEHICLE_CLASSES:
            col, label, t = _color(it["cls"]), f"{it['cls']} {it['conf']:.2f}", thick
        elif detect == "vehicles":
            continue
        elif it["cls"] == "person":
            col, label, t = (200, 200, 255), f"person {it['conf']:.2f}", max(1, thick - 1)
        elif detect == "road":
            continue
        else:
            col, label, t = (190, 190, 190), f"{it['cls']} {it['conf']:.2f}", 1
        cv2.rectangle(out, (x1, y1), (x2, y2), col, t)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, fs, 1)
        cv2.rectangle(out, (x1, max(0, y1 - th - 8)), (x1 + tw + 8, y1), col, -1)
        cv2.putText(out, label, (x1 + 4, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, fs, (20, 20, 20), 1, cv2.LINE_AA)
    rows = [f"vehicles {len(vehicles)}  people {len(people)}  other objects {len(others)}",
            f"occupancy {occupancy * 100:.0f}%  congestion {level}" + (f"  AMBULANCE x{len(ambulances)}" if ambulances else "")]
    ph = int(26 * fs / 0.5)
    cv2.rectangle(out, (8, 8), (8 + int(560 * fs / 0.5), 14 + ph * len(rows)), (15, 15, 20), -1)
    cv2.rectangle(out, (8, 8), (14, 14 + ph * len(rows)), CONGESTION_COLORS.get(level, (200, 200, 200)), -1)
    for i, r in enumerate(rows):
        cv2.putText(out, r, (22, 8 + ph * (i + 1) - 4), cv2.FONT_HERSHEY_SIMPLEX, fs, (240, 240, 240), 1, cv2.LINE_AA)
    if ambulances:
        cv2.rectangle(out, (0, 0), (w - 1, h - 1), (0, 0, 255), thick * 3)

    result = {
        "width": w, "height": h, "detector": detector.name, "ms": round(ms),
        "vehicles": len(vehicles), "people": len(people), "other_objects": len(others),
        "vehicles_by_class": by_class, "objects": objects, "occupancy": round(occupancy, 3), "congestion": level,
        "ambulances": len(ambulances), "ambulance_model": model_used, "text_check": text_used,
        "classifier": bool(type_probs),
        "ambulance_details": [{"score": a["ambulance_score"], "reasons": a["reasons"], "box": a["box"]} for a in ambulances],
        "detections": items,
    }
    return out, result
