"""Accurate detection for crowded photos, and the text cue for ambulances.

* ``EnsembleDetector`` – runs several detectors (the ResQRoute model trained on Indian traffic + ambulances,
  a larger COCO YOLO) on the whole photo and on overlapping tiles (small, distant and partly hidden
  vehicles become big enough to detect), then merges everything with weighted box fusion:
  one box per vehicle, the class decided by a confidence-weighted vote.
  Boxes that wrap several vehicles at once (a classic error in dense traffic) are removed.
* ``AmbulanceText`` – reads text on vehicles with OCR (RapidOCR, runs offline). "AMBULANCE" (also mirrored, as
  painted on bonnets), "108", "102", "EMERGENCY" on a vehicle is very strong evidence for an Indian ambulance.
"""
from __future__ import annotations

import difflib
import logging
import re
import threading
from typing import Optional

import numpy as np

from .detectors import VEHICLE_CLASSES, BaseDetector, Det

log = logging.getLogger("resqroute.video")


def _iou(a, b) -> float:
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0:
        return 0.0
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-9)


def _cover(a, b) -> float:
    """share of box a inside box b"""
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    return inter / max(1e-9, (a[2] - a[0]) * (a[3] - a[1]))


# class names of different models -> one vocabulary
CANON = {"auto rickshaw": "auto_rickshaw", "autorickshaw": "auto_rickshaw", "rickshaw": "auto_rickshaw",
         "van": "car", "tempo": "truck", "fire truck": "truck", "police car": "car"}


def canon(c: str) -> str:
    return CANON.get(c, c)


def weighted_box_fusion(groups: list[tuple[list[Det], float]], iou_thr: float = 0.55, n_sources=1,
                        prefer: Optional[dict] = None) -> list[Det]:
    """groups: [(detections of one model/tile pass, weight)]. Vehicles fuse class-agnostically (the class is voted),
    other objects fuse per class. `prefer` = {class: min_conf} classes that win the vote when a model is that sure
    (e.g. ambulance from the trained model)."""
    items = []
    for gi, (dets, w) in enumerate(groups):
        for d in dets:
            items.append((d, w, gi))
    items.sort(key=lambda t: -t[0].conf * t[1])
    clusters: list[dict] = []
    for d, w, gi in items:
        c = canon(d.cls)
        veh = c in VEHICLE_CLASSES
        best, bi = None, iou_thr
        for cl in clusters:
            if cl["veh"] != veh or (not veh and cl["cls"] != c):
                continue
            v = _iou(cl["box"], d.box)
            if v > bi:
                best, bi = cl, v
        if best is None:
            clusters.append({"veh": veh, "cls": c, "box": list(d.box), "members": [(d, w, c)], "groups": {gi}})
        else:
            best["members"].append((d, w, c))
            best["groups"].add(gi)
            ws = np.array([m[0].conf * m[1] for m in best["members"]])
            bx = np.array([m[0].box for m in best["members"]], dtype=np.float64)
            best["box"] = list((bx * ws[:, None]).sum(0) / ws.sum())
    out = []
    for cl in clusters:
        votes: dict[str, float] = {}
        for d, w, c in cl["members"]:
            votes[c] = votes.get(c, 0.0) + d.conf * w
        cls = max(votes, key=votes.get)
        for pc, thr in (prefer or {}).items():
            if any(c == pc and d.conf >= thr for d, w, c in cl["members"]):
                cls = pc
        sources = len(cl["groups"])
        top = max(d.conf for d, _, _ in cl["members"])
        mean = sum(d.conf * w for d, w, _ in cl["members"]) / max(1e-9, sum(w for _, w, _ in cl["members"]))
        expected = n_sources(cl["box"]) if callable(n_sources) else n_sources  # passes that could have found it
        conf = min(0.99, 0.6 * top + 0.4 * mean) * min(1.0, 0.6 + 0.4 * sources / max(1, expected))
        out.append(Det(tuple(float(v) for v in cl["box"]), cls, float(conf)))
    return out


def remove_wrapping_boxes(dets: list[Det]) -> list[Det]:
    """Drop a vehicle box that mostly consists of two or more other vehicles (one box over several cars)."""
    veh = [d for d in dets if d.cls in VEHICLE_CLASSES]
    drop = set()
    for d in veh:
        inner = [o for o in veh if o is not d and _cover(o.box, d.box) > 0.8
                 and (o.box[2] - o.box[0]) * (o.box[3] - o.box[1]) < 0.75 * (d.box[2] - d.box[0]) * (d.box[3] - d.box[1])]
        if len(inner) >= 2:
            area = (d.box[2] - d.box[0]) * (d.box[3] - d.box[1])
            covered = sum((o.box[2] - o.box[0]) * (o.box[3] - o.box[1]) for o in inner)
            if covered > 0.55 * area and d.cls != "ambulance":
                drop.add(id(d))
    return [d for d in dets if id(d) not in drop]


def tiles(w: int, h: int, n: int = 2, overlap: float = 0.25) -> list[tuple[int, int, int, int]]:
    tw, th = int(w / (n - (n - 1) * overlap)), int(h / (n - (n - 1) * overlap))
    xs = [int(i * tw * (1 - overlap)) for i in range(n)]
    ys = [int(i * th * (1 - overlap)) for i in range(n)]
    return [(x, y, min(w, x + tw), min(h, y + th)) for y in ys for x in xs]


class EnsembleDetector(BaseDetector):
    """Several models × several scales (+ tiles), fused. For photos, where accuracy matters more than speed.

    Scale-aware: every pass is responsible for the object sizes it sees well. The native-scale pass (640 px) keeps
    every box – close-up and mid-size vehicles. High-resolution passes and tiles only contribute *small* boxes
    (distant vehicles in crowded scenes); their large boxes are often fragments of a close vehicle and are dropped.
    An image is never upsampled beyond its own size, which degrades detection of vehicles that fill the frame.
    """

    name = "ensemble"
    keep = None
    SMALL = 0.33  # high-res passes / tiles: keep boxes whose longest side is < this share of the image

    def __init__(self, members: list[tuple], tile_grid: int = 2, conf: float = 0.2):
        """members: (detector, weight, full-image input size[, "all" | "small"]). Without the 4th field a member at
        <= 704 px keeps all boxes and a larger one keeps small boxes only."""
        super().__init__(conf)
        self.members = [tuple(m) + (("all" if m[2] <= 704 else "small"),) if len(m) == 3 else tuple(m) for m in members]
        self.tile_grid = tile_grid

    def detect(self, frame: np.ndarray, imgsz: Optional[int] = None) -> list[Det]:
        h, w = frame.shape[:2]
        side = max(w, h)
        native = max(320, int(np.ceil(side / 32) * 32))

        def small(b, W, H):
            return max((b[2] - b[0]) / W, (b[3] - b[1]) / H) < self.SMALL

        groups = []
        ran = set()
        for det, wt, size, role in self.members:
            size = min(size, native) if role == "small" else size
            if role == "small" and (id(det), size) in ran:
                continue  # image too small for a separate high-res pass
            ran.add((id(det), size))
            ds = [d for d in det.detect(frame, imgsz=size) if d.conf >= self.conf]
            if role == "small":
                ds = [d for d in ds if small(d.box, w, h)]
            groups.append((ds, wt))
        if self.tile_grid > 1 and side >= 600:
            det, wt = self.members[0][0], self.members[0][1]
            for x1, y1, x2, y2 in tiles(w, h, self.tile_grid):
                sub = frame[y1:y2, x1:x2]
                td = []
                for d in det.detect(sub, imgsz=640):
                    if d.conf < self.conf:
                        continue
                    b = (d.box[0] + x1, d.box[1] + y1, d.box[2] + x1, d.box[3] + y1)
                    # boxes cut by the tile border are unreliable: the full-image pass covers those vehicles
                    edge = (d.box[0] < 3 and x1 > 0) or (d.box[1] < 3 and y1 > 0) or \
                           (d.box[2] > sub.shape[1] - 3 and x2 < w) or (d.box[3] > sub.shape[0] - 3 and y2 < h)
                    if not edge and small(b, w, h):
                        td.append(Det(b, d.cls, d.conf))
                groups.append((td, wt * 0.8))
        n_all = len(ran)
        n_big = sum(1 for m in self.members if m[3] == "all")

        def expected(b):  # large boxes can only come from the native-scale passes
            return min(n_all, 2) if small(b, w, h) else max(1, n_big)

        fused = weighted_box_fusion(groups, n_sources=expected, prefer={"ambulance": 0.4, "auto_rickshaw": 0.45})
        return remove_wrapping_boxes(fused)

    def info(self) -> dict:
        return {"name": self.name, "members": [m[0].info() for m in self.members], "tiles": self.tile_grid}


# ------------------------------------------------------------------------------------ OCR
_STRONG = ("AMBULANCE",)
_MEDIUM = ("EMERGENCY", "108", "102", "MEDICAL", "PATIENT", "ICU", "CARDIAC", "RESCUE")


def text_evidence(text: str) -> tuple[float, str]:
    """Score for one OCR'd string."""
    t = re.sub(r"[^A-Z0-9]", "", text.upper())
    if not t:
        return 0.0, ""
    for cand in (t, t[::-1]):  # mirrored writing reads backwards
        for s in _STRONG:
            if s in cand or (len(cand) >= 6 and difflib.SequenceMatcher(None, cand, s).ratio() >= 0.75):
                return 0.95, s
        for frag in ("MBULAN", "AMBULA", "BULANC", "ULANCE"):
            if frag in cand:
                return 0.9, "AMBULANCE"
    for s in _MEDIUM:
        if t == s or (len(s) > 3 and s in t):
            return (0.6 if s in ("108", "102") else 0.7), s
    return 0.0, ""


class AmbulanceText:
    """OCR on photos / vehicle crops (RapidOCR, ONNX, offline). Optional: missing package -> no text cue."""

    def __init__(self):
        self._ocr = None
        self._lock = threading.Lock()
        self.available = True

    def _engine(self):
        if self._ocr is None and self.available:
            try:
                from rapidocr_onnxruntime import RapidOCR

                self._ocr = RapidOCR()
            except Exception as exc:  # not installed
                log.info("OCR text cue unavailable: %s", exc)
                self.available = False
        return self._ocr

    def read(self, img: np.ndarray) -> list[tuple[tuple[float, float, float, float], str, float]]:
        """[(box, text, confidence)] for the image and its mirror image."""
        eng = self._engine()
        if eng is None:
            return []
        out = []
        with self._lock:
            for flip in (False, True):
                src = img[:, ::-1] if flip else img
                try:
                    res, _ = eng(np.ascontiguousarray(src))
                except Exception:
                    res = None
                w = img.shape[1]
                for pts, txt, c in res or []:
                    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                    x1, x2 = (w - max(xs), w - min(xs)) if flip else (min(xs), max(xs))
                    out.append(((float(x1), float(min(ys)), float(x2), float(max(ys))), str(txt), float(c)))
        return out

    def score_vehicles(self, img: np.ndarray, boxes: list) -> list[tuple[float, str]]:
        """Text evidence per vehicle box: text found inside the box (or just above it, e.g. a roof sign)."""
        found = [(b, *text_evidence(t), c) for b, t, c in self.read(img)]
        found = [f for f in found if f[1] > 0]
        res = []
        for vb in boxes:
            best, word = 0.0, ""
            vh = vb[3] - vb[1]
            grown = (vb[0] - 0.05 * (vb[2] - vb[0]), vb[1] - 0.15 * vh, vb[2] + 0.05 * (vb[2] - vb[0]), vb[3])
            for tb, sc, w, c in found:
                cx, cy = (tb[0] + tb[2]) / 2, (tb[1] + tb[3]) / 2
                if grown[0] <= cx <= grown[2] and grown[1] <= cy <= grown[3]:
                    # text width plausible for this vehicle (not a shop sign behind it)
                    if (tb[2] - tb[0]) <= 1.1 * (vb[2] - vb[0]):
                        s = sc * min(1.0, 0.5 + c)
                        if s > best:
                            best, word = s, w
            res.append((best, word))
        return res


_SHARED: Optional[AmbulanceText] = None


def shared_text_reader() -> Optional[AmbulanceText]:
    """One OCR engine for the whole backend (None if RapidOCR is not installed)."""
    global _SHARED
    if _SHARED is None:
        _SHARED = AmbulanceText()
        _SHARED._engine()
    return _SHARED if _SHARED.available else None

