"""Real map areas from OpenStreetMap: presets, place search, download/build, upload, status."""
from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ..config import REPO_ROOT
from ..services.live import LiveSimulation
from .deps import get_live, require_admin

router = APIRouter(prefix="/api/map", tags=["map"])


class MapLoad(BaseModel):
    area: Optional[str] = Field(default=None, description="preset or cached area id, or 'demo' for the offline grid")
    bbox: Optional[list[float]] = Field(default=None, description="[south, west, north, east]")
    name: Optional[str] = None
    detail: str = Field(default="auto", description="auto | full (every street) | main (main roads only)")
    refresh: bool = False


@router.get("/areas")
def areas(live: LiveSimulation = Depends(get_live)):
    cur = live.maps.current()
    return {"areas": live.maps.areas(), "current": {"name": live.net.name, "source": live.net.source,
                                                     "key": cur.get("key") if cur and live.net.source == "osm" else None,
                                                     **live.net.summary()},
            "status": live.maps.status}


@router.get("/status")
def status(live: LiveSimulation = Depends(get_live)):
    return {**live.maps.status, "network_version": live.network_version, "current": live.net.name}


@router.get("/search")
async def search(q: str, live: LiveSimulation = Depends(get_live)):
    from ..mapdata.overpass import search_places

    if len(q.strip()) < 2:
        raise HTTPException(422, "type at least 2 characters")
    try:
        return await asyncio.to_thread(search_places, q.strip())
    except Exception as exc:
        raise HTTPException(502, f"place search (Nominatim) is not reachable: {exc}")


@router.post("/load", dependencies=[Depends(require_admin)])
def load(req: MapLoad, live: LiveSimulation = Depends(get_live)):
    if req.detail not in ("auto", "full", "main"):
        raise HTTPException(422, "detail must be auto, full or main")
    if req.area == "demo":
        (Path(live.settings.maps_dir) / "current.json").unlink(missing_ok=True)
        live.switch_network(REPO_ROOT / "data" / "network.demo.json")
        return {"state": "ready", "message": "Switched to the offline demo grid", "network_version": live.network_version}
    try:
        return live.maps.request(area=req.area, bbox=tuple(req.bbox) if req.bbox else None, name=req.name,
                                 detail=req.detail, refresh=req.refresh)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(422, str(exc))


@router.post("/upload", dependencies=[Depends(require_admin)])
async def upload(file: UploadFile = File(...), name: Optional[str] = Form(None), detail: str = Form("auto"),
                 live: LiveSimulation = Depends(get_live)):
    """An .osm export from openstreetmap.org (or an Overpass .json) – works without the Overpass API."""
    suffix = "".join(Path(file.filename or "map.osm").suffixes[-2:]) or ".osm"
    if not re.search(r"\.(osm|xml|json)(\.gz)?$", suffix):
        raise HTTPException(422, "upload an .osm (XML) or Overpass .json file")
    tmp = Path(tempfile.mkdtemp(prefix="resq_map_")) / f"upload{suffix}"
    with open(tmp, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    try:
        return live.maps.import_file(tmp, name or Path(file.filename or "Uploaded map").stem, detail)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc))
