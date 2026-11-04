"""Classical OpenCV vehicle detector for fixed overhead cameras.

Pipeline: blur -> per-pixel colour difference against the empty-road plate ->
threshold -> morphological clean-up -> contours -> boxes, with a split step for
blobs that are clearly several queued vehicles merged together.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ..rendering.camera import LANE_PX, MARGIN_PX, SCALE


@dataclass
class Detection:
    box: tuple[int, int, int, int]
    lane_row: int
    confidence: float
    split: bool = False

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) / 2


class OpenCVDetector:
    def __init__(self, threshold: int = 28, min_area: int = 140, expected_len_m: float = 4.5):
        self.threshold = threshold
        self.min_area = min_area
        self.expected_px = expected_len_m * SCALE

    def detect(self, frame: np.ndarray, background: np.ndarray, lanes: int) -> list[Detection]:
        f = cv2.GaussianBlur(frame, (5, 5), 0).astype(np.int16)
        b = cv2.GaussianBlur(background, (5, 5), 0).astype(np.int16)
        diff = np.abs(f - b).max(axis=2).astype(np.uint8)
        _, mask = cv2.threshold(diff, self.threshold, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 3), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        out: list[Detection] = []
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            if w * h < self.min_area or h < 10 or w < 8:
                continue
            fill = cv2.contourArea(c) / float(w * h)
            yc = y + h / 2
            lane_row = int(min(max((yc - MARGIN_PX) // LANE_PX, 0), lanes - 1))
            n = int(round(w / (self.expected_px + 2.0 * SCALE)))
            if w > 1.7 * self.expected_px and n >= 2:
                step = w / n
                for k in range(n):
                    out.append(Detection((int(x + k * step), y, int(x + (k + 1) * step), y + h), lane_row,
                                         round(0.5 * fill, 2), split=True))
            else:
                out.append(Detection((x, y, x + w, y + h), lane_row, round(min(1.0, 0.4 + 0.6 * fill), 2)))
        return out
