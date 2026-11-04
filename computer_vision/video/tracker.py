"""Lightweight multi-object tracker (IoU matching with a centroid fallback).

Works with every detector back-end and with irregular frame timing (live streams
drop frames), because track ageing and speeds use timestamps, not frame counts.
"""
from __future__ import annotations

import itertools
import math
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Optional

from .detectors import Det


def iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0:
        return 0.0
    area = lambda r: (r[2] - r[0]) * (r[3] - r[1])  # noqa: E731
    return inter / (area(a) + area(b) - inter + 1e-9)


@dataclass
class Track:
    id: int
    box: tuple[float, float, float, float]
    conf: float
    first_seen: float
    last_seen: float
    votes: Counter = field(default_factory=Counter)
    history: deque = field(default_factory=lambda: deque(maxlen=60))  # (t, x, y) of the ground point
    hits: int = 1
    speed_px_s: float = 0.0
    stopped: bool = False
    stopped_since: Optional[float] = None
    counted: bool = False
    side: Optional[int] = None
    emergency_reported: bool = False
    # ambulance recognition (see ambulance.py)
    amb_model: float = 0.0
    amb_score: float = 0.0
    amb_frames: int = 0
    is_ambulance: bool = False
    amb_reasons: list = field(default_factory=list)
    amb_cls: float = -1.0  # trained classifier P(ambulance), -1 = not classified yet
    amb_text: float = 0.0  # 'AMBULANCE' text seen on this vehicle (decays)
    type_probs: Optional[dict] = None
    beacon_hist: deque = field(default_factory=lambda: deque(maxlen=40))
    # corridor events
    in_zone: bool = False
    zone_reported: bool = False
    passed: bool = False
    line_dist_hist: deque = field(default_factory=lambda: deque(maxlen=20))

    @property
    def base_cls(self) -> str:
        return self.votes.most_common(1)[0][0] if self.votes else "vehicle"

    @property
    def cls(self) -> str:
        return "ambulance" if self.is_ambulance else self.base_cls

    @property
    def ground_point(self) -> tuple[float, float]:
        """Bottom-centre of the box: where the vehicle touches the road."""
        x1, y1, x2, y2 = self.box
        return (x1 + x2) / 2, y2

    @property
    def confirmed(self) -> bool:
        return self.hits >= 2


class Tracker:
    def __init__(self, iou_threshold: float = 0.25, max_age_s: float = 1.2, speed_window_s: float = 1.0):
        self.iou_threshold = iou_threshold
        self.max_age_s = max_age_s
        self.speed_window_s = speed_window_s
        self.tracks: dict[int, Track] = {}
        self._ids = itertools.count(1)
        self.total_created = 0
        self.removed: list[Track] = []  # tracks dropped by the last update

    def reset(self):
        self.removed = list(self.tracks.values())
        self.tracks.clear()

    def update(self, dets: list[Det], t: float) -> list[Track]:
        tracks = list(self.tracks.values())
        pairs = []
        for ti, tr in enumerate(tracks):
            for di, d in enumerate(dets):
                v = iou(tr.box, d.box)
                if v >= self.iou_threshold:
                    pairs.append((v, ti, di))
        pairs.sort(reverse=True)
        used_t, used_d, matches = set(), set(), []
        for v, ti, di in pairs:
            if ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            matches.append((ti, di))
        # centroid fallback for fast vehicles / low frame rate (boxes no longer overlap)
        for ti, tr in enumerate(tracks):
            if ti in used_t:
                continue
            tx, ty = tr.ground_point
            size = max(tr.box[2] - tr.box[0], tr.box[3] - tr.box[1])
            best, best_d = None, math.inf
            for di, d in enumerate(dets):
                if di in used_d:
                    continue
                dx = (d.box[0] + d.box[2]) / 2 - tx
                dy = d.box[3] - ty
                dist = math.hypot(dx, dy)
                if dist < best_d and dist < 1.2 * size:
                    best, best_d = di, dist
            if best is not None:
                used_t.add(ti)
                used_d.add(best)
                matches.append((ti, best))
        for ti, di in matches:
            tr, d = tracks[ti], dets[di]
            tr.box, tr.conf, tr.last_seen = d.box, d.conf, t
            tr.votes[d.cls] += 1
            tr.hits += 1
            self._update_motion(tr, t)
        for di, d in enumerate(dets):
            if di in used_d:
                continue
            tr = Track(next(self._ids), d.box, d.conf, t, t)
            tr.votes[d.cls] += 1
            self._update_motion(tr, t)
            self.tracks[tr.id] = tr
            self.total_created += 1
        self.removed = []
        for tid in [k for k, tr in self.tracks.items() if t - tr.last_seen > self.max_age_s]:
            self.removed.append(self.tracks.pop(tid))
        return [tr for tr in self.tracks.values() if tr.last_seen == t]

    def _update_motion(self, tr: Track, t: float):
        x, y = tr.ground_point
        tr.history.append((t, x, y))
        old = [h for h in tr.history if t - h[0] >= self.speed_window_s * 0.5]
        ref = old[-1] if old else tr.history[0]
        dt = t - ref[0]
        if dt > 0:
            inst = math.hypot(x - ref[1], y - ref[2]) / dt
            tr.speed_px_s = inst if tr.hits <= 2 else 0.6 * tr.speed_px_s + 0.4 * inst
