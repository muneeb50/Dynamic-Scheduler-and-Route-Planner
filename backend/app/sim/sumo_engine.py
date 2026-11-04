"""Eclipse SUMO engine driven through TraCI.

Enable with SIM_ENGINE=sumo (build the net first: python simulation/tools/build_sumo_net.py).

The backend keeps full control of every traffic light: each step the signal
controllers' approach states are translated into a SUMO state string with
`trafficlight.setRedYellowGreenState`, so the very same priority/recovery logic
runs on both engines. Vehicle and edge data are read through TraCI
subscriptions (one round-trip per step instead of one per vehicle).
"""
from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path
from typing import Optional

from ..core.network import RoadNetwork
from .base import DemandConfig, TrafficEngine, TripRecord, VehicleState
from .builtin import arrival_pos

ROOT = Path(__file__).resolve().parents[3]
_labels = itertools.count(1)


def _import_traci():
    home = os.environ.get("SUMO_HOME")
    if home and str(Path(home) / "tools") not in sys.path:
        sys.path.append(str(Path(home) / "tools"))
    import traci  # type: ignore
    import traci.constants as tc  # type: ignore

    return traci, tc


def _sumo_binary(gui: bool) -> str:
    sys.path.insert(0, str(ROOT / "simulation" / "tools"))
    from build_sumo_net import find_sumo_binary  # type: ignore

    return find_sumo_binary("sumo-gui" if gui else "sumo")


class SumoEngine(TrafficEngine):
    name = "sumo"

    def __init__(self, network: RoadNetwork, dt: float = 0.5, demand: Optional[DemandConfig] = None,
                 net_file: Optional[str] = None, gui: bool = False, extra_args: Optional[list[str]] = None):
        super().__init__(network, dt, demand)
        traci, tc = _import_traci()
        self.traci, self.tc = traci, tc
        net_file = net_file or str(ROOT / "simulation" / "network" / "resq.net.xml")
        if not Path(net_file).exists():
            raise FileNotFoundError(f"{net_file} not found – run: python simulation/tools/build_sumo_net.py")
        self.label = f"resq{next(_labels)}_{os.getpid()}"
        cmd = [
            _sumo_binary(gui), "-n", net_file, "--step-length", str(dt), "--no-step-log", "true",
            "--time-to-teleport", "180", "--collision.action", "warn", "--no-warnings", "true",
            "--seed", str(self.demand.seed), "--default.speeddev", "0.1",
        ] + (extra_args or [])
        traci.start(cmd, label=self.label)
        self.conn = traci.getConnection(self.label)
        self._setup_types()
        self.sub: dict[str, dict] = {}
        self.edge_sub: dict[str, dict] = {}
        self.routes: dict[str, list[str]] = {}
        self.vtypes: dict[str, str] = {}
        self.free_flow: dict[str, float] = {}
        self.departed_at: dict[str, float] = {}
        self.pending_ids: set[str] = set()
        self.arrived_ids: set[str] = set()
        self._tls_links: dict[str, list[tuple[str, str]]] = {}
        self._tls_last: dict[str, str] = {}
        self.teleports = 0
        for e in network.edges:
            self.conn.edge.subscribe(e, [tc.LAST_STEP_VEHICLE_HALTING_NUMBER, tc.LAST_STEP_MEAN_SPEED,
                                         tc.LAST_STEP_VEHICLE_NUMBER, tc.LAST_STEP_VEHICLE_ID_LIST])
        self.conn_tl_ids = set(self.conn.trafficlight.getIDList())

    def _setup_types(self):
        vt = self.conn.vehicletype
        vt.copy("DEFAULT_VEHTYPE", "car")
        vt.setLength("car", 4.5)
        vt.setAccel("car", 1.6)
        vt.setDecel("car", 2.5)
        vt.setTau("car", 1.4)
        vt.copy("DEFAULT_VEHTYPE", "ambulance")
        vt.setVehicleClass("ambulance", "emergency")
        vt.setShapeClass("ambulance", "emergency")
        vt.setLength("ambulance", 6.0)
        vt.setAccel("ambulance", 2.2)
        vt.setDecel("ambulance", 3.0)
        vt.setTau("ambulance", 1.0)
        vt.setSpeedFactor("ambulance", 1.25)
        vt.setMaxSpeed("ambulance", 30.0)
        vt.setColor("ambulance", (220, 30, 30, 255))

    # ---------------------------------------------------------------- vehicles
    def add_vehicle(self, vid: str, route: list[str], vtype: str = "car", speed_factor: float = 1.0) -> bool:
        if not route:
            return False
        rid = f"r_{vid}"
        try:
            self.conn.route.add(rid, route)
            last_len = self.net.edges[route[-1]].length
            self.conn.vehicle.add(vid, rid, typeID=vtype, depart="now", departLane="best", departSpeed="max",
                                  arrivalPos=f"{arrival_pos(last_len, vtype):.1f}")
            if vtype == "car":
                self.conn.vehicle.setSpeedFactor(vid, speed_factor)
        except self.traci.TraCIException:
            return False
        self.routes[vid] = list(route)
        self.vtypes[vid] = vtype
        self.free_flow[vid] = self.net.route_free_flow_time(route)
        self.pending_ids.add(vid)
        return True

    def remove_vehicle(self, vid: str) -> None:
        try:
            self.conn.vehicle.remove(vid)
        except self.traci.TraCIException:
            pass
        self.sub.pop(vid, None)
        self.pending_ids.discard(vid)

    def has_arrived(self, vid: str) -> bool:
        return vid in self.arrived_ids

    def is_pending(self, vid: str) -> bool:
        return vid in self.pending_ids

    # ----------------------------------------------------------------- signals
    def _links(self, tls: str) -> list[tuple[str, str]]:
        """(incoming edge, link kind 'G'|'g') for every controlled link index."""
        if tls not in self._tls_links:
            conflicting = "r" if self.net.left_hand_traffic else "l"
            out = []
            for group in self.conn.trafficlight.getControlledLinks(tls):
                if not group:
                    out.append(("", "G"))
                    continue
                in_lane, out_lane, _ = group[0]
                direction = "s"
                for link in self.conn.lane.getLinks(in_lane, True):
                    if link[0] == out_lane:
                        direction = link[6]
                        break
                kind = "g" if direction in (conflicting, "t") else "G"
                out.append((in_lane.rsplit("_", 1)[0], kind))
            self._tls_links[tls] = out
        return self._tls_links[tls]

    def set_signal_state(self, node_id: str, states: dict[str, str]) -> None:
        if node_id not in self.conn_tl_ids:
            return
        chars = []
        for edge, kind in self._links(node_id):
            s = states.get(edge, "R")
            chars.append(kind if s == "G" else ("y" if s == "Y" else "r"))
        state = "".join(chars)
        if self._tls_last.get(node_id) != state:
            self.conn.trafficlight.setRedYellowGreenState(node_id, state)
            self._tls_last[node_id] = state

    # ------------------------------------------------------------------- query
    def _state(self, vid: str) -> Optional[VehicleState]:
        r = self.sub.get(vid)
        if r is None:
            return None
        tc = self.tc
        road = r[tc.VAR_ROAD_ID]
        idx = r[tc.VAR_ROUTE_INDEX]
        route = self.routes.get(vid, [])
        if road.startswith(":") or road not in self.net.edges:
            edge = route[idx] if 0 <= idx < len(route) else (route[-1] if route else road)
            pos = self.net.edges[edge].length if edge in self.net.edges else 0.0
        else:
            edge, pos = road, r[tc.VAR_LANEPOSITION]
        x, y = r[tc.VAR_POSITION]
        lat, lon = self.net.proj.to_latlon(x, y)
        return VehicleState(
            id=vid, vtype=self.vtypes.get(vid, "car"), edge=edge, route_index=max(idx, 0), pos=pos,
            speed=r[tc.VAR_SPEED], lat=lat, lon=lon, heading=r[tc.VAR_ANGLE], lane=r[tc.VAR_LANE_INDEX],
        )

    def vehicle_state(self, vid: str) -> Optional[VehicleState]:
        return self._state(vid)

    def halting(self, edge_id: str) -> int:
        r = self.edge_sub.get(edge_id)
        return int(r[self.tc.LAST_STEP_VEHICLE_HALTING_NUMBER]) if r else 0

    def edge_mean_speed(self, edge_id: str) -> Optional[float]:
        r = self.edge_sub.get(edge_id)
        if not r or r[self.tc.LAST_STEP_VEHICLE_NUMBER] == 0:
            return None
        return float(r[self.tc.LAST_STEP_MEAN_SPEED])

    def edge_vehicles(self, edge_id: str) -> list[VehicleState]:
        r = self.edge_sub.get(edge_id)
        if not r:
            return []
        return [s for s in (self._state(v) for v in r[self.tc.LAST_STEP_VEHICLE_ID_LIST]) if s]

    def vehicles_ahead(self, vid: str) -> int:
        me = self._state(vid)
        if not me:
            return 0
        n = 0
        for other in self.edge_vehicles(me.edge):
            if other.id != vid and other.lane == me.lane and other.pos > me.pos:
                n += 1
        return n

    def snapshot(self) -> list[VehicleState]:
        return [s for s in (self._state(v) for v in list(self.sub)) if s]

    # -------------------------------------------------------------- simulation
    def step(self) -> None:
        self._spawn_background()
        tc = self.tc
        self.conn.simulationStep()
        self.time = self.conn.simulation.getTime()
        for vid in self.conn.simulation.getDepartedIDList():
            self.pending_ids.discard(vid)
            self.departed_at[vid] = self.time
            self.conn.vehicle.subscribe(vid, [tc.VAR_ROAD_ID, tc.VAR_LANEPOSITION, tc.VAR_SPEED, tc.VAR_POSITION,
                                              tc.VAR_ANGLE, tc.VAR_ROUTE_INDEX, tc.VAR_LANE_INDEX])
        for vid in self.conn.simulation.getArrivedIDList():
            self.arrived_ids.add(vid)
            self.sub.pop(vid, None)
            if self.vtypes.get(vid) == "car" and vid in self.departed_at:
                self.trips.append(TripRecord(vid, self.departed_at.pop(vid), self.time, self.free_flow.pop(vid, 0.0)))
            self.routes.pop(vid, None) if self.vtypes.get(vid) == "car" else None
        self.teleports += self.conn.simulation.getStartingTeleportNumber()
        self.sub = dict(self.conn.vehicle.getAllSubscriptionResults())
        self.edge_sub = dict(self.conn.edge.getAllSubscriptionResults())

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
