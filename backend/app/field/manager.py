"""Field signal manager: junctions, ambulance GPS tracks, camera triggers, simulated drives.

Runs inside the backend's event loop (tick every 0.25 s). Camera events arrive from the
video threads through a queue; GPS positions arrive through the API.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from pathlib import Path
from typing import Callable, Optional

from ..core.geo import haversine_m
from .junction import ApproachCfg, FieldJunction, GpsTrack, JunctionConfig, gps_assess

log = logging.getLogger("resqroute.field")


def offset_latlon(lat: float, lon: float, bearing: float, dist_m: float) -> tuple[float, float]:
    b = math.radians(bearing)
    dlat = dist_m * math.cos(b) / 110540.0
    dlon = dist_m * math.sin(b) / (111320.0 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


class FieldManager:
    TICK_S = 0.25

    def __init__(self, store: Path, emit: Optional[Callable[[dict], None]] = None):
        self.store = store
        self.emit_cb = emit or (lambda ev: None)
        self.junctions: dict[str, FieldJunction] = {}
        self.gps: dict[str, GpsTrack] = {}
        self.sim_tasks: dict[str, asyncio.Task] = {}
        self.events: list[dict] = []
        self._task: Optional[asyncio.Task] = None
        self._lost_timers: dict[str, tuple[float, str]] = {}  # camera owner -> (wall time to release, reason)
        self._cam_tracks: dict[str, set] = {}  # camera owner -> ambulance tracks still before the stop line

    # ----------------------------------------------------------- persistence
    def load(self, default_center: tuple[float, float]):
        if self.store.exists():
            try:
                for d in json.loads(self.store.read_text(encoding="utf-8")):
                    try:
                        self._add(JunctionConfig.from_dict(d), save=False)
                    except Exception as exc:  # e.g. serial port gone: keep it as virtual so the UI still works
                        log.warning("junction %s: %s – falling back to the virtual driver", d.get("id"), exc)
                        d["driver"] = {"type": "virtual"}
                        d["source"] = {"type": "none"}
                        self._add(JunctionConfig.from_dict(d), save=False)
            except Exception:
                log.exception("could not read %s", self.store)
        if not self.junctions:
            lat, lon = default_center
            self._add(JunctionConfig(
                id="J1", name="Demo junction (virtual signal)", lat=lat, lon=lon,
                approaches=[ApproachCfg("N", "from north", 0), ApproachCfg("E", "from east", 90),
                            ApproachCfg("S", "from south", 180), ApproachCfg("W", "from west", 270)],
            ))

    def recenter_default(self, center: tuple[float, float], max_km: float = 3.0):
        """Move the auto-created demo junction to a newly loaded map area (user-made junctions stay put)."""
        j = self.junctions.get("J1")
        if j is None or not j.cfg.name.startswith("Demo junction"):
            return
        if haversine_m(j.cfg.lat, j.cfg.lon, *center) > max_km * 1000:
            j.cfg.lat, j.cfg.lon = center
            self.save()

    def save(self):
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text(json.dumps([j.cfg.to_dict() for j in self.junctions.values()], indent=1), encoding="utf-8")

    def _emit(self, ev: dict):
        self.events.append(ev)
        if len(self.events) > 500:
            del self.events[:100]
        self.emit_cb(ev)

    def _add(self, cfg: JunctionConfig, save: bool = True) -> FieldJunction:
        if cfg.id in self.junctions:
            self.junctions.pop(cfg.id).close()
        j = FieldJunction(cfg, self._emit)
        self.junctions[cfg.id] = j
        if save:
            self.save()
        return j

    def upsert(self, d: dict) -> FieldJunction:
        if not d.get("id"):
            n = 1
            while f"J{n}" in self.junctions:
                n += 1
            d["id"] = f"J{n}"
        cfg = JunctionConfig.from_dict(d)
        if not cfg.approaches:
            raise ValueError("a junction needs at least one approach")
        if len({a.id for a in cfg.approaches}) != len(cfg.approaches):
            raise ValueError("approach ids must be unique")
        if cfg.mode not in ("control", "mirror"):
            raise ValueError("mode must be control or mirror")
        if cfg.mode == "mirror" and (cfg.source or {}).get("type", "none") == "none":
            raise ValueError("mirror mode needs a live signal source")
        if cfg.groups:
            ids = [a for g in cfg.groups for a in g]
            if sorted(ids) != sorted(a.id for a in cfg.approaches):
                raise ValueError("groups must contain every approach exactly once")
        return self._add(cfg)

    def delete(self, jid: str):
        j = self.junctions.pop(jid, None)
        if j:
            j.close()
            self.save()

    # ----------------------------------------------------------------- loop
    def start(self):
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        last = time.monotonic()
        while True:
            now = time.monotonic()
            dt = now - last
            last = now
            try:
                for j in list(self.junctions.values()):
                    j.tick(dt)
                self._check_gps()
                self.process_timers()
            except Exception:
                log.exception("field tick failed")
            await asyncio.sleep(self.TICK_S)

    def process_timers(self):
        wall = time.time()
        for owner, (due, reason) in list(self._lost_timers.items()):
            if wall >= due:
                del self._lost_timers[owner]
                for j in self.junctions.values():
                    j.release(owner, reason)

    def shutdown(self):
        if self._task:
            self._task.cancel()
        for t in self.sim_tasks.values():
            t.cancel()
        for j in self.junctions.values():
            j.close()

    # -------------------------------------------------------- camera triggers
    def camera_stats(self, attach: dict, queue: float):
        j = self.junctions.get(attach.get("field", ""))
        if j and attach.get("approach") in j.approach:
            j.camera_queues[attach["approach"]] = (queue, time.time())

    def camera_event(self, ev: dict):
        """AMBULANCE_IN_ZONE -> request; AMBULANCE_PASSED / AMBULANCE_LOST -> release (after clearance)."""
        att = ev.get("attach") or {}
        j = self.junctions.get(att.get("field", ""))
        approach = att.get("approach")
        if not j or approach not in j.approach:
            return
        # one priority owner per camera + approach: a fragmented track (or two ambulances in a row)
        # keeps the same green until every ambulance seen by this camera has passed
        owner = f"cam:{ev.get('source')}:{approach}"
        tracks = self._cam_tracks.setdefault(owner, set())
        typ = ev["type"]
        if typ == "AMBULANCE_IN_ZONE":
            tracks.add(ev.get("track"))
            self._lost_timers.pop(owner, None)
            j.request(approach, owner, "camera", camera=ev.get("source_name"), track=ev.get("track"),
                      approaching=ev.get("approaching"), reasons=ev.get("reasons"))
        elif typ in ("AMBULANCE_PASSED", "AMBULANCE_LOST"):
            tracks.discard(ev.get("track"))
            if not tracks:
                if typ == "AMBULANCE_PASSED":
                    self._lost_timers[owner] = (time.time() + j.corridor.clearance_after_pass_s, "PASSED")
                else:  # brief grace: it may reappear as a new track
                    self._lost_timers[owner] = (time.time() + 4.0, "CAMERA_LOST")

    # ----------------------------------------------------------- GPS corridor
    def gps_update(self, amb_id: str, lat: float, lon: float, speed: Optional[float] = None,
                   heading: Optional[float] = None) -> dict:
        trk = self.gps.setdefault(amb_id, GpsTrack(amb_id))
        trk.points.append((time.time(), lat, lon, speed, heading))
        return self._check_gps(only=amb_id)

    def _check_gps(self, only: Optional[str] = None) -> dict:
        out = {}
        now = time.time()
        for amb_id, trk in list(self.gps.items()):
            if only and amb_id != only:
                continue
            p = trk.last
            if not p:
                continue
            stale = now - p[0] > 30
            for jid, j in self.junctions.items():
                a = gps_assess(j, trk)
                if a is None:
                    continue
                out[jid] = a
                sess = trk.sessions.get(jid)
                owner = f"gps:{amb_id}"
                if sess:
                    sess["min_d"] = min(sess["min_d"], a["distance_m"])
                    passed = sess["min_d"] < 80 and a["distance_m"] > sess["min_d"] + j.corridor.exit_m
                    turned_away = a["distance_m"] > sess["min_d"] + 100  # e.g. took another road before the junction
                    if passed or stale or turned_away or a["distance_m"] > j.corridor.radius_m * 1.5:
                        j.release(owner, "PASSED" if passed else ("GPS_LOST" if stale else "LEFT_CORRIDOR"))
                        trk.sessions.pop(jid, None)
                        trk.sessions[f"done:{jid}"] = now
                    continue
                if stale or not a["approach"] or not a["approaching"]:
                    continue
                if now - trk.sessions.get(f"done:{jid}", 0) < 30:
                    continue  # just passed this junction
                lead = j.lead_time(a["approach"])
                if a["distance_m"] <= j.corridor.forced_m or (a["distance_m"] <= j.corridor.radius_m and a["eta_s"] <= lead):
                    trk.sessions[jid] = {"approach": a["approach"], "min_d": a["distance_m"]}
                    j.request(a["approach"], owner, "gps", ambulance=amb_id, distance_m=a["distance_m"],
                              eta_s=a["eta_s"], lead_s=round(lead, 1))
            if stale and not trk.sessions:
                self.gps.pop(amb_id, None)
        return out

    def corridor_path(self, junction_ids: list[str], entry_approach: Optional[str] = None,
                      before_m: float = 600, after_m: float = 400) -> list[list[float]]:
        """A drive that enters the first junction on `entry_approach` and passes through the others in order."""
        js = [self.junctions[j] for j in junction_ids if j in self.junctions]
        if not js:
            raise ValueError("choose at least one junction")
        first = js[0]
        appr = first.approach.get(entry_approach) if entry_approach else None
        if appr is None:
            if len(js) > 1:  # come from the side opposite to the next junction
                b = math.degrees(math.atan2((js[1].cfg.lon - first.cfg.lon) * math.cos(math.radians(first.cfg.lat)),
                                            js[1].cfg.lat - first.cfg.lat)) % 360
                appr_b = (b + 180) % 360
            else:
                appr_b = first.cfg.approaches[0].bearing
        else:
            appr_b = appr.bearing
        pts = [list(offset_latlon(first.cfg.lat, first.cfg.lon, appr_b, before_m))]
        pts += [[j.cfg.lat, j.cfg.lon] for j in js]
        last = js[-1]
        if len(js) > 1:
            prev = js[-2]
            out_b = math.degrees(math.atan2((last.cfg.lon - prev.cfg.lon) * math.cos(math.radians(last.cfg.lat)),
                                            last.cfg.lat - prev.cfg.lat)) % 360
        else:
            out_b = (appr_b + 180) % 360
        pts.append(list(offset_latlon(last.cfg.lat, last.cfg.lon, out_b, after_m)))
        return pts

    async def simulate_drive(self, amb_id: str, path: list[list[float]], speed_kmh: float = 40.0,
                             interval_s: float = 1.0):
        """Feed GPS positions along a polyline as if an ambulance were driving it."""
        speed = speed_kmh / 3.6
        self._emit({"type": "GPS_SIMULATION_STARTED", "ambulance": amb_id, "points": len(path), "wall": time.time()})
        for (la0, lo0), (la1, lo1) in zip(path, path[1:]):
            seg = haversine_m(la0, lo0, la1, lo1)
            heading = math.degrees(math.atan2((lo1 - lo0) * math.cos(math.radians(la0)), la1 - la0)) % 360
            steps = max(1, int(seg / (speed * interval_s)))
            for k in range(steps):
                f = k / steps
                self.gps_update(amb_id, la0 + (la1 - la0) * f, lo0 + (lo1 - lo0) * f, speed, heading)
                await asyncio.sleep(interval_s)
        self.gps_update(amb_id, path[-1][0], path[-1][1], 0.0, None)
        self._emit({"type": "GPS_SIMULATION_FINISHED", "ambulance": amb_id, "wall": time.time()})

    def start_simulation(self, amb_id: str, path: list[list[float]], speed_kmh: float):
        old = self.sim_tasks.pop(amb_id, None)
        if old:
            old.cancel()
        self.sim_tasks[amb_id] = asyncio.create_task(self.simulate_drive(amb_id, path, speed_kmh))

    def gps_status(self) -> list[dict]:
        out = []
        for amb_id, trk in self.gps.items():
            p = trk.last
            out.append({"id": amb_id, "lat": p[1], "lon": p[2], "speed": p[3], "heading": p[4],
                        "age_s": round(time.time() - p[0], 1), "simulated": amb_id in self.sim_tasks
                        and not self.sim_tasks[amb_id].done(),
                        "corridor": {jid: s for jid, s in trk.sessions.items() if not jid.startswith("done:")}})
        return out

    def snapshot(self) -> dict:
        return {"type": "field", "junctions": [j.to_dict() for j in self.junctions.values()],
                "ambulances": self.gps_status(), "events": self.events[-40:]}
