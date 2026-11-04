"""Vehicle detectors for real video.

Three interchangeable back-ends, all returning the same `Det` objects:

* `yolo`        – Ultralytics YOLO (PyTorch). Best accuracy; default when installed.
                  Weights (yolo11n.pt, ~5 MB) download automatically on first use.
* `opencv-dnn`  – the same YOLO network exported to ONNX and run by OpenCV's DNN
                  module (no PyTorch needed at run time).
                  Export once with: python computer_vision/tools/export_onnx.py
* `motion`      – classical OpenCV background subtraction (MOG2) + contours.
                  Works with no ML model at all, but only for a fixed camera and it
                  cannot tell vehicle types apart (everything is "vehicle").

`create_detector("auto")` picks the best one available.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger("resqroute.video")

MODELS_DIR = Path(__file__).resolve().parents[1] / "models"
DEFAULT_WEIGHTS = "yolo11n.pt"

# COCO classes that matter on the road. Auto-rickshaws are not a COCO class: a stock
# model usually reports them as car / motorcycle / truck – fine-tune (computer_vision/train)
# to add "auto_rickshaw", "ambulance", etc.
VEHICLE_CLASSES = {"car", "motorcycle", "bus", "truck", "bicycle", "auto_rickshaw", "autorickshaw", "rickshaw",
                   "auto rickshaw", "van", "ambulance", "fire_truck", "fire truck", "police_car", "vehicle", "fire_or_police"}
OPEN_VOCAB_WEIGHTS = "yoloe-11s-traffic.pt"
# YOLO11-S fine-tuned for ResQRoute (computer_vision/train): car, motorcycle, bus, truck, auto_rickshaw, bicycle,
# person, ambulance – trained on ambulance photos + Indian / South-Asian traffic + crowded-scene composites
TRAFFIC_WEIGHTS = "resq-traffic.pt"
LARGE_WEIGHTS = "yolo11m.pt"  # bigger COCO model used in the photo ensemble when present  # YOLOE with a traffic vocabulary incl. ambulance & auto rickshaw
EMERGENCY_CLASSES = {"ambulance", "fire_truck", "fire truck", "fire_engine", "police_car", "police car", "emergency"}

COCO80 = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
    "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]


@dataclass
class Det:
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 in pixels
    cls: str
    conf: float


class BaseDetector:
    name = "base"
    #: classes kept by default (None = keep everything)
    keep: Optional[set[str]] = VEHICLE_CLASSES

    def __init__(self, conf: float = 0.3, keep_classes: Optional[set[str]] = None):
        self.conf = conf
        if keep_classes is not None:
            self.keep = keep_classes
        self.lock = threading.Lock()  # one model instance can be shared by several sources

    def _filter(self, dets: list[Det]) -> list[Det]:
        return [d for d in dets if d.conf >= self.conf and (self.keep is None or d.cls in self.keep)]

    def detect(self, frame: np.ndarray) -> list[Det]:  # pragma: no cover - abstract
        raise NotImplementedError

    def info(self) -> dict:
        return {"name": self.name, "conf": self.conf}


# --------------------------------------------------------------------------- YOLO (PyTorch)
def ultralytics_available() -> bool:
    try:
        import ultralytics  # noqa: F401

        return True
    except Exception:
        return False


def resolve_weights(weights: Optional[str]) -> str:
    """Local path for the weights, downloading the official file into computer_vision/models if needed."""
    weights = weights or DEFAULT_WEIGHTS
    p = Path(weights)
    if p.exists():
        return str(p)
    local = MODELS_DIR / p.name
    if local.exists():
        return str(local)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from ultralytics.utils.downloads import attempt_download_asset

        return str(attempt_download_asset(local))
    except Exception as exc:  # offline or custom name: let YOLO() try / fail with its own message
        log.warning("could not pre-download %s: %s", weights, exc)
        return weights


_DEVICE: Optional[str] = None


def best_device() -> str:
    """cuda:0 (NVIDIA) > mps (Apple silicon) > cpu. Override with the YOLO_DEVICE environment variable."""
    global _DEVICE
    if _DEVICE is None:
        import os

        _DEVICE = os.environ.get("YOLO_DEVICE", "")
        if not _DEVICE:
            try:
                import torch

                if torch.cuda.is_available():
                    _DEVICE = "cuda:0"
                elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                    _DEVICE = "mps"
                else:
                    _DEVICE = "cpu"
                    torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))
            except Exception:
                _DEVICE = "cpu"
        log.info("YOLO device: %s", _DEVICE)
    return _DEVICE


class UltralyticsYolo(BaseDetector):
    name = "yolo"

    @property
    def label(self) -> str:
        return "resq" if TRAFFIC_WEIGHTS in str(self.weights) else self.name

    def __init__(self, weights: Optional[str] = None, conf: float = 0.3, imgsz: int = 640,
                 keep_classes: Optional[set[str]] = None, device: Optional[str] = None):
        super().__init__(conf, keep_classes)
        from ultralytics import YOLO

        self.weights = resolve_weights(weights)
        self.model = YOLO(self.weights)
        self.imgsz = imgsz
        self.device = device or best_device()
        self.half = self.device.startswith("cuda")  # FP16 on NVIDIA GPUs: ~2× faster
        self.names = self.model.names

    def detect(self, frame: np.ndarray, imgsz: Optional[int] = None) -> list[Det]:
        with self.lock:
            extra = {"half": True} if self.half else {}
            res = self.model.predict(frame, conf=max(0.05, self.conf * 0.8), imgsz=imgsz or self.imgsz, verbose=False,
                                     device=self.device, **extra)[0]
        out = []
        boxes = res.boxes
        if boxes is not None and len(boxes):
            xyxy = boxes.xyxy.cpu().numpy()
            cls = boxes.cls.cpu().numpy().astype(int)
            conf = boxes.conf.cpu().numpy()
            for b, c, s in zip(xyxy, cls, conf):
                out.append(Det(tuple(float(v) for v in b), str(self.names[int(c)]), float(s)))
        return self._filter(out)

    def info(self) -> dict:
        return {"name": self.name, "conf": self.conf, "weights": Path(self.weights).name, "imgsz": self.imgsz,
                "classes": len(self.names), "device": self.device}


# ------------------------------------------------------------------------- YOLO via OpenCV
class OpenCvDnnYolo(BaseDetector):
    """YOLOv8/YOLO11 ONNX export run with cv2.dnn (output shape 1 x (4+C) x N)."""

    name = "opencv-dnn"

    def __init__(self, onnx_path: Optional[str] = None, conf: float = 0.3, nms: float = 0.45, imgsz: int = 640,
                 names: Optional[list[str]] = None, keep_classes: Optional[set[str]] = None):
        super().__init__(conf, keep_classes)
        path = Path(onnx_path) if onnx_path else MODELS_DIR / DEFAULT_WEIGHTS.replace(".pt", ".onnx")
        if not path.exists():
            raise FileNotFoundError(f"{path} not found – run: python computer_vision/tools/export_onnx.py")
        self.path = path
        self.net = cv2.dnn.readNetFromONNX(str(path))
        self.nms = nms
        self.imgsz = imgsz
        names_file = path.with_suffix(".names")
        if not names_file.exists():
            names_file = MODELS_DIR / "yolo11n.names"
        self.names = names or (names_file.read_text().splitlines() if names_file.exists() else COCO80)
        # a smaller static export (computer_vision/models/yolo11n-416.onnx) for the fast setting
        self._small = None
        small = path.with_name(path.stem + "-416.onnx")
        if small.exists():
            self._small = cv2.dnn.readNetFromONNX(str(small))

    def detect(self, frame: np.ndarray, imgsz: Optional[int] = None) -> list[Det]:
        h, w = frame.shape[:2]
        use_small = self._small is not None and imgsz is not None and imgsz < 600
        size = 416 if use_small else self.imgsz
        net = self._small if use_small else self.net
        s = size / max(h, w)
        nh, nw = int(round(h * s)), int(round(w * s))
        canvas = np.full((size, size, 3), 114, np.uint8)  # letterbox
        canvas[:nh, :nw] = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
        blob = cv2.dnn.blobFromImage(canvas, 1 / 255.0, (size, size), swapRB=True, crop=False)
        with self.lock:
            net.setInput(blob)
            out = net.forward()
        pred = np.squeeze(out, 0)
        if pred.shape[0] < pred.shape[1]:  # (4+C, N) -> (N, 4+C)
            pred = pred.T
        scores = pred[:, 4:]
        cls_ids = scores.argmax(1)
        confs = scores[np.arange(len(scores)), cls_ids]
        mask = confs >= max(0.05, self.conf * 0.8)
        pred, cls_ids, confs = pred[mask], cls_ids[mask], confs[mask]
        if not len(pred):
            return []
        cx, cy, bw, bh = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
        boxes = np.stack([(cx - bw / 2) / s, (cy - bh / 2) / s, bw / s, bh / s], 1)
        idx = cv2.dnn.NMSBoxes(boxes.tolist(), confs.tolist(), self.conf * 0.8, self.nms)
        dets = []
        for i in np.array(idx).flatten():
            x, y, bw_, bh_ = boxes[i]
            name = self.names[int(cls_ids[i])] if int(cls_ids[i]) < len(self.names) else str(int(cls_ids[i]))
            dets.append(Det((max(0.0, x), max(0.0, y), min(float(w), x + bw_), min(float(h), y + bh_)), name,
                            float(confs[i])))
        return self._filter(dets)

    def info(self) -> dict:
        return {"name": self.name, "conf": self.conf, "weights": self.path.name, "imgsz": self.imgsz}


# --------------------------------------------------------------------- classical OpenCV
class MotionDetector(BaseDetector):  # noqa: D101
    """MOG2 background subtraction. Stationary vehicles fade into the background after
    `history` frames, so long queues are under-counted – use YOLO for queue measurement."""

    name = "motion"
    keep = None

    def __init__(self, conf: float = 0.3, min_area_frac: float = 0.0015, history: int = 500):
        super().__init__(conf)
        self.sub = cv2.createBackgroundSubtractorMOG2(history=history, varThreshold=32, detectShadows=True)
        self.min_area_frac = min_area_frac

    def detect(self, frame: np.ndarray, imgsz: Optional[int] = None) -> list[Det]:
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (w // 2, h // 2)) if w > 960 else frame
        sc = w / small.shape[1]
        with self.lock:
            mask = self.sub.apply(small)
        mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)[1]  # drop shadows (value 127)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        min_area = self.min_area_frac * small.shape[0] * small.shape[1]
        out = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < min_area:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            fill = area / float(bw * bh)
            out.append(Det((x * sc, y * sc, (x + bw) * sc, (y + bh) * sc), "vehicle", round(min(0.99, 0.4 + fill / 2), 2)))
        return out


# ------------------------------------------------------------------------------ factory
def onnx_available(onnx_path: Optional[str] = None) -> bool:
    p = Path(onnx_path) if onnx_path else MODELS_DIR / DEFAULT_WEIGHTS.replace(".pt", ".onnx")
    return p.exists()


def available_detectors() -> dict:
    return {
        "yolo": ultralytics_available(),
        "opencv-dnn": onnx_available(),
        "yoloe": ultralytics_available() and (MODELS_DIR / OPEN_VOCAB_WEIGHTS).exists(),
        "resq": ultralytics_available() and (MODELS_DIR / TRAFFIC_WEIGHTS).exists(),
        "motion": True,
        "weights_present": (MODELS_DIR / DEFAULT_WEIGHTS).exists(),
    }


def create_detector(kind: str = "auto", weights: Optional[str] = None, conf: float = 0.3,
                    keep_classes: Optional[set[str]] = None) -> BaseDetector:
    if kind == "auto":
        if ultralytics_available():
            try:
                if weights is None and (MODELS_DIR / TRAFFIC_WEIGHTS).exists():
                    return UltralyticsYolo(str(MODELS_DIR / TRAFFIC_WEIGHTS), conf, keep_classes=keep_classes)
                return UltralyticsYolo(weights, conf, keep_classes=keep_classes)
            except Exception as exc:  # e.g. offline and weights missing
                log.warning("YOLO unavailable (%s); trying OpenCV DNN / motion", exc)
        if onnx_available(weights if weights and weights.endswith(".onnx") else None):
            return OpenCvDnnYolo(weights if weights and weights.endswith(".onnx") else None, conf,
                                 keep_classes=keep_classes)
        return MotionDetector(conf)
    if kind == "yolo":
        return UltralyticsYolo(weights, conf, keep_classes=keep_classes)
    if kind == "resq":  # the trained traffic model (ambulance, auto-rickshaw …)
        return UltralyticsYolo(weights or str(MODELS_DIR / TRAFFIC_WEIGHTS), conf, keep_classes=keep_classes)
    if kind == "yoloe":  # open-vocabulary: also detects ambulances and auto-rickshaws directly
        return UltralyticsYolo(weights or OPEN_VOCAB_WEIGHTS, conf, keep_classes=keep_classes)
    if kind == "opencv-dnn":
        return OpenCvDnnYolo(weights if weights and weights.endswith(".onnx") else None, conf,
                             keep_classes=keep_classes)
    if kind == "motion":
        return MotionDetector(conf)
    raise ValueError(f"unknown detector '{kind}' (auto | resq | yolo | yoloe | opencv-dnn | motion)")
