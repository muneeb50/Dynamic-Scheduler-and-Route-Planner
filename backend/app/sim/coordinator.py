"""Simulation coordinator: the single place where time advances.

Each step:
  1. signal controllers tick and push their states into the traffic engine
  2. the traffic engine moves every vehicle
  3. once per simulated second: queues are sampled, recovery is updated
  4. the priority manager evaluates the next signal on every ambulance route
  5. ambulance journeys progress (pickup, dwell, hospital, return), with
     timeout / no-progress detection

All side effects are emitted as plain dict events to registered listeners. The
live service persists them to the database and pushes them over WebSockets;
the experiment runner just collects them. The coordinator itself has no
database or web dependencies, so it can run headless in tests and experiments.
"""
from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..core.network import RoadNetwork
from ..recovery.engine import QueueHistory, RecoveryConfig, RecoveryEngine
from ..traffic_control.priority import AMBULANCE_SPEED_FACTOR, PriorityConfig, PriorityManager
from ..traffic_control.signal import SignalController, SignalPlan
from .base import DemandConfig, TrafficEngine

AMB_AVAILABLE = "AVAILABLE"
AMB_TO_PICKUP = "EN_ROUTE_TO_PICKUP"
AMB_AT_PICKUP = "AT_PICKUP"
AMB_TRANSPORTING = "TRANSPORTING"
AMB_AT_HOSPITAL = "AT_HOSPITAL"
AMB_RETURNING = "RETURNING"

J_WAITING = "WAITING_FOR_AMBULANCE"
J_ASSIGNED = "ASSIGNED"
J_TO_PICKUP = "EN_ROUTE_TO_PICKUP"
J_AT_PICKUP = "AT_PICKUP"
J_TRANSPORTING = "TRANSPORTING"
J_COMPLETED = "COMPLETED"
J_CANCELLED = "CANCELLED"
J_FAILED = "FAILED"
J_FINAL = {J_COMPLETED, J_CANCELLED, J_FAILED}


@dataclass
class Leg:
    kind: str  # to_pickup | to_hospital | return
    route: list[str]
    vehicle_id: str
    requested_at: float
    free_flow_s: float
    length_m: float
    inserted_at: Optional[float] = None
    finished_at: Optional[float] = None
    last_progress: tuple = (None, None)
    last_progress_t: float = 0.0
    edge_enter: dict[int, float] = field(default_factory=dict)
    stops: int = 0
    waiting_s: float = 0.0
    signal_delays: list[dict] = field(default_factory=list)
    _moving: bool = True
    _last_idx: int = -1

    @property
    def duration(self) -> Optional[float]:
        return None if self.finished_at is None else self.finished_at - self.requested_at


@dataclass
class Ambulance:
    id: str
    name: str
    base_node: str
    node: str
    status: str = AMB_AVAILABLE
    journey_id: Optional[str] = None
    leg: Optional[Leg] = None
    lat: float = 0.0
    lon: float = 0.0
    speed: float = 0.0
    hold_until: float = 0.0
    trips: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "status": self.status, "journey_id": self.journey_id,
            "lat": round(self.lat, 6), "lon": round(self.lon, 6), "speed": round(self.speed, 2),
            "base_node": self.base_node, "vehicle_id": self.leg.vehicle_id if self.leg else None,
        }


@dataclass
class Journey:
    id: str
    pickup_lat: float
    pickup_lon: float
    pickup_node: str
    hospital_id: str
    hospital_node: str
    created_at: float
    status: str = J_WAITING
    ambulance_id: Optional[str] = None
    assigned_at: Optional[float] = None
    pickup_at: Optional[float] = None
    completed_at: Optional[float] = None
    legs: list[Leg] = field(default_factory=list)
    failure_reason: Optional[str] = None
    eta_s: Optional[float] = None
    metrics: dict = field(default_factory=dict)

    @property
    def active_leg(self) -> Optional[Leg]:
        for leg in reversed(self.legs):
            if leg.kind != "return":
                return leg if leg.finished_at is None else None
        return None


@dataclass
class CoordinatorConfig:
    dt: float = 0.5
    pickup_dwell_s: float = 30.0
    hospital_dwell_s: float = 30.0
    no_progress_timeout_s: float = 240.0
    leg_timeout_factor: float = 6.0
    leg_timeout_min_s: float = 600.0
    service_radius_m: float = 1500.0


class Coordinator:
    CV_ALL_NODES_LIMIT = 4

    def __init__(
        self,
        net: RoadNetwork,
        engine: TrafficEngine,
        priority_cfg: Optional[PriorityConfig] = None,
        recovery_cfg: Optional[RecoveryConfig] = None,
        plan: Optional[SignalPlan] = None,
        config: Optional[CoordinatorConfig] = None,
    ):
        self.net = net
        self.engine = engine
        self.cfg = config or CoordinatorConfig(dt=engine.dt)
        self.plan = plan or SignalPlan()
        self.controllers: dict[str, SignalController] = {}
        for n in net.signal_nodes:
            groups = net.signal_groups(n.id)
            if groups:
                self.controllers[n.id] = SignalController(n.id, groups, SignalPlan(**vars(self.plan)))
        self.queue_source = "sim"
        self.cv_estimator = None  # set by the live service when CV is enabled
        self._cv_cache: dict[str, float] = {}
        # live queue measurements from real cameras attached to approaches:
        # approach edge -> (queued vehicles, time.monotonic() of the measurement)
        self.external_queues: dict[str, tuple[float, float]] = {}
        self.external_max_age_s = 5.0
        self.history = QueueHistory()
        self.priority = PriorityManager(net, engine, self.controllers, priority_cfg, self.queue_for)
        self.recovery = RecoveryEngine(net, self.controllers, self.history, self.queue_for, recovery_cfg)
        self.ambulances: dict[str, Ambulance] = {}
        for b in net.bases:
            node = net.nodes[b.node]
            self.ambulances[b.id] = Ambulance(b.id, b.name, b.node, b.node, lat=node.lat, lon=node.lon)
        self.journeys: dict[str, Journey] = {}
        self.listeners: list[Callable[[dict], None]] = []
        self.events: list[dict] = []  # bounded in-memory log for the dashboard
        self._jid = itertools.count(1)
        self._legid = itertools.count(1)
        self._test_ids = itertools.count(1)
        self._vnum: dict[str, int] = {}
        self._vnum_ids = itertools.count(1)
        self.test_ambulances: dict[str, dict] = {}
        self._next_sample = 0.0
        self._push_signals()

    # ---------------------------------------------------------------- events
    @property
    def now(self) -> float:
        return self.engine.time

    def emit(self, ev: dict):
        ev.setdefault("t", round(self.now, 1))
        self.events.append(ev)
        if len(self.events) > 2000:
            del self.events[:500]
        for fn in self.listeners:
            fn(ev)

    def _emit_all(self, evs: list[dict]):
        for ev in evs:
            self.emit(ev)

    # ---------------------------------------------------------------- queues
    def camera_queue(self, edge_id: str) -> Optional[float]:
        ext = self.external_queues.get(edge_id)
        if ext and time.monotonic() - ext[1] <= self.external_max_age_s:
            return ext[0]
        return None

    def queue_for(self, edge_id: str) -> float:
        cam = self.camera_queue(edge_id)
        if cam is not None:  # a live camera watching this approach always wins
            return cam
        if self.queue_source == "cv" and edge_id in self._cv_cache:
            return self._cv_cache[edge_id]
        return float(self.engine.halting(edge_id))

    def watched_nodes(self) -> set[str]:
        """Intersections the controller is currently reasoning about: active priority or
        recovery, plus the next signal ahead of every tracked ambulance."""
        nodes = {s.node for s in self.priority.active.values()} | set(self.recovery.active)
        nodes |= {s.node for s in self.priority.external.values()}
        for vid, t in self.priority.tracked.items():
            st = self.engine.vehicle_state(vid)
            if st:
                nxt = self.priority._next_signal(t, st)
                if nxt:
                    nodes.add(nxt[1])
        return nodes

    def _refresh_cv(self):
        """In CV mode, cameras are processed at 1 Hz for watched intersections (all of them
        when there are few signals). Elsewhere the simulator's queue is used and the
        cached CV value is dropped so it never goes stale."""
        if self.queue_source != "cv" or self.cv_estimator is None:
            return
        nodes = set(self.controllers) if len(self.controllers) <= self.CV_ALL_NODES_LIMIT else self.watched_nodes()
        fresh = set()
        for node_id in nodes:
            for a in self.net.nodes[node_id].incoming:
                est = self.cv_estimator.estimate(self, a)
                self._cv_cache[a] = est.queue_used
                fresh.add(a)
        for a in list(self._cv_cache):
            if a not in fresh:
                del self._cv_cache[a]

    # ------------------------------------------------------------- routing
    def emergency_edge_cost(self, e) -> float:
        """Travel time estimate combining the road graph with live simulation state."""
        vmax = e.speed * AMBULANCE_SPEED_FACTOR
        t = e.length / vmax
        q = self.queue_for(e.id)
        # spill-back: a road that is mostly a standing queue is slow even with a green
        occupancy = q * 7.5 / (e.length * e.lanes)
        t *= 1.0 + 3.0 * max(0.0, occupancy - 0.4)
        if self.net.nodes[e.to_node].signal:
            # queue discharge (2 s/vehicle/lane); with priority the green comes early, so
            # the queue matters but the red wait mostly does not
            red_wait = 0.0 if self.priority.cfg.enabled else 0.25 * self.plan.cycle / 2
            t += red_wait + 2.0 * q / e.lanes
        return t

    def plan_route(self, src: str, dst: str, emergency: bool = True) -> Optional[list[str]]:
        return self.net.shortest_path(src, dst, self.emergency_edge_cost if emergency else None)

    def route_eta(self, route: list[str], from_index: int = 0, pos: float = 0.0) -> float:
        t = 0.0
        for j in range(from_index, len(route)):
            e = self.net.edges[route[j]]
            frac = 1.0 if j != from_index else max(0.0, 1.0 - pos / e.length)
            t += self.emergency_edge_cost(e) * frac
        return t

    # ------------------------------------------------------------- journeys
    def create_journey(self, pickup_lat: float, pickup_lon: float, hospital_id: str,
                       journey_id: Optional[str] = None, auto_assign: bool = True) -> Journey:
        if hospital_id not in self.net.hospitals:
            raise ValueError(f"unknown hospital {hospital_id}")
        node = self.net.nearest_node(pickup_lat, pickup_lon, lambda n: bool(n.outgoing and n.incoming))
        if self.net.distance_to_node_m(pickup_lat, pickup_lon, node.id) > self.cfg.service_radius_m:
            raise ValueError("pickup location is outside the simulated service area")
        jid = journey_id or f"J{next(self._jid)}"
        h = self.net.hospitals[hospital_id]
        j = Journey(jid, pickup_lat, pickup_lon, node.id, hospital_id, h.node, created_at=self.now)
        self.journeys[jid] = j
        self.emit({"type": "BOOKING_CREATED", "journey_id": jid, "pickup_node": node.id, "hospital": hospital_id})
        if auto_assign:
            self.assign(jid)
        return j

    def assign(self, jid: str, ambulance_id: Optional[str] = None) -> Optional[Ambulance]:
        j = self.journeys[jid]
        if j.status != J_WAITING:
            return self.ambulances.get(j.ambulance_id) if j.ambulance_id else None
        candidates = [a for a in self.ambulances.values() if a.status == AMB_AVAILABLE]
        if ambulance_id:
            candidates = [a for a in candidates if a.id == ambulance_id]
        best, best_t, best_route = None, math.inf, None
        for a in candidates:
            route = self.plan_route(a.node, j.pickup_node)
            if route is None:
                continue
            t = self.route_eta(route)
            if t < best_t:
                best, best_t, best_route = a, t, route
        if best is None:
            self.emit({"type": "NO_AMBULANCE_AVAILABLE", "journey_id": jid})
            return None
        j.status = J_ASSIGNED
        j.ambulance_id = best.id
        j.assigned_at = self.now
        best.journey_id = jid
        best.trips += 1
        self.emit({"type": "AMBULANCE_ASSIGNED", "journey_id": jid, "ambulance_id": best.id, "eta_s": round(best_t, 1)})
        self._start_leg(best, j, "to_pickup", best_route)
        return best

    def _start_leg(self, amb: Ambulance, j: Optional[Journey], kind: str, route: list[str]):
        vid = f"{amb.id}_L{next(self._legid)}"
        leg = Leg(kind, route, vid, self.now, self.net.route_free_flow_time(route) / AMBULANCE_SPEED_FACTOR,
                  self.net.route_length(route), last_progress_t=self.now)
        amb.leg = leg
        amb.status = {"to_pickup": AMB_TO_PICKUP, "to_hospital": AMB_TRANSPORTING, "return": AMB_RETURNING}[kind]
        if j is not None:
            j.legs.append(leg)
            j.status = {"to_pickup": J_TO_PICKUP, "to_hospital": J_TRANSPORTING}.get(kind, j.status)
            j.eta_s = self.route_eta(route)
            self.emit({"type": "LEG_STARTED", "journey_id": j.id, "ambulance_id": amb.id, "leg": kind,
                       "route": route, "eta_s": round(j.eta_s, 1), "length_m": round(leg.length_m)})
        if not route:  # already at the destination node
            leg.inserted_at = leg.finished_at = self.now
            self._on_leg_finished(amb, leg)
            return
        self.engine.add_vehicle(vid, route, "ambulance")
        if kind != "return":
            self.priority.track(vid, j.id if j else "", len(j.legs) if j else 0, route)

    def cancel(self, jid: str, reason: str = "cancelled by user") -> Journey:
        j = self.journeys[jid]
        if j.status in J_FINAL:
            return j
        j.status = J_CANCELLED
        j.failure_reason = reason
        j.completed_at = self.now
        self.emit({"type": "BOOKING_CANCELLED", "journey_id": jid, "reason": reason})
        self._abort_ambulance(j)
        self._finalize_metrics(j)
        return j

    def _abort_ambulance(self, j: Journey):
        amb = self.ambulances.get(j.ambulance_id) if j.ambulance_id else None
        if not amb or amb.journey_id != j.id:
            return
        leg = amb.leg
        restart_node = amb.node
        if leg:
            self._emit_all(self.priority.untrack(leg.vehicle_id, self.now, "CANCELLED"))
            st = self.engine.vehicle_state(leg.vehicle_id)
            if st is not None:
                restart_node = self.net.edges[st.edge].to_node
            elif leg.inserted_at is None and leg.route:
                restart_node = self.net.edges[leg.route[0]].from_node
            self.engine.remove_vehicle(leg.vehicle_id)
            if leg.finished_at is None:
                leg.finished_at = self.now
        amb.node = restart_node
        amb.journey_id = None
        self._send_home(amb)

    def _send_home(self, amb: Ambulance):
        route = self.plan_route(amb.node, amb.base_node, emergency=False) or []
        self._start_leg(amb, None, "return", route)

    # ---------------------------------------------------------- leg progress
    def _on_leg_finished(self, amb: Ambulance, leg: Leg):
        j = self.journeys.get(amb.journey_id) if amb.journey_id else None
        if leg.route:
            amb.node = self.net.edges[leg.route[-1]].to_node
        amb.speed = 0.0
        if j is not None:
            j.eta_s = None if leg.kind == "to_pickup" else 0.0
        self._emit_all(self.priority.untrack(leg.vehicle_id, self.now, "CANCELLED"))
        if leg.kind == "to_pickup" and j:
            j.status = J_AT_PICKUP
            j.pickup_at = self.now
            amb.status = AMB_AT_PICKUP
            amb.hold_until = self.now + self.cfg.pickup_dwell_s
            self.emit({"type": "ARRIVED_AT_PICKUP", "journey_id": j.id, "ambulance_id": amb.id,
                       "leg_time_s": round(leg.duration or 0, 1)})
        elif leg.kind == "to_hospital" and j:
            j.status = J_COMPLETED
            j.completed_at = self.now
            amb.status = AMB_AT_HOSPITAL
            amb.hold_until = self.now + self.cfg.hospital_dwell_s
            self.emit({"type": "ARRIVED_AT_HOSPITAL", "journey_id": j.id, "ambulance_id": amb.id,
                       "leg_time_s": round(leg.duration or 0, 1)})
            self._finalize_metrics(j)
        elif leg.kind == "return":
            amb.status = AMB_AVAILABLE
            amb.leg = None
            amb.journey_id = None
            self.emit({"type": "AMBULANCE_AVAILABLE", "ambulance_id": amb.id})
            self._assign_waiting()

    def _assign_waiting(self):
        for j in sorted(self.journeys.values(), key=lambda x: x.created_at):
            if j.status == J_WAITING:
                if not self.assign(j.id):
                    break

    def _fail(self, amb: Ambulance, j: Journey, reason: str):
        j.status = J_FAILED
        j.failure_reason = reason
        j.completed_at = self.now
        self.emit({"type": "JOURNEY_FAILED", "journey_id": j.id, "ambulance_id": amb.id, "reason": reason})
        self._abort_ambulance(j)
        self._finalize_metrics(j)

    def _update_ambulances(self):
        for amb in self.ambulances.values():
            j = self.journeys.get(amb.journey_id) if amb.journey_id else None
            if amb.status in (AMB_AT_PICKUP, AMB_AT_HOSPITAL):
                if self.now >= amb.hold_until:
                    if amb.status == AMB_AT_PICKUP and j:
                        h = self.net.hospitals[j.hospital_id]
                        route = self.plan_route(amb.node, h.node)
                        if route is None:
                            self._fail(amb, j, "no route to hospital")
                            continue
                        self._start_leg(amb, j, "to_hospital", route)
                    else:
                        amb.journey_id = None
                        self._send_home(amb)
                continue
            leg = amb.leg
            if leg is None or leg.finished_at is not None:
                continue
            vid = leg.vehicle_id
            if self.engine.has_arrived(vid):
                leg.finished_at = self.now
                self._close_signal_delay(leg, len(leg.route) - 1)
                self._on_leg_finished(amb, leg)
                continue
            st = self.engine.vehicle_state(vid)
            if st is None:
                if self.now - leg.requested_at > self.cfg.no_progress_timeout_s and j and leg.kind != "return":
                    self._fail(amb, j, "ambulance could not enter the road network (blocked)")
                continue
            if leg.inserted_at is None:
                leg.inserted_at = self.now
                leg.edge_enter[0] = self.now
            amb.lat, amb.lon, amb.speed = st.lat, st.lon, st.speed
            # stops / waiting
            if st.speed < 0.5:
                leg.waiting_s += self.engine.dt
                if leg._moving:
                    leg.stops += 1
                    leg._moving = False
            elif st.speed > 2.0:
                leg._moving = True
            # per-edge timing for intersection delay
            if st.route_index != leg._last_idx:
                for k in range(max(leg._last_idx, 0), st.route_index):
                    self._close_signal_delay(leg, k)
                    leg.edge_enter[k + 1] = self.now
                leg._last_idx = st.route_index
            # progress / timeout detection
            prog = (st.route_index, round(st.pos))
            if prog != leg.last_progress:
                leg.last_progress = prog
                leg.last_progress_t = self.now
            if j and leg.kind != "return":
                j.eta_s = self.route_eta(leg.route, st.route_index, st.pos)
                limit = max(self.cfg.leg_timeout_min_s, self.cfg.leg_timeout_factor * leg.free_flow_s)
                if self.now - leg.last_progress_t > self.cfg.no_progress_timeout_s:
                    self._fail(amb, j, f"ambulance made no progress for {self.cfg.no_progress_timeout_s:.0f}s")
                elif self.now - leg.requested_at > limit:
                    self._fail(amb, j, f"leg exceeded time limit of {limit:.0f}s")

    def _close_signal_delay(self, leg: Leg, k: int):
        if k not in leg.edge_enter or k >= len(leg.route) - 1:
            return
        e = self.net.edges[leg.route[k]]
        if not (self.net.nodes[e.to_node].signal and e.to_node in self.controllers):
            return
        if any(d["route_index"] == k for d in leg.signal_delays):
            return
        spent = self.now - leg.edge_enter[k]
        ff = e.length / (e.speed * AMBULANCE_SPEED_FACTOR)
        had_priority = any(s.vehicle_id == leg.vehicle_id and s.route_index == k for s in self.priority.history) or (
            leg.vehicle_id in self.priority.active and self.priority.active[leg.vehicle_id].route_index == k)
        leg.signal_delays.append({"route_index": k, "node": e.to_node, "approach": e.id,
                                  "delay_s": round(max(0.0, spent - ff), 1), "priority": had_priority})

    # --------------------------------------------------------------- metrics
    def _finalize_metrics(self, j: Journey):
        legs = [l for l in j.legs if l.kind != "return"]
        sessions = [s for s in self.priority.history if s.journey_id == j.id]
        recs = [r for r in self.recovery.all_events() if r.journey_id == j.id]
        done = [r for r in recs if r.status == "COMPLETED"]
        j.metrics = {
            "status": j.status,
            "total_time_s": None if j.completed_at is None or j.assigned_at is None else round(j.completed_at - j.assigned_at, 1),
            "to_pickup_s": round(legs[0].duration, 1) if legs and legs[0].duration else None,
            "to_hospital_s": round(legs[1].duration, 1) if len(legs) > 1 and legs[1].duration else None,
            "free_flow_s": round(sum(l.free_flow_s for l in legs), 1),
            "distance_m": round(sum(l.length_m for l in legs)),
            "stops": sum(l.stops for l in legs),
            "waiting_s": round(sum(l.waiting_s for l in legs), 1),
            "signal_delay_s": round(sum(d["delay_s"] for l in legs for d in l.signal_delays), 1),
            "intersections": [d for l in legs for d in l.signal_delays],
            "priority_requests": len(sessions),
            "priority_granted": sum(1 for s in sessions if s.granted_at is not None),
            "priority_timeouts": sum(1 for s in sessions if s.status == "TIMEOUT"),
            "mean_hold_s": round(sum(s.hold_time or 0 for s in sessions) / len(sessions), 1) if sessions else None,
            "recoveries": len(recs),
            "recoveries_completed": len(done),
            "recoveries_timeout": sum(1 for r in recs if r.status == "TIMEOUT"),
            "mean_recovery_s": round(sum(r.duration for r in done) / len(done), 1) if done else None,
            "failure_reason": j.failure_reason,
        }
        self.emit({"type": "JOURNEY_METRICS", "journey_id": j.id, "metrics": j.metrics})

    # ------------------------------------------------------------------ step
    def _push_signals(self):
        for node_id, ctrl in self.controllers.items():
            self.engine.set_signal_state(node_id, ctrl.approach_states())

    def step(self):
        for ctrl in self.controllers.values():
            self._emit_all(ctrl.tick(self.cfg.dt))
        self._push_signals()
        self.engine.step()
        if self.now >= self._next_sample:
            self._next_sample = self.now + 1.0
            self._refresh_cv()
            for node_id in self.controllers:
                self.history.record(node_id, self.now, {a: self.queue_for(a) for a in self.net.nodes[node_id].incoming})
            self._emit_all(self.recovery.update(self.now))
        self._handle_priority_events(self.priority.update(self.now))
        self._update_ambulances()
        self._update_test_ambulances()

    def _handle_priority_events(self, evs: list[dict]):
        for ev in evs:
            if ev["type"] == "PRIORITY_REQUESTED":
                self._emit_all(self.recovery.on_priority_requested(ev["node"], self.now))
            self.emit(ev)
            if ev["type"] in ("PRIORITY_RELEASED", "PRIORITY_TIMEOUT"):
                s = next(x for x in reversed(self.priority.history) if x.id == ev["id"])
                self._emit_all(self.recovery.start({**ev, "queues_at_request": s.queues_at_request}, self.now))

    # ------------------------------------------------- external / manual priority
    def external_priority(self, node_id: str, approach: str, owner: str, source: str = "camera",
                          detail: Optional[dict] = None, max_hold_s: Optional[float] = None) -> list[dict]:
        """Turn `approach` green (safely) because an ambulance was seen there by a camera or an operator."""
        evs = self.priority.request_external(node_id, approach, owner, self.now, source, detail, max_hold_s)
        self._handle_priority_events(evs)
        return evs

    def release_external(self, owner: str, status: str = "RELEASED") -> list[dict]:
        evs = self.priority.release_external(owner, self.now, status)
        self._handle_priority_events(evs)
        return evs

    def spawn_test_ambulance(self, node_id: str, approach: Optional[str] = None, upstream_m: float = 800.0) -> dict:
        """A simulated ambulance that drives through `node_id` (via `approach`), using route-aware priority.

        Lets an operator see a full priority cycle at any junction of the real map: request when the
        queue-clearance lead time is reached, green, release after it passes, then recovery."""
        if node_id not in self.net.nodes:
            raise ValueError(f"unknown junction {node_id}")
        node = self.net.nodes[node_id]
        approaches = [approach] if approach else sorted(node.incoming, key=lambda a: -self.net.edges[a].length)
        for app in approaches:
            if app not in node.incoming:
                raise ValueError(f"{app} is not an approach of {node_id}")
            e_in = self.net.edges[app]
            # where to start: a node roughly `upstream_m` before the approach (reverse Dijkstra)
            dist = {e_in.from_node: 0.0}
            heap = [(0.0, e_in.from_node)]
            import heapq as _hq
            cands = []
            while heap:
                d, u = _hq.heappop(heap)
                if d > dist.get(u, math.inf) or d > upstream_m * 1.6:
                    continue
                if d >= upstream_m * 0.6 and self.net.nodes[u].outgoing and self.net.nodes[u].incoming:
                    cands.append((abs(d - upstream_m), u))
                for eid in self.net.nodes[u].incoming:
                    e = self.net.edges[eid]
                    nd = d + e.length
                    if e.from_node != node_id and nd < dist.get(e.from_node, math.inf):
                        dist[e.from_node] = nd
                        _hq.heappush(heap, (nd, e.from_node))
            cands.sort()
            for _, start in cands[:12]:
                head = self.net.shortest_path(start, e_in.from_node)
                if head is None or any(self.net.edges[x].to_node == node_id for x in head):
                    continue
                # continue straight on for a few hundred metres after the junction
                tail, cur, gone, last = [], node_id, 0.0, app
                while gone < 250:
                    outs = [o for o in self.net.nodes[cur].outgoing if not self.net.is_uturn(last, o)]
                    if not outs:
                        break
                    nxt = min(outs, key=lambda o: abs(((self.net.edges[o].departure_bearing()
                                                        - self.net.edges[last].approach_bearing()) + 180) % 360 - 180))
                    tail.append(nxt)
                    gone += self.net.edges[nxt].length
                    cur, last = self.net.edges[nxt].to_node, nxt
                if not tail:
                    continue
                route = head + [app] + tail
                tid = f"T{next(self._test_ids)}"
                vid = f"test_{tid}"
                self.engine.add_vehicle(vid, route, "ambulance")
                self.priority.track(vid, tid, 0, route)
                info = {"id": tid, "vehicle_id": vid, "node": node_id, "approach": app, "route": route,
                        "started_at": self.now, "length_m": round(self.net.route_length(route))}
                self.test_ambulances[tid] = info
                self.emit({"type": "TEST_AMBULANCE_STARTED", "test_id": tid, "node": node_id, "approach": app,
                           "length_m": info["length_m"]})
                return info
        raise ValueError(f"no suitable route through {node_id} found")

    def _update_test_ambulances(self):
        for tid, info in list(self.test_ambulances.items()):
            vid = info["vehicle_id"]
            if self.engine.has_arrived(vid) or self.now - info["started_at"] > 900:
                self._emit_all(self.priority.untrack(vid, self.now, "CANCELLED"))
                self.engine.remove_vehicle(vid)
                self.test_ambulances.pop(tid)
                self.emit({"type": "TEST_AMBULANCE_FINISHED", "test_id": tid, "node": info["node"],
                           "duration_s": round(self.now - info["started_at"], 1)})

    def run_for(self, seconds: float):
        steps = int(round(seconds / self.cfg.dt))
        for _ in range(steps):
            self.step()

    # -------------------------------------------------------------- snapshot
    def ambulance_route_geometry(self, amb: Ambulance) -> list[list[float]]:
        if not amb.leg or amb.leg.finished_at is not None or not amb.leg.route:
            return []
        st = self.engine.vehicle_state(amb.leg.vehicle_id)
        start = st.route_index if st else 0
        coords = self.net.route_geometry(amb.leg.route[start:])
        if st:
            coords = [(st.lat, st.lon)] + coords[1:]
        return [[round(a, 6), round(b, 6)] for a, b in coords]

    def signal_snapshot(self) -> dict:
        out = {}
        for node_id, ctrl in self.controllers.items():
            d = ctrl.to_dict()
            d["queues"] = {a: round(self.queue_for(a), 1) for a in self.net.nodes[node_id].incoming}
            rec = self.recovery.active.get(node_id)
            d["recovery"] = rec.id if rec else None
            d["camera_approaches"] = [a for a in self.net.nodes[node_id].incoming if self.camera_queue(a) is not None]
            out[node_id] = d
        return out

    def signal_snapshot_compact(self) -> dict:
        """What the live map needs, ~4× smaller than signal_snapshot() (static groups come with /api/network)."""
        out = {}
        for node_id, ctrl in self.controllers.items():
            out[node_id] = {"mode": ctrl.mode.value, "stage": ctrl.stage.value, "current_group": ctrl.current,
                            "approaches": ctrl.approach_states(),
                            "queues": {a: int(round(self.queue_for(a))) for a in self.net.nodes[node_id].incoming}}
        return out

    def snapshot(self, include_vehicles: bool = True, compact: bool = False) -> dict:
        snap = {
            "type": "simulation",
            "t": round(self.now, 1),
            "engine": self.engine.name,
            "demand": self.engine.demand.level,
            "priority_enabled": self.priority.cfg.enabled,
            "recovery_enabled": self.recovery.cfg.enabled,
            "queue_source": self.queue_source,
            "signals": self.signal_snapshot_compact() if compact else self.signal_snapshot(),
            "ambulances": [{**a.to_dict(), "route": self.ambulance_route_geometry(a)} for a in self.ambulances.values()],
            "priority": self.priority.active_sessions(),
            "recovery": [r.to_dict() for r in self.recovery.active.values()],
            "journeys": [self.journey_dict(j) for j in self.journeys.values() if j.status not in J_FINAL],
            "safety_violations": sum(c.violations for c in self.controllers.values()),
        }
        tests = []
        for tid, info in self.test_ambulances.items():
            st = self.engine.vehicle_state(info["vehicle_id"])
            if st:
                tests.append({"id": tid, "node": info["node"], "approach": info["approach"], "lat": round(st.lat, 6),
                              "lon": round(st.lon, 6), "speed": round(st.speed, 1)})
        snap["test_ambulances"] = tests
        if include_vehicles:
            vs = self.engine.snapshot()
            snap["vehicle_count"] = len(vs)
            if compact:  # flat integers relative to the map origin: [dlat·1e5, dlon·1e5, speed·10, ambulance, ...]
                lat0, lon0 = self.net.proj.lat0, self.net.proj.lon0
                flat = []
                nums = self._vnum
                for v in vs:
                    n = nums.get(v.id)
                    if n is None:
                        n = nums[v.id] = next(self._vnum_ids)
                    flat += [round((v.lat - lat0) * 1e5), round((v.lon - lon0) * 1e5), round(v.speed * 10),
                             1 if v.vtype == "ambulance" else 0, n]
                if len(nums) > 4 * len(vs) + 1000:  # forget vehicles that have left
                    live_ids = {v.id for v in vs}
                    self._vnum = {k: x for k, x in nums.items() if k in live_ids}
                # 5 integers per vehicle; the id lets the dashboard animate smoothly between snapshots
                snap["vehicles_q"] = {"o": [lat0, lon0], "n": 5, "d": flat}
            else:
                snap["vehicles"] = [[round(v.lat, 5), round(v.lon, 5), round(v.speed, 1), 1 if v.vtype == "ambulance" else 0]
                                    for v in vs]
        return snap

    def journey_dict(self, j: Journey) -> dict:
        amb = self.ambulances.get(j.ambulance_id) if j.ambulance_id else None
        return {
            "id": j.id, "status": j.status, "ambulance_id": j.ambulance_id,
            "pickup": [j.pickup_lat, j.pickup_lon], "pickup_node": j.pickup_node,
            "hospital_id": j.hospital_id, "eta_s": None if j.eta_s is None else round(j.eta_s, 1),
            "ambulance": amb.to_dict() if amb and amb.journey_id == j.id else None,
            "route": self.ambulance_route_geometry(amb) if amb and amb.journey_id == j.id else [],
            "failure_reason": j.failure_reason,
            "metrics": j.metrics or None,
            "created_at": round(j.created_at, 1),
        }


def build_engine(net: RoadNetwork, engine: str = "builtin", dt: float = 0.5, demand: Optional[DemandConfig] = None,
                 sumo_options: Optional[dict] = None) -> TrafficEngine:
    if engine == "sumo":
        from .sumo_engine import SumoEngine

        return SumoEngine(net, dt=dt, demand=demand, **(sumo_options or {}))
    from .builtin import BuiltinEngine

    return BuiltinEngine(net, dt=dt, demand=demand)
