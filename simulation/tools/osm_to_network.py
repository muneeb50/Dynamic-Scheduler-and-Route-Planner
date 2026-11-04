"""Convert an OpenStreetMap extract into a simulation network (real road geometry).

    python simulation/tools/osm_to_network.py                                   # data/osm/area.osm -> data/network.json
    python simulation/tools/osm_to_network.py --in export.osm --out data/network.json
    python simulation/tools/osm_to_network.py --in area.osm.json --detail main  # main roads only

The dashboard does this for you (Live map → Map area). Accepts OSM XML (an export
from openstreetmap.org) or an Overpass JSON response. See backend/app/mapdata/osm.py
for what the importer does (junction consolidation, roundabouts, signal mapping and
inference, hospitals, ambulance bases).

After importing, rebuild the SUMO network if you use SUMO:
    python simulation/tools/build_sumo_net.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.mapdata.osm import BuildOptions, build_network, load_osm  # noqa: E402


def convert(osm_path: Path, guess_signals: Optional[int] = None, hospitals_file: Optional[str] = None,
            bases_file: Optional[str] = None, n_bases: Optional[int] = None, detail: str = "auto",
            name: Optional[str] = None) -> dict:
    osm = load_osm(osm_path)
    opts = BuildOptions(
        detail=detail, guess_signals=guess_signals, n_bases=n_bases, name=name or f"OSM import ({Path(osm_path).name})",
        hospitals=json.loads(Path(hospitals_file).read_text(encoding="utf-8")) if hospitals_file else None,
        bases=json.loads(Path(bases_file).read_text(encoding="utf-8")) if bases_file else None,
    )
    return build_network(osm, opts)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", default=str(ROOT / "data" / "osm" / "area.osm"))
    ap.add_argument("--out", default=str(ROOT / "data" / "network.json"))
    ap.add_argument("--detail", choices=["auto", "full", "main"], default="auto")
    ap.add_argument("--guess-signals", type=int, default=None,
                    help="default: infer signals where main roads cross; 0 = only OSM-tagged; N = at least N")
    ap.add_argument("--hospitals", help="JSON list of {id, name, lat, lon} to use instead of OSM hospitals")
    ap.add_argument("--bases", help="JSON list of {id, name, lat, lon} ambulance bases")
    ap.add_argument("--name", help="display name of the area")
    args = ap.parse_args()
    net = convert(Path(args.inp), args.guess_signals, args.hospitals, args.bases, detail=args.detail, name=args.name)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(net), encoding="utf-8")
    sig = sum(1 for n in net["nodes"] if n["signal"])
    print(f"wrote {args.out}: {len(net['nodes'])} junctions, {len(net['edges'])} road segments, {sig} signals "
          f"({net['meta']['osm_signals']} from OSM), {len(net['hospitals'])} hospitals, {len(net['ambulance_bases'])} bases")


if __name__ == "__main__":
    main()
