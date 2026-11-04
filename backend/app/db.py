"""Database engine/session setup.

SQLite is the zero-setup default. Set DATABASE_URL to a PostgreSQL URL (see
docker-compose.yml) to use PostgreSQL + PostGIS; geometry columns are then added
as generated PostGIS columns derived from the GeoJSON / lat-lon columns, so the
application code is identical on both databases.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

log = logging.getLogger("resqroute.db")


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
    eng = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        from sqlalchemy import event

        @event.listens_for(eng, "connect")
        def _pragmas(conn, _):  # write-ahead log: readers never wait for the writer; far fewer fsyncs
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
    return eng


# called before a request reads the database, so writes queued by the live simulation are visible
read_barriers: list = []


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

POSTGIS_DDL = [
    "CREATE EXTENSION IF NOT EXISTS postgis",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS pickup_geom geometry(Point, 4326) "
    "GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(pickup_lon, pickup_lat), 4326)) STORED",
    "ALTER TABLE routes ADD COLUMN IF NOT EXISTS geom geometry(LineString, 4326) "
    "GENERATED ALWAYS AS (ST_SetSRID(ST_GeomFromGeoJSON(geometry_geojson), 4326)) STORED",
    "ALTER TABLE intersections ADD COLUMN IF NOT EXISTS geom geometry(Point, 4326) "
    "GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED",
    "ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS geom geometry(Point, 4326) "
    "GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED",
    "CREATE INDEX IF NOT EXISTS ix_routes_geom ON routes USING GIST (geom)",
    "CREATE INDEX IF NOT EXISTS ix_bookings_pickup_geom ON bookings USING GIST (pickup_geom)",
]


def init_db():
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        try:
            with engine.begin() as conn:
                for stmt in POSTGIS_DDL:
                    conn.execute(text(stmt))
            log.info("PostGIS geometry columns ready")
        except Exception as exc:  # PostGIS not installed: keep running on plain PostgreSQL
            log.warning("PostGIS setup skipped: %s", exc)


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db():
    for fn in read_barriers:
        fn()
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
