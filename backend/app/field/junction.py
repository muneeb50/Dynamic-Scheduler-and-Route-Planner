"""A real-time signalised junction ("field junction") with emergency corridor priority.

Two modes:
  control – our SignalController runs the junction in real (wall-clock) time and its
            states are sent to a driver (dashboard, Arduino/ESP32 LED model, HTTP/MQTT device).
  mirror  – states come from an external live source (SPaT, SensorThings, MQTT, JSON);
            we cannot switch the lights, so priority decisions are logged as ADVISORY
            (what a connected controller should do) and shown on the dashboard.

Priority lifecycle (same safety rules as the simulator):
  request(approach) → current conflicting green truncated only after min green →
  amber → all-red → green for the ambulance's approach → held while the ambulance is
  approaching → release when it has passed (camera crossed the stop line / GPS moving
  away after closest approach) → normal cycle resumes, with compensation green for the
  approaches that were held (bounded) and queue-based extension when cameras watch them.
"""
from __future__ import annotations

import itertools
import math
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Optional

from ..core.geo import axis_difference, bearing_deg, haversine_m
from ..traffic_control.signal import Mode, SignalController, SignalPlan, Stage
from .io import SignalDriver, SignalSource, make_driver, make_source

_ids = itertools.count(1)


@dataclass
class ApproachCfg:
    id: str  # short code, e.g. "N"
    name: str = ""
    bearing: float = 0.0  # direction from the junction towards where this approach's traffic comes from
    lanes: int = 1


@dataclass
class CorridorCfg:
    radius_m: float = 400.0  # start watching an approaching ambulance
    forced_m: float = 120.0  # always request inside this distance if approaching
    exit_m: float = 25.0  # moving away by this much after closest approach = passed
    margin_s: float = 6.0
    max_hold_s: float = 90.0
    min_speed_mps: float = 4.0
    headway_s: float = 2.0  # queue discharge per vehicle per lane (camera queues)
    clearance_after_pass_s: float = 2.0
    compensation_factor: float = 0.5  # extra green for held groups = factor * hold time …
    compensation_max_s: float = 20.0  # … capped


@dataclass
class JunctionConfig:
    id: str
    name: str
    lat: float
    lon: float
    approaches: list[ApproachCfg]
    groups: Optional[list[list[str]]] = None
    plan: dict = field(default_factory=dict)
    mode: str = "control"  # control | mirror
    driver: dict = field(default_factory=lambda: {"type": "virtual"})
    source: dict = field(default_factory=lambda: {"type": "none"})
    corridor: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "JunctionConfig":
        d = dict(d)
        d["approaches"] = [a if isinstance(a, ApproachCfg) else ApproachCfg(**a) for a in d["approaches"]]
        return cls(**d)

    def to_dict(self) -> dict:
        return asdict(self)


def default_groups(approaches: list[ApproachCfg]) -> list[list[str]]:
    """Opposite approaches share a phase (same rule as the simulator)."""
    if not approaches:
        return []
    ref = approaches[0]
    g1 = [a.id for a in approaches if axis_difference(a.bearing, ref.bearing) <= 45]
    g2 = [a.id for a in approaches if a.id not in g1]
    return [g for g in (g1, g2) if g]


@dataclass
class PriorityRequest:
    owner: str
    approach: str
    trigger: str  # camera | gps | manual
    requested_at: float
    detail: dict = field(default_factory=dict)
    granted_at: Optional[float] = None
    released_at: Optional[float] = None
    status: str = "REQUESTED"  # REQUESTED | GRANTED | RELEASED | TIMEOUT | ADVISORY | QUEUED | CANCELLED

    def to_dict(self) -> dict:
        d = asdict(self)
        d["hold_s"] = None if self.granted_at is None else round((self.released_at or time.time()) - self.granted_at, 1)
        return d


class FieldJunction:
    def __init__(self, cfg: JunctionConfig, emit=None):
        self.cfg = cfg
        self.emit = emit or (lambda ev: None)
        self.corridor = CorridorCfg(**cfg.corridor) if cfg.corridor else CorridorCfg()
        self.groups = cfg.groups or default_groups(cfg.approaches)
        self.approach = {a.id: a for a in cfg.approaches}
        self.ctrl = SignalController(cfg.id, self.groups, SignalPlan(**cfg.plan) if cfg.plan else SignalPlan())
        self.ctrl.green_time_provider = self._green_time
        self.driver: SignalDriver = make_driver(cfg.driver)
        self.source: Optional[SignalSource] = make_source(cfg.source)
        if self.source:
            self.source.start()
        self.active: Optional[PriorityRequest] = None
        self.pending: deque[PriorityRequest] = deque()
        self.history: deque[PriorityRequest] = deque(maxlen=50)
        self.events: deque[dict] = deque(maxlen=200)
        self.compensation: dict[int, float] = {}  # group -> extra green seconds still owed
        self.camera_queues: dict[str, tuple[float, float]] = {}  # approach -> (queue, wall time)
        self._last_sent: Optional[dict] = None
        self._last_push_at = 0.0
        self._last_states: dict[str, str] = {}
        self.state_changed_at = time.time()
        self._push(force=True)

    # ------------------------------------------------------------------ state
    @property
    def mirror(self) -> bool:
        return self.cfg.mode == "mirror"

    def states(self) -> dict[str, str]:
        if self.mirror:
            latest = self.source.latest if self.source else {}
            return {a.id: latest.get(a.id, {}).get("state", "X") for a in self.cfg.approaches}
        return self.ctrl.approach_states()

    def remaining(self) -> dict[str, float]:
        """Seconds until each approach's current state changes (best estimate)."""
        out = {}
        p = self.ctrl.plan
        for gi, g in enumerate(self.ctrl.groups):
            if self.ctrl.current == gi and self.ctrl.stage == Stage.GREEN:
                r = math.inf if self.ctrl.priority_group == gi else max(0.0, self.ctrl.current_green - self.ctrl.elapsed)
            elif self.ctrl.current == gi and self.ctrl.stage == Stage.YELLOW:
                r = max(0.0, p.yellow - self.ctrl.elapsed)
            else:
                r = self.ctrl.time_to_priority_green(gi) if self.ctrl.priority_group == gi else \
                    max(0.0, (self.ctrl.current_green - self.ctrl.elapsed if self.ctrl.stage == Stage.GREEN else 0)) + p.yellow + p.all_red
            for a in g:
                out[a] = round(r, 1) if r != math.inf else 999.0
        return out

    def group_of(self, approach: str) -> Optional[int]:
        return self.ctrl.group_of.get(approach)

    def _event(self, typ: str, **kw):
        ev = {"type": typ, "junction": self.cfg.id, "junction_name": self.cfg.name, "wall": round(time.time(), 2), **kw}
        self.events.append(ev)
        self.emit(ev)

    # --------------------------------------------------------------- priority
    def request(self, approach: str, owner: str, trigger: str, **detail) -> PriorityRequest:
        if approach not in self.approach:
            raise ValueError(f"unknown approach '{approach}'")
        if self.active and self.active.owner == owner:
            return self.active
        req = PriorityRequest(owner, approach, trigger, time.time(), detail)
        if self.mirror:
            req.status = "ADVISORY"
            self.history.append(req)
            self._event("PRIORITY_ADVISORY", approach=approach, trigger=trigger, owner=owner,
                        note="read-only live source: a connected controller should give green to this approach",
                        current=self.states().get(approach), **detail)
            return req
        if self.active:
            if any(p.owner == owner for p in self.pending):
                return req
            req.status = "QUEUED"
            self.pending.append(req)
            self._event("PRIORITY_QUEUED", approach=approach, trigger=trigger, owner=owner,
                        busy_with=self.active.owner)
            return req
        self._activate(req)
        return req

    def _activate(self, req: PriorityRequest):
        group = self.group_of(req.approach)
        self.ctrl.request_priority(group, req.owner)
        req.status = "REQUESTED"
        self.active = req
        self._event("PRIORITY_REQUESTED", approach=req.approach, trigger=req.trigger, owner=req.owner,
                    switch_s=round(self.ctrl.time_to_priority_green(group), 1), **req.detail)

    def release(self, owner: str, reason: str = "PASSED"):
        self.pending = deque(p for p in self.pending if p.owner != owner)
        if not self.active or self.active.owner != owner:
            return
        req = self.active
        now = time.time()
        req.released_at = now
        req.status = "TIMEOUT" if reason == "TIMEOUT" else "RELEASED"
        hold = (now - req.granted_at) if req.granted_at else 0.0
        held_group = self.group_of(req.approach)
        # compensation for the approaches that were kept red while we held the green
        for gi in range(len(self.ctrl.groups)):
            if gi != held_group and hold > 0:
                extra = min(self.corridor.compensation_max_s, self.corridor.compensation_factor * hold)
                self.compensation[gi] = max(self.compensation.get(gi, 0.0), extra)
        self.ctrl.release_priority(owner, recovery=True)
        # the ambulance has gone: end its green as soon as minimum green allows, so the held approaches
        # are served next instead of waiting for a full extra green
        if self.ctrl.stage == Stage.GREEN and self.ctrl.current == held_group:
            self.ctrl.current_green = max(self.ctrl.elapsed, self.ctrl.plan.min_green)
        self.history.append(req)
        self.active = None
        self._event("PRIORITY_RELEASED", approach=req.approach, owner=owner, reason=reason,
                    hold_s=round(hold, 1), compensation={str(k): round(v, 1) for k, v in self.compensation.items()})
        if self.pending:
            self._activate(self.pending.popleft())

    def cancel_all(self):
        if self.active:
            self.release(self.active.owner, "CANCELLED")
        self.pending.clear()

    # --------------------------------------------------------------- timing
    def camera_queue(self, approach: str) -> Optional[float]:
        q = self.camera_queues.get(approach)
        return q[0] if q and time.time() - q[1] <= 5.0 else None

    def _green_time(self, group: int) -> Optional[float]:
        """Recovery after priority: owed compensation + queue-based extension from cameras."""
        plan = self.ctrl.plan
        extra = self.compensation.pop(group, 0.0)
        backlog = 0.0
        for a in self.ctrl.groups[group]:
            q = self.camera_queue(a)
            if q is not None:
                lanes = max(1, self.approach[a].lanes)
                backlog = max(backlog, q / lanes)
        queue_green = plan.min_green + self.corridor.headway_s * backlog + 2.0 if backlog else 0.0
        green = max(plan.green + extra, queue_green)
        if extra or backlog:
            self._event("RECOVERY_TIMING", group=group, green_s=round(min(green, plan.max_green), 1),
                        compensation_s=round(extra, 1), camera_backlog_per_lane=round(backlog, 1))
            if not self.compensation and self.ctrl.mode == Mode.RECOVERY:
                self.ctrl.mode = Mode.NORMAL
        return green

    def lead_time(self, approach: str) -> float:
        g = self.group_of(approach)
        q = self.camera_queue(approach) or 0.0
        lanes = max(1, self.approach[approach].lanes)
        clearance = (2.0 + self.corridor.headway_s * q / lanes) if q else 0.0
        return self.ctrl.time_to_priority_green(g) + clearance + self.corridor.margin_s

    # ----------------------------------------------------------------- tick
    def tick(self, dt: float):
        if not self.mirror:
            for ev in self.ctrl.tick(dt):
                if ev["type"] == "SAFETY_VIOLATION":
                    self._event("SAFETY_VIOLATION", **{k: str(v) for k, v in ev.items() if k != "type"})
            if self.active and self.active.granted_at is None and self.ctrl.is_green_for(self.group_of(self.active.approach)):
                self.active.granted_at = time.time()
                self.active.status = "GRANTED"
                self._event("PRIORITY_GRANTED", approach=self.active.approach, owner=self.active.owner,
                            after_s=round(self.active.granted_at - self.active.requested_at, 1))
            if self.active and time.time() - self.active.requested_at > self.corridor.max_hold_s:
                self.release(self.active.owner, "TIMEOUT")
            if self.ctrl.mode == Mode.RECOVERY and not self.compensation and not self.active \
                    and self.ctrl.stage == Stage.GREEN and self.ctrl.priority_group is None and self.ctrl.elapsed > 1.0:
                self.ctrl.mode = Mode.NORMAL
        self._push()

    def _push(self, force: bool = False):
        st = self.states()
        if st != self._last_states:
            self._last_states = dict(st)
            self.state_changed_at = time.time()
        msg = {"id": self.cfg.id, "name": self.cfg.name, "approaches": st, "mode": self.mode,
               "groups": self.group_numbers(), "remaining": self.remaining()}
        key = {"approaches": st, "mode": self.mode}
        # send on every change, plus a 2 s heartbeat so hardware watchdogs (fail-safe flashing amber) stay quiet
        if force or key != self._last_sent or time.time() - self._last_push_at >= 2.0:
            self._last_sent = key
            self._last_push_at = time.time()
            self.driver.apply(msg)

    @property
    def mode(self) -> str:
        if self.mirror:
            return "MIRROR"
        return self.ctrl.mode.value

    def group_numbers(self) -> dict[str, int]:
        return {a: gi * 2 + 2 + (i % 2) for gi, g in enumerate(self.groups) for i, a in enumerate(g)}

    def close(self):
        self.driver.close()
        if self.source:
            self.source.stop()

    def to_dict(self) -> dict:
        return {
            **self.cfg.to_dict(),
            "groups": self.groups,
            "states": self.states(),
            "remaining": self.remaining() if not self.mirror else {},
            "mode_now": self.mode,
            "stage": self.ctrl.stage.value if not self.mirror else None,
            "current_group": self.ctrl.current if not self.mirror else None,
            "elapsed": round(self.ctrl.elapsed, 1) if not self.mirror else None,
            "active": self.active.to_dict() if self.active else None,
            "pending": [p.to_dict() for p in self.pending],
            "history": [h.to_dict() for h in list(self.history)[-10:]],
            "events": list(self.events)[-30:],
            "camera_queues": {a: self.camera_queue(a) for a in self.approach},
            "driver_status": self.driver.status(),
            "source_status": self.source.status() if self.source else None,
            "violations": self.ctrl.violations,
            "compensation": {str(k): round(v, 1) for k, v in self.compensation.items()},
        }


# ------------------------------------------------------------------- GPS corridor
@dataclass
class GpsTrack:
    id: str
    points: deque = field(default_factory=lambda: deque(maxlen=30))  # (wall t, lat, lon, speed, heading)
    sessions: dict = field(default_factory=dict)  # junction id -> {"approach", "min_d", "requested"}

    @property
    def last(self):
        return self.points[-1] if self.points else None

    def speed(self) -> float:
        p = self.last
        if p and p[3] is not None:
            return p[3]
        if len(self.points) >= 2:
            (t0, la0, lo0, *_), (t1, la1, lo1, *_) = self.points[-2], self.points[-1]
            if t1 > t0:
                return haversine_m(la0, lo0, la1, lo1) / (t1 - t0)
        return 0.0


def gps_assess(j: FieldJunction, trk: GpsTrack) -> Optional[dict]:
    """Where is the ambulance relative to this junction? Returns approach, distance, approaching, eta."""
    p = trk.last
    if not p:
        return None
    _, lat, lon, _, heading = p
    d = haversine_m(j.cfg.lat, j.cfg.lon, lat, lon)
    if d > max(j.corridor.radius_m * 1.5, 600):
        return {"distance_m": d, "approach": None, "approaching": False, "eta_s": None}
    # bearing from the junction to the ambulance -> which approach it is on
    from_junction = bearing_deg(0, 0, (lon - j.cfg.lon) * math.cos(math.radians(lat)), lat - j.cfg.lat)
    best = min(j.cfg.approaches, key=lambda a: abs((a.bearing - from_junction + 180) % 360 - 180))
    off = abs((best.bearing - from_junction + 180) % 360 - 180)
    approach = best.id if off <= 50 else None
    approaching = False
    if len(trk.points) >= 2:
        _, la0, lo0, *_ = trk.points[-2]
        approaching = haversine_m(j.cfg.lat, j.cfg.lon, la0, lo0) - d > 0.5
    if heading is not None and trk.speed() > 1.0:
        to_junction = (from_junction + 180) % 360
        approaching = approaching or abs((heading - to_junction + 180) % 360 - 180) < 60
    speed = max(trk.speed(), j.corridor.min_speed_mps)
    return {"distance_m": round(d, 1), "approach": approach, "approaching": approaching,
            "eta_s": round(d / speed, 1), "speed_mps": round(trk.speed(), 1)}
