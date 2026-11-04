"""Generate the bundled demo network (data/network.demo.json).

This is a *stylised* road grid laid over the Nerul–Seawoods area so the whole system
runs out of the box without internet access. It is NOT real road geometry. For the
real roads, run:

    python simulation/tools/fetch_osm.py          # downloads OpenStreetMap data
    python simulation/tools/osm_to_network.py     # writes data/network.json

Usage:
    python simulation/tools/make_demo_network.py [--out data/network.demo.json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

LATS = [19.0180, 19.0240, 19.0300, 19.0360, 19.0420]  # rows, south -> north
LONS = [73.0100, 73.0170, 73.0240, 73.0310, 73.0380]  # columns, west -> east
ARTERIAL_ROW = 2  # east-west arterial
ARTERIAL_COL = 2  # north-south arterial


def nid(r: int, c: int) -> str:
    return f"n{r}{c}"


def build() -> dict:
    nodes = []
    for r, lat in enumerate(LATS):
        for c, lon in enumerate(LONS):
            interior = 0 < r < len(LATS) - 1 and 0 < c < len(LONS) - 1
            nodes.append({"id": nid(r, c), "lat": lat, "lon": lon, "signal": interior, "name": f"Junction {r}-{c}"})
    edges = []

    def add(a: tuple[int, int], b: tuple[int, int], arterial: bool, name: str):
        for (p, q) in ((a, b), (b, a)):
            pa, pb = nid(*p), nid(*q)
            edges.append(
                {
                    "id": f"{pa}_{pb}",
                    "from": pa,
                    "to": pb,
                    "lanes": 2 if arterial else 1,
                    "speed": 13.89 if arterial else 11.11,
                    "name": name,
                    "road_class": "primary" if arterial else "residential",
                    "geometry": [[LATS[p[0]], LONS[p[1]]], [LATS[q[0]], LONS[q[1]]]],
                }
            )

    for r in range(len(LATS)):
        for c in range(len(LONS) - 1):
            art = r == ARTERIAL_ROW
            add((r, c), (r, c + 1), art, "Demo Arterial East-West" if art else f"Demo Sector Road {r + 1}")
    for c in range(len(LONS)):
        for r in range(len(LATS) - 1):
            art = c == ARTERIAL_COL
            add((r, c), (r + 1, c), art, "Demo Arterial North-South" if art else f"Demo Cross Road {chr(65 + c)}")

    return {
        "name": "ResQRoute demo grid (Nerul–Seawoods area)",
        "source": "demo",
        "description": "Stylised 5x5 grid placed over Nerul–Seawoods for offline use. Not real road geometry; "
        "run the OSM import for real roads.",
        "left_hand_traffic": True,
        "nodes": nodes,
        "edges": edges,
        "hospitals": [
            {"id": "H1", "name": "Demo Hospital - North", "lat": LATS[4], "lon": LONS[3], "node": nid(4, 3)},
            {"id": "H2", "name": "Demo Hospital - South-East", "lat": LATS[0], "lon": LONS[4], "node": nid(0, 4)},
        ],
        "ambulance_bases": [
            {"id": "A1", "name": "Station West", "node": nid(2, 0)},
            {"id": "A2", "name": "Station North-West", "node": nid(4, 0)},
            {"id": "A3", "name": "Station East", "node": nid(2, 4)},
        ],
        "scenario": {
            "experiment_base": "A1",
            "experiment_pickup_node": nid(2, 3),
            "experiment_hospital": "H2",
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "network.demo.json"))
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build(), indent=1), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
