"""Queue estimation from consecutive detections (1 s apart).

A vehicle count alone is not a queue: cars may be visible but moving freely.
A detection is "stopped" when a detection in the same lane in the previous frame
lies within `still_px` of it (≈ 0.4 m/s at 1 s spacing). The queue is the number
of stopped vehicles in view; queue length in metres is the distance from the stop
line to the rear of the furthest stopped vehicle still connected to the stop line.

Confidence drops when the previous frame is missing or blobs had to be split;
low-confidence estimates are combined conservatively with the previous value so
the controller does not under-estimate a queue because of a single bad frame.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..detection.opencv_detector import Detection
from ..rendering.camera import SCALE, STOP_X


@dataclass
class QueueEstimate:
    vehicles: int
    stopped: int
    queue_m: float
    confidence: float
    queue_used: float
    detections: list[Detection] = field(default_factory=list)
    stopped_flags: list[bool] = field(default_factory=list)


class QueueEstimator:
    def __init__(self, still_px: float = 0.45 * SCALE, chain_gap_m: float = 12.0, min_confidence: float = 0.6):
        self.still_px = still_px
        self.chain_gap_m = chain_gap_m
        self.min_confidence = min_confidence

    def estimate(self, dets: list[Detection], prev: Optional[list[Detection]], last_used: Optional[float]) -> QueueEstimate:
        flags: list[bool] = []
        for d in dets:
            if prev is None:
                flags.append(False)
                continue
            same_lane = [p for p in prev if p.lane_row == d.lane_row]
            flags.append(any(abs(p.box[2] - d.box[2]) <= self.still_px for p in same_lane))
        stopped = sum(flags)
        # contiguous queue length per lane, measured from the stop line
        queue_m = 0.0
        for lane in {d.lane_row for d in dets}:
            lane_stopped = sorted((d for d, f in zip(dets, flags) if f and d.lane_row == lane), key=lambda d: -d.box[2])
            reach = STOP_X
            for d in lane_stopped:
                if (reach - d.box[2]) / SCALE > self.chain_gap_m:
                    break
                reach = d.box[0]
            queue_m = max(queue_m, (STOP_X - reach) / SCALE)
        n_split = sum(1 for d in dets if d.split)
        confidence = 1.0
        if prev is None:
            confidence -= 0.7
        if dets:
            confidence -= 0.4 * n_split / len(dets)
        confidence = round(max(0.0, confidence), 2)
        if prev is None:
            est = float(len(dets))  # cannot tell moving from stopped: assume worst case
        else:
            est = float(stopped)
        used = est if confidence >= self.min_confidence or last_used is None else max(est, 0.8 * last_used)
        return QueueEstimate(len(dets), stopped, round(queue_m, 1), confidence, used, dets, flags)
