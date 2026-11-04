"""Telling ambulances apart from other vehicles.

A stock COCO detector sees an ambulance as a "truck" or "car". We combine three
independent cues per tracked vehicle and require them to agree over several frames:

1. Model evidence – an open-vocabulary YOLOE model with an "ambulance" class
   (computer_vision/models/yoloe-11s-traffic.pt, text embeddings baked in), or any
   custom-trained model that has an "ambulance" class. Runs every `model_every`
   processed frames on the full frame; detections are matched to tracks by IoU.
2. Beacon evidence – flashing red/blue emergency lights: the share of bright,
   saturated red and blue pixels in the light-bar region (top of the box) is
   tracked over ~2.5 s; strong *variation* (flashing), not just colour, scores.
   This mirrors optical pre-emption systems that detect an emitter's strobe.
3. Livery evidence (weak) – a mostly white body with high-saturation markings.

frame score = max(model evidence, 0.8·beacon + 0.2·livery)
track score = exponential moving average; "ambulance" once score ≥ threshold for
`min_frames` updates (hysteresis: released below threshold/2).
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .detectors import MODELS_DIR, BaseDetector, Det
from .ambulance_fusion import ambulance_probability, decision_score
from .tracker import Track, iou

log = logging.getLogger("resqroute.video")

AMBULANCE_MODEL = "yoloe-11s-traffic.pt"
CANDIDATE_CLASSES = {"car", "truck", "bus", "van", "vehicle", "ambulance", "auto rickshaw", "auto_rickshaw", "fire_or_police"}
AMBULANCE_NAMES = {"ambulance", "emergency", "ambulance_vehicle"}


@dataclass
class AmbulanceConfig:
    enabled: bool = True
    use_model: bool = True
    use_beacon: bool = True
    use_livery: bool = True
    model_every: int = 3
    use_text: bool = True  # OCR "AMBULANCE" / "108" on vehicles (needs rapidocr_onnxruntime)
    model_conf: float = 0.25
    threshold: float = 0.5
    min_frames: int = 3

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "AmbulanceConfig":
        c = cls()
        for k, v in (d or {}).items():
            if hasattr(c, k) and v is not None:
                setattr(c, k, v)
        return c


def ambulance_model_path() -> Optional[Path]:
    """The trained ResQRoute traffic model (has an 'ambulance' class) if present, else the open-vocabulary model."""
    from .detectors import TRAFFIC_WEIGHTS

    for name in (TRAFFIC_WEIGHTS, AMBULANCE_MODEL):
        p = MODELS_DIR / name
        if p.exists():
            return p
    return None


def _hsv_masks(region: np.ndarray):
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    bright = (s >= 110) & (v >= 170)
    red = bright & ((h <= 8) | (h >= 172))
    blue = bright & (h >= 95) & (h <= 130)
    return red, blue, hsv


def beacon_sample(frame: np.ndarray, box) -> Optional[tuple[float, float]]:
    """(red share, blue share) of bright saturated pixels in the light-bar region of a vehicle."""
    H, W = frame.shape[:2]
    x1, y1, x2, y2 = box
    bh = y2 - y1
    rx1, rx2 = int(max(0, x1)), int(min(W, x2))
    ry1, ry2 = int(max(0, y1 - 0.12 * bh)), int(min(H, y1 + 0.38 * bh))
    if rx2 - rx1 < 8 or ry2 - ry1 < 6:
        return None
    region = frame[ry1:ry2, rx1:rx2]
    red, blue, _ = _hsv_masks(region)
    n = float(region.shape[0] * region.shape[1])
    return float(red.sum()) / n, float(blue.sum()) / n


def livery_score(frame: np.ndarray, box) -> float:
    H, W = frame.shape[:2]
    x1, y1, x2, y2 = box
    bh = y2 - y1
    rx1, rx2 = int(max(0, x1)), int(min(W, x2))
    ry1, ry2 = int(max(0, y1 + 0.3 * bh)), int(min(H, y1 + 0.85 * bh))
    if rx2 - rx1 < 8 or ry2 - ry1 < 8:
        return 0.0
    region = frame[ry1:ry2, rx1:rx2]
    red, blue, hsv = _hsv_masks(region)
    s, v = hsv[..., 1], hsv[..., 2]
    n = float(region.shape[0] * region.shape[1])
    white = float(((s < 45) & (v > 165)).sum()) / n
    yellow = float(((hsv[..., 0] >= 18) & (hsv[..., 0] <= 34) & (s > 120) & (v > 150)).sum()) / n
    marking = float((red | blue).sum()) / n
    body = max(white, yellow)
    return float(np.clip(body / 0.45, 0, 1) * np.clip(marking / 0.03, 0, 1))


def flashing_score(hist: deque) -> float:
    """Score in [0, 1] from a history of (t, red, blue) light-bar samples."""
    if len(hist) < 5 or hist[-1][0] - hist[0][0] < 0.8:
        return 0.0
    r = np.array([h[1] for h in hist])
    b = np.array([h[2] for h in hist])
    score = 0.0
    for series in (r, b):
        mean, sd, peak = series.mean(), series.std(), series.max()
        if mean > 0.25:  # that much red/blue is the vehicle's paint, not a beacon
            continue
        presence = np.clip(peak / 0.02, 0, 1)
        variation = np.clip(sd / max(0.006, 0.35 * mean), 0, 1)
        score = max(score, float(presence * variation))
    # red and blue alternating is the classic light-bar pattern
    if r.std() > 0.002 and b.std() > 0.002:
        corr = float(np.corrcoef(r, b)[0, 1])
        if corr < -0.2:
            score = min(1.0, score + 0.2)
    return score


class _BackgroundModel:
    """Runs the (slow) open-vocabulary verifier in its own thread on the newest frame, so the
    camera keeps its frame rate; results are matched to the tracks when they arrive."""

    def __init__(self, model: Optional[BaseDetector], conf: float, text_reader=None, classifier=None):
        import threading

        self.model, self.conf = model, conf
        self.text_reader = text_reader
        self.classifier = classifier
        self.text_interval_s = 1.5  # OCR at most this often (it is the slowest part)
        self._last_text = 0.0
        self._lock = threading.Lock()
        self._job = None  # (frame, t)
        self._result: Optional[tuple[float, list[Det]]] = None
        self._event = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="ambulance-model", daemon=True)
        self._thread.start()

    def submit(self, frame: np.ndarray, t: float, boxes: Optional[list] = None):
        with self._lock:
            self._job = (frame, t, boxes or [])
        self._event.set()

    def busy(self) -> bool:
        return self._job is not None

    def take(self) -> Optional[tuple[float, list[Det]]]:
        with self._lock:
            r, self._result = self._result, None
        return r

    def _loop(self):
        while True:
            self._event.wait()
            self._event.clear()
            with self._lock:
                job = self._job
            if job is None:
                continue
            frame, t, boxes = job
            try:
                dets = [d for d in self.model.detect(frame) if d.cls in AMBULANCE_NAMES and d.conf >= self.conf] \
                    if self.model is not None else []
            except Exception:
                log.exception("ambulance model failed")
                dets = []
            cls_res = []
            if self.classifier is not None and boxes:
                try:  # trained vehicle classifier on each candidate vehicle
                    probs = self.classifier.classify_boxes(frame, [b for _, b in boxes])
                    cls_res = [(tid, b, p) for (tid, b), p in zip(boxes, probs) if p]
                except Exception:
                    log.exception("vehicle classifier failed")
            import time as _time

            if self.text_reader is not None and _time.monotonic() - self._last_text >= self.text_interval_s:
                self._last_text = _time.monotonic()
                try:  # "AMBULANCE" / "108" written on a vehicle -> a text detection at that spot
                    from .ensemble import text_evidence

                    for box, txt, c in self.text_reader.read(frame):
                        sc, word = text_evidence(txt)
                        if sc > 0:
                            dets.append(Det(box, "ambulance_text", sc * min(1.0, 0.5 + c)))
                except Exception:
                    log.exception("text check failed")
            with self._lock:
                self._result = (t, dets, cls_res)
                if self._job is job:
                    self._job = None


class AmbulanceVerifier:
    """Per-track ambulance decision from several cues, fused by ambulance_fusion:
    trained vehicle classifier, 'AMBULANCE' text (OCR), flashing beacon, livery, open-vocabulary model."""

    def __init__(self, cfg: Optional[AmbulanceConfig] = None, model: Optional[BaseDetector] = None,
                 background: bool = True, text_reader=None, classifier=None):
        self.cfg = cfg or AmbulanceConfig()
        self.model = model
        self.classifier = classifier
        self._n = 0
        self._bg = _BackgroundModel(model, self.cfg.model_conf, text_reader, classifier) \
            if (background and (model is not None or text_reader is not None or classifier is not None)) else None
        self._text: list[Det] = []  # latest text hits (kept ~3 s)
        self._text_t = -1e9

    def _apply_classifier(self, tracks_by_id: dict, cls_res: list):
        for tid, _, probs in cls_res:
            tr = tracks_by_id.get(tid)
            if tr is None:
                continue
            p = probs.get("ambulance", 0.0)
            tr.amb_cls = p if tr.amb_cls < 0 else 0.6 * tr.amb_cls + 0.4 * p
            tr.type_probs = probs
            top = max(probs, key=probs.get)
            if top == "auto_rickshaw" and probs[top] > 0.55:  # COCO has no auto-rickshaw class
                tr.votes["auto_rickshaw"] += 2
            elif top == "emergency_other" and probs[top] > 0.7:
                tr.votes["fire_or_police"] += 2

    def update(self, frame: np.ndarray, tracks: list[Track], t: float, dets: Optional[list[Det]] = None) -> list[Track]:
        """Update ambulance scores; returns tracks that became ambulances on this frame."""
        if not self.cfg.enabled:
            return []
        self._n += 1
        vehicles = [tr for tr in tracks if tr.confirmed and (tr.cls in CANDIDATE_CLASSES or tr.cls in AMBULANCE_NAMES
                                                             or tr.base_cls in ("motorcycle", "fire_or_police"))]
        cands = [tr for tr in vehicles if tr.cls in CANDIDATE_CLASSES or tr.cls in AMBULANCE_NAMES
                 or tr.base_cls == "fire_or_police"]
        if not vehicles:
            return []
        # 1) model evidence: the main detector itself, the classifier / OCR / open-vocabulary model in the background
        direct = [d for d in (dets or []) if d.cls in AMBULANCE_NAMES]
        model_dets: list[Det] = list(direct)
        ran_model = False
        by_id = {tr.id: tr for tr in vehicles}
        if self.cfg.use_model and (self.model is not None or self._bg is not None or self.classifier is not None):
            if self._bg is not None:  # asynchronous: never blocks the camera
                res = self._bg.take()
                if res is not None and t - res[0] <= 1.5:
                    model_dets += [d for d in res[1] if d.cls != "ambulance_text"]
                    texts = [d for d in res[1] if d.cls == "ambulance_text"]
                    if texts:
                        self._text, self._text_t = texts, t
                    self._apply_classifier(by_id, res[2] if len(res) > 2 else [])
                    ran_model = self.model is not None
                if not self._bg.busy():
                    big = sorted(vehicles, key=lambda tr: -(tr.box[2] - tr.box[0]) * (tr.box[3] - tr.box[1]))[:10]
                    self._bg.submit(frame, t, [(tr.id, tr.box) for tr in big])
            else:
                if self.classifier is not None and self._n % max(1, self.cfg.model_every) == 1 % max(1, self.cfg.model_every):
                    try:
                        probs = self.classifier.classify_boxes(frame, [tr.box for tr in vehicles])
                        self._apply_classifier(by_id, [(tr.id, tr.box, p) for tr, p in zip(vehicles, probs) if p])
                    except Exception:
                        log.exception("vehicle classifier failed")
                if self.model is not None and self._n % max(1, self.cfg.model_every) == 0:
                    try:
                        model_dets += [d for d in self.model.detect(frame) if d.cls in AMBULANCE_NAMES and d.conf >= self.cfg.model_conf]
                        ran_model = True
                    except Exception:
                        log.exception("ambulance model failed")
        newly = []
        for tr in cands:
            ev = 0.0
            for d in model_dets:
                if iou(tr.box, d.box) >= 0.3:
                    ev = max(ev, d.conf)
            if ev > 0 or ran_model or direct:
                tr.amb_model = max(ev, 0.6 * tr.amb_model) if (ran_model or direct) else tr.amb_model
            else:
                tr.amb_model *= 0.95
            txt = 0.0
            if self._text and t - self._text_t <= 3.0:  # text written on this vehicle
                x1, y1, x2, y2 = tr.box
                for d in self._text:
                    cx, cy = (d.box[0] + d.box[2]) / 2, (d.box[1] + d.box[3]) / 2
                    if x1 <= cx <= x2 and y1 - 0.15 * (y2 - y1) <= cy <= y2:
                        txt = max(txt, d.conf)
            tr.amb_text = max(txt, 0.97 * tr.amb_text)
            # 2) beacon
            beacon = 0.0
            if self.cfg.use_beacon:
                smp = beacon_sample(frame, tr.box)
                if smp:
                    tr.beacon_hist.append((t, smp[0], smp[1]))
                    while tr.beacon_hist and t - tr.beacon_hist[0][0] > 2.5:
                        tr.beacon_hist.popleft()
                beacon = flashing_score(tr.beacon_hist)
            # 3) livery
            livery = livery_score(frame, tr.box) if self.cfg.use_livery else 0.0
            cls_p = tr.amb_cls if tr.amb_cls >= 0 else None
            prob, reasons = ambulance_probability(cls_p, tr.amb_text, beacon, livery, tr.amb_model)
            frame_score = decision_score(prob, cls_p)  # 0.5 = the fitted decision threshold
            tr.amb_score = max(0.7 * tr.amb_score + 0.3 * frame_score, 0.9 * frame_score if frame_score > 0.6 else 0.0)
            tr.amb_reasons = [r.replace("light bar", "flashing beacon") for r in reasons]
            if tr.amb_score >= self.cfg.threshold:
                tr.amb_frames += 1
                if not tr.is_ambulance and tr.amb_frames >= self.cfg.min_frames:
                    tr.is_ambulance = True
                    newly.append(tr)
            else:
                tr.amb_frames = 0
                if tr.is_ambulance and tr.amb_score < self.cfg.threshold / 2:
                    tr.is_ambulance = False
        return newly
