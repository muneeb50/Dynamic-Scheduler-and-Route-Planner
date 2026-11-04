"""Common interface implemented by every traffic engine.

Two engines implement it:
  * BuiltinEngine – a pure-Python microscopic simulator (default, zero setup)
  * SumoEngine    – drives Eclipse SUMO through TraCI (optional, SIM_ENGINE=sumo)

The coordinator, priority controller and recovery engine only talk to this
interface, so the control logic is identical for both engines.
"""
from __future__ import annotations

import abc
import random
from dataclasses import dataclass, field
from typing import Optional

from ..core.network import RoadNetwork

# Demand presets: background vehicles inserted per hour for the reference (demo) network.
# Real map areas are larger, so the rate is multiplied by `network_demand_scale` (about
# proportional to the lane-kilometres of road), giving similar traffic density everywhere.
DEMAND_LEVELS = {"off": 0, "low": 900, "medium": 1800, "high": 2700, "peak": 3600}
REFERENCE_LANE_KM = 67.0
MAX_DEMAND_SCALE = 40.0
SMALL_NETWORK_NODES = 60  # below this, trips run between the network edges only (as in the original demo)
MAX_ZONES = 240


def network_demand_scale(net: RoadNetwork) -> float:
    lane_km = sum(e.length * e.lanes for e in net.edges.values()) / 1000.0
    if lane_km <= REFERENCE_LANE_KM * 1.2:  # the demo network and anything of similar size: unchanged
        return 1.0
    return min(MAX_DEMAND_SCALE, (lane_km / REFERENCE_LANE_KM) ** 0.85)


@dataclass
class VehicleState:
    id: str
    vtype: str
    edge: str  # current edge id (last known normal edge if inside a junction)
    route_index: int
    pos: float  # metres from the start of `edge`
    speed: float
    lat: float
    lon: float
    heading: float = 0.0
    lane: int = 0
    stops: int = 0
    waiting_time: float = 0.0
    arrived: bool = False


@dataclass
class TripRecord:
    vid: str
    depart: float
    arrival: float
    free_flow_time: float

    @property
    def time_loss(self) -> float:
        return max(0.0, (self.arrival - self.depart) - self.free_flow_time)


@dataclass
class DemandConfig:
    level: str = "medium"
    vehicles_per_hour: float = DEMAND_LEVELS["medium"]
    seed: int = 42
    min_trip_m: float = 600.0
    scale_with_network: bool = True  # multiply by network_demand_scale (1.0 for the demo network)


class TrafficEngine(abc.ABC):
    name = "base"

    def __init__(self, network: RoadNetwork, dt: float = 0.5, demand: Optional[DemandConfig] = None):
        self.net = network
        self.dt = dt
        self.time = 0.0
        self.demand = demand or DemandConfig()
        self.rng = random.Random(self.demand.seed)
        self._spawn_acc = 0.0
        self._veh_counter = 0
        self.trips: list[TripRecord] = []
        self._route_cache: dict[tuple[str, str], Optional[list[str]]] = {}
        self._trees: dict[str, dict[str, str]] = {}
        self._fringe = self._compute_fringe_nodes()
        self.demand_scale = network_demand_scale(network)
        self._zones, self._zone_w, self.internal_share = self._compute_zones()

    @property
    def effective_vehicles_per_hour(self) -> float:
        return self.demand.vehicles_per_hour * (self.demand_scale if self.demand.scale_with_network else 1.0)

    def _compute_zones(self) -> tuple[list[str], list[float], float]:
        """Trip ends for city traffic: junctions weighted by the importance of their roads.

        Small networks keep the original edge-to-edge demand (through traffic only)."""
        usable = [n for n in self.net.nodes.values() if n.outgoing and n.incoming]
        if len(usable) < SMALL_NETWORK_NODES:
            return self._fringe, [1.0] * len(self._fringe), 0.0
        rank = {"motorway": 5, "trunk": 5, "primary": 4, "secondary": 3, "tertiary": 2}

        def weight(n) -> float:
            return 1.0 + max((rank.get(self.net.edges[e].road_class.replace("_link", ""), 1) for e in n.incoming), default=1)

        rng = random.Random(self.demand.seed * 7919 + 17)
        pool = sorted(usable, key=lambda n: n.id)
        weights = [weight(n) for n in pool]
        chosen: dict[str, float] = {}
        k = min(MAX_ZONES, len(pool))
        while len(chosen) < k:  # weighted sample without replacement, deterministic
            n = rng.choices(pool, weights)[0]
            chosen.setdefault(n.id, weight(n))
        for f in self._fringe[: MAX_ZONES // 4]:
            chosen.setdefault(f, 2.0)
        ids = sorted(chosen)
        return ids, [chosen[i] for i in ids], 0.7

    # ----------------------------------------------------------- demand model
    def _compute_fringe_nodes(self) -> list[str]:
        lat0, lon0, lat1, lon1 = self.net.bbox()
        dlat, dlon = (lat1 - lat0) or 1e-6, (lon1 - lon0) or 1e-6
        fringe = []
        for n in self.net.nodes.values():
            if not n.outgoing or not n.incoming:
                continue
            fx = min(n.lon - lon0, lon1 - n.lon) / dlon
            fy = min(n.lat - lat0, lat1 - n.lat) / dlat
            if min(fx, fy) < 0.12:
                fringe.append(n.id)
        if len(fringe) < 4:
            fringe = [n.id for n in self.net.nodes.values() if n.outgoing and n.incoming]
        return sorted(fringe)

    def set_demand(self, level: str, vehicles_per_hour: Optional[float] = None):
        self.demand.level = level
        self.demand.vehicles_per_hour = (
            vehicles_per_hour if vehicles_per_hour is not None else DEMAND_LEVELS.get(level, DEMAND_LEVELS["medium"])
        )

    def _route(self, a: str, b: str) -> Optional[list[str]]:
        key = (a, b)
        if key not in self._route_cache:
            if len(self.net.nodes) < SMALL_NETWORK_NODES:
                self._route_cache[key] = self.net.shortest_path(a, b)
            else:  # one shortest-path tree per origin serves every destination
                tree = self._trees.get(a)
                if tree is None:
                    if len(self._trees) > 600:
                        self._trees.clear()
                    tree = self._trees[a] = self.net.shortest_path_tree(a)
                self._route_cache[key] = self.net.path_from_tree(tree, a, b)
            if len(self._route_cache) > 200_000:
                self._route_cache.clear()
        return self._route_cache[key]

    def _spawn_background(self):
        """Poisson-like insertion of background cars. All random draws happen here, in a
        fixed order, so two runs with the same seed get the same demand sequence even if
        the ambulance changes how traffic evolves (needed for matched comparisons)."""
        rate = self.effective_vehicles_per_hour / 3600.0
        if rate <= 0:
            self._spawn_acc = self.time
            return
        if self._spawn_acc < self.time - 60.0:  # demand was switched on (or raised) – don't burst
            self._spawn_acc = self.time
        while self._spawn_acc <= self.time:
            self._spawn_acc += self.rng.expovariate(rate)  # Poisson arrivals
            if self.internal_share and self.rng.random() < self.internal_share:
                src, dst = self.rng.choices(self._zones, self._zone_w, k=2)
            else:
                src = self.rng.choice(self._fringe)
                dst = self.rng.choice(self._fringe)
            factor = self.rng.uniform(0.8, 1.1)
            self._veh_counter += 1
            if src == dst:
                continue
            route = self._route(src, dst)
            if not route or self.net.route_length(route) < self.demand.min_trip_m:
                continue
            self.add_vehicle(f"car{self._veh_counter}", route, "car", speed_factor=factor)

    # ------------------------------------------------------------ abstract API
    @abc.abstractmethod
    def step(self) -> None: ...

    @abc.abstractmethod
    def add_vehicle(self, vid: str, route: list[str], vtype: str = "car", speed_factor: float = 1.0) -> bool:
        """Queue a vehicle for insertion at the start of route[0]."""

    @abc.abstractmethod
    def remove_vehicle(self, vid: str) -> None: ...

    @abc.abstractmethod
    def vehicle_state(self, vid: str) -> Optional[VehicleState]:
        """None if the vehicle is unknown or still waiting for insertion."""

    @abc.abstractmethod
    def has_arrived(self, vid: str) -> bool: ...

    @abc.abstractmethod
    def is_pending(self, vid: str) -> bool:
        """True while the vehicle is waiting to be inserted."""

    @abc.abstractmethod
    def set_signal_state(self, node_id: str, states: dict[str, str]) -> None:
        """states maps approach edge id -> 'G' | 'Y' | 'R'."""

    @abc.abstractmethod
    def halting(self, edge_id: str) -> int:
        """Number of vehicles slower than 0.5 m/s on the edge (a queue proxy)."""

    @abc.abstractmethod
    def edge_mean_speed(self, edge_id: str) -> Optional[float]: ...

    @abc.abstractmethod
    def edge_vehicles(self, edge_id: str) -> list[VehicleState]:
        """Vehicles currently on an edge (used by the camera renderer and ground truth)."""

    @abc.abstractmethod
    def vehicles_ahead(self, vid: str) -> int:
        """Vehicles between `vid` and the end of its current edge in the same lane."""

    @abc.abstractmethod
    def snapshot(self) -> list[VehicleState]: ...

    def close(self) -> None:  # pragma: no cover - overridden by SUMO
        pass

    @property
    def vehicle_count(self) -> int:
        return len(self.snapshot())
