"""Road network model shared by every part of the system.

`data/network.json` is the single source of truth for road geometry. It is
produced either by the bundled demo generator or by the OpenStreetMap importer
(`simulation/tools/osm_to_network.py`). The SUMO network is *derived* from the
same file, so node and edge identifiers are identical in the built-in engine,
the SUMO engine, the database and the dashboard.
"""
from __future__ import annotations

import bisect
import heapq
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .geo import LocalProjection, axis_difference, bearing_deg, point_along, polyline_length_xy


@dataclass
class Node:
    id: str
    lat: float
    lon: float
    signal: bool = False
    name: str = ""
    signal_source: str = ""  # "osm" (tagged in OpenStreetMap) | "inferred" | "" (demo network)
    roundabout: bool = False
    x: float = 0.0
    y: float = 0.0
    incoming: list[str] = field(default_factory=list)
    outgoing: list[str] = field(default_factory=list)


@dataclass
class Edge:
    id: str
    from_node: str
    to_node: str
    lanes: int
    speed: float  # m/s speed limit
    name: str
    geometry: list[tuple[float, float]]  # [(lat, lon), ...] including both end nodes
    road_class: str = "road"
    xy: list[tuple[float, float]] = field(default_factory=list)
    length: float = 0.0
    cum: list[float] = field(default_factory=list)  # cumulative length at each shape point
    free_flow_time: float = 0.0
    _approach: Optional[float] = None
    _departure: Optional[float] = None

    def finalize(self):
        self.cum = [0.0]
        for i in range(len(self.xy) - 1):
            self.cum.append(self.cum[-1] + math.dist(self.xy[i], self.xy[i + 1]))
        self.length = max(self.cum[-1], 1.0)
        self.free_flow_time = self.length / max(self.speed, 0.1)
        x2, y2, _ = point_along(self.xy, self.length)
        x1, y1, _ = point_along(self.xy, max(0.0, self.length - 30.0))
        self._approach = bearing_deg(x1, y1, x2, y2)
        x1, y1, _ = point_along(self.xy, 0.0)
        x2, y2, _ = point_along(self.xy, min(self.length, 30.0))
        self._departure = bearing_deg(x1, y1, x2, y2)

    def approach_bearing(self) -> float:
        """Direction of travel on the final ~30 m before the downstream node."""
        return self._approach

    def departure_bearing(self) -> float:
        return self._departure

    def point_at(self, dist: float) -> tuple[float, float, float]:
        """(x, y, heading) at `dist` metres along the edge – O(log n) using cumulative lengths."""
        cum, xy = self.cum, self.xy
        if dist <= 0 or len(xy) < 2:
            return point_along(xy, max(dist, 0.0))
        i = bisect.bisect_right(cum, dist) - 1
        if i >= len(xy) - 1:
            return point_along(xy, dist)
        (x1, y1), (x2, y2) = xy[i], xy[i + 1]
        seg = cum[i + 1] - cum[i]
        f = (dist - cum[i]) / seg if seg > 0 else 0.0
        return x1 + f * (x2 - x1), y1 + f * (y2 - y1), bearing_deg(x1, y1, x2, y2)


@dataclass
class Hospital:
    id: str
    name: str
    lat: float
    lon: float
    node: str


@dataclass
class AmbulanceBase:
    id: str
    name: str
    node: str


class RoadNetwork:
    def __init__(self, data: dict):
        self.name: str = data.get("name", "network")
        self.source: str = data.get("source", "unknown")
        self.description: str = data.get("description", "")
        self.left_hand_traffic: bool = bool(data.get("left_hand_traffic", True))
        self.scenario: dict = data.get("scenario", {}) or {}
        self.meta: dict = data.get("meta", {}) or {}
        self.nodes: dict[str, Node] = {}
        self.edges: dict[str, Edge] = {}
        for n in data["nodes"]:
            self.nodes[n["id"]] = Node(
                id=n["id"], lat=n["lat"], lon=n["lon"], signal=bool(n.get("signal", False)), name=n.get("name", ""),
                signal_source=n.get("signal_source", ""), roundabout=bool(n.get("roundabout", False)),
            )
        self.proj = LocalProjection.for_points([(n.lat, n.lon) for n in self.nodes.values()])
        for node in self.nodes.values():
            node.x, node.y = self.proj.to_xy(node.lat, node.lon)
        for e in data["edges"]:
            geom = [tuple(p) for p in e.get("geometry") or []]
            a, b = self.nodes[e["from"]], self.nodes[e["to"]]
            if len(geom) < 2:
                geom = [(a.lat, a.lon), (b.lat, b.lon)]
            edge = Edge(
                id=e["id"],
                from_node=e["from"],
                to_node=e["to"],
                lanes=max(1, int(e.get("lanes", 1))),
                speed=float(e.get("speed", 11.1)),
                name=e.get("name", ""),
                geometry=geom,
                road_class=e.get("road_class", "road"),
            )
            edge.xy = [self.proj.to_xy(lat, lon) for lat, lon in geom]
            edge.finalize()
            self.edges[edge.id] = edge
            a.outgoing.append(edge.id)
            b.incoming.append(edge.id)
        self.hospitals: dict[str, Hospital] = {}
        for h in data.get("hospitals", []):
            node = h.get("node") or self.nearest_node(h["lat"], h["lon"]).id
            self.hospitals[h["id"]] = Hospital(h["id"], h["name"], h["lat"], h["lon"], node)
        self.bases: list[AmbulanceBase] = [
            AmbulanceBase(b["id"], b.get("name", b["id"]), b["node"]) for b in data.get("ambulance_bases", [])
        ]
        self._edge_between: dict[tuple[str, str], str] = {}
        for e in self.edges.values():
            cur = self._edge_between.get((e.from_node, e.to_node))
            if cur is None or self.edges[cur].length > e.length:
                self._edge_between[(e.from_node, e.to_node)] = e.id

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str | Path) -> "RoadNetwork":
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    # ------------------------------------------------------------------ queries
    @property
    def signal_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.signal]

    def bbox(self) -> tuple[float, float, float, float]:
        lats = [n.lat for n in self.nodes.values()]
        lons = [n.lon for n in self.nodes.values()]
        return min(lats), min(lons), max(lats), max(lons)

    def nearest_node(self, lat: float, lon: float, predicate: Optional[Callable[[Node], bool]] = None) -> Node:
        x, y = self.proj.to_xy(lat, lon)
        best, best_d = None, math.inf
        for n in self.nodes.values():
            if predicate and not predicate(n):
                continue
            d = (n.x - x) ** 2 + (n.y - y) ** 2
            if d < best_d:
                best, best_d = n, d
        assert best is not None
        return best

    def distance_to_node_m(self, lat: float, lon: float, node_id: str) -> float:
        x, y = self.proj.to_xy(lat, lon)
        n = self.nodes[node_id]
        return math.hypot(n.x - x, n.y - y)

    def edge_between(self, a: str, b: str) -> Optional[str]:
        return self._edge_between.get((a, b))

    def is_uturn(self, edge_in: str, edge_out: str) -> bool:
        a, b = self.edges[edge_in], self.edges[edge_out]
        return a.from_node == b.to_node and a.to_node == b.from_node

    # ------------------------------------------------------------------ routing
    def shortest_path(
        self,
        src: str,
        dst: str,
        cost: Optional[Callable[[Edge], float]] = None,
    ) -> Optional[list[str]]:
        """Dijkstra over nodes. Returns the list of edge ids (empty if src == dst)."""
        if src == dst:
            return []
        cost = cost or (lambda e: e.free_flow_time)
        dist: dict[str, float] = {src: 0.0}
        prev: dict[str, str] = {}
        heap: list[tuple[float, str]] = [(0.0, src)]
        while heap:
            d, u = heapq.heappop(heap)
            if u == dst:
                break
            if d > dist.get(u, math.inf):
                continue
            for eid in self.nodes[u].outgoing:
                e = self.edges[eid]
                nd = d + max(cost(e), 1e-6)
                if nd < dist.get(e.to_node, math.inf):
                    dist[e.to_node] = nd
                    prev[e.to_node] = eid
                    heapq.heappush(heap, (nd, e.to_node))
        if dst not in prev:
            return None
        path: list[str] = []
        cur = dst
        while cur != src:
            eid = prev[cur]
            path.append(eid)
            cur = self.edges[eid].from_node
        path.reverse()
        return path

    def shortest_path_tree(self, src: str) -> dict[str, str]:
        """Free-flow Dijkstra from `src` to every node: node -> edge used to reach it."""
        dist: dict[str, float] = {src: 0.0}
        prev: dict[str, str] = {}
        heap: list[tuple[float, str]] = [(0.0, src)]
        nodes, edges = self.nodes, self.edges
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist.get(u, math.inf):
                continue
            for eid in nodes[u].outgoing:
                e = edges[eid]
                nd = d + e.free_flow_time
                if nd < dist.get(e.to_node, math.inf):
                    dist[e.to_node] = nd
                    prev[e.to_node] = eid
                    heapq.heappush(heap, (nd, e.to_node))
        return prev

    def path_from_tree(self, prev: dict[str, str], src: str, dst: str) -> Optional[list[str]]:
        if src == dst:
            return []
        if dst not in prev:
            return None
        path, cur = [], dst
        while cur != src:
            eid = prev[cur]
            path.append(eid)
            cur = self.edges[eid].from_node
        path.reverse()
        return path

    def route_length(self, edges: Iterable[str]) -> float:
        return sum(self.edges[e].length for e in edges)

    def route_free_flow_time(self, edges: Iterable[str]) -> float:
        return sum(self.edges[e].free_flow_time for e in edges)

    def route_geometry(self, edges: list[str]) -> list[tuple[float, float]]:
        coords: list[tuple[float, float]] = []
        for eid in edges:
            g = self.edges[eid].geometry
            coords.extend(g if not coords else g[1:])
        return coords

    def latlon_at(self, edge_id: str, pos: float, lateral: float = 0.0) -> tuple[float, float, float]:
        """(lat, lon, heading) at `pos` metres along an edge, optionally shifted sideways (metres, + = kerb side)."""
        e = self.edges[edge_id]
        x, y, h = e.point_at(min(max(pos, 0.0), e.length))
        if lateral:
            # left-hand traffic: kerb is on the left of the direction of travel
            sign = -1.0 if self.left_hand_traffic else 1.0
            hr = math.radians(h)
            x += sign * lateral * math.cos(hr)
            y -= sign * lateral * math.sin(hr)
        lat, lon = self.proj.to_latlon(x, y)
        return lat, lon, h

    # ----------------------------------------------------------- signal phases
    def signal_groups(self, node_id: str) -> list[list[str]]:
        """Split a signalised node's approaches into (at most two) non-conflicting phase groups.

        Approaches whose direction of travel lies on the same axis (e.g. northbound and
        southbound) share a phase; crossing approaches get the other phase. Every
        movement from a green approach is allowed, so two groups never run together.
        """
        node = self.nodes[node_id]
        approaches = sorted(node.incoming)
        if not approaches:
            return []
        bearings = {a: self.edges[a].approach_bearing() for a in approaches}
        # choose the reference axis as the approach of the highest-capacity road
        ref = max(approaches, key=lambda a: (self.edges[a].lanes, self.edges[a].speed, a))
        g1 = [a for a in approaches if axis_difference(bearings[a], bearings[ref]) <= 45.0]
        g2 = [a for a in approaches if a not in g1]
        return [g for g in (g1, g2) if g]

    def to_geojson(self) -> dict:
        feats = []
        for e in self.edges.values():
            feats.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": [[lon, lat] for lat, lon in e.geometry]},
                    "properties": {"id": e.id, "name": e.name, "lanes": e.lanes, "speed": e.speed, "class": e.road_class},
                }
            )
        return {"type": "FeatureCollection", "features": feats}

    def summary(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "description": self.description,
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "signals": len(self.signal_nodes),
            "bbox": self.bbox(),
            "left_hand_traffic": self.left_hand_traffic,
            "lane_km": round(sum(e.length * e.lanes for e in self.edges.values()) / 1000.0, 1),
            "hospitals": len(self.hospitals),
            "bases": len(self.bases),
            "meta": self.meta,
        }
