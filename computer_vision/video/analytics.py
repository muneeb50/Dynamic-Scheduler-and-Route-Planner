"""Traffic measures from tracked vehicles.

Per frame (inside the region of interest – the approach lanes):
  * vehicles by class
  * queue = vehicles that are tracked and (almost) not moving; queue length = distance
    from the stop line (counting line) to the furthest stopped vehicle
  * occupancy = share of the ROI covered by vehicle boxes
  * congestion level from occupancy and the stopped share
  * mean speed (px/s, or km/h when metres-per-pixel is calibrated)
Over time:
  * line crossings per direction and flow (vehicles / minute, sliding 60 s window)
  * emergency-vehicle alerts when a model with such classes is used
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from typing import Optional

import cv2
import numpy as np

from .detectors import EMERGENCY_CLASSES
from .tracker import Track


@dataclass
class ZoneConfig:
    # coordinates are fractions of the frame size so they survive resolution changes
    roi: list[list[float]] = field(default_factory=lambda: [[0.0, 0.3], [1.0, 0.3], [1.0, 1.0], [0.0, 1.0]])
    line: list[list[float]] = field(default_factory=lambda: [[0.0, 0.7], [1.0, 0.7]])
    stop_speed_frac: float = 0.02  # "stopped" if moving slower than 2% of the frame width per second
    stop_min_s: float = 1.0  # …for at least this long
    meters_per_pixel: Optional[float] = None
    vehicle_length_m: float = 6.5  # road space per queued vehicle (for metres <-> vehicles)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "ZoneConfig":
        z = cls()
        for k, v in (d or {}).items():
            if hasattr(z, k) and v is not None:
                setattr(z, k, v)
        return z

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FrameStats:
    t: float
    vehicles: int
    by_class: dict[str, int]
    queue: int
    queue_length_px: float
    queue_length_m: Optional[float]
    occupancy: float
    mean_speed: Optional[float]
    speed_unit: str
    congestion: str
    crossings_total: int
    crossings_by_dir: dict[str, int]
    flow_per_min: float
    tracks_total: int
    ambulances: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["occupancy"] = round(self.occupancy, 3)
        d["queue_length_px"] = round(self.queue_length_px, 1)
        if self.queue_length_m is not None:
            d["queue_length_m"] = round(self.queue_length_m, 1)
        if self.mean_speed is not None:
            d["mean_speed"] = round(self.mean_speed, 1)
        d["flow_per_min"] = round(self.flow_per_min, 1)
        d["t"] = round(self.t, 2)
        return d


def _side(p, a, b) -> int:
    v = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    return 1 if v > 0 else -1 if v < 0 else 0


def _segments_intersect(p1, p2, a, b) -> bool:
    return _side(p1, a, b) != _side(p2, a, b) and _side(a, p1, p2) != _side(b, p1, p2)


class TrafficAnalyzer:
    def __init__(self, zone: Optional[ZoneConfig] = None):
        self.zone = zone or ZoneConfig()
        self.crossings: deque = deque()  # (t, direction, class)
        self.crossings_total = 0
        self.crossings_by_dir: Counter = Counter()
        self.crossings_by_class: Counter = Counter()
        self.events: list[dict] = []
        self._roi_px = None
        self._shape = None

    def set_zone(self, zone: ZoneConfig):
        self.zone = zone
        self._shape = None

    def _geometry(self, shape):
        if self._shape != shape[:2]:
            h, w = shape[:2]
            self._shape = shape[:2]
            self._roi_px = np.array([[x * w, y * h] for x, y in self.zone.roi], np.float32)
            self._line_px = [(x * w, y * h) for x, y in self.zone.line]
            self._roi_area = max(1.0, float(cv2.contourArea(self._roi_px)))
            self._w = w
        return self._roi_px, self._line_px

    def in_roi(self, p, shape) -> bool:
        roi, _ = self._geometry(shape)
        return cv2.pointPolygonTest(roi, (float(p[0]), float(p[1])), False) >= 0

    def line_distance(self, p) -> float:
        (ax, ay), (bx, by) = self._line_px
        norm = max(1e-6, float(np.hypot(bx - ax, by - ay)))
        return abs((bx - ax) * (ay - p[1]) - (ax - p[0]) * (by - ay)) / norm

    def _corridor(self, tr: Track, t: float, line):
        """Ambulance events for signal priority: entering the approach zone, passing the stop line."""
        if tr.counted or tr.passed:
            return
        p = tr.ground_point
        d = self.line_distance(p)
        tr.line_dist_hist.append((t, d))
        if not self.in_roi(p, (self._shape[0], self._shape[1])):
            return
        tr.in_zone = True
        old = [h for h in tr.line_dist_hist if t - h[0] >= 0.8]
        approaching = bool(old) and old[-1][1] - d > 0.01 * self._w
        if not tr.zone_reported:
            tr.zone_reported = True
            speed = tr.speed_px_s if tr.speed_px_s > 1 else None
            self.events.append({"type": "AMBULANCE_IN_ZONE", "t": round(t, 2), "track": tr.id,
                                "distance_px": round(d, 1), "approaching": approaching,
                                "eta_s": round(d / speed, 1) if speed else None, "reasons": list(tr.amb_reasons)})

    def track_lost(self, tr: Track, t: float):
        if tr.cls in EMERGENCY_CLASSES and tr.zone_reported and not tr.passed:
            tr.passed = True
            self.events.append({"type": "AMBULANCE_LOST", "t": round(t, 2), "track": tr.id,
                                "note": "left the camera view without crossing the line"})

    def update(self, tracks: list[Track], t: float, shape) -> FrameStats:
        roi, line = self._geometry(shape)
        stop_px = self.zone.stop_speed_frac * self._w
        inside, by_class = [], Counter()
        occupied = 0.0
        speeds = []
        for tr in tracks:
            # stopped state with a minimum duration to ignore jitter
            if tr.speed_px_s < stop_px and tr.hits >= 3:
                if tr.stopped_since is None:
                    tr.stopped_since = t
                tr.stopped = t - tr.stopped_since >= self.zone.stop_min_s
            else:
                tr.stopped_since = None
                tr.stopped = False
            # line crossing using the last two ground points
            if len(tr.history) >= 2 and not tr.counted:
                (_, x0, y0), (_, x1, y1) = tr.history[-2], tr.history[-1]
                if _segments_intersect((x0, y0), (x1, y1), line[0], line[1]):
                    direction = "forward" if _side((x1, y1), line[0], line[1]) > 0 else "backward"
                    tr.counted = True
                    if tr.cls in EMERGENCY_CLASSES and tr.zone_reported and not tr.passed:
                        tr.passed = True
                        self.events.append({"type": "AMBULANCE_PASSED", "t": round(t, 2), "track": tr.id,
                                            "direction": direction})
                    self.crossings.append((t, direction, tr.cls))
                    self.crossings_total += 1
                    self.crossings_by_dir[direction] += 1
                    self.crossings_by_class[tr.cls] += 1
            emergency = tr.cls in EMERGENCY_CLASSES
            if emergency and not tr.emergency_reported and tr.confirmed:
                tr.emergency_reported = True
                self.events.append({"type": "EMERGENCY_VEHICLE", "t": round(t, 2), "track": tr.id, "class": tr.cls,
                                    "conf": round(max(tr.conf, tr.amb_score), 2), "reasons": list(tr.amb_reasons),
                                    "already_past_line": tr.counted})
            if emergency and tr.confirmed:
                self._corridor(tr, t, line)
            if not tr.confirmed or not self.in_roi(tr.ground_point, shape):
                continue
            inside.append(tr)
            by_class[tr.cls] += 1
            x1, y1, x2, y2 = tr.box
            occupied += (x2 - x1) * (y2 - y1)
            if not tr.stopped:
                speeds.append(tr.speed_px_s)
        while self.crossings and t - self.crossings[0][0] > 60.0:
            self.crossings.popleft()
        # queue = stopped vehicles; its length is measured from the counting (stop) line
        stopped = [tr for tr in inside if tr.stopped]
        q_px = 0.0
        if stopped:
            (ax, ay), (bx, by) = line
            norm = max(1e-6, float(np.hypot(bx - ax, by - ay)))
            q_px = max(abs((bx - ax) * (ay - tr.ground_point[1]) - (ax - tr.ground_point[0]) * (by - ay)) / norm
                       for tr in stopped)
        occupancy = min(1.0, occupied / self._roi_area)
        stopped_share = len(stopped) / len(inside) if inside else 0.0
        if occupancy > 0.45 or (stopped_share > 0.7 and len(stopped) >= 6):
            level = "jam"
        elif occupancy > 0.25 or stopped_share > 0.5:
            level = "heavy"
        elif occupancy > 0.10 or stopped_share > 0.25:
            level = "moderate"
        else:
            level = "free"
        mpp = self.zone.meters_per_pixel
        if speeds:
            mean = sum(speeds) / len(speeds)
            mean_speed, unit = (mean * mpp * 3.6, "km/h") if mpp else (mean, "px/s")
        else:
            mean_speed, unit = None, ("km/h" if mpp else "px/s")
        window = min(60.0, max(t, 30.0))  # avoid huge rates from one early crossing
        return FrameStats(
            t=t, vehicles=len(inside), by_class=dict(by_class), queue=len(stopped), queue_length_px=q_px,
            queue_length_m=q_px * mpp if mpp else None, occupancy=occupancy, mean_speed=mean_speed, speed_unit=unit,
            congestion=level, crossings_total=self.crossings_total, crossings_by_dir=dict(self.crossings_by_dir),
            flow_per_min=len(self.crossings) * 60.0 / window, tracks_total=0,
            ambulances=sum(1 for tr in tracks if tr.is_ambulance),
        )
