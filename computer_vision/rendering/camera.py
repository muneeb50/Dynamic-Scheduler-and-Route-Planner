"""Synthetic traffic-camera frames rendered from the simulation state.

Each signalised approach gets a virtual overhead camera covering the last
VIEW_M metres before the stop line. Frames contain an asphalt texture, lane
markings, a static tree shadow, per-frame sensor noise and lighting drift, and
vehicles in varied colours (including greys close to the road colour, which
are genuinely hard to detect). The renderer also returns the ground truth
(boxes, speeds) so detection and queue estimates can be scored.

IMPORTANT: results on these frames do not transfer automatically to real CCTV.
Use computer_vision/detect_video.py with a YOLO model for real footage.
"""
from __future__ import annotations

import zlib
from functools import lru_cache
from dataclasses import dataclass

import cv2
import numpy as np

VIEW_M = 150.0
WIDTH = 760
LANE_PX = 56
MARGIN_PX = 18
STOP_X = WIDTH - 30
SCALE = (STOP_X - 10) / VIEW_M  # px per metre along the road
CAR_W_PX = 26

PALETTE = [  # BGR
    (235, 235, 235), (40, 40, 40), (160, 160, 160), (110, 110, 115), (30, 30, 170), (160, 70, 20),
    (40, 120, 40), (20, 160, 220), (200, 200, 190), (90, 60, 40), (70, 70, 75), (0, 90, 200),
]


@dataclass
class GroundTruthVehicle:
    id: str
    box: tuple[int, int, int, int]  # x1, y1, x2, y2
    speed: float
    lane: int
    distance_to_stop_m: float
    vtype: str


def _hash(s: str) -> int:
    return zlib.crc32(s.encode())


def frame_height(lanes: int) -> int:
    return lanes * LANE_PX + 2 * MARGIN_PX


@lru_cache(maxsize=512)
def _plate(edge_id: str, lanes: int) -> np.ndarray:
    h = frame_height(lanes)
    rng = np.random.default_rng(_hash(edge_id))
    img = np.full((h, WIDTH, 3), 78, np.uint8)
    texture = rng.normal(0, 5, (h, WIDTH, 1)).astype(np.int16)
    img = np.clip(img.astype(np.int16) + texture, 0, 255).astype(np.uint8)
    cv2.rectangle(img, (0, 0), (WIDTH, MARGIN_PX - 4), (90, 120, 90), -1)  # verge
    cv2.rectangle(img, (0, h - MARGIN_PX + 4), (WIDTH, h), (90, 120, 90), -1)
    for i in range(1, lanes):
        y = MARGIN_PX + i * LANE_PX
        for x in range(0, STOP_X, 30):
            cv2.line(img, (x, y), (x + 15, y), (225, 225, 225), 2)
    cv2.line(img, (STOP_X, MARGIN_PX), (STOP_X, h - MARGIN_PX), (240, 240, 240), 4)
    # static tree shadow – a classic source of background-subtraction errors
    shadow = np.zeros((h, WIDTH), np.uint8)
    cx = 120 + _hash(edge_id) % 300
    cv2.ellipse(shadow, (cx, h // 2), (70, h // 2), 0, 0, 360, 255, -1)
    img[shadow > 0] = (img[shadow > 0] * 0.7).astype(np.uint8)
    img.setflags(write=False)
    return img


def empty_road(edge_id: str, lanes: int) -> np.ndarray:
    """Clean background plate for an approach (the 'calibration' image with no traffic)."""
    return _plate(edge_id, lanes).copy()


def render_frame(edge_id: str, lanes: int, edge_length: float, vehicles: list, t: float,
                 noise: bool = True) -> tuple[np.ndarray, list[GroundTruthVehicle]]:
    """vehicles: iterable of objects with id, pos, speed, lane, vtype (VehicleState)."""
    img = empty_road(edge_id, lanes)
    gts: list[GroundTruthVehicle] = []
    for v in sorted(vehicles, key=lambda v: v.pos):
        d_front = edge_length - v.pos
        length_m = 6.0 if v.vtype == "ambulance" else 4.5
        if d_front > VIEW_M or d_front < -1:
            continue
        x2 = int(STOP_X - d_front * SCALE)
        x1 = int(x2 - length_m * SCALE)
        if x2 <= 0:
            continue
        lane = min(max(int(v.lane), 0), lanes - 1)
        lane_row = lanes - 1 - lane
        hsh = _hash(v.id)
        jitter = (hsh >> 8) % 7 - 3
        yc = MARGIN_PX + lane_row * LANE_PX + LANE_PX // 2 + jitter
        y1, y2 = yc - CAR_W_PX // 2, yc + CAR_W_PX // 2
        color = (245, 245, 245) if v.vtype == "ambulance" else PALETTE[hsh % len(PALETTE)]
        cv2.rectangle(img, (max(x1, 0), y1), (x2, y2), color, -1)
        wx = x2 - int(1.3 * SCALE)
        shade = tuple(int(c * 0.45) for c in color)
        cv2.rectangle(img, (max(wx - int(0.8 * SCALE), 0), y1 + 3), (wx, y2 - 3), shade, -1)  # windscreen
        if v.vtype == "ambulance":
            cx = (max(x1, 0) + x2) // 2
            cv2.line(img, (cx - 6, yc), (cx + 6, yc), (0, 0, 220), 3)
            cv2.line(img, (cx, yc - 6), (cx, yc + 6), (0, 0, 220), 3)
        gts.append(GroundTruthVehicle(v.id, (max(x1, 0), y1, x2, y2), float(v.speed), lane, d_front, v.vtype))
    if noise:
        rng = np.random.default_rng(int(t * 10) + _hash(edge_id))
        light = 1.0 + 0.08 * np.sin(t / 300.0)
        noise_img = rng.integers(-10, 11, img.shape, dtype=np.int16)  # ~N(0, 6) sensor noise, cheaper to draw
        img = np.clip(img.astype(np.int16) * light + noise_img, 0, 255).astype(np.uint8)
    return img, gts
