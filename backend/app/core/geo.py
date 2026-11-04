"""Small geodesy helpers.

The project area (a few km of Nerul–Seawoods) is small enough that a local
equirectangular projection around the network centre is accurate to well under
a metre, so we avoid heavyweight projection libraries entirely.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


class LocalProjection:
    """Equirectangular projection centred on (lat0, lon0); x east, y north, metres."""

    def __init__(self, lat0: float, lon0: float):
        self.lat0 = lat0
        self.lon0 = lon0
        self.kx = math.cos(math.radians(lat0)) * math.pi * EARTH_RADIUS_M / 180.0
        self.ky = math.pi * EARTH_RADIUS_M / 180.0

    def to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)

    def to_latlon(self, x: float, y: float) -> tuple[float, float]:
        return (self.lat0 + y / self.ky, self.lon0 + x / self.kx)

    @classmethod
    def for_points(cls, points: Iterable[Sequence[float]]) -> "LocalProjection":
        pts = list(points)
        lat0 = sum(p[0] for p in pts) / len(pts)
        lon0 = sum(p[1] for p in pts) / len(pts)
        return cls(lat0, lon0)


def polyline_length_xy(xy: Sequence[tuple[float, float]]) -> float:
    return sum(math.dist(xy[i], xy[i + 1]) for i in range(len(xy) - 1))


def bearing_deg(x1: float, y1: float, x2: float, y2: float) -> float:
    """Compass bearing (0 = north, clockwise) of the vector from point 1 to point 2."""
    return (math.degrees(math.atan2(x2 - x1, y2 - y1)) + 360.0) % 360.0


def point_along(xy: Sequence[tuple[float, float]], dist: float) -> tuple[float, float, float]:
    """Return (x, y, heading_deg) at `dist` metres along a polyline (clamped)."""
    if dist <= 0:
        x1, y1 = xy[0]
        x2, y2 = xy[1]
        return x1, y1, bearing_deg(x1, y1, x2, y2)
    remaining = dist
    for i in range(len(xy) - 1):
        (x1, y1), (x2, y2) = xy[i], xy[i + 1]
        seg = math.dist((x1, y1), (x2, y2))
        if seg <= 0:
            continue
        if remaining <= seg:
            f = remaining / seg
            return x1 + f * (x2 - x1), y1 + f * (y2 - y1), bearing_deg(x1, y1, x2, y2)
        remaining -= seg
    (x1, y1), (x2, y2) = xy[-2], xy[-1]
    return x2, y2, bearing_deg(x1, y1, x2, y2)


def offset_right(x: float, y: float, heading_deg: float, offset: float) -> tuple[float, float]:
    """Shift a point perpendicular to the right of the travel direction (used to draw lanes)."""
    h = math.radians(heading_deg)
    # right-hand normal of (sin h, cos h) is (cos h, -sin h)
    return x + offset * math.cos(h), y - offset * math.sin(h)


def axis_difference(b1: float, b2: float) -> float:
    """Angle between two undirected axes (0..90 degrees)."""
    d = abs((b1 - b2) % 180.0)
    return min(d, 180.0 - d)
