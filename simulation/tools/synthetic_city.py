"""Synthetic OpenStreetMap-like city (Overpass JSON) for offline tests and benchmarks.

This is NOT real map data. It reproduces the *structure* that makes real OSM data
hard to simulate – curved roads drawn with many shape points, dual carriageways
(two one-way ways ~20 m apart), roundabouts, one-way residential streets, signals
tagged on the approaches rather than on the junction, hospitals mapped as building
outlines – so the importer and the simulator can be tested at city scale without
internet access.

    python simulation/tools/synthetic_city.py --km 6 5 --out data/maps/synthetic.osm.json
"""
from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path


def make_city(width_km: float = 6.0, height_km: float = 5.0, spacing_m: float = 600.0, seed: int = 7,
              lat0: float = 19.020, lon0: float = 73.000, residential: bool = True) -> dict:
    rng = random.Random(seed)
    ky = 111_195.0
    kx = 111_195.0 * math.cos(math.radians(lat0))
    elements: list[dict] = []
    nid = [10_000_000]
    wid = [50_000_000]

    def node(x: float, y: float, tags: dict | None = None) -> int:
        nid[0] += 1
        el = {"type": "node", "id": nid[0], "lat": round(lat0 + y / ky, 7), "lon": round(lon0 + x / kx, 7)}
        if tags:
            el["tags"] = tags
        elements.append(el)
        return nid[0]

    def way(refs: list[int], tags: dict):
        wid[0] += 1
        elements.append({"type": "way", "id": wid[0], "nodes": refs, "tags": tags})

    def shape(a: tuple, b: tuple, step: float = 45.0, wobble: float = 6.0) -> list[tuple]:
        """Intermediate points between a and b with a gentle curve (exclusive of a and b)."""
        d = math.dist(a, b)
        n = max(0, int(d // step) - 1)
        nx, ny = (-(b[1] - a[1]) / (d or 1), (b[0] - a[0]) / (d or 1))
        amp = rng.uniform(-wobble, wobble)
        pts = []
        for k in range(1, n + 1):
            f = k / (n + 1)
            off = amp * math.sin(math.pi * f)
            pts.append((a[0] + f * (b[0] - a[0]) + nx * off, a[1] + f * (b[1] - a[1]) + ny * off))
        return pts

    W, H = width_km * 1000, height_km * 1000
    nc, nr = int(W // spacing_m) + 1, int(H // spacing_m) + 1
    P = [[(c * spacing_m + rng.uniform(-60, 60) if 0 < c < nc - 1 else c * spacing_m,
           r * spacing_m + rng.uniform(-60, 60) if 0 < r < nr - 1 else r * spacing_m)
          for c in range(nc)] for r in range(nr)]
    h_class = ["primary" if r % 3 == 1 else "secondary" if r % 3 == 2 else "tertiary" for r in range(nr)]
    v_class = ["primary" if c % 4 == 2 else "secondary" if c % 2 else "tertiary" for c in range(nc)]
    dual_rows = {r for r in range(nr) if h_class[r] == "primary" and 0 < r < nr - 1}
    roundabouts = {(r, c) for r in range(1, nr - 1) for c in range(1, nc - 1)
                   if r not in dual_rows and h_class[r] != "primary" and v_class[c] != "primary" and rng.random() < 0.12}
    OFF = 10.0  # half distance between the carriageways of a dual road
    FR = (1 / 3, 2 / 3) if residential else ()

    # lattice nodes on arterials: crossing nodes and the points where residential streets attach
    cross_v: dict = {}  # (r, c) -> node id(s) on the vertical road at the crossing (two for dual rows)
    cross_h: dict = {}  # (r, c, side) -> node id on the horizontal carriageway
    ring: dict = {}
    for r in range(nr):
        for c in range(nc):
            x, y = P[r][c]
            if (r, c) in roundabouts:
                ids = [node(x + 18 * math.cos(a), y + 18 * math.sin(a)) for a in [k * math.pi / 4 for k in range(8)]]
                ring[(r, c)] = ids
                way(ids + [ids[0]], {"highway": h_class[r], "junction": "roundabout", "name": f"Circle {r}-{c}"})
            elif r in dual_rows:
                cross_h[(r, c, "n")] = node(x, y + OFF)
                cross_h[(r, c, "s")] = node(x, y - OFF)
            else:
                cross_h[(r, c, "")] = node(x, y)

    def hnode(r, c, side, toward_east: bool):
        if (r, c) in ring:
            return ring[(r, c)][4 if toward_east else 0]  # west point when arriving from the west
        return cross_h[(r, c, side)]

    def vnode(r, c, toward_north: bool, carriageway: str = ""):
        if (r, c) in ring:
            return ring[(r, c)][6 if toward_north else 2]
        if r in dual_rows:
            return cross_h[(r, c, carriageway or ("s" if toward_north else "n"))]
        return cross_h[(r, c, "")]

    def lerp(p, q, f):
        return (p[0] + f * (q[0] - p[0]), p[1] + f * (q[1] - p[1]))

    signals_tagged = 0
    attach_h: dict = {}  # (r, c, side, f) -> node on horizontal road between c and c+1
    attach_v: dict = {}  # (r, c, f) -> node on vertical road between r and r+1

    # horizontal arterials, one way per block (as OSM often splits them)
    for r in range(nr):
        sides = [("n", "yes", True), ("s", "yes", False)] if r in dual_rows else [("", None, None)]
        for side, oneway, westbound in sides:
            for c in range(nc - 1):
                a = hnode(r, c, side, toward_east=False)
                b = hnode(r, c + 1, side, toward_east=True)
                pa, pb = P[r][c], P[r][c + 1]
                dy = OFF if side == "n" else -OFF if side == "s" else 0
                pa, pb = (pa[0], pa[1] + dy), (pb[0], pb[1] + dy)
                refs = [a]
                prev = pa
                for f in FR:
                    q = lerp(pa, pb, f)
                    for s in shape(prev, q):
                        refs.append(node(*s))
                    k = node(*q)
                    attach_h[(r, c, side, f)] = k
                    refs.append(k)
                    prev = q
                for s in shape(prev, pb):
                    refs.append(node(*s))
                refs.append(b)
                tags = {"highway": h_class[r], "name": f"Sector Road {r}"}
                if h_class[r] == "primary":
                    tags["lanes"] = "3" if oneway else "4"
                if oneway:
                    tags["oneway"] = "yes"
                    if westbound:
                        refs = refs[::-1]
                way(refs, tags)

    # vertical arterials
    for c in range(nc):
        for r in range(nr - 1):
            a = vnode(r, c, toward_north=False, carriageway="n")
            b = vnode(r + 1, c, toward_north=True, carriageway="s")
            pa = (P[r][c][0], P[r][c][1] + (OFF if r in dual_rows else 0))
            pb = (P[r + 1][c][0], P[r + 1][c][1] - (OFF if r + 1 in dual_rows else 0))
            refs = [a]
            prev = pa
            sig_s = sig_n = None
            for f in FR:
                q = lerp(pa, pb, f)
                for s in shape(prev, q):
                    refs.append(node(*s))
                k = node(*q)
                attach_v[(r, c, f)] = k
                refs.append(k)
                prev = q
            for s in shape(prev, pb):
                refs.append(node(*s))
            refs.append(b)
            # signals tagged ~20 m before the major crossings (only some, like real OSM tagging)
            major_a = (r, c) not in ring and h_class[r] != "tertiary" and v_class[c] != "tertiary"
            major_b = (r + 1, c) not in ring and h_class[r + 1] != "tertiary" and v_class[c] != "tertiary"
            if major_a and rng.random() < 0.6:
                sig_s = node(pa[0] + rng.uniform(-2, 2), pa[1] + 20, {"highway": "traffic_signals"})
                refs.insert(1, sig_s)
                signals_tagged += 1
            if major_b and rng.random() < 0.6:
                sig_n = node(pb[0] + rng.uniform(-2, 2), pb[1] - 20, {"highway": "traffic_signals"})
                refs.insert(len(refs) - 1, sig_n)
                signals_tagged += 1
            if r in dual_rows:  # the vertical road also crosses between the two carriageways
                way([cross_h[(r, c, "s")], cross_h[(r, c, "n")]], {"highway": v_class[c], "name": f"Avenue {c}"})
            way(refs, {"highway": v_class[c], "name": f"Avenue {c}"})
        if nr - 1 in dual_rows:
            way([cross_h[(nr - 1, c, "s")], cross_h[(nr - 1, c, "n")]], {"highway": v_class[c], "name": f"Avenue {c}"})

    # residential streets inside each block
    if residential:
        for r in range(nr - 1):
            for c in range(nc - 1):
                c00, c10, c01, c11 = P[r][c], P[r][c + 1], P[r + 1][c], P[r + 1][c + 1]

                def Q(u, v):
                    return lerp(lerp(c00, c10, u), lerp(c01, c11, u), v)

                inner = {(u, v): node(*Q(u, v)) for u in FR for v in FR}
                south_side = "n" if r in dual_rows else ""
                north_side = "s" if (r + 1) in dual_rows else ""
                for v in FR:  # east-west streets from the west avenue to the east avenue
                    pts = [attach_v[(r, c, v)]] + [inner[(u, v)] for u in FR] + [attach_v[(r, c + 1, v)]]
                    xy = [Q(0, v)] + [Q(u, v) for u in FR] + [Q(1, v)]
                    _street(pts, xy, shape, node, way, rng, f"Sector {r}{c} Lane {int(v * 3)}")
                for u in FR:  # north-south streets
                    pts = [attach_h[(r, c, south_side, u)]] + [inner[(u, v)] for v in FR] + [attach_h[(r + 1, c, north_side, u)]]
                    xy = [Q(u, 0)] + [Q(u, v) for v in FR] + [Q(u, 1)]
                    _street(pts, xy, shape, node, way, rng, f"Sector {r}{c} Street {int(u * 3)}")

    # hospitals (some as building outlines) and fire stations
    for k in range(max(3, int(width_km * height_km / 6))):
        x, y = rng.uniform(0.15, 0.85) * W, rng.uniform(0.15, 0.85) * H
        if k % 2:
            ids = [node(x + dx, y + dy) for dx, dy in ((0, 0), (60, 0), (60, 40), (0, 40))]
            way(ids + [ids[0]], {"amenity": "hospital", "building": "hospital", "name": f"City Hospital {k + 1}"})
        else:
            node(x, y, {"amenity": "hospital", "name": f"City Hospital {k + 1}"})
    for k in range(max(2, int(width_km * height_km / 12))):
        node(rng.uniform(0.1, 0.9) * W, rng.uniform(0.1, 0.9) * H, {"amenity": "fire_station", "name": f"Fire Station {k + 1}"})
    return {"version": 0.6, "generator": "resqroute synthetic_city (NOT real data)",
            "osm3s": {"copyright": "synthetic test data"}, "elements": elements,
            "_stats": {"signals_tagged": signals_tagged, "roundabouts": len(roundabouts), "dual_rows": len(dual_rows),
                       "bbox": [lat0, lon0, lat0 + H / ky, lon0 + W / kx]}}


def _street(pts, xy, shape, node, way, rng, name):
    refs = [pts[0]]
    for i in range(1, len(pts)):
        refs += [node(*s) for s in shape(xy[i - 1], xy[i], 40.0, 3.0)]
        refs.append(pts[i])
    if rng.random() < 0.06:
        return  # some blocks have no through street
    tags = {"highway": "residential", "name": name}
    if rng.random() < 0.2:
        tags["oneway"] = "yes"
        if rng.random() < 0.5:
            refs = refs[::-1]
    way(refs, tags)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--km", nargs=2, type=float, default=(6.0, 5.0), metavar=("WIDTH", "HEIGHT"))
    ap.add_argument("--spacing", type=float, default=600.0, help="main-road spacing in metres")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-residential", action="store_true")
    ap.add_argument("--out", default="data/maps/synthetic.osm.json")
    a = ap.parse_args()
    city = make_city(a.km[0], a.km[1], a.spacing, a.seed, residential=not a.no_residential)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(city), encoding="utf-8")
    print(f"wrote {a.out}: {len(city['elements']):,} elements {city['_stats']}")


if __name__ == "__main__":
    main()
