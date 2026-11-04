"""ResQRoute AI backend entry point.

    cd backend
    uvicorn app.main:app --reload          # http://localhost:8000/docs
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import analytics, bookings, field, maps, system, video, ws
from .config import REPO_ROOT, settings
from .db import init_db
from .services.live import LiveSimulation

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("resqroute")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    live = LiveSimulation(settings)
    live.seed_reference_data()
    live.start_run()
    app.state.live = live
    lat0, lon0, lat1, lon1 = live.net.bbox()
    center = live.net.nearest_node((lat0 + lat1) / 2, (lon0 + lon1) / 2, lambda n: n.signal) if live.net.signal_nodes \
        else live.net.nearest_node((lat0 + lat1) / 2, (lon0 + lon1) / 2)
    live.field.load((center.lat, center.lon))
    live.field.start()
    live._task = asyncio.create_task(live.run_forever())
    log.info("network: %s", {k: v for k, v in live.net.summary().items() if k != "meta"})
    if settings.map_auto_fetch and live.net.source != "osm":
        # first start: download the real roads of the project area from OpenStreetMap in the background;
        # the demo grid runs meanwhile and is replaced automatically when the map is ready
        try:
            live.maps.request(area=settings.map_area)
        except Exception as exc:
            log.warning("automatic map download not started: %s", exc)
    log.info("engine=%s demand=%s priority=%s", settings.sim_engine, settings.demand, settings.priority_enabled)
    try:
        yield
    finally:
        live.shutdown()


app = FastAPI(
    title="ResQRoute AI",
    description="Ambulance dispatch, live tracking, emergency signal priority and adaptive traffic recovery "
                "(simulation-based prototype).",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(system.router)
app.include_router(bookings.router)
app.include_router(analytics.router)
app.include_router(ws.router)
app.include_router(video.router)
app.include_router(field.router)
app.include_router(maps.router)

# Serve the built admin dashboard (admin_dashboard/dist) if present, so one process serves everything.
DIST = REPO_ROOT / "admin_dashboard" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(DIST / "index.html")
else:
    @app.get("/", include_in_schema=False)
    async def index_missing():
        return JSONResponse({"message": "ResQRoute AI API is running. Open /docs for the API, or start the dashboard "
                                        "with `npm run dev` in admin_dashboard/."})
