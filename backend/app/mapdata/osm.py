"""OpenStreetMap data -> ResQRoute road network (``network.json`` format).

Input: an Overpass API JSON response or an OSM XML file (e.g. an export from
openstreetmap.org). Output: the dict that :class:`app.core.network.RoadNetwork`
loads – real road geometry, lanes, speed limits, signals, hospitals and
ambulance bases.

Steps
  1. keep drivable ``highway`` ways (all streets for small areas, main roads only
     for large areas), honour ``oneway`` / roundabouts / ``lanes`` / ``maxspeed``
  2. split ways at junctions; each piece keeps the way's real shape
  3. *consolidate* junctions: OSM draws a dual-carriageway crossing as 2–4 nodes a
     few metres apart and a roundabout as a ring of nodes. For a traffic simulation
     each of those is one intersection, so nodes closer than ``consolidate_m`` and
     every node of a small roundabout are merged into one junction
  4. keep the largest strongly connected component (every trip has a route)
  5. traffic signals: OSM ``highway=traffic_signals`` nodes – usually tagged on the
     stop lines of the approaches, not on the junction – are mapped to the nearest
     junction; where tagging is sparse, signals are inferred at junctions where two
     main roads cross (marked ``signal_source: "inferred"``)
  6. hospitals (``amenity=hospital`` / ``healthcare=hospital``) become destinations;
     ambulance bases are placed at hospitals and fire stations, spread over the area

Map data © OpenStreetMap contributors, ODbL.
"""
from __future__ import annotations

import gzip
import json
import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Union

from ..core.geo import haversine_m

CLASS_RANK = {
    "motorway": 6, "trunk": 5, "primary": 4, "secondary": 3, "tertiary": 2,
    "unclassified": 1, "residential": 1, "living_street": 0,
    "motorway_link": 3, "trunk_link": 3, "primary_link": 2, "secondary_link": 2, "tertiary_link": 1,
}
MAIN_ROADS = {"motorway", "trunk", "primary", "secondary", "tertiary",
              "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}
LOCAL_ROADS = {"unclassified", "residential", "living_street"}
# typical urban speeds in Indian cities when OSM has no maxspeed tag
DEFAULT_SPEED_KMH = {
    "motorway": 80, "trunk": 60, "primary": 50, "secondary": 40, "tertiary": 35, "unclassified": 30,
    "residential": 25, "living_street": 15, "motorway_link": 40, "trunk_link": 40, "primary_link": 30,
    "secondary_link": 30, "tertiary_link": 25,
}
DEFAULT_LANES = {"motorway": 3, "trunk": 2, "primary": 2, "secondary": 1, "tertiary": 1}
HIGHWAY_REGEX = "|".join(sorted(CLASS_RANK))
AUTO_FULL_DETAIL_MAX_KM2 = 40.0


# ----------------------------------------------------------------------------- input
@dataclass
class Poi:
    kind: str  # hospital | fire_station
    name: Optional[str]
    lat: float
    lon: float
    osm_id: str


@dataclass
class OsmData:
    coords: dict[int, tuple[float, float]] = field(default_factory=dict)
    node_tags: dict[int, dict] = field(default_factory=dict)
    ways: list[tuple[int, dict, list[int]]] = field(default_factory=list)
    pois: list[Poi] = field(default_factory=list)
    bbox: Optional[tuple[float, float, float, float]] = None  # requested area (south, west, north, east)

    def summary(self) -> dict:
        return {"nodes": len(self.coords), "ways": len(self.ways), "pois": len(self.pois)}


def _poi_kind(tags: dict) -> Optional[str]:
    if tags.get("amenity") == "hospital" or tags.get("healthcare") == "hospital":
        return "hospital"
    if tags.get("amenity") == "fire_station":
        return "fire_station"
    return None


def parse_overpass_json(obj: dict) -> OsmData:
    d = OsmData()
    way_elems = []
    for el in obj.get("elements", []):
        t = el.get("type")
        tags = el.get("tags") or {}
        if t == "node":
            nid = int(el["id"])
            if "lat" in el:
                d.coords[nid] = (float(el["lat"]), float(el["lon"]))
            if tags:
                d.node_tags[nid] = {**d.node_tags.get(nid, {}), **tags}
                kind = _poi_kind(tags)
                if kind and "lat" in el:
                    d.pois.append(Poi(kind, tags.get("name"), float(el["lat"]), float(el["lon"]), f"n{nid}"))
        elif t in ("way", "relation"):
            kind = _poi_kind(tags)
            if kind and "center" in el:
                c = el["center"]
                d.pois.append(Poi(kind, tags.get("name"), float(c["lat"]), float(c["lon"]), f"{t[0]}{el['id']}"))
            if t == "way" and el.get("nodes"):
                way_elems.append((int(el["id"]), tags, [int(n) for n in el["nodes"]], kind and "center" not in el))
    for wid, tags, refs, poi_needs_centroid in way_elems:
        if poi_needs_centroid:
            pts = [d.coords[r] for r in refs if r in d.coords]
            if pts:
                d.pois.append(Poi(_poi_kind(tags), tags.get("name"), sum(p[0] for p in pts) / len(pts),
                                  sum(p[1] for p in pts) / len(pts), f"w{wid}"))
        if "highway" in tags:
            d.ways.append((wid, tags, refs))
    _dedupe_pois(d)
    return d


def parse_osm_xml(source: Union[str, Path, bytes]) -> OsmData:
    """Streaming parse of an OSM XML file (works for files of hundreds of MB)."""
    import io

    d = OsmData()
    stream = io.BytesIO(source) if isinstance(source, bytes) else open(source, "rb")
    way_elems = []
    try:
        for _, el in ET.iterparse(stream, events=("end",)):
            tag = el.tag
            if tag == "node":
                nid = int(el.get("id"))
                d.coords[nid] = (float(el.get("lat")), float(el.get("lon")))
                tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
                if tags:
                    d.node_tags[nid] = tags
                    kind = _poi_kind(tags)
                    if kind:
                        d.pois.append(Poi(kind, tags.get("name"), *d.coords[nid], f"n{nid}"))
                el.clear()
            elif tag == "way":
                tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
                refs = [int(nd.get("ref")) for nd in el.findall("nd")]
                way_elems.append((int(el.get("id")), tags, refs))
                el.clear()
            elif tag == "bounds":
                d.bbox = (float(el.get("minlat")), float(el.get("minlon")), float(el.get("maxlat")), float(el.get("maxlon")))
    finally:
        if not isinstance(source, bytes):
            stream.close()
    for wid, tags, refs in way_elems:
        kind = _poi_kind(tags)
        if kind:
            pts = [d.coords[r] for r in refs if r in d.coords]
            if pts:
                d.pois.append(Poi(kind, tags.get("name"), sum(p[0] for p in pts) / len(pts),
                                  sum(p[1] for p in pts) / len(pts), f"w{wid}"))
        if "highway" in tags:
            d.ways.append((wid, tags, refs))
    _dedupe_pois(d)
    return d


def _dedupe_pois(d: OsmData):
    """A hospital is often mapped both as a node and as a building outline."""
    out: list[Poi] = []
    for p in d.pois:
        dup = next((q for q in out if q.kind == p.kind and haversine_m(p.lat, p.lon, q.lat, q.lon) < 120
                    and (not p.name or not q.name or p.name == q.name)), None)
        if dup is None:
            out.append(p)
        elif not dup.name and p.name:
            dup.name = p.name
    d.pois = out


def load_osm(path: Union[str, Path]) -> OsmData:
    """Overpass JSON (.json / .json.gz) or OSM XML (.osm / .xml / .osm.gz)."""
    path = Path(path)
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    head = raw[:200].lstrip()
    if head.startswith(b"{"):
        return parse_overpass_json(json.loads(raw))
    return parse_osm_xml(raw)


# ----------------------------------------------------------------------------- helpers
def parse_speed(v: Optional[str], cls: str) -> float:
    kmh = DEFAULT_SPEED_KMH.get(cls, 30)
    if v:
        m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(mph|km/h|kmh)?", v)
        if m:
            kmh = float(m.group(1)) * (1.609 if m.group(2) == "mph" else 1.0)
    return round(kmh / 3.6, 2)


def parse_int(v: Optional[str]) -> Optional[int]:
    try:
        return int(float(v.split(";")[0])) if v else None
    except ValueError:
        return None


def lanes_for(tags: dict, cls: str, oneway: bool, forward: bool) -> int:
    if forward and parse_int(tags.get("lanes:forward")):
        return max(1, min(5, parse_int(tags["lanes:forward"])))
    if not forward and parse_int(tags.get("lanes:backward")):
        return max(1, min(5, parse_int(tags["lanes:backward"])))
    total = parse_int(tags.get("lanes"))
    if total:
        return max(1, min(5, total if oneway else total // 2))
    return DEFAULT_LANES.get(cls, 1)


class GridIndex:
    """Uniform grid over lat/lon for nearest-point queries (metres)."""

    def __init__(self, cell_m: float, lat0: float):
        self.cell = cell_m
        self.ky = 111_195.0
        self.kx = 111_195.0 * math.cos(math.radians(lat0))
        self.cells: dict[tuple[int, int], list] = defaultdict(list)

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        return lon * self.kx, lat * self.ky

    def add(self, key, lat: float, lon: float):
        x, y = self.xy(lat, lon)
        self.cells[(int(x // self.cell), int(y // self.cell))].append((key, x, y))

    def near(self, lat: float, lon: float, radius_m: float) -> list[tuple[float, object]]:
        x, y = self.xy(lat, lon)
        r = int(math.ceil(radius_m / self.cell))
        cx, cy = int(x // self.cell), int(y // self.cell)
        out = []
        for i in range(cx - r, cx + r + 1):
            for j in range(cy - r, cy + r + 1):
                for key, px, py in self.cells.get((i, j), ()):
                    dd = math.hypot(px - x, py - y)
                    if dd <= radius_m:
                        out.append((dd, key))
        out.sort(key=lambda t: t[0])
        return out

    def nearest(self, lat: float, lon: float, max_m: float = 5000.0):
        radius = self.cell
        while radius <= max_m * 2:
            hits = self.near(lat, lon, radius)
            if hits:
                return hits[0][1]
            radius *= 2
        return None


def largest_scc(nodes: Iterable, adj: dict) -> set:
    """Kosaraju, iterative."""
    nodes = list(nodes)
    order, seen = [], set()
    for s in nodes:
        if s in seen:
            continue
        stack = [(s, iter(adj.get(s, ())))]
        seen.add(s)
        while stack:
            u, it = stack[-1]
            for w in it:
                if w not in seen:
                    seen.add(w)
                    stack.append((w, iter(adj.get(w, ()))))
                    break
            else:
                order.append(u)
                stack.pop()
    radj = defaultdict(list)
    for u, ws in adj.items():
        for w in ws:
            radj[w].append(u)
    comp, best = {}, set()
    for s in reversed(order):
        if s in comp:
            continue
        cur, stack = {s}, [s]
        comp[s] = s
        while stack:
            u = stack.pop()
            for w in radj.get(u, ()):
                if w not in comp:
                    comp[w] = s
                    cur.add(w)
                    stack.append(w)
        if len(cur) > len(best):
            best = cur
    return best


class _UnionFind:
    def __init__(self):
        self.parent: dict = {}
        self.box: dict = {}  # root -> (min_lat, min_lon, max_lat, max_lon)

    def add(self, k, lat, lon):
        if k not in self.parent:
            self.parent[k] = k
            self.box[k] = (lat, lon, lat, lon)

    def find(self, k):
        p = self.parent
        while p[k] != k:
            p[k] = p[p[k]]
            k = p[k]
        return k

    def union(self, a, b, max_extent_m: Optional[float] = None) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        A, B = self.box[ra], self.box[rb]
        box = (min(A[0], B[0]), min(A[1], B[1]), max(A[2], B[2]), max(A[3], B[3]))
        if max_extent_m is not None and haversine_m(box[0], box[1], box[2], box[3]) > max_extent_m:
            return False
        if str(ra) > str(rb):
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.box[ra] = box
        del self.box[rb]
        return True


# ----------------------------------------------------------------------------- build
@dataclass
class BuildOptions:
    detail: str = "auto"  # auto | full | main
    consolidate_m: float = 12.0
    short_link_m: float = 28.0
    max_junction_extent_m: float = 70.0
    roundabout_max_diameter_m: float = 90.0
    guess_signals: Optional[int] = None  # None = infer where main roads cross; 0 = OSM-tagged only; N = at least N
    signal_snap_m: float = 50.0
    signal_spacing_m: float = 180.0
    n_bases: Optional[int] = None
    hospitals: Optional[list[dict]] = None
    bases: Optional[list[dict]] = None
    name: Optional[str] = None
    area_id: Optional[str] = None


def area_km2(bbox: tuple[float, float, float, float]) -> float:
    s, w, n, e = bbox
    return haversine_m(s, w, n, w) * haversine_m(s, w, s, e) / 1e6


def build_network(osm: OsmData, opts: Optional[BuildOptions] = None) -> dict:
    opts = opts or BuildOptions()
    coords = osm.coords

    # ---- 1. road ways
    def drivable(tags: dict) -> bool:
        cls = tags.get("highway")
        if cls not in CLASS_RANK or tags.get("area") == "yes":
            return False
        if tags.get("access") in ("private", "no") and tags.get("motor_vehicle") not in ("yes", "designated"):
            return False
        if tags.get("motor_vehicle") == "no" or tags.get("motorcar") == "no":
            return False
        return True

    road_ways = [(w, t, [r for r in refs if r in coords]) for w, t, refs in osm.ways if drivable(t)]
    road_ways = [(w, t, refs) for w, t, refs in road_ways if len(refs) >= 2]
    if not road_ways:
        raise ValueError("no drivable roads found in the OpenStreetMap data")
    lats = [coords[r][0] for _, _, refs in road_ways for r in refs]
    lons = [coords[r][1] for _, _, refs in road_ways for r in refs]
    data_bbox = (min(lats), min(lons), max(lats), max(lons))
    bbox = osm.bbox or data_bbox
    km2 = area_km2(bbox)
    detail = opts.detail
    if detail == "auto":
        detail = "full" if km2 <= AUTO_FULL_DETAIL_MAX_KM2 else "main"
    if detail == "main":
        main_only = [x for x in road_ways if x[1]["highway"] in MAIN_ROADS]
        if len(main_only) >= 10:
            road_ways = main_only
        else:
            detail = "full"

    # ---- 2. junction nodes (way endpoints and nodes shared by several ways)
    usage = defaultdict(int)
    for _, _, refs in road_ways:
        for i, r in enumerate(refs):
            usage[r] += 2 if i in (0, len(refs) - 1) else 1
    junction_osm = {r for r, u in usage.items() if u >= 2}

    # ---- 3. consolidation
    uf = _UnionFind()
    for r in junction_osm:
        uf.add(r, *coords[r])
    roundabout_roots: set = set()
    for _, tags, refs in road_ways:
        if tags.get("junction") in ("roundabout", "circular"):
            js = [r for r in refs if r in junction_osm]
            if len(js) < 2:
                continue
            la = [coords[r][0] for r in refs]
            lo = [coords[r][1] for r in refs]
            if haversine_m(min(la), min(lo), max(la), max(lo)) <= opts.roundabout_max_diameter_m * 1.42:
                for r in js[1:]:
                    uf.union(js[0], r)
                roundabout_roots.add(js[0])
    if opts.consolidate_m > 0:
        grid = GridIndex(opts.consolidate_m, (bbox[0] + bbox[2]) / 2)
        for r in junction_osm:
            grid.add(r, *coords[r])
        for r in sorted(junction_osm):
            for d, other in grid.near(*coords[r], opts.consolidate_m):
                if other != r:
                    uf.union(r, other, opts.max_junction_extent_m)
        # short links between two real intersections (the crossing pieces of dual carriageways,
        # staggered junctions) are part of one intersection for traffic purposes
        degree = defaultdict(set)
        pieces = []
        for _, tags, refs in road_ways:
            last = 0
            for i in range(1, len(refs)):
                if refs[i] in junction_osm:
                    a, b = refs[last], refs[i]
                    if a in junction_osm and a != b:
                        degree[a].add(b)
                        degree[b].add(a)
                        L = sum(haversine_m(*coords[refs[j]], *coords[refs[j + 1]]) for j in range(last, i))
                        pieces.append((L, a, b))
                    last = i
        for L, a, b in sorted(pieces):
            if L > opts.short_link_m:
                break
            if len(degree[a]) >= 3 and len(degree[b]) >= 3:
                uf.union(a, b, opts.max_junction_extent_m)
    roundabout_clusters = {uf.find(r) for r in roundabout_roots}
    members = defaultdict(list)
    for r in junction_osm:
        members[uf.find(r)].append(r)
    cluster_coord = {root: (sum(coords[m][0] for m in ms) / len(ms), sum(coords[m][1] for m in ms) / len(ms))
                     for root, ms in members.items()}
    cluster_of = {r: uf.find(r) for r in junction_osm}
    cluster_radius = {root: max(haversine_m(*coords[m], *cluster_coord[root]) for m in ms) + opts.consolidate_m * 0.5
                      for root, ms in members.items()}

    # ---- 4. edges
    edges: dict[tuple, dict] = {}
    node_rank = defaultdict(int)
    node_names = defaultdict(lambda: defaultdict(int))
    for wid, tags, refs in road_ways:
        cls = tags["highway"]
        oneway_tag = tags.get("oneway", "")
        roundabout = tags.get("junction") in ("roundabout", "circular")
        if oneway_tag == "-1":
            refs = list(reversed(refs))
            oneway = True
        else:
            oneway = oneway_tag in ("yes", "true", "1") or roundabout or cls in ("motorway", "motorway_link")
        name = tags.get("name") or tags.get("ref") or cls.replace("_", " ").title()
        seg_start = 0
        k = 0
        for i in range(1, len(refs)):
            if refs[i] not in junction_osm and i != len(refs) - 1:
                continue
            seg = refs[seg_start: i + 1]
            seg_start = i
            if seg[0] not in junction_osm or seg[-1] not in junction_osm:
                continue
            a, b = cluster_of[seg[0]], cluster_of[seg[-1]]
            k += 1
            if a == b:
                continue  # inside one consolidated junction / roundabout
            pts = [coords[r] for r in seg]
            # snap the ends onto the consolidated junction centre; drop shape points inside the junction
            ca, cb = cluster_coord[a], cluster_coord[b]
            inner = [p for p in pts[1:-1]
                     if haversine_m(*p, *ca) > cluster_radius[a] and haversine_m(*p, *cb) > cluster_radius[b]]
            geom = [ca] + inner + [cb]
            for forward in ((True,) if oneway else (True, False)):
                g = geom if forward else list(reversed(geom))
                length = sum(haversine_m(*g[j], *g[j + 1]) for j in range(len(g) - 1))
                if length < 2.0:
                    continue
                u, v = (a, b) if forward else (b, a)
                cand = {
                    "id": f"w{wid}_{k}{'f' if forward else 'b'}", "from": u, "to": v,
                    "lanes": lanes_for(tags, cls, oneway, forward),
                    "speed": parse_speed(tags.get("maxspeed"), cls), "name": name,
                    "road_class": cls, "geometry": [[round(p[0], 7), round(p[1], 7)] for p in g], "_len": length,
                }
                key = (u, v)
                if key not in edges or (CLASS_RANK[cls], -length) > (CLASS_RANK[edges[key]["road_class"]], -edges[key]["_len"]):
                    edges[key] = cand
            for c in (a, b):
                node_rank[c] = max(node_rank[c], CLASS_RANK[cls])
                if tags.get("name"):
                    node_names[c][tags["name"]] += CLASS_RANK[cls] + 1

    adj = defaultdict(list)
    for (a, b) in edges:
        adj[a].append(b)
    keep = largest_scc({n for k in edges for n in k}, adj)
    edges = {k: v for k, v in edges.items() if k[0] in keep and k[1] in keep}
    nbrs = defaultdict(set)
    in_classes = defaultdict(list)
    importance = defaultdict(int)
    for (a, b), e in edges.items():
        nbrs[a].add(b)
        nbrs[b].add(a)
        in_classes[b].append((CLASS_RANK[e["road_class"]], e["name"]))
        importance[a] += CLASS_RANK[e["road_class"]]
        importance[b] += CLASS_RANK[e["road_class"]]
    junctions = sorted(n for n in keep if len(nbrs[n]) >= 3)
    lat_c = (bbox[0] + bbox[2]) / 2
    jgrid = GridIndex(60.0, lat_c)
    for n in junctions:
        jgrid.add(n, *cluster_coord[n])

    # ---- 5. traffic signals
    signals: dict = {}
    osm_signal_nodes = [nid for nid, t in osm.node_tags.items()
                        if t.get("highway") == "traffic_signals" and nid in coords
                        and t.get("traffic_signals") not in ("crossing", "crossing_only", "blinker", "emergency", "ramp_meter")]
    for nid in osm_signal_nodes:
        c = cluster_of.get(nid)
        if c in keep and len(nbrs[c]) >= 3 and c not in roundabout_clusters:
            signals[c] = "osm"
            continue
        for d, j in jgrid.near(*coords[nid], opts.signal_snap_m):
            if j not in roundabout_clusters:
                signals[j] = "osm"
                break
    n_osm = len(signals)

    def far_from_signals(j) -> bool:
        return not any(s in signals for _, s in jgrid.near(*cluster_coord[j], opts.signal_spacing_m))

    def major_crossing(j) -> bool:
        """Two different main roads meet here: one secondary or better, the other tertiary or better."""
        best: dict[str, int] = {}
        for rank, nm in in_classes[j]:
            best[nm] = max(best.get(nm, 0), rank)
        ranks = sorted(best.values(), reverse=True)
        return len(ranks) >= 2 and ranks[0] >= 3 and ranks[1] >= 2 and len(nbrs[j]) >= 3

    candidates = sorted((j for j in junctions if j not in roundabout_clusters and j not in signals),
                        key=lambda n: (-importance[n], str(n)))
    if opts.guess_signals is None:
        for j in candidates:
            if major_crossing(j) and far_from_signals(j):
                signals[j] = "inferred"
    elif opts.guess_signals > 0:
        for j in candidates:
            if len(signals) >= opts.guess_signals:
                break
            if far_from_signals(j):
                signals[j] = "inferred"

    # ---- nodes
    def jname(c) -> str:
        names = sorted(node_names[c].items(), key=lambda kv: -kv[1])
        if len(names) >= 2:
            return f"{names[0][0]} × {names[1][0]}"
        if names:
            return f"{names[0][0]} (junction)"
        return f"Junction {c}"

    node_id = {c: f"n{c}" for c in keep}
    nodes = []
    for c in sorted(keep, key=str):
        lat, lon = cluster_coord[c]
        nd = {"id": node_id[c], "lat": round(lat, 7), "lon": round(lon, 7), "signal": c in signals,
              "name": jname(c)}
        if c in signals:
            nd["signal_source"] = signals[c]
        if c in roundabout_clusters:
            nd["roundabout"] = True
        nodes.append(nd)
    out_edges = []
    for e in edges.values():
        e = dict(e)
        e.pop("_len")
        e["from"], e["to"] = node_id[e["from"]], node_id[e["to"]]
        out_edges.append(e)

    # nodes usable as trip ends (have both in and out edges)
    io_grid = GridIndex(150.0, lat_c)
    has_in = {e["to"] for e in out_edges}
    has_out = {e["from"] for e in out_edges}
    for nd in nodes:
        if nd["id"] in has_in and nd["id"] in has_out:
            io_grid.add(nd["id"], nd["lat"], nd["lon"])

    def snap(lat, lon) -> str:
        return io_grid.nearest(lat, lon, 20000)

    def inside(p: Poi) -> bool:
        s, w, n, e = data_bbox
        return s - 0.002 <= p.lat <= n + 0.002 and w - 0.002 <= p.lon <= e + 0.002

    # ---- 6. hospitals
    hospitals = []
    if opts.hospitals:
        for h in opts.hospitals:
            hospitals.append({**h, "node": snap(h["lat"], h["lon"])})
    else:
        for i, p in enumerate(sorted((p for p in osm.pois if p.kind == "hospital" and inside(p)), key=lambda p: p.osm_id)):
            hospitals.append({"id": f"H{i + 1}", "name": p.name or f"Hospital (OSM {p.osm_id})", "lat": p.lat,
                              "lon": p.lon, "node": snap(p.lat, p.lon), "osm_id": p.osm_id})
    if not hospitals:
        c = snap(lat_c, (bbox[1] + bbox[3]) / 2)
        cn = next(n for n in nodes if n["id"] == c)
        hospitals.append({"id": "H1", "name": "Hospital (placeholder – none mapped in this area)",
                          "lat": cn["lat"], "lon": cn["lon"], "node": c})

    # ---- ambulance bases: hospitals and fire stations first, spread out (farthest-point sampling)
    if opts.bases:
        bases = [{**b, "node": snap(b["lat"], b["lon"])} for b in opts.bases]
    else:
        n_bases = opts.n_bases or int(min(12, max(3, round(km2 / 5.0))))
        pool = [("Fire station " + (p.name or p.osm_id) if not (p.name or "").lower().startswith("fire") else p.name,
                 p.lat, p.lon) for p in osm.pois if p.kind == "fire_station" and inside(p)]
        pool += [(f"Ambulance bay – {h['name']}", h["lat"], h["lon"]) for h in hospitals]
        pts: list[tuple[str, float, float]] = []
        if pool:
            pts.append(min(pool, key=lambda p: haversine_m(p[1], p[2], lat_c, (bbox[1] + bbox[3]) / 2)))
        while len(pts) < min(n_bases, len(pool)):
            nxt = max((p for p in pool if p not in pts), key=lambda p: min(haversine_m(p[1], p[2], q[1], q[2]) for q in pts))
            if min(haversine_m(nxt[1], nxt[2], q[1], q[2]) for q in pts) < 600:
                break
            pts.append(nxt)
        jpool = [n for n in nodes if n["id"] in has_in and n["id"] in has_out and len(nbrs[_cid(n["id"])]) >= 3]
        while len(pts) < n_bases and jpool:
            if pts:
                nd = max(jpool, key=lambda n: min(haversine_m(n["lat"], n["lon"], q[1], q[2]) for q in pts))
            else:
                nd = jpool[0]
            pts.append((f"Ambulance base {len(pts) + 1}", nd["lat"], nd["lon"]))
        bases, used = [], set()
        for name, lat, lon in pts:
            node = snap(lat, lon)
            if node in used:
                continue
            used.add(node)
            bases.append({"id": f"A{len(bases) + 1}", "name": name, "node": node, "lat": lat, "lon": lon})

    # ---- scenario used by the matched experiments: a base far from a central pickup
    centre = snap(lat_c, (bbox[1] + bbox[3]) / 2)
    cn = next(n for n in nodes if n["id"] == centre)
    exp_base = max(bases, key=lambda b: haversine_m(b["lat"], b["lon"], cn["lat"], cn["lon"])) if bases else None
    exp_h = min(hospitals, key=lambda h: abs(haversine_m(h["lat"], h["lon"], cn["lat"], cn["lon"]) - 2000))
    n_sig = len(signals)
    name = opts.name or "OpenStreetMap area"
    return {
        "name": name,
        "source": "osm",
        "description": f"{name}: real roads from OpenStreetMap ({detail} detail). "
                       "Map data © OpenStreetMap contributors (ODbL).",
        "left_hand_traffic": True,
        "meta": {
            "area_id": opts.area_id, "bbox": list(bbox), "area_km2": round(km2, 1), "detail": detail,
            "osm_signals": n_osm, "inferred_signals": n_sig - n_osm, "osm_ways": len(road_ways),
            "hospitals_from_osm": not (len(hospitals) == 1 and "placeholder" in hospitals[0]["name"]),
        },
        "nodes": nodes,
        "edges": out_edges,
        "hospitals": hospitals,
        "ambulance_bases": bases,
        "scenario": {
            "experiment_base": exp_base["id"] if exp_base else None,
            "experiment_pickup_node": centre,
            "experiment_hospital": exp_h["id"],
        },
    }


def _cid(node_id: str):
    """Cluster key from a node id ("n123" -> 123)."""
    s = node_id[1:]
    return int(s) if s.lstrip("-").isdigit() else s
