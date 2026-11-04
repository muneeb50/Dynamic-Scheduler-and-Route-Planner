"""Download a real map area from OpenStreetMap (Overpass API) and build the simulation network.

The dashboard does the same from Live map → Map area. Command line:

    python simulation/tools/fetch_osm.py                         # Nerul – Seawoods (default preset)
    python simulation/tools/fetch_osm.py --area navi-mumbai      # city scale, main roads
    python simulation/tools/fetch_osm.py --bbox 19.010 73.005 19.050 73.045 --name "My area"
    python simulation/tools/fetch_osm.py --list

The result is stored in data/maps/ and becomes the map used on the next start.
If Overpass is busy, export the area from https://www.openstreetmap.org/export and
run osm_to_network.py on the file (or upload it in the dashboard).

Map data © OpenStreetMap contributors, available under the ODbL.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.mapdata.manager import DEFAULT_AREA, PRESETS, MapManager  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--area", default=None, help=f"preset id ({', '.join(PRESETS)})")
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    ap.add_argument("--name")
    ap.add_argument("--detail", choices=["auto", "full", "main"], default="auto")
    ap.add_argument("--refresh", action="store_true", help="download again even if cached")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    mm = MapManager(ROOT / "data" / "maps")
    if a.list:
        for ar in mm.areas():
            print(f"{ar['id']:22s} {ar['name']:45s} {ar.get('area_km2') or '':>6} km²  {'cached' if ar['cached'] else ''}")
        return
    if a.bbox:
        mm.request(bbox=tuple(a.bbox), name=a.name or "Custom area", detail=a.detail, refresh=a.refresh)
    else:
        mm.request(area=a.area or DEFAULT_AREA, detail=a.detail, refresh=a.refresh)
    last = ""
    while mm.busy():
        msg = mm.status.get("message", "")
        if msg != last:
            print(msg)
            last = msg
        time.sleep(0.5)
    print(mm.status.get("message"))
    if mm.status["state"] != "ready":
        sys.exit(1)
    print("Restart the backend (or it switches automatically if it is running and you used the dashboard).")


if __name__ == "__main__":
    main()
