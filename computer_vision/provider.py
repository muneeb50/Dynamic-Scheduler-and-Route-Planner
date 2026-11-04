"""Glue between the simulation and the CV pipeline.

`CVQueueProvider.estimate(coordinator, edge_id)` renders the virtual camera for an
approach, runs detection and queue estimation, and remembers the result so the
dashboard can show the annotated frame. Plug it into the controller with
QUEUE_SOURCE=cv (or the dashboard toggle).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

from .detection.opencv_detector import Detection, OpenCVDetector
from .estimation.queue import QueueEstimate, QueueEstimator
from .rendering.camera import SCALE, STOP_X, VIEW_M, GroundTruthVehicle, empty_road, render_frame


@dataclass
class CVResult:
    t: float
    edge: str
    frame: np.ndarray
    estimate: QueueEstimate
    ground_truth: list[GroundTruthVehicle]

    @property
    def gt_stopped(self) -> int:
        return sum(1 for g in self.ground_truth if g.speed < 0.5)

    def summary(self) -> dict:
        e = self.estimate
        return {
            "t": round(self.t, 1), "edge": self.edge,
            "detected_vehicles": e.vehicles, "estimated_queue": e.stopped, "queue_used": round(e.queue_used, 1),
            "queue_m": e.queue_m, "confidence": e.confidence,
            "true_vehicles_in_view": len(self.ground_truth), "true_queue_in_view": self.gt_stopped,
            "view_m": VIEW_M,
        }


class CVQueueProvider:
    def __init__(self, detector: Optional[OpenCVDetector] = None, estimator: Optional[QueueEstimator] = None):
        self.detector = detector or OpenCVDetector()
        self.estimator = estimator or QueueEstimator()
        self._plates: dict[str, np.ndarray] = {}
        self._prev: dict[str, tuple[float, list[Detection]]] = {}
        self._used: dict[str, float] = {}
        self.last: dict[str, CVResult] = {}
        # frames further apart than this cannot tell stopped from moving vehicles reliably
        self.max_prev_age_s = 15.0

    def plate(self, edge_id: str, lanes: int) -> np.ndarray:
        if edge_id not in self._plates:
            self._plates[edge_id] = empty_road(edge_id, lanes)
        return self._plates[edge_id]

    def estimate(self, coord, edge_id: str) -> QueueEstimate:
        e = coord.net.edges[edge_id]
        t = coord.now
        frame, gts = render_frame(edge_id, e.lanes, e.length, coord.engine.edge_vehicles(edge_id), t)
        dets = self.detector.detect(frame, self.plate(edge_id, e.lanes), e.lanes)
        prev = self._prev.get(edge_id)
        prev_dets = prev[1] if prev and 0 < t - prev[0] <= self.max_prev_age_s else None
        est = self.estimator.estimate(dets, prev_dets, self._used.get(edge_id))
        self._prev[edge_id] = (t, dets)
        self._used[edge_id] = est.queue_used
        self.last[edge_id] = CVResult(t, edge_id, frame, est, gts)
        return est


def annotate(result: CVResult, show_truth: bool = True) -> np.ndarray:
    img = result.frame.copy()
    if show_truth:
        for g in result.ground_truth:
            x1, y1, x2, y2 = g.box
            cv2.rectangle(img, (x1 - 2, y1 - 2), (x2 + 2, y2 + 2), (255, 200, 0), 1)
    for d, stopped in zip(result.estimate.detections, result.estimate.stopped_flags):
        x1, y1, x2, y2 = d.box
        color = (0, 0, 255) if stopped else (0, 200, 0)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
    if result.estimate.queue_m > 0:
        x = int(STOP_X - result.estimate.queue_m * SCALE)
        cv2.line(img, (x, 4), (STOP_X, 4), (0, 0, 255), 3)
    s = result.summary()
    text = (f"t={s['t']}s det={s['detected_vehicles']} queue_est={s['estimated_queue']} "
            f"(true {s['true_queue_in_view']}) conf={s['confidence']}")
    cv2.rectangle(img, (0, img.shape[0] - 16), (img.shape[1], img.shape[0]), (0, 0, 0), -1)
    cv2.putText(img, text, (6, img.shape[0] - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1, cv2.LINE_AA)
    return img
