"""Build the SUMO network from data/network.json (or the demo network).

Node and edge ids are kept identical to network.json so the backend, the
built-in engine, the dashboard and SUMO all refer to the same roads. Coordinates
are written in the backend's local metric projection with offset normalisation
disabled, so SUMO x/y convert back to lat/lon without any projection library.

    python simulation/tools/build_sumo_net.py                  # uses data/network.json if present, else the demo
    python simulation/tools/build_sumo_net.py --network data/network.demo.json

Requires SUMO: either `pip install eclipse-sumo` or a system install with SUMO_HOME set.
Outputs: simulation/network/resq.net.xml and simulation/network/resq.sumocfg (open the
latter in sumo-gui to inspect the network visually).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.core.network import RoadNetwork  # noqa: E402


def find_sumo_binary(name: str) -> str:
    home = os.environ.get("SUMO_HOME")
    if not home:
        try:
            import sumo  # type: ignore  # provided by `pip install eclipse-sumo`

            home = sumo.SUMO_HOME
        except ImportError:
            home = None
    if home:
        for cand in (Path(home) / "bin" / name, Path(home) / "bin" / f"{name}.exe"):
            if cand.exists():
                return str(cand)
    found = shutil.which(name)
    if found:
        return found
    raise SystemExit(f"could not find '{name}'. Install SUMO (pip install eclipse-sumo) or set SUMO_HOME.")


def default_network_path() -> Path:
    real = ROOT / "data" / "network.json"
    return real if real.exists() else ROOT / "data" / "network.demo.json"


def build(network_path: Path, out_dir: Path) -> Path:
    net = RoadNetwork.load(network_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    nod, edg = out_dir / "resq.nod.xml", out_dir / "resq.edg.xml"
    with open(nod, "w", encoding="utf-8") as f:
        f.write("<nodes>\n")
        for n in net.nodes.values():
            typ = "traffic_light" if n.signal else "priority"
            f.write(f'  <node id={quoteattr(n.id)} x="{n.x:.2f}" y="{n.y:.2f}" type="{typ}"/>\n')
        f.write("</nodes>\n")
    with open(edg, "w", encoding="utf-8") as f:
        f.write("<edges>\n")
        for e in net.edges.values():
            shape = " ".join(f"{x:.2f},{y:.2f}" for x, y in e.xy)
            f.write(
                f'  <edge id={quoteattr(e.id)} from={quoteattr(e.from_node)} to={quoteattr(e.to_node)} '
                f'numLanes="{e.lanes}" speed="{e.speed:.2f}" name={quoteattr(e.name)} shape="{shape}"/>\n'
            )
        f.write("</edges>\n")
    net_file = out_dir / "resq.net.xml"
    cmd = [
        find_sumo_binary("netconvert"),
        "--node-files", str(nod), "--edge-files", str(edg),
        "--output-file", str(net_file),
        "--offset.disable-normalization", "true",
        "--lefthand", "true" if net.left_hand_traffic else "false",
        "--no-turnarounds", "true",
        "--tls.default-type", "static",
        "--junctions.join", "false",
        "--no-warnings", "true",
    ]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    (out_dir / "resq.sumocfg").write_text(
        '<configuration>\n  <input><net-file value="resq.net.xml"/></input>\n'
        '  <time><step-length value="0.5"/></time>\n</configuration>\n',
        encoding="utf-8",
    )
    print(f"wrote {net_file}")
    return net_file


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--network", default=None)
    ap.add_argument("--out-dir", default=str(ROOT / "simulation" / "network"))
    args = ap.parse_args()
    build(Path(args.network) if args.network else default_network_path(), Path(args.out_dir))


if __name__ == "__main__":
    main()
