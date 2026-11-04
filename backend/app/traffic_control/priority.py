"""Route-aware emergency signal priority.

For every moving ambulance the manager looks ahead *along its planned route*
(never by straight-line proximity) for the next signalised junction and decides
when to request a green, using:

    lead time needed = time to switch the signal safely (min green + amber + all-red)
                     + time for the queue in front of the ambulance to discharge
                     + safety margin

and requests priority once the ambulance's ETA falls inside that lead time (or it is
within a forced range). Priority is released only after the ambulance is *observed*
on the downstream edge past the junction, never on a timer – except for the
max-hold safety timeout, which is logged as a failure.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..core.network import RoadNetwork
from ..sim.base import TrafficEngine, VehicleState
from .signal import SignalController

AMBULANCE_SPEED_FACTOR = 1.25


@dataclass
class PriorityConfig:
    enabled: bool = True
    detection_range_m: float = 700.0
    forced_range_m: float = 120.0
    headway_s: float = 2.0  # saturation headway per queued vehicle per lane
    startup_lost_s: float = 2.0
    safety_margin_s: float = 6.0
    exit_distance_m: float = 8.0
    max_hold_s: float = 90.0


@dataclass
class PrioritySession:
    id: str
    node: str
    approach: str
    group: int
    vehicle_id: str
    journey_id: str
    leg: int
    route_index: int
    requested_at: float
    eta_s: float
    distance_m: float
    queue_ahead: int
    lead_time_s: float
    queues_at_request: dict[str, int]
    granted_at: Optional[float] = None
    released_at: Optional[float] = None
    status: str = "REQUESTED"  # REQUESTED -> GRANTED -> RELEASED | TIMEOUT | CANCELLED
    deferred: bool = False
    source: str = "route"  # route (simulated ambulance) | camera (real video) | manual (operator)
    detail: dict = field(default_factory=dict)

    @property
    def hold_time(self) -> Optional[float]:
        if self.granted_at is None or self.released_at is None:
            return None
        return self.released_at - self.granted_at

    def to_dict(self) -> dict:
        return {
            "id": self.id, "node": self.node, "approach": self.approach, "group": self.group,
            "vehicle_id": self.vehicle_id, "journey_id": self.journey_id, "status": self.status,
            "requested_at": round(self.requested_at, 1),
            "granted_at": None if self.granted_at is None else round(self.granted_at, 1),
            "released_at": None if self.released_at is None else round(self.released_at, 1),
            "eta_s": round(self.eta_s, 1), "distance_m": round(self.distance_m, 1),
            "queue_ahead": self.queue_ahead, "lead_time_s": round(self.lead_time_s, 1),
            "hold_time_s": None if self.hold_time is None else round(self.hold_time, 1),
            "source": self.source, **({"detail": self.detail} if self.detail else {}),
        }


@dataclass
class TrackedAmbulance:
    vehicle_id: str
    journey_id: str
    leg: int
    route: list[str]
    served: set[int] = field(default_factory=set)  # route indices already handled


QueueProvider = Callable[[str], float]  # approach edge -> queued vehicles


class PriorityManager:
    def __init__(
        self,
        net: RoadNetwork,
        engine: TrafficEngine,
        controllers: dict[str, SignalController],
        config: Optional[PriorityConfig] = None,
        queue_provider: Optional[QueueProvider] = None,
    ):
        self.net = net
        self.engine = engine
        self.controllers = controllers
        self.cfg = config or PriorityConfig()
        self.queue_provider = queue_provider or (lambda edge: float(engine.halting(edge)))
        self.active: dict[str, PrioritySession] = {}  # vehicle id -> session
        self.history: list[PrioritySession] = []
        self.tracked: dict[str, TrackedAmbulance] = {}
        # requests that do not come from a simulated ambulance route: a camera that recognised an
        # ambulance on an approach, or an operator. owner -> session; waiting ones are retried.
        self.external: dict[str, PrioritySession] = {}
        self.external_waiting: dict[str, tuple] = {}
        self._ids = itertools.count(1)
        self._deferred_logged: set[tuple[str, str]] = set()

    # ------------------------------------------------------------ tracking
    def track(self, vehicle_id: str, journey_id: str, leg: int, route: list[str]):
        self.tracked[vehicle_id] = TrackedAmbulance(vehicle_id, journey_id, leg, list(route))

    def untrack(self, vehicle_id: str, now: float, reason: str = "CANCELLED") -> list[dict]:
        events = []
        s = self.active.pop(vehicle_id, None)
        if s:
            events.append(self._release(s, now, reason))
        self.tracked.pop(vehicle_id, None)
        return events

    # -------------------------------------------------------------- helpers
    def _next_signal(self, t: TrackedAmbulance, st: VehicleState) -> Optional[tuple[int, str, float, float]]:
        """(route index, node id, distance to stop line, free-flow ETA) of the next signal."""
        dist = 0.0
        eta = 0.0
        for j in range(st.route_index, len(t.route)):
            e = self.net.edges[t.route[j]]
            seg = e.length - st.pos if j == st.route_index else e.length
            seg = max(seg, 0.0)
            dist += seg
            eta += seg / (e.speed * AMBULANCE_SPEED_FACTOR)
            if j == len(t.route) - 1:
                return None  # destination node: the ambulance stops before it
            if dist > self.cfg.detection_range_m:
                return None
            node = self.net.nodes[e.to_node]
            if node.signal and node.id in self.controllers and j not in t.served:
                return j, node.id, dist, eta
        return None

    def _queue_ahead(self, t: TrackedAmbulance, st: VehicleState, j: int) -> int:
        """Vehicles that must discharge before the ambulance reaches the stop line (per lane)."""
        approach = t.route[j]
        if j == st.route_index:
            return self.engine.vehicles_ahead(t.vehicle_id)
        lanes = self.net.edges[approach].lanes
        return int(math.ceil(self.queue_provider(approach) / lanes))

    def queue_snapshot(self, node_id: str) -> dict[str, int]:
        return {a: int(round(self.queue_provider(a))) for a in self.net.nodes[node_id].incoming}

    # ------------------------------------------------------- external requests
    def request_external(self, node_id: str, approach: str, owner: str, now: float, source: str = "camera",
                         detail: Optional[dict] = None, max_hold_s: Optional[float] = None) -> list[dict]:
        """Green for `approach` at `node_id` until release_external(owner) (or the max-hold timeout).

        Same safety path as route priority: min green, amber and all-red are never skipped."""
        if node_id not in self.controllers:
            raise ValueError(f"{node_id} is not a signalised junction")
        ctrl = self.controllers[node_id]
        group = ctrl.group_of.get(approach)
        if group is None:
            raise ValueError(f"{approach} is not an approach of {node_id}")
        if not self.cfg.enabled:
            return [{"type": "PRIORITY_DEFERRED", "node": node_id, "approach": approach, "vehicle_id": owner,
                     "source": source, "reason": "emergency priority is switched off"}]
        if owner in self.external:
            self.external[owner].detail.update(detail or {})
            return []
        sid = f"X{next(self._ids)}"
        if not ctrl.request_priority(group, sid):
            first = owner not in self.external_waiting
            self.external_waiting[owner] = (node_id, approach, source, detail or {}, max_hold_s, now)
            return [{"type": "PRIORITY_DEFERRED", "node": node_id, "approach": approach, "vehicle_id": owner,
                     "source": source, "reason": "intersection serving another emergency vehicle"}] if first else []
        self.external_waiting.pop(owner, None)
        lanes = self.net.edges[approach].lanes
        q = int(math.ceil(self.queue_provider(approach) / lanes))
        transition = ctrl.time_to_priority_green(group)
        sess = PrioritySession(
            id=sid, node=node_id, approach=approach, group=group, vehicle_id=owner, journey_id="", leg=0,
            route_index=-1, requested_at=now, eta_s=transition, distance_m=0.0, queue_ahead=q,
            lead_time_s=transition, queues_at_request=self.queue_snapshot(node_id), source=source,
            detail={**(detail or {}), "max_hold_s": max_hold_s or self.cfg.max_hold_s},
        )
        self.external[owner] = sess
        events = [{"type": "PRIORITY_REQUESTED", **sess.to_dict(), "transition_s": round(transition, 1),
                   "queues": sess.queues_at_request}]
        if ctrl.is_green_for(group):
            sess.granted_at = now
            sess.status = "GRANTED"
            events.append({"type": "PRIORITY_GRANTED", **sess.to_dict(), "already_green": True})
        return events

    def release_external(self, owner: str, now: float, status: str = "RELEASED") -> list[dict]:
        self.external_waiting.pop(owner, None)
        s = self.external.pop(owner, None)
        return [self._release(s, now, status)] if s else []

    def _update_external(self, now: float) -> list[dict]:
        events = []
        for owner, s in list(self.external.items()):
            ctrl = self.controllers[s.node]
            if s.granted_at is None and ctrl.is_green_for(s.group):
                s.granted_at = now
                s.status = "GRANTED"
                events.append({"type": "PRIORITY_GRANTED", **s.to_dict()})
            if now - s.requested_at > s.detail.get("max_hold_s", self.cfg.max_hold_s):
                events.extend(self.release_external(owner, now, "TIMEOUT"))
        for owner, (node_id, approach, source, detail, max_hold, t0) in list(self.external_waiting.items()):
            if now - t0 > self.cfg.max_hold_s:
                self.external_waiting.pop(owner, None)
                continue
            events.extend(self.request_external(node_id, approach, owner, now, source, detail, max_hold))
        return events

    # ----------------------------------------------------------------- main
    def update(self, now: float) -> list[dict]:
        events: list[dict] = []
        if not self.cfg.enabled:
            return events
        events.extend(self._update_external(now))
        for vid, t in list(self.tracked.items()):
            st = self.engine.vehicle_state(vid)
            if st is None:
                continue
            sess = self.active.get(vid)
            if sess:
                events.extend(self._update_session(sess, t, st, now))
                continue
            nxt = self._next_signal(t, st)
            if not nxt:
                continue
            j, node_id, dist, eta = nxt
            ctrl = self.controllers[node_id]
            approach = t.route[j]
            group = ctrl.group_of.get(approach)
            if group is None:
                t.served.add(j)
                continue
            q_ahead = self._queue_ahead(t, st, j)
            clearance = self.cfg.startup_lost_s + q_ahead * self.cfg.headway_s if q_ahead > 0 else 0.0
            transition = ctrl.time_to_priority_green(group)
            lead = clearance + transition + self.cfg.safety_margin_s
            if not (dist <= self.cfg.forced_range_m or eta <= lead):
                continue
            sid = f"P{next(self._ids)}"
            if not ctrl.request_priority(group, sid):
                key = (vid, node_id)
                if key not in self._deferred_logged:
                    self._deferred_logged.add(key)
                    events.append({"type": "PRIORITY_DEFERRED", "node": node_id, "vehicle_id": vid,
                                   "journey_id": t.journey_id, "reason": "intersection serving another emergency vehicle"})
                continue
            sess = PrioritySession(
                id=sid, node=node_id, approach=approach, group=group, vehicle_id=vid, journey_id=t.journey_id,
                leg=t.leg, route_index=j, requested_at=now, eta_s=eta, distance_m=dist, queue_ahead=q_ahead,
                lead_time_s=lead, queues_at_request=self.queue_snapshot(node_id),
            )
            self.active[vid] = sess
            events.append({"type": "PRIORITY_REQUESTED", **sess.to_dict(),
                           "transition_s": round(transition, 1), "clearance_s": round(clearance, 1),
                           "queues": sess.queues_at_request})
            if ctrl.is_green_for(group):
                sess.granted_at = now
                sess.status = "GRANTED"
                events.append({"type": "PRIORITY_GRANTED", **sess.to_dict(), "already_green": True})
        return events

    def _update_session(self, s: PrioritySession, t: TrackedAmbulance, st: VehicleState, now: float) -> list[dict]:
        events = []
        ctrl = self.controllers[s.node]
        if s.granted_at is None and ctrl.is_green_for(s.group):
            s.granted_at = now
            s.status = "GRANTED"
            events.append({"type": "PRIORITY_GRANTED", **s.to_dict()})
        passed = st.route_index > s.route_index + 1 or (
            st.route_index == s.route_index + 1 and st.pos >= self.cfg.exit_distance_m
        )
        if passed:
            t.served.add(s.route_index)
            self.active.pop(t.vehicle_id, None)
            events.append(self._release(s, now, "RELEASED"))
        elif now - s.requested_at > self.cfg.max_hold_s:
            t.served.add(s.route_index)
            self.active.pop(t.vehicle_id, None)
            events.append(self._release(s, now, "TIMEOUT"))
        return events

    def _release(self, s: PrioritySession, now: float, status: str) -> dict:
        s.released_at = now
        s.status = status
        self.controllers[s.node].release_priority(s.id, recovery=(status in ("RELEASED", "TIMEOUT")))
        self.history.append(s)
        return {"type": "PRIORITY_RELEASED" if status == "RELEASED" else f"PRIORITY_{status}", **s.to_dict(),
                "queues": self.queue_snapshot(s.node)}

    def active_sessions(self) -> list[dict]:
        return [s.to_dict() for s in self.active.values()] + [s.to_dict() for s in self.external.values()]
