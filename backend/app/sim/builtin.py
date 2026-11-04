"""Built-in microscopic traffic simulator.

A compact, deterministic simulator so the project runs without installing SUMO:

* vehicles follow the Intelligent Driver Model (IDM) per lane;
* signalised junctions block approaches that are not green (amber: stop if possible);
* vehicles cannot overtake inside a lane – an ambulance stuck behind a queue only
  moves once that queue discharges, which is exactly why the priority controller
  must request the green *early* (queue-clearance lead time);
* queues are measured the same way SUMO measures them (vehicles below 0.5 m/s).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from ..core.network import RoadNetwork
from .base import DemandConfig, TrafficEngine, TripRecord, VehicleState

HALT_SPEED = 0.5
VTYPES = {
    #            length, accel, decel, headway T, min gap, speed factor
    "car": dict(length=4.5, a=1.6, b=2.5, T=1.4, s0=2.0, vf=1.0),
    "ambulance": dict(length=6.0, a=2.2, b=3.0, T=1.0, s0=1.5, vf=1.25),
}
B_MAX = 4.5  # maximum (emergency) deceleration used for the amber decision


def arrival_pos(edge_length: float, vtype: str = "car") -> float:
    """Trips end at the kerb before the downstream junction (not across it).

    Pickup points and hospital gates are snapped to a junction; the ambulance stops
    ~60 m short of it, where it is not stuck in the junction's signal queue."""
    back = 60.0 if vtype == "ambulance" else 15.0
    return max(edge_length - back, edge_length * 0.5)


@dataclass
class _Veh:
    id: str
    vtype: str
    route: list[str]
    idx: int = 0
    pos: float = 0.0
    speed: float = 0.0
    lane: int = 0
    params: dict = field(default_factory=dict)
    v_factor: float = 1.0
    depart: float = 0.0
    free_flow: float = 0.0
    stops: int = 0
    waiting: float = 0.0
    moving: bool = True
    acc: float = 0.0
    blocked_since: Optional[float] = None

    @property
    def edge(self) -> str:
        return self.route[self.idx]

    @property
    def length(self) -> float:
        return self.params["length"]


class BuiltinEngine(TrafficEngine):
    name = "builtin"
    TELEPORT_AFTER_S = 180.0

    def __init__(self, network: RoadNetwork, dt: float = 0.5, demand: Optional[DemandConfig] = None):
        super().__init__(network, dt, demand)
        self.vehicles: dict[str, _Veh] = {}
        self.pending: list[_Veh] = []
        self.arrived_ids: set[str] = set()
        # (edge, lane) -> list of vehicle ids ordered front (largest pos) to back
        self.lanes: dict[tuple[str, int], list[str]] = {
            (e.id, i): [] for e in network.edges.values() for i in range(e.lanes)
        }
        self.signal_state: dict[str, dict[str, str]] = {}  # node -> approach -> G/Y/R
        self._approach_state: dict[str, str] = {}  # edge -> G/Y/R (cache of the above)
        self.teleports = 0
        self._turn_cache: dict[tuple[str, str], float] = {}

    # ---------------------------------------------------------------- vehicles
    def add_vehicle(self, vid: str, route: list[str], vtype: str = "car", speed_factor: float = 1.0) -> bool:
        if not route:
            return False
        params = VTYPES.get(vtype, VTYPES["car"])
        v = _Veh(
            id=vid,
            vtype=vtype,
            route=list(route),
            params=params,
            v_factor=params["vf"] * speed_factor,
            free_flow=self.net.route_free_flow_time(route),
        )
        self.pending.append(v)
        return True

    def remove_vehicle(self, vid: str) -> None:
        self.pending = [p for p in self.pending if p.id != vid]
        v = self.vehicles.pop(vid, None)
        if v:
            lst = self.lanes[(v.edge, v.lane)]
            if vid in lst:
                lst.remove(vid)

    def has_arrived(self, vid: str) -> bool:
        return vid in self.arrived_ids

    def is_pending(self, vid: str) -> bool:
        return any(p.id == vid for p in self.pending)

    def _try_insert(self, v: _Veh) -> bool:
        e = self.net.edges[v.route[0]]
        best_lane, best_gap = None, -1.0
        for lane in range(e.lanes):
            lst = self.lanes[(e.id, lane)]
            if lst:
                last = self.vehicles[lst[-1]]
                gap = last.pos - last.length
            else:
                gap = e.length
            if gap > best_gap:
                best_lane, best_gap = lane, gap
        need = v.params["s0"] + 1.0
        if best_lane is None or best_gap < need:
            return False
        v.lane = best_lane
        v.pos = 0.0
        # enter at a speed that is safe for the gap ahead
        v.speed = min(e.speed * v.v_factor * 0.6, math.sqrt(max(0.0, 2 * v.params["b"] * (best_gap - need))))
        v.depart = self.time
        self.vehicles[v.id] = v
        self.lanes[(e.id, best_lane)].append(v.id)
        return True

    def _turn(self, e_in: str, e_out: str) -> float:
        key = (e_in, e_out)
        t = self._turn_cache.get(key)
        if t is None:
            t = abs(((self.net.edges[e_out].departure_bearing() - self.net.edges[e_in].approach_bearing()) + 180) % 360 - 180)
            self._turn_cache[key] = t
        return t

    # ----------------------------------------------------------------- signals
    def set_signal_state(self, node_id: str, states: dict[str, str]) -> None:
        self.signal_state[node_id] = dict(states)
        for edge_id, s in states.items():
            self._approach_state[edge_id] = s

    def _state_for(self, edge_id: str) -> str:
        return self._approach_state.get(edge_id, "G")

    # ------------------------------------------------------------------- query
    def _to_state(self, v: _Veh) -> VehicleState:
        e = self.net.edges[v.edge]
        lateral = 1.6 + 3.2 * (e.lanes - 1 - v.lane)
        lat, lon, h = self.net.latlon_at(v.edge, v.pos, lateral)
        return VehicleState(
            id=v.id, vtype=v.vtype, edge=v.edge, route_index=v.idx, pos=v.pos, speed=v.speed,
            lat=lat, lon=lon, heading=h, lane=v.lane, stops=v.stops, waiting_time=v.waiting,
        )

    def vehicle_state(self, vid: str) -> Optional[VehicleState]:
        v = self.vehicles.get(vid)
        return self._to_state(v) if v else None

    def halting(self, edge_id: str) -> int:
        e = self.net.edges[edge_id]
        n = 0
        for lane in range(e.lanes):
            for vid in self.lanes[(edge_id, lane)]:
                if self.vehicles[vid].speed < HALT_SPEED:
                    n += 1
        return n

    def edge_mean_speed(self, edge_id: str) -> Optional[float]:
        e = self.net.edges[edge_id]
        speeds = [self.vehicles[vid].speed for lane in range(e.lanes) for vid in self.lanes[(edge_id, lane)]]
        return sum(speeds) / len(speeds) if speeds else None

    def edge_vehicles(self, edge_id: str) -> list[VehicleState]:
        e = self.net.edges[edge_id]
        return [self._to_state(self.vehicles[vid]) for lane in range(e.lanes) for vid in self.lanes[(edge_id, lane)]]

    def vehicles_ahead(self, vid: str) -> int:
        v = self.vehicles.get(vid)
        if not v:
            return 0
        lst = self.lanes[(v.edge, v.lane)]
        return lst.index(vid) if vid in lst else 0

    def snapshot(self) -> list[VehicleState]:
        return [self._to_state(v) for v in self.vehicles.values()]

    # -------------------------------------------------------------- simulation
    def _next_edge_rear_gap(self, v: _Veh) -> tuple[float, float, int]:
        """(gap from next-edge start to the rearmost vehicle's back, its speed, best lane)."""
        if v.idx + 1 >= len(v.route):
            return math.inf, 0.0, 0
        ne = self.net.edges[v.route[v.idx + 1]]
        best = (-math.inf, 0.0, 0)
        for lane in range(ne.lanes):
            lst = self.lanes[(ne.id, lane)]
            if not lst:
                return math.inf, 0.0, lane
            last = self.vehicles[lst[-1]]
            gap = last.pos - last.length
            if gap > best[0]:
                best = (gap, last.speed, lane)
        return best

    def _idm(self, v: _Veh, v0: float, gap: float, lead_speed: float) -> float:
        p = v.params
        a, b, T, s0 = p["a"], p["b"], p["T"], p["s0"]
        free = 1.0 - (v.speed / max(v0, 0.1)) ** 4
        if math.isinf(gap):
            return a * free
        gap = max(gap, 0.01)
        dv = v.speed - lead_speed
        s_star = s0 + max(0.0, v.speed * T + v.speed * dv / (2.0 * math.sqrt(a * b)))
        return a * (free - (s_star / gap) ** 2)

    def step(self) -> None:
        self._spawn_background()
        # insertion (FIFO, at most a few per step so queues at the network edge build realistically)
        if self.pending:
            still = []
            for p in self.pending:
                if not self._try_insert(p):
                    still.append(p)
            self.pending = still[-400:]

        # lanes that hold vehicles (most lanes of a city network are empty at any moment)
        active = [(k, lst) for k, lst in self.lanes.items() if lst]
        # 1) compute accelerations from the current state
        stop_line: dict[str, float] = {}
        for (edge_id, lane), lst in active:
            e = self.net.edges[edge_id]
            node_signal = self.net.nodes[e.to_node].signal
            state = self._state_for(edge_id) if node_signal else "G"
            for i, vid in enumerate(lst):
                v = self.vehicles[vid]
                v0 = e.speed * v.v_factor
                if i > 0:
                    lead = self.vehicles[lst[i - 1]]
                    gap = lead.pos - lead.length - v.pos
                    v.acc = self._idm(v, v0, gap, lead.speed)
                    continue
                d_end = e.length - v.pos
                must_stop = False
                if v.idx + 1 >= len(v.route):
                    # final edge: the trip ends at the kerb before the junction, signals do not apply
                    v.acc = self._idm(v, v0, math.inf, 0.0)
                    continue
                if state == "R":
                    must_stop = True
                elif state == "Y":
                    must_stop = d_end > (v.speed ** 2) / (2 * B_MAX) + 1.0
                if must_stop:
                    v.acc = self._idm(v, v0, max(d_end - 0.5, 0.01), 0.0)
                    stop_line[vid] = e.length - 0.5
                else:
                    gap_next, lead_speed, _ = self._next_edge_rear_gap(v)
                    if v.idx + 1 >= len(v.route):
                        v.acc = self._idm(v, v0, math.inf, 0.0)
                    else:
                        v.acc = self._idm(v, v0, d_end + gap_next, lead_speed)
                    # slow down a little for turns at junctions
                    if v.idx + 1 < len(v.route) and d_end < 40:
                        if self._turn(edge_id, v.route[v.idx + 1]) > 45:
                            vt = 6.0
                            if v.speed > vt:
                                v.acc = min(v.acc, (vt ** 2 - v.speed ** 2) / (2 * max(d_end, 1.0)))

        # 2) integrate (ballistic) and clamp to leaders / stop lines
        dt = self.dt
        for (edge_id, lane), lst in active:
            prev_pos_limit = math.inf
            for vid in lst:
                v = self.vehicles[vid]
                acc = max(-9.0, min(v.acc, v.params["a"]))
                new_speed = max(0.0, v.speed + acc * dt)
                adv = max(0.0, (v.speed + new_speed) * 0.5 * dt)
                new_pos = v.pos + adv
                limit = prev_pos_limit
                if vid in stop_line:
                    limit = min(limit, stop_line[vid])
                if new_pos > limit:
                    new_pos = max(v.pos, limit)
                    new_speed = 0.0 if new_pos == limit else new_speed
                v.pos, v.speed = new_pos, new_speed
                prev_pos_limit = v.pos - v.length - 0.5
                # stop accounting
                if v.speed < HALT_SPEED:
                    v.waiting += dt
                    if v.moving:
                        v.stops += 1
                        v.moving = False
                elif v.speed > 2.0:
                    v.moving = True

        # 3a) arrivals: any vehicle on its final edge that reached its stopping point
        for (edge_id, lane), lst in active:
            if not lst:
                continue
            e = self.net.edges[edge_id]
            done = [vid for vid in lst if self.vehicles[vid].idx + 1 >= len(self.vehicles[vid].route)
                    and self.vehicles[vid].pos >= arrival_pos(e.length, self.vehicles[vid].vtype)]
            for vid in done:
                v = self.vehicles.pop(vid)
                lst.remove(vid)
                self.arrived_ids.add(vid)
                if v.vtype == "car":
                    self.trips.append(TripRecord(v.id, v.depart, self.time + dt, v.free_flow))

        # 3b) edge transitions, processed front-first per lane
        for (edge_id, lane), lst in active:
            e = self.net.edges[edge_id]
            while lst:
                v = self.vehicles[lst[0]]
                if v.idx + 1 >= len(v.route) or v.pos < e.length:
                    break
                if self.net.nodes[e.to_node].signal and self._state_for(edge_id) == "R":
                    v.pos, v.speed = e.length - 0.01, 0.0
                    break
                gap, _, nlane = self._next_edge_rear_gap(v)
                overflow = v.pos - e.length
                if gap - overflow < v.params["s0"] * 0.5:
                    v.pos, v.speed = e.length - 0.01, 0.0
                    if v.blocked_since is None:
                        v.blocked_since = self.time
                    elif v.vtype == "car" and self.time - v.blocked_since > self.TELEPORT_AFTER_S:
                        # gridlock breaker (same idea as SUMO's time-to-teleport)
                        lst.pop(0)
                        del self.vehicles[v.id]
                        self.arrived_ids.add(v.id)
                        self.teleports += 1
                        continue
                    break
                v.blocked_since = None
                lst.pop(0)
                v.idx += 1
                v.lane = nlane
                v.pos = min(overflow, max(0.0, gap - v.params["s0"] * 0.5))
                self.lanes[(v.edge, v.lane)].append(v.id)
                self.lanes[(v.edge, v.lane)].sort(key=lambda i: -self.vehicles[i].pos)
        self.time += dt
