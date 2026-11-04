"""Second-stage vehicle classifier: looks at each detected vehicle (its crop) and says what it is.

Classes: ambulance · fire_or_police · auto_rickshaw · bus · motorcycle · car

Trained by us on ~9,400 labelled vehicle crops – Indian traffic scenes with ambulances, auto-rickshaws,
buses, two-wheelers and cars, plus ambulance / fire / police photos (computer_vision/train/
build_ambulance_dataset.py, train_vehicle_classifier.py; accuracy in docs/ambulance_model.md).
The COCO detector finds *where* vehicles are, even in crowded scenes; this model decides *what* each
one is – COCO has no "ambulance" or "auto-rickshaw" class, which is why an ambulance used to be "car".

Weights: computer_vision/models/vehicle_cls.pt (PyTorch / Ultralytics) or vehicle_cls.onnx (OpenCV DNN,
no PyTorch needed).
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .detectors import MODELS_DIR

log = logging.getLogger("resqroute.video")
CLASSES = ["ambulance", "auto_rickshaw", "bus", "car", "emergency_other", "motorcycle"]  # Ultralytics sorts folder names
DISPLAY = {"emergency_other": "fire_or_police", "auto_rickshaw": "auto_rickshaw"}
PAD = 0.06
SIZE = 224


def classifier_path() -> Optional[Path]:
    for name in ("vehicle_cls.pt", "vehicle_cls.onnx"):
        p = MODELS_DIR / name
        if p.exists():
            return p
    return None


def pad_crop(img: np.ndarray, box) -> Optional[np.ndarray]:
    h, w = img.shape[:2]
    x1, y1, x2, y2 = box
    px, py = (x2 - x1) * PAD, (y2 - y1) * PAD
    x1, y1 = int(max(0, x1 - px)), int(max(0, y1 - py))
    x2, y2 = int(min(w, x2 + px)), int(min(h, y2 + py))
    if x2 - x1 < 12 or y2 - y1 < 12:
        return None
    return img[y1:y2, x1:x2]


class VehicleClassifier:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else classifier_path()
        if self.path is None:
            raise FileNotFoundError("vehicle classifier weights not found (computer_vision/models/vehicle_cls.pt)")
        self.lock = threading.Lock()
        meta = self.path.with_suffix(".json")
        self.classes = json.loads(meta.read_text())["classes"] if meta.exists() else CLASSES
        self.kind = "onnx" if self.path.suffix == ".onnx" else "torch"
        if self.kind == "torch":
            try:
                from ultralytics import YOLO

                self.model = YOLO(str(self.path), task="classify")
                self.classes = [self.model.names[i] for i in range(len(self.model.names))]
            except Exception:
                onnx = self.path.with_suffix(".onnx")
                if not onnx.exists():
                    raise
                self.path, self.kind = onnx, "onnx"
        if self.kind == "onnx":
            self.net = cv2.dnn.readNetFromONNX(str(self.path))

    def classify(self, crops: list[np.ndarray]) -> list[dict[str, float]]:
        """Class probabilities for each crop."""
        crops = [c for c in crops]
        if not crops:
            return []
        with self.lock:
            if self.kind == "torch":
                res = self.model.predict(crops, imgsz=SIZE, verbose=False)
                return [{self.classes[i]: float(p) for i, p in enumerate(r.probs.data.tolist())} for r in res]
            out = []
            for c in crops:  # Ultralytics classification preprocessing: resize short side + centre crop, RGB, /255
                h, w = c.shape[:2]
                s = SIZE / min(h, w)
                r = cv2.resize(c, (max(SIZE, round(w * s)), max(SIZE, round(h * s))), interpolation=cv2.INTER_LINEAR)
                y0, x0 = (r.shape[0] - SIZE) // 2, (r.shape[1] - SIZE) // 2
                r = r[y0:y0 + SIZE, x0:x0 + SIZE]
                blob = cv2.dnn.blobFromImage(r, 1 / 255.0, (SIZE, SIZE), swapRB=True)
                self.net.setInput(blob)
                p = self.net.forward().flatten()
                if p.min() < 0 or abs(p.sum() - 1) > 1e-3:  # logits -> softmax
                    p = np.exp(p - p.max())
                    p = p / p.sum()
                out.append({self.classes[i]: float(v) for i, v in enumerate(p)})
            return out

    def classify_boxes(self, img: np.ndarray, boxes: list, occlusion: bool = True) -> list[Optional[dict[str, float]]]:
        """Classify every vehicle box of one image. With occlusion=True, the parts of a crop covered by a vehicle
        *in front of it* (box reaching lower in the image = closer to the camera) are greyed out first, so in a
        crowded scene a car's crop is not classified by the ambulance overlapping it."""
        crops, idx = [], []
        for i, b in enumerate(boxes):
            c = occlusion_crop(img, boxes, i) if occlusion and len(boxes) > 1 else pad_crop(img, b)
            if c is not None:
                crops.append(c)
                idx.append(i)
        probs = self.classify(crops)
        out: list[Optional[dict]] = [None] * len(boxes)
        for i, p in zip(idx, probs):
            out[i] = p
        return out


def occlusion_crop(img: np.ndarray, boxes: list, i: int, max_masked: float = 0.5) -> Optional[np.ndarray]:
    """Padded crop of boxes[i] with the regions of overlapping vehicles in front of it filled with grey. If more than
    `max_masked` of the crop would be hidden, the plain crop is used (too little left to judge)."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = boxes[i]
    px, py = (x2 - x1) * PAD, (y2 - y1) * PAD
    cx1, cy1 = int(max(0, x1 - px)), int(max(0, y1 - py))
    cx2, cy2 = int(min(w, x2 + px)), int(min(h, y2 + py))
    if cx2 - cx1 < 12 or cy2 - cy1 < 12:
        return None
    mask = np.zeros((cy2 - cy1, cx2 - cx1), bool)
    for j, o in enumerate(boxes):
        if j == i or o[3] <= y2:  # only vehicles closer to the camera (lower bottom edge) occlude this one
            continue
        ix1, iy1, ix2, iy2 = max(cx1, int(o[0])), max(cy1, int(o[1])), min(cx2, int(o[2])), min(cy2, int(o[3]))
        if ix2 > ix1 and iy2 > iy1:
            mask[iy1 - cy1:iy2 - cy1, ix1 - cx1:ix2 - cx1] = True
    crop = img[cy1:cy2, cx1:cx2]
    frac = float(mask.mean())
    if frac == 0 or frac > max_masked:
        return crop
    crop = crop.copy()
    crop[mask] = 114
    return crop


_shared: Optional[VehicleClassifier] = None
_shared_lock = threading.Lock()
_failed = False


def shared_classifier() -> Optional[VehicleClassifier]:
    """One classifier instance for the whole process (None if the weights are missing)."""
    global _shared, _failed
    if _shared is None and not _failed:
        with _shared_lock:
            if _shared is None and not _failed:
                try:
                    _shared = VehicleClassifier()
                except Exception as exc:
                    _failed = True
                    log.warning("vehicle classifier unavailable: %s", exc)
    return _shared
