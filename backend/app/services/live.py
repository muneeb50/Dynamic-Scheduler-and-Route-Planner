"""Live simulation service: runs the coordinator in real time, persists events, feeds WebSockets."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import queue
import random
import time
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from fastapi import WebSocket
from sqlalchemy import select

from ..config import REPO_ROOT, Settings
from ..core.network import RoadNetwork
from ..db import session_scope
from ..models import (
    Ambulance as AmbulanceRow, Booking, Hospital, Intersection, JourneyMetrics, RecoveryEventRow, Route,
    SignalEvent, SimulationRun, SystemEvent, TrafficSnapshot, User, utcnow,
)
from ..recovery.engine import RecoveryConfig
from ..sim.base import DEMAND_LEVELS, DemandConfig
from ..sim.coordinator import J_FINAL, Coordinator, build_engine
from ..traffic_control.priority import PriorityConfig

log = logging.getLogger("resqroute.live")

BOOKING_EVENTS = {
    "BOOKING_CREATED", "AMBULANCE_ASSIGNED", "NO_AMBULANCE_AVAILABLE", "LEG_STARTED", "ARRIVED_AT_PICKUP",
    "ARRIVED_AT_HOSPITAL", "BOOKING_CANCELLED", "JOURNEY_FAILED", "JOURNEY_METRICS", "AMBULANCE_AVAILABLE",
}
SIGNAL_EVENTS = {
    "PRIORITY_REQUESTED", "PRIORITY_GRANTED", "PRIORITY_RELEASED", "PRIORITY_TIMEOUT", "PRIORITY_CANCELLED",
    "PRIORITY_DEFERRED", "SIGNAL_TRANSITION", "SAFETY_VIOLATION", "TEST_AMBULANCE_STARTED", "TEST_AMBULANCE_FINISHED",
}
CAMERA_CLEARANCE_S = 2.0  # keep the green this long after the ambulance crossed the stop line
CAMERA_LOST_GRACE_S = 4.0  # a lost track may come back as a new one


def booking_id_of(journey_id: Optional[str]) -> Optional[int]:
    if journey_id and journey_id.startswith("B") and journey_id[1:].isdigit():
        return int(journey_id[1:])
    return None


class DbWriter:
    """All database writes of the live simulation happen in this thread, in order, so the event loop
    (simulation steps, WebSockets, cameras) never waits for disk I/O."""

    def __init__(self):
        import threading

        self.q: "queue.Queue" = queue.Queue()
        self.pending = 0
        self._cv = threading.Condition()
        threading.Thread(target=self._run, name="db-writer", daemon=True).start()

    def submit(self, fn, *args):
        with self._cv:
            self.pending += 1
        self.q.put((fn, args))

    def _run(self):
        while True:
            fn, args = self.q.get()
            try:
                fn(*args)
            except Exception:
                log.exception("database write failed")
            finally:
                with self._cv:
                    self.pending -= 1
                    self._cv.notify_all()

    def flush(self, timeout: float = 3.0):
        with self._cv:
            self._cv.wait_for(lambda: self.pending == 0, timeout=timeout)


class Hub:
    """Minimal WebSocket pub/sub keyed by channel name."""

    def __init__(self):
        self.channels: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, channel: str, ws: WebSocket):
        await ws.accept()
        self.channels[channel].add(ws)

    def disconnect(self, channel: str, ws: WebSocket):
        self.channels[channel].discard(ws)

    def has(self, channel: str) -> bool:
        return bool(self.channels.get(channel))

    async def broadcast(self, channel: str, message: Any):
        dead = []
        # big city snapshots are serialised off the event loop
        data = await asyncio.to_thread(json.dumps, message, default=str) if channel == "simulation" \
            else json.dumps(message, default=str)
        for ws in list(self.channels.get(channel, ())):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(channel, ws)


class LiveSimulation:
    TICK_S = 0.1
    BROADCAST_S = 0.5

    def __init__(self, settings: Settings):
        self.settings = settings
        self.net = RoadNetwork.load(settings.network_file)
        self.hub = Hub()
        self.db = DbWriter()
        from ..db import read_barriers

        read_barriers.clear()
        read_barriers.append(lambda: self.db.flush(2.0))
        self.running = settings.autostart
        self.speed = settings.sim_speed
        self.coord: Optional[Coordinator] = None
        self.run_id: Optional[int] = None
        self.error: Optional[str] = None
        self.cv_provider = None
        self._outbox: list[dict] = []
        self._task: Optional[asyncio.Task] = None
        self._last_snapshot_sim = -1e9
        self.params: dict = {}
        # real video analytics (YOLO / OpenCV) – cameras can feed approach queues to the controller
        from computer_vision.video.manager import VideoManager

        self.external_queues: dict[str, tuple[float, float]] = {}
        self.video_events: "queue.Queue[dict]" = queue.Queue()
        self.video = VideoManager(Path(settings.results_dir) / "video", REPO_ROOT / "data" / "videos" / "uploads",
                                  on_queue=self._on_camera_queue, on_event=self.video_events.put)
        # real-time ("field") signals: open-format live inputs, physical/virtual outputs, ambulance corridors
        from ..field.manager import FieldManager

        self.field = FieldManager(Path(settings.field_store), emit=self._on_field_event)
        self.field_follow_sim = True
        self._last_follow = 0.0
        self.video.on_field_update = lambda src, stats: self.field.camera_stats(src.cfg.attach, float(stats.queue))
        # cameras attached to simulated junctions: owner -> ambulance tracks before the stop line, release timers
        self._cam_tracks: dict[str, set] = {}
        self._cam_release: dict[str, tuple[float, str]] = {}
        # real maps from OpenStreetMap
        from ..mapdata.manager import MapManager

        self.maps = MapManager(Path(settings.maps_dir))
        self.network_version = 1
        self.auto_emergencies_per_hour = settings.auto_emergencies_per_hour
        self._auto_acc: Optional[float] = None
        self._auto_rng = random.Random(settings.seed + 99)

    # ------------------------------------------------------------ lifecycle
    def seed_reference_data(self):
        with session_scope() as s:
            for h in self.net.hospitals.values():
                s.merge(Hospital(id=h.id, name=h.name, lat=h.lat, lon=h.lon, node=h.node))
            for n in self.net.signal_nodes:
                s.merge(Intersection(
                    id=n.id, name=n.name or n.id, lat=n.lat, lon=n.lon, approaches=sorted(n.incoming),
                    signal_config={"groups": self.net.signal_groups(n.id)},
                ))
            for b in self.net.bases:
                node = self.net.nodes[b.node]
                s.merge(AmbulanceRow(id=b.id, name=b.name, status="AVAILABLE", base_node=b.node,
                                     current_lat=node.lat, current_lon=node.lon))

    def start_run(self, demand: Optional[str] = None, priority: Optional[bool] = None, recovery: Optional[bool] = None,
                  queue_source: Optional[str] = None, seed: Optional[int] = None, engine: Optional[str] = None):
        p = {
            "demand": demand or self.params.get("demand", self.settings.demand),
            "priority": self.params.get("priority", self.settings.priority_enabled) if priority is None else priority,
            "recovery": self.params.get("recovery", self.settings.recovery_enabled) if recovery is None else recovery,
            "queue_source": queue_source or self.params.get("queue_source", self.settings.queue_source),
            "seed": self.params.get("seed", self.settings.seed) if seed is None else seed,
            "engine": engine or self.params.get("engine", self.settings.sim_engine),
        }
        if p["demand"] not in DEMAND_LEVELS:
            raise ValueError(f"demand must be one of {list(DEMAND_LEVELS)}")
        if p["queue_source"] not in ("sim", "cv"):
            raise ValueError("queue_source must be 'sim' or 'cv'")
        old = self.coord
        if old is not None:
            self._close_run(old)
        sumo_opts = {"net_file": self.settings.sumo_net_file, "gui": self.settings.sumo_gui}
        eng = build_engine(self.net, p["engine"], self.settings.sim_dt,
                           DemandConfig(level=p["demand"], vehicles_per_hour=DEMAND_LEVELS[p["demand"]], seed=p["seed"]),
                           sumo_opts)
        coord = Coordinator(self.net, eng, PriorityConfig(enabled=p["priority"]), RecoveryConfig(enabled=p["recovery"]))
        coord.listeners.append(self._on_event)
        coord.external_queues = self.external_queues  # live cameras survive simulation resets
        self._set_queue_source(coord, p["queue_source"])
        with session_scope() as s:
            run = SimulationRun(network_name=self.net.name, network_source=self.net.source, engine=p["engine"],
                                demand=p["demand"], seed=p["seed"], priority_enabled=p["priority"],
                                recovery_enabled=p["recovery"], queue_source=p["queue_source"])
            s.add(run)
            s.flush()
            self.run_id = run.id
            for a in s.scalars(select(AmbulanceRow)):
                base = self.net.nodes.get(a.base_node)
                a.status = "AVAILABLE"
                if base:
                    a.current_lat, a.current_lon = base.lat, base.lon
        self.coord = coord
        self.params = p
        self.error = None
        self._last_snapshot_sim = -1e9
        log.info("simulation run %s started: %s", self.run_id, p)
        return p

    def _set_queue_source(self, coord: Coordinator, source: str):
        coord.queue_source = source
        if source == "cv":
            if self.cv_provider is None:
                from computer_vision.provider import CVQueueProvider

                self.cv_provider = CVQueueProvider()
            coord.cv_estimator = self.cv_provider

    def set_queue_source(self, source: str):
        if source not in ("sim", "cv"):
            raise ValueError("queue_source must be 'sim' or 'cv'")
        self._set_queue_source(self.coord, source)
        self.params["queue_source"] = source

    def set_priority(self, enabled: bool):
        self.coord.priority.cfg.enabled = enabled
        self.params["priority"] = enabled
        if not enabled:
            for vid in list(self.coord.priority.active):
                s = self.coord.priority.active.pop(vid)
                self.coord.emit(self.coord.priority._release(s, self.coord.now, "CANCELLED"))
            for owner in list(self.coord.priority.external):
                self.coord.release_external(owner, "CANCELLED")

    def _close_run(self, coord: Coordinator):
        with session_scope() as s:
            if self.run_id:
                run = s.get(SimulationRun, self.run_id)
                if run:
                    run.ended_at = utcnow()
            for b in s.scalars(select(Booking).where(Booking.run_id == self.run_id)):
                if b.status not in J_FINAL:
                    b.status = "CANCELLED"
                    b.failure_reason = "simulation was reset"
        try:
            coord.engine.close()
        except Exception:
            pass

    def _on_camera_queue(self, approach: str, queued: float):
        """Called from video threads: store the latest camera queue for an approach (thread-safe dict write)."""
        if approach in self.net.edges:
            self.external_queues[approach] = (queued, time.monotonic())

    def camera_summary(self) -> list[dict]:
        out = []
        for sid, src in list(self.video.sources.items()):
            st = src.stats
            out.append({"id": sid, "name": src.cfg.name, "kind": src.cfg.kind, "status": src.status,
                        "attach": src.cfg.attach, "proc_fps": round(src.proc_fps, 1),
                        "vehicles": st.vehicles if st else None, "queue": st.queue if st else None,
                        "congestion": st.congestion if st else None,
                        "flow_per_min": round(st.flow_per_min, 1) if st else None})
        return out

    def _on_field_event(self, ev: dict):
        if self.coord is not None:
            self.coord.emit({**ev, "type": f"FIELD_{ev['type']}", "t": round(self.coord.now, 1)})

    def _follow_sim_ambulances(self):
        """Feed the simulated ambulances' positions to the field corridor logic (demo without hardware)."""
        if not self.field_follow_sim or self.coord is None:
            return
        for amb in self.coord.ambulances.values():
            if amb.leg and amb.leg.finished_at is None and amb.leg.kind != "return" and amb.lat:
                self.field.gps_update(f"sim-{amb.id}", amb.lat, amb.lon, amb.speed or None, None)

    # ------------------------------------------------------------ real maps
    def switch_network(self, path: str | Path, info: Optional[dict] = None):
        """Replace the road network (e.g. a newly downloaded OpenStreetMap area) and restart the run."""
        net = RoadNetwork.load(path)
        if not net.hospitals or not net.signal_nodes:
            log.warning("network %s has %d hospitals and %d signals", path, len(net.hospitals), len(net.signal_nodes))
        self.net = net
        self.network_file = str(path)
        self.network_version += 1
        for src in self.video.sources.values():  # cameras attached to junctions of the old map
            a = src.cfg.attach
            if a and a.get("node") and (a["node"] not in net.nodes or a.get("approach") not in net.nodes[a["node"]].incoming):
                src.cfg.attach = None
        self.external_queues.clear()
        self._cam_tracks.clear()
        self._cam_release.clear()
        self.seed_reference_data()
        engine = self.params.get("engine", self.settings.sim_engine)
        self.start_run(engine="builtin" if engine == "sumo" else None)  # the SUMO net must be rebuilt for a new map
        lat0, lon0, lat1, lon1 = net.bbox()
        self.field.recenter_default(((lat0 + lat1) / 2, (lon0 + lon1) / 2))
        self.coord.emit({"type": "MAP_LOADED", "name": net.name, "network_version": self.network_version,
                         **{k: v for k, v in (info or {}).items() if k in ("nodes", "edges", "signals", "hospitals")}})
        log.info("switched to network %s: %s", path, net.summary())

    def _check_map_ready(self):
        ready = self.maps.ready
        if ready is not None:
            self.maps.ready = None
            try:
                self.switch_network(*ready)
            except Exception as exc:
                log.exception("could not switch to the new map")
                self.maps._set("error", f"Loaded the map but could not start it: {exc}")

    # -------------------------------------------- cameras on simulated junctions
    def _camera_sim_event(self, ev: dict):
        """A camera attached to a simulated junction approach: an ambulance in its zone turns that
        approach green in the simulation; when it crosses the stop line the junction recovers."""
        att = ev.get("attach") or {}
        node, approach = att.get("node"), att.get("approach")
        if self.coord is None or node not in self.coord.controllers:
            return
        owner = f"cam:{ev.get('source')}:{approach}"
        tracks = self._cam_tracks.setdefault(owner, set())
        typ = ev["type"]
        if typ == "AMBULANCE_IN_ZONE":
            tracks.add(ev.get("track"))
            self._cam_release.pop(owner, None)
            try:
                self.coord.external_priority(node, approach, owner, "camera", detail={
                    "camera": ev.get("source_name"), "track": ev.get("track"), "reasons": ev.get("reasons"),
                    "approaching": ev.get("approaching")})
            except ValueError as exc:
                log.warning("camera priority: %s", exc)
        elif typ in ("AMBULANCE_PASSED", "AMBULANCE_LOST"):
            tracks.discard(ev.get("track"))
            if not tracks and owner in self.coord.priority.external:
                delay, why = (CAMERA_CLEARANCE_S, "RELEASED") if typ == "AMBULANCE_PASSED" else (CAMERA_LOST_GRACE_S, "RELEASED")
                self._cam_release[owner] = (time.monotonic() + delay, why)

    def _process_camera_releases(self):
        now = time.monotonic()
        for owner, (due, status) in list(self._cam_release.items()):
            if now >= due:
                self._cam_release.pop(owner, None)
                self._cam_tracks.pop(owner, None)
                if self.coord is not None:
                    self.coord.release_external(owner, status)

    # --------------------------------------------------------- auto emergencies
    def create_booking(self, lat: float, lon: float, hospital_id: str, patient: str = "Auto-generated call",
                       phone: str = "+910000000000", notes: Optional[str] = None) -> int:
        with session_scope() as s:
            user = s.scalar(select(User).where(User.phone == phone))
            if user is None:
                user = User(name=patient, phone=phone)
                s.add(user)
                s.flush()
            b = Booking(user_id=user.id, run_id=self.run_id, hospital_id=hospital_id, pickup_lat=lat, pickup_lon=lon,
                        notes=notes, status="REQUESTED")
            s.add(b)
            s.flush()
            bid, jid = b.id, b.journey_id
        j = self.coord.create_journey(lat, lon, hospital_id, journey_id=jid)
        with session_scope() as s:
            b = s.get(Booking, bid)
            b.pickup_node, b.status, b.ambulance_id = j.pickup_node, j.status, j.ambulance_id
        return bid

    def _auto_emergencies(self):
        rate = self.auto_emergencies_per_hour
        c = self.coord
        if rate <= 0 or c is None:
            self._auto_acc = None
            return
        if self._auto_acc is None or self._auto_acc < c.now - 600:
            self._auto_acc = c.now + self._auto_rng.expovariate(rate / 3600.0)
        while self._auto_acc <= c.now:
            self._auto_acc += self._auto_rng.expovariate(rate / 3600.0)
            zones = getattr(c.engine, "_zones", None) or [n.id for n in self.net.nodes.values() if n.incoming and n.outgoing]
            n = self.net.nodes[self._auto_rng.choice(zones)]
            lat = n.lat + self._auto_rng.uniform(-0.0008, 0.0008)
            lon = n.lon + self._auto_rng.uniform(-0.0008, 0.0008)
            hs = sorted(self.net.hospitals.values(), key=lambda h: (h.lat - lat) ** 2 + ((h.lon - lon) * math.cos(math.radians(lat))) ** 2)
            h = hs[0] if len(hs) == 1 or self._auto_rng.random() < 0.7 else hs[1]
            try:
                self.create_booking(lat, lon, h.id, notes="random emergency (auto-generated)")
            except Exception as exc:
                log.warning("auto emergency skipped: %s", exc)

    def shutdown(self):
        self.field.shutdown()
        self.video.stop_all()
        if self._task:
            self._task.cancel()
        if self.coord:
            self._close_run(self.coord)

    # -------------------------------------------------------------- events
    def _on_event(self, ev: dict):
        self._outbox.append(ev)
        # persisted in the writer thread; snapshot the journey state the row needs now
        self.db.submit(self._persist, dict(ev), self._journey_state(ev.get("journey_id")),
                       self._amb_status(ev.get("ambulance_id")), self.run_id,
                       self.coord.priority.cfg.enabled if self.coord else None)

    def _journey_state(self, jid: Optional[str]) -> Optional[dict]:
        j = self.coord.journeys.get(jid) if (self.coord and jid) else None
        if j is None:
            return None
        return {"status": j.status, "ambulance_id": j.ambulance_id, "failure_reason": j.failure_reason, "eta_s": j.eta_s}

    def _amb_status(self, aid: Optional[str]) -> Optional[str]:
        amb = self.coord.ambulances.get(aid) if (self.coord and aid) else None
        return amb.status if amb else None

    def _persist(self, ev: dict, jstate: Optional[dict] = None, amb_status: Optional[str] = None,
                 run_id: Optional[int] = None, priority_enabled: Optional[bool] = None):
        typ = ev["type"]
        bid = booking_id_of(ev.get("journey_id"))
        t = ev.get("t", 0.0)
        run_id = run_id if run_id is not None else run_id
        with session_scope() as s:
            if typ in BOOKING_EVENTS:
                s.add(SystemEvent(run_id=run_id, booking_id=bid, event_type=typ, sim_time=t, payload=ev))
                if bid:
                    b = s.get(Booking, bid)
                    j = jstate
                    if b and j:
                        b.status = j["status"]
                        b.ambulance_id = j["ambulance_id"]
                        b.failure_reason = j["failure_reason"]
                        b.eta_s = j["eta_s"]
                        if typ == "AMBULANCE_ASSIGNED":
                            b.assigned_at = utcnow()
                        if j["status"] in J_FINAL and b.completed_at is None:
                            b.completed_at = utcnow()
                    if typ == "LEG_STARTED" and ev.get("route"):
                        coords = self.net.route_geometry(ev["route"])
                        s.add(Route(
                            booking_id=bid, leg=ev["leg"], edges=ev["route"],
                            geometry_geojson=json.dumps({"type": "LineString", "coordinates": [[lo, la] for la, lo in coords]}),
                            length_m=ev.get("length_m", 0), estimated_time_s=ev.get("eta_s", 0),
                        ))
                    if typ == "JOURNEY_METRICS" and b:
                        m = ev["metrics"]
                        row = s.scalar(select(JourneyMetrics).where(JourneyMetrics.booking_id == bid))
                        if row is None:
                            row = JourneyMetrics(booking_id=bid, run_id=run_id)
                            s.add(row)
                        row.status = m["status"]
                        row.travel_time_s = m.get("total_time_s")
                        row.stops = m.get("stops", 0)
                        row.signal_delay_s = m.get("signal_delay_s", 0.0)
                        row.priority_enabled = bool(priority_enabled)
                        row.metrics = m
                if ev.get("ambulance_id") and amb_status:
                    a = s.get(AmbulanceRow, ev["ambulance_id"])
                    if a:
                        a.status = amb_status
            elif typ in SIGNAL_EVENTS:
                if typ == "SIGNAL_TRANSITION" and ev.get("reason") != "priority":
                    return
                s.add(SignalEvent(run_id=run_id, intersection_id=ev.get("node", ""), booking_id=bid,
                                  event_type=typ, sim_time=t, payload=ev))
            elif typ.startswith("FIELD_"):
                s.add(SignalEvent(run_id=run_id, intersection_id=f"field:{ev.get('junction', '')}", booking_id=None,
                                  event_type=typ, sim_time=t, payload=ev))
            elif typ.startswith("CAMERA_"):
                s.add(SystemEvent(run_id=run_id, booking_id=None, event_type=typ, sim_time=t, payload=ev))
            elif typ.startswith("RECOVERY_"):
                row = s.scalar(select(RecoveryEventRow).where(
                    RecoveryEventRow.run_id == run_id, RecoveryEventRow.recovery_key == ev["id"]))
                if row is None:
                    row = RecoveryEventRow(run_id=run_id, recovery_key=ev["id"], intersection_id=ev["node"],
                                           booking_id=bid, priority_session=ev["priority_session"],
                                           started_sim=ev["started_at"], status=ev["status"], details=ev)
                    s.add(row)
                row.status = ev["status"]
                row.completed_sim = ev.get("completed_at")
                row.duration_s = ev.get("duration_s")
                row.details = ev
                if ev["status"] != "ACTIVE":
                    row.completed_at = utcnow()

    def _persist_periodic(self):
        """Collect the data now (event-loop thread), write it in the writer thread."""
        coord = self.coord
        ambs = [(a.id, a.lat, a.lon, a.status) for a in coord.ambulances.values()]
        etas = [(booking_id_of(j.id), j.eta_s) for j in coord.journeys.values()
                if booking_id_of(j.id) and j.status not in J_FINAL]
        snaps = []
        if coord.now - self._last_snapshot_sim >= 10.0:
            self._last_snapshot_sim = coord.now
            source = "cv_estimated" if coord.queue_source == "cv" else "simulated"
            for node_id in coord.controllers:
                q = coord.history.latest(node_id)
                if q is not None:
                    snaps.append(TrafficSnapshot(run_id=self.run_id, intersection_id=node_id, sim_time=coord.now,
                                                 queue_lengths=dict(q), source=source))
        self.db.submit(self._write_periodic, ambs, etas, snaps)

    @staticmethod
    def _write_periodic(ambs, etas, snaps):
        with session_scope() as s:
            for aid, lat, lon, status in ambs:
                row = s.get(AmbulanceRow, aid)
                if row:
                    row.current_lat, row.current_lon, row.status = lat, lon, status
            for bid, eta in etas:
                b = s.get(Booking, bid)
                if b:
                    b.eta_s = eta
            s.add_all(snaps)

    # ---------------------------------------------------------------- loop
    async def run_forever(self):
        acc = 0.0
        last_broadcast = 0.0
        last_persist = 0.0
        while True:
            t0 = time.perf_counter()
            try:
                self._check_map_ready()
                while not self.video_events.empty() and self.coord is not None:
                    ev = self.video_events.get_nowait()
                    att = ev.get("attach") or {}
                    if att.get("field"):
                        self.field.camera_event(ev)
                    elif att.get("node"):
                        self._camera_sim_event(ev)
                    self.coord.emit({**ev, "type": f"CAMERA_{ev['type']}", "video_t": ev.get("t"), "t": round(self.coord.now, 1)})
                self._process_camera_releases()
                if time.monotonic() - self._last_follow >= 1.0:
                    self._last_follow = time.monotonic()
                    self._follow_sim_ambulances()
                if self.running and self.coord is not None:
                    acc += self.speed * self.TICK_S
                    n = int(acc / self.coord.cfg.dt)
                    acc -= n * self.coord.cfg.dt
                    t_budget = time.perf_counter() + 0.25  # never block the event loop (and WebSockets) for long
                    for _ in range(min(n, 400)):
                        self.coord.step()
                        if time.perf_counter() > t_budget:
                            acc = 0.0  # the machine cannot keep up with this speed: drop the backlog
                            break
                    self._auto_emergencies()
                now = time.monotonic()
                if now - last_persist >= 2.0 and self.coord is not None:
                    last_persist = now
                    self._persist_periodic()
                if now - last_broadcast >= self.BROADCAST_S and self.coord is not None:
                    last_broadcast = now
                    await self._broadcast()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.running = False
                self.error = f"{type(exc).__name__}: {exc}"
                log.error("simulation paused after error:\n%s", traceback.format_exc())
            await asyncio.sleep(max(0.0, self.TICK_S - (time.perf_counter() - t0)))

    async def _broadcast(self):
        coord = self.coord
        events, self._outbox = self._outbox, []
        if self.hub.has("simulation"):
            snap = coord.snapshot(compact=True)
            snap["running"] = self.running
            snap["speed"] = self.speed
            snap["run_id"] = self.run_id
            snap["events"] = events[-50:]
            snap["cameras"] = self.camera_summary()
            snap["network_version"] = self.network_version
            snap["map"] = {"state": self.maps.status.get("state"), "message": self.maps.status.get("message")}
            snap["auto_emergencies_per_hour"] = self.auto_emergencies_per_hour
            await self.hub.broadcast("simulation", snap)
        if self.hub.has("field"):
            fs = self.field.snapshot()
            fs["follow_sim"] = self.field_follow_sim
            await self.hub.broadcast("field", fs)
        for channel in [c for c in self.hub.channels if c.startswith("video:") and self.hub.channels[c]]:
            src = self.video.get(channel.split(":", 1)[1])
            if src is not None:
                await self.hub.broadcast(channel, {"type": "video", **src.to_dict(),
                                                   "timeline": list(src.timeline)[-120:]})
        for channel in [c for c in self.hub.channels if c.startswith("booking:") and self.hub.channels[c]]:
            bid = int(channel.split(":")[1])
            await self.hub.broadcast(channel, self.booking_live(bid, events))

    # ------------------------------------------------------------- queries
    def booking_live(self, bid: int, events: Optional[list[dict]] = None) -> dict:
        j = self.coord.journeys.get(f"B{bid}") if self.coord else None
        msg = {"type": "booking", "booking_id": bid, "sim_time": round(self.coord.now, 1) if self.coord else None,
               "sent_at": time.time(), "running": self.running}
        if j is None:
            with session_scope() as s:
                b = s.get(Booking, bid)
                msg["booking"] = None if b is None else {"id": f"B{b.id}", "status": b.status,
                                                         "failure_reason": b.failure_reason, "ambulance": None,
                                                         "route": [], "eta_s": b.eta_s}
            return msg
        msg["booking"] = self.coord.journey_dict(j)
        if events:
            msg["events"] = [e for e in events if e.get("journey_id") == j.id]
        return msg

    def status(self) -> dict:
        c = self.coord
        return {
            "running": self.running,
            "speed": self.speed,
            "run_id": self.run_id,
            "sim_time": round(c.now, 1) if c else None,
            "error": self.error,
            "params": self.params,
            "network": self.net.summary(),
            "vehicles": c.engine.vehicle_count if c else 0,
            "teleports": getattr(c.engine, "teleports", 0) if c else 0,
            "safety_violations": sum(x.violations for x in c.controllers.values()) if c else 0,
            "demand_levels": DEMAND_LEVELS,
            "demand_scale": round(c.engine.demand_scale, 2) if c else 1.0,
            "vehicles_per_hour": round(c.engine.effective_vehicles_per_hour) if c else 0,
            "network_version": self.network_version,
            "map": self.maps.status,
            "auto_emergencies_per_hour": self.auto_emergencies_per_hour,
            "ambulances": len(c.ambulances) if c else 0,
        }
