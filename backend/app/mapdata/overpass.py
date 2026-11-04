"""Open map APIs: Overpass (OpenStreetMap data) and Nominatim (place search).

Both are free, open services run by the OpenStreetMap community. Please keep
requests modest (the app caches every downloaded area under ``data/maps``) and
see https://operations.osmfoundation.org/policies/nominatim/ for the usage policy.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from .osm import HIGHWAY_REGEX

log = logging.getLogger("resqroute.map")

USER_AGENT = "ResQRouteAI/1.2 (student traffic-simulation project; https://github.com/)"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
NOMINATIM = "https://nominatim.openstreetmap.org/search"
MAX_AREA_KM2 = 900.0


def overpass_query(bbox: tuple[float, float, float, float], main_roads_only: bool = False, timeout_s: int = 180) -> str:
    s, w, n, e = bbox
    b = f"{s:.6f},{w:.6f},{n:.6f},{e:.6f}"
    classes = ("motorway|trunk|primary|secondary|tertiary|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link"
               if main_roads_only else HIGHWAY_REGEX)
    return f"""[out:json][timeout:{timeout_s}][maxsize:536870912][bbox:{b}];
way["highway"~"^({classes})$"]->.roads;
.roads out body;
.roads >;
out skel qt;
node["highway"="traffic_signals"];
out body;
nwr["amenity"~"^(hospital|fire_station)$"];
out center tags;
nwr["healthcare"="hospital"];
out center tags;
"""


def fetch_overpass(bbox: tuple[float, float, float, float], main_roads_only: bool = False,
                   progress: Optional[Callable[[str], None]] = None, endpoints: Optional[list[str]] = None,
                   timeout_s: int = 240) -> bytes:
    """Download roads, signals, hospitals and fire stations for a bounding box. Returns raw JSON bytes."""
    say = progress or (lambda m: None)
    body = urllib.parse.urlencode({"data": overpass_query(bbox, main_roads_only)}).encode()
    errors = []
    for url in endpoints or OVERPASS_ENDPOINTS:
        host = urllib.parse.urlparse(url).netloc
        for attempt in range(2):
            try:
                say(f"Downloading OpenStreetMap data from {host}…")
                req = urllib.request.Request(url, data=body, headers={
                    "User-Agent": USER_AGENT, "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded"})
                t0 = time.time()
                with urllib.request.urlopen(req, timeout=timeout_s) as r:
                    chunks, total = [], 0
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        say(f"Downloading from {host}: {total / 1e6:.1f} MB")
                data = b"".join(chunks)
                head = data[:400].lstrip()
                if not head.startswith(b"{"):
                    raise RuntimeError(head[:200].decode(errors="replace"))
                obj = json.loads(data)
                if obj.get("remark") and not obj.get("elements"):
                    raise RuntimeError(obj["remark"][:200])
                log.info("overpass %s: %.1f MB in %.1f s", host, len(data) / 1e6, time.time() - t0)
                return data
            except urllib.error.HTTPError as exc:  # 429 rate limit / 504 busy
                errors.append(f"{host}: HTTP {exc.code}")
                if exc.code in (429, 504) and attempt == 0:
                    time.sleep(4)
                    continue
                break
            except Exception as exc:
                reason = getattr(exc, "reason", None) or exc
                errors.append(f"{host}: {str(reason)[:80]}")
                break
    log.warning("overpass failed: %s", "; ".join(errors))
    raise ConnectionError(f"Could not reach the OpenStreetMap Overpass API ({len(errors)} servers tried; {errors[0]}). "
                          "Check the internet connection and try again in a minute, or upload an .osm export "
                          "from openstreetmap.org/export.")


def search_places(q: str, limit: int = 6, timeout_s: int = 15) -> list[dict]:
    """Nominatim geocoding: place name -> candidates with bounding boxes."""
    params = urllib.parse.urlencode({"q": q, "format": "jsonv2", "limit": limit, "addressdetails": 0,
                                     "polygon_geojson": 0})
    req = urllib.request.Request(f"{NOMINATIM}?{params}", headers={"User-Agent": USER_AGENT,
                                                                   "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        rows = json.loads(r.read())
    out = []
    for row in rows:
        bb = row.get("boundingbox")  # [south, north, west, east] as strings
        if not bb:
            continue
        s, n, w, e = (float(x) for x in bb)
        out.append({"name": row.get("display_name"), "type": row.get("type"), "lat": float(row["lat"]),
                    "lon": float(row["lon"]), "bbox": [s, w, n, e]})
    return out
