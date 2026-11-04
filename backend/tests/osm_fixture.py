"""Builds a small synthetic OpenStreetMap XML file for testing the importer offline."""
from __future__ import annotations

from pathlib import Path


def write_fixture(path: Path) -> Path:
    lat0, lon0, d = 19.0300, 73.0200, 0.0040
    nodes, ways = [], []
    nid = 1000

    def node(lat, lon, tags=None):
        nonlocal nid
        nid += 1
        nodes.append((nid, lat, lon, tags or {}))
        return nid

    grid = {(r, c): node(lat0 + r * d, lon0 + c * d) for r in range(3) for c in range(3)}
    wid = 500
    # horizontal roads with a mid shape point (curved), primary in the middle row
    for r in range(3):
        refs = []
        for c in range(3):
            refs.append(grid[(r, c)])
            if c < 2:
                refs.append(node(lat0 + r * d + 0.0004, lon0 + (c + 0.5) * d))
        wid += 1
        tags = {"highway": "primary" if r == 1 else "residential", "name": f"Test Road {r}"}
        if r == 1:
            tags["lanes"] = "4"
            tags["maxspeed"] = "50"
        ways.append((wid, refs, tags))
    for c in range(3):
        wid += 1
        tags = {"highway": "tertiary", "name": f"Cross {c}"}
        if c == 2:
            tags["oneway"] = "yes"  # northbound only
        ways.append((wid, [grid[(r, c)] for r in range(3)], tags))
    # a traffic signal tagged on the approach, 15 m south of the centre junction
    sig = node(lat0 + d - 0.000135, lon0 + d, {"highway": "traffic_signals"})
    # splice the signal node into the cross road 1
    for i, (w, refs, tags) in enumerate(ways):
        if tags.get("name") == "Cross 1":
            refs.insert(1, sig)
    node(lat0 + 2 * d + 0.0003, lon0 + 2 * d, {"amenity": "hospital", "name": "Test Hospital"})
    # a disconnected stub that must be dropped by the SCC filter
    a, b = node(lat0 - 0.01, lon0 - 0.01), node(lat0 - 0.011, lon0 - 0.01)
    wid += 1
    ways.append((wid, [a, b], {"highway": "residential", "oneway": "yes"}))

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6" generator="test">']
    for i, lat, lon, tags in nodes:
        if tags:
            lines.append(f'<node id="{i}" lat="{lat}" lon="{lon}">')
            lines += [f'<tag k="{k}" v="{v}"/>' for k, v in tags.items()]
            lines.append("</node>")
        else:
            lines.append(f'<node id="{i}" lat="{lat}" lon="{lon}"/>')
    for w, refs, tags in ways:
        lines.append(f'<way id="{w}">')
        lines += [f'<nd ref="{r}"/>' for r in refs]
        lines += [f'<tag k="{k}" v="{v}"/>' for k, v in tags.items()]
        lines.append("</way>")
    lines.append("</osm>")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
