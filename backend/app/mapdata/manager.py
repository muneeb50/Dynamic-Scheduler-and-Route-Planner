"""Map areas: presets, cache, background download/build, and the "current map" pointer.

Files (all under ``data/maps``):
  <key>.osm.json.gz      raw Overpass response (re-used offline; rebuild without downloading)
  <key>.network.json     the simulation network built from it
  current.json           {"key": ..., "network": ...} – loaded on the next start
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .osm import BuildOptions, area_km2, build_network, load_osm, parse_overpass_json
from .overpass import MAX_AREA_KM2, fetch_overpass

log = logging.getLogger("resqroute.map")

# (south, west, north, east)
PRESETS: dict[str, dict] = {
    "nerul-seawoods": {"name": "Nerul – Seawoods", "bbox": (19.0150, 73.0080, 19.0480, 73.0420),
                       "note": "Project area, every street"},
    "vashi": {"name": "Vashi", "bbox": (19.0600, 72.9850, 19.0900, 73.0200), "note": "Every street"},
    "belapur-kharghar": {"name": "CBD Belapur – Kharghar", "bbox": (19.0050, 73.0300, 19.0500, 73.0850),
                         "note": "Every street"},
    "navi-mumbai-south": {"name": "Navi Mumbai south (Nerul–Belapur–Kharghar)", "bbox": (18.9950, 72.9950, 19.0600, 73.0900),
                          "note": "Main roads, large area"},
    "navi-mumbai": {"name": "Navi Mumbai (Vashi → Belapur)", "bbox": (18.9900, 72.9750, 19.1150, 73.1050),
                    "note": "Main roads, city scale"},
}
DEFAULT_AREA = "nerul-seawoods"


def area_key(bbox: tuple, detail: str) -> str:
    return "custom-" + "_".join(f"{v:.4f}" for v in bbox).replace("-", "m") + f"-{detail}"


class MapManager:
    def __init__(self, cache_dir: Path):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.status: dict = {"state": "idle", "message": "", "area": None, "updated": time.time()}
        self.ready: Optional[tuple[Path, dict]] = None  # set by the worker thread, consumed by the live loop
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- queries
    def _set(self, state: str, message: str = "", **kw):
        self.status = {**self.status, "state": state, "message": message, "updated": time.time(), **kw}
        if state in ("error",):
            log.warning("map: %s", message)
        else:
            log.info("map: %s %s", state, message)

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def current(self) -> Optional[dict]:
        p = self.dir / "current.json"
        if p.exists():
            try:
                cur = json.loads(p.read_text(encoding="utf-8"))
                if Path(cur["network"]).exists():
                    return cur
            except Exception:
                return None
        return None

    def set_current(self, key: str, network_path: Path, name: str):
        (self.dir / "current.json").write_text(json.dumps({"key": key, "network": str(network_path), "name": name}),
                                               encoding="utf-8")

    def areas(self) -> list[dict]:
        out = []
        for key, p in PRESETS.items():
            out.append({"id": key, "name": p["name"], "bbox": list(p["bbox"]), "note": p["note"],
                        "area_km2": round(area_km2(p["bbox"]), 1), "preset": True,
                        "cached": (self.dir / f"{key}.network.json").exists()})
        for f in sorted(self.dir.glob("*.network.json")):
            key = f.name[: -len(".network.json")]
            if key in PRESETS:
                continue
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                out.append({"id": key, "name": meta.get("name", key), "bbox": meta.get("meta", {}).get("bbox"),
                            "note": f"{meta.get('meta', {}).get('detail', '')} detail", "preset": False, "cached": True,
                            "area_km2": meta.get("meta", {}).get("area_km2")})
            except Exception:
                continue
        return out

    # ------------------------------------------------------------- loading
    def request(self, area: Optional[str] = None, bbox: Optional[tuple] = None, name: Optional[str] = None,
                detail: str = "auto", refresh: bool = False) -> dict:
        """Start a background download+build. Returns the status immediately."""
        with self._lock:
            if self.busy():
                raise RuntimeError("a map is already being loaded – wait for it to finish")
            if area:
                if area in PRESETS:
                    bbox = PRESETS[area]["bbox"]
                    name = name or PRESETS[area]["name"]
                    key = area
                elif (self.dir / f"{area}.network.json").exists():
                    key = area
                    meta = json.loads((self.dir / f"{area}.network.json").read_text(encoding="utf-8"))
                    bbox = tuple(meta.get("meta", {}).get("bbox") or ())
                    name = name or meta.get("name")
                else:
                    raise ValueError(f"unknown area '{area}'")
            else:
                if not bbox or len(bbox) != 4:
                    raise ValueError("give an area id or a bbox [south, west, north, east]")
                s, w, n, e = (float(v) for v in bbox)
                if not (-90 <= s < n <= 90 and -180 <= w < e <= 180):
                    raise ValueError("bbox must be [south, west, north, east] with south < north and west < east")
                bbox = (s, w, n, e)
                key = area_key(bbox, detail)
            km2 = area_km2(bbox) if bbox else 0
            if km2 > MAX_AREA_KM2:
                raise ValueError(f"area is {km2:.0f} km² – the limit is {MAX_AREA_KM2:.0f} km². Zoom in or pick a preset.")
            if km2 and km2 < 0.3:
                raise ValueError("area is too small – zoom out a little")
            self.ready = None
            self._set("queued", "Starting…", area={"id": key, "name": name or key, "bbox": list(bbox) if bbox else None,
                                                    "area_km2": round(km2, 1)})
            self._thread = threading.Thread(target=self._work, args=(key, bbox, name or key, detail, refresh),
                                            name="map-loader", daemon=True)
            self._thread.start()
            return self.status

    def import_file(self, path: Path, name: str, detail: str = "auto") -> dict:
        """Build from a user-supplied .osm / Overpass .json file (works without internet)."""
        with self._lock:
            if self.busy():
                raise RuntimeError("a map is already being loaded")
            key = "upload-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
            self.ready = None
            self._set("queued", "Reading file…", area={"id": key, "name": name, "bbox": None})
            self._thread = threading.Thread(target=self._work_file, args=(key, path, name, detail),
                                            name="map-import", daemon=True)
            self._thread.start()
            return self.status

    def _build_and_store(self, key: str, osm, name: str, detail: str) -> Path:
        self._set("building", f"Building the road network ({osm.summary()['ways']:,} OSM ways)…")
        t0 = time.time()
        net = build_network(osm, BuildOptions(detail=detail, name=name, area_id=key))
        out = self.dir / f"{key}.network.json"
        out.write_text(json.dumps(net, separators=(",", ":")), encoding="utf-8")
        log.info("built %s in %.1f s: %d nodes, %d edges, %d signals", key, time.time() - t0, len(net["nodes"]),
                 len(net["edges"]), sum(1 for n in net["nodes"] if n["signal"]))
        return out

    def _finish(self, key: str, out: Path, name: str):
        net = json.loads(out.read_text(encoding="utf-8"))
        info = {"nodes": len(net["nodes"]), "edges": len(net["edges"]),
                "signals": sum(1 for n in net["nodes"] if n["signal"]), "hospitals": len(net["hospitals"]),
                **net.get("meta", {})}
        self.set_current(key, out, name)
        self.ready = (out, info)
        self._set("ready", f"{name}: {info['nodes']:,} junctions, {info['edges']:,} road segments, "
                           f"{info['signals']} signals, {info['hospitals']} hospitals", info=info)

    def _work(self, key: str, bbox: tuple, name: str, detail: str, refresh: bool):
        try:
            raw_path = self.dir / f"{key}.osm.json.gz"
            out = self.dir / f"{key}.network.json"
            if out.exists() and not refresh and detail == "auto":
                self._finish(key, out, name)
                return
            if raw_path.exists() and not refresh:
                self._set("building", "Using the cached OpenStreetMap download…")
                osm = load_osm(raw_path)
            else:
                main_only = detail == "main" or (detail == "auto" and area_km2(bbox) > 40.0)
                raw = fetch_overpass(bbox, main_only, progress=lambda m: self._set("downloading", m))
                raw_path.write_bytes(gzip.compress(raw, compresslevel=6))
                self._set("building", "Parsing OpenStreetMap data…")
                osm = parse_overpass_json(json.loads(raw))
            osm.bbox = tuple(bbox)
            out = self._build_and_store(key, osm, name, detail)
            self._finish(key, out, name)
        except ConnectionError as exc:  # offline / Overpass busy: expected, no traceback
            self._set("error", str(exc))
        except Exception as exc:
            log.exception("map load failed")
            self._set("error", str(exc))

    def _work_file(self, key: str, path: Path, name: str, detail: str):
        try:
            osm = load_osm(path)
            out = self._build_and_store(key, osm, name, detail)
            self._finish(key, out, name)
        except Exception as exc:
            log.exception("map import failed")
            self._set("error", f"{type(exc).__name__}: {exc}")
