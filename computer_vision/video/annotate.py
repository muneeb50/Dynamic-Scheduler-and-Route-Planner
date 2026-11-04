"""Draw detections, tracks, the ROI, the counting line and a stats panel on a frame."""
from __future__ import annotations

import zlib
from typing import Optional

import cv2
import numpy as np

from .analytics import FrameStats, TrafficAnalyzer
from .detectors import EMERGENCY_CLASSES
from .tracker import Track

CLASS_COLORS = {  # BGR
    "car": (255, 170, 60), "motorcycle": (60, 200, 255), "bus": (200, 90, 220), "truck": (80, 120, 255),
    "bicycle": (120, 255, 120), "auto_rickshaw": (0, 220, 220), "vehicle": (255, 200, 0),
}
CONGESTION_COLORS = {"free": (80, 200, 80), "moderate": (0, 210, 255), "heavy": (0, 140, 255), "jam": (60, 60, 230)}


def _color(cls: str) -> tuple[int, int, int]:
    if cls in CLASS_COLORS:
        return CLASS_COLORS[cls]
    h = zlib.crc32(cls.encode())
    return (h & 255, (h >> 8) & 255, (h >> 16) & 255)


def annotate(frame: np.ndarray, tracks: list[Track], stats: Optional[FrameStats], analyzer: TrafficAnalyzer,
             info: str = "", road_ids: Optional[set] = None) -> np.ndarray:
    img = frame.copy()
    h, w = img.shape[:2]
    roi, line = analyzer._geometry(img.shape)
    overlay = img.copy()
    cv2.fillPoly(overlay, [roi.astype(np.int32)], (255, 160, 40))
    img = cv2.addWeighted(overlay, 0.12, img, 0.88, 0)
    cv2.polylines(img, [roi.astype(np.int32)], True, (255, 160, 40), 2)
    (ax, ay), (bx, by) = line
    cv2.line(img, (int(ax), int(ay)), (int(bx), int(by)), (40, 40, 255), 2)
    cv2.putText(img, "count line", (int(ax) + 6, int(ay) - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (40, 40, 255), 1, cv2.LINE_AA)
    thick = max(1, int(round(w / 640)))
    for tr in tracks:
        if not tr.confirmed:
            continue
        x1, y1, x2, y2 = (int(v) for v in tr.box)
        if road_ids is not None and tr.id not in road_ids:  # other objects (shown in "all objects" mode)
            cv2.rectangle(img, (x1, y1), (x2, y2), (190, 190, 190), 1)
            label = f"{tr.cls} {tr.conf:.2f}"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)
            cv2.rectangle(img, (x1, max(0, y1 - th - 6)), (x1 + tw + 6, y1), (190, 190, 190), -1)
            cv2.putText(img, label, (x1 + 3, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (20, 20, 20), 1, cv2.LINE_AA)
            continue
        emergency = tr.cls in EMERGENCY_CLASSES
        color = (0, 0, 255) if emergency else _color(tr.cls)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2 * thick if (tr.stopped or emergency) else thick)
        if tr.stopped:
            cv2.rectangle(img, (x1 - 3, y1 - 3), (x2 + 3, y2 + 3), (0, 0, 230), 1)
        if tr.is_ambulance:
            label = f"#{tr.id} AMBULANCE {tr.amb_score:.2f}" + (" PASSED" if tr.passed else " IN ZONE" if tr.in_zone else "")
        else:
            label = f"#{tr.id} {tr.cls} {tr.conf:.2f}" + (" STOP" if tr.stopped else "")
            if tr.amb_score > 0.25:
                label += f" amb?{tr.amb_score:.2f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)
        cv2.rectangle(img, (x1, max(0, y1 - th - 6)), (x1 + tw + 6, y1), color, -1)
        cv2.putText(img, label, (x1 + 3, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (20, 20, 20), 1, cv2.LINE_AA)
        pts = [(int(x), int(y)) for _, x, y in list(tr.history)[-15:]]
        for p, q in zip(pts, pts[1:]):
            cv2.line(img, p, q, color, 1)
    if stats is not None:
        rows = [
            f"vehicles in zone: {stats.vehicles}",
            f"queue (stopped): {stats.queue}" + (f"  ~{stats.queue_length_m:.0f} m" if stats.queue_length_m else ""),
            f"crossed line: {stats.crossings_total}   flow: {stats.flow_per_min:.0f}/min",
            f"congestion: {stats.congestion.upper()}  occupancy {stats.occupancy * 100:.0f}%",
        ]
        if info:
            rows.append(info)
        pw = 330
        cv2.rectangle(img, (8, 8), (8 + pw, 14 + 20 * len(rows)), (15, 15, 20), -1)
        cv2.rectangle(img, (8, 8), (14, 14 + 20 * len(rows)), CONGESTION_COLORS.get(stats.congestion, (200, 200, 200)), -1)
        for i, r in enumerate(rows):
            cv2.putText(img, r, (20, 26 + 20 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (240, 240, 240), 1, cv2.LINE_AA)
    if any(tr.cls in EMERGENCY_CLASSES and tr.confirmed for tr in tracks):
        cv2.rectangle(img, (0, 0), (w - 1, h - 1), (0, 0, 255), 6)
        cv2.putText(img, "AMBULANCE DETECTED", (w - 270, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2, cv2.LINE_AA)
        amb = next(tr for tr in tracks if tr.cls in EMERGENCY_CLASSES and tr.confirmed)
        if amb.amb_reasons:
            cv2.putText(img, ", ".join(amb.amb_reasons)[:60], (w - 420, 54), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 1, cv2.LINE_AA)
    return img
