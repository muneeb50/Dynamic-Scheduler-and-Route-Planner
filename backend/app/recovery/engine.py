"""Adaptive traffic recovery after an emergency priority intervention.

Lifecycle per intersection (one RecoveryEvent may span several signal phases):

  A. baseline   – mean queue per approach over the window *before* priority was requested
  B/C. priority granted, ambulance confirmed past the junction (PriorityManager)
  D. backlog    – ΔQ_i = Q_i,current − Q_i,baseline for each approach i
  E. timing     – each new green for a group gets
                      green = clamp(base_green + gain · max_i(ΔQ_i⁺ / lanes_i), min_green, max_green)
                  so the most delayed approaches are flushed first, while every group
                  still gets at least its minimum green and amber/all-red are untouched
  F. stop       – COMPLETED once every approach satisfies ΔQ_i ≤ max(abs_tol, rel_tol·baseline_i)
                  for `stable_s` seconds; TIMEOUT (degraded) if that never happens within
                  `max_duration_s`. Residual backlog is logged either way.
"""
from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..core.network import RoadNetwork
from ..traffic_control.signal import Mode, SignalController


@dataclass
class RecoveryConfig:
    enabled: bool = True
    baseline_window_s: float = 90.0
    abs_tolerance: float = 2.0
    rel_tolerance: float = 0.25
    stable_s: float = 10.0
    max_duration_s: float = 240.0
    gain_s_per_vehicle: float = 2.0


@dataclass
class RecoveryEvent:
    id: str
    node: str
    priority_session: str
    journey_id: str
    started_at: float
    baseline: dict[str, float]
    at_release: dict[str, float]
    peak_backlog: dict[str, float] = field(default_factory=dict)
    completed_at: Optional[float] = None
    status: str = "ACTIVE"  # ACTIVE | COMPLETED | TIMEOUT | SUPERSEDED
    residual: dict[str, float] = field(default_factory=dict)
    timing_adjustments: list[dict] = field(default_factory=list)
    stable_since: Optional[float] = None
    timeline: list[dict] = field(default_factory=list)

    @property
    def duration(self) -> Optional[float]:
        return None if self.completed_at is None else self.completed_at - self.started_at

    def to_dict(self, include_timeline: bool = False) -> dict:
        d = {
            "id": self.id, "node": self.node, "priority_session": self.priority_session,
            "journey_id": self.journey_id, "status": self.status,
            "started_at": round(self.started_at, 1),
            "completed_at": None if self.completed_at is None else round(self.completed_at, 1),
            "duration_s": None if self.duration is None else round(self.duration, 1),
            "baseline": {k: round(v, 2) for k, v in self.baseline.items()},
            "at_release": self.at_release,
            "peak_backlog": {k: round(v, 2) for k, v in self.peak_backlog.items()},
            "residual": {k: round(v, 2) for k, v in self.residual.items()},
            "timing_adjustments": self.timing_adjustments[-20:],
        }
        if include_timeline:
            d["timeline"] = self.timeline
        return d


class QueueHistory:
    """1 Hz queue samples per intersection, used for pre-priority baselines and charts."""

    def __init__(self, horizon_s: float = 600.0):
        self.horizon = horizon_s
        self.samples: dict[str, deque] = {}

    def record(self, node: str, t: float, queues: dict[str, float]):
        dq = self.samples.setdefault(node, deque())
        dq.append((t, dict(queues)))
        while dq and dq[0][0] < t - self.horizon:
            dq.popleft()

    def mean_between(self, node: str, t0: float, t1: float) -> Optional[dict[str, float]]:
        rows = [q for (t, q) in self.samples.get(node, ()) if t0 <= t < t1]
        if not rows:
            return None
        keys = rows[0].keys()
        return {k: sum(r.get(k, 0) for r in rows) / len(rows) for k in keys}

    def latest(self, node: str) -> Optional[dict[str, float]]:
        dq = self.samples.get(node)
        return dict(dq[-1][1]) if dq else None

    def series(self, node: str, since: float = 0.0) -> list[dict]:
        return [{"t": round(t, 1), **q} for (t, q) in self.samples.get(node, ()) if t >= since]


class RecoveryEngine:
    def __init__(
        self,
        net: RoadNetwork,
        controllers: dict[str, SignalController],
        history: QueueHistory,
        queue_provider: Callable[[str], float],
        config: Optional[RecoveryConfig] = None,
    ):
        self.net = net
        self.controllers = controllers
        self.history = history
        self.queue_provider = queue_provider
        self.cfg = config or RecoveryConfig()
        self.active: dict[str, RecoveryEvent] = {}  # node -> event
        self.finished: list[RecoveryEvent] = []
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------ start
    def on_priority_requested(self, node: str, now: float) -> list[dict]:
        ev = self.active.pop(node, None)
        if not ev:
            return []
        self._finish(ev, now, "SUPERSEDED")
        return [{"type": "RECOVERY_SUPERSEDED", **ev.to_dict()}]

    def start(self, session: dict, now: float) -> list[dict]:
        if not self.cfg.enabled:
            ctrl = self.controllers[session["node"]]
            ctrl.mode = Mode.NORMAL
            return []
        node = session["node"]
        requested_at = session["requested_at"]
        baseline = self.history.mean_between(node, requested_at - self.cfg.baseline_window_s, requested_at)
        current = self._queues(node)
        if baseline is None:
            baseline = {a: float(q) for a, q in session.get("queues_at_request", current).items()}
        ev = RecoveryEvent(
            id=f"R{next(self._ids)}", node=node, priority_session=session["id"], journey_id=session["journey_id"],
            started_at=now, baseline=baseline, at_release=current,
        )
        self.active[node] = ev
        ctrl = self.controllers[node]
        ctrl.mode = Mode.RECOVERY
        ctrl.green_time_provider = lambda g, ev=ev: self._green_for(ev, g)
        return [{"type": "RECOVERY_STARTED", **ev.to_dict()}]

    # ----------------------------------------------------------------- policy
    def _queues(self, node: str) -> dict[str, float]:
        return {a: float(self.queue_provider(a)) for a in self.net.nodes[node].incoming}

    def _tolerance(self, base: float) -> float:
        return max(self.cfg.abs_tolerance, self.cfg.rel_tolerance * base)

    def _green_for(self, ev: RecoveryEvent, group: int) -> Optional[float]:
        if ev.status != "ACTIVE":
            return None
        ctrl = self.controllers[ev.node]
        q = self._queues(ev.node)
        per_lane_backlog = 0.0
        for a in ctrl.groups[group]:
            dq = max(0.0, q.get(a, 0.0) - ev.baseline.get(a, 0.0))
            per_lane_backlog = max(per_lane_backlog, dq / self.net.edges[a].lanes)
        green = ctrl.plan.green + self.cfg.gain_s_per_vehicle * per_lane_backlog
        green = min(max(green, ctrl.plan.min_green), ctrl.plan.max_green)
        ev.timing_adjustments.append({"group": group, "green_s": round(green, 1), "backlog_per_lane": round(per_lane_backlog, 1)})
        return green

    # ----------------------------------------------------------------- update
    def update(self, now: float) -> list[dict]:
        events: list[dict] = []
        for node, ev in list(self.active.items()):
            q = self._queues(node)
            all_ok = True
            for a, cur in q.items():
                base = ev.baseline.get(a, 0.0)
                delta = cur - base
                ev.peak_backlog[a] = max(ev.peak_backlog.get(a, 0.0), delta)
                if delta > self._tolerance(base):
                    all_ok = False
            ev.timeline.append({"t": round(now, 1), **{a: round(v - ev.baseline.get(a, 0.0), 1) for a, v in q.items()}})
            if all_ok:
                if ev.stable_since is None:
                    ev.stable_since = now
                if now - ev.stable_since >= self.cfg.stable_s:
                    ev.residual = {a: q[a] - ev.baseline.get(a, 0.0) for a in q}
                    self._finish(ev, ev.stable_since, "COMPLETED")
                    events.append({"type": "RECOVERY_COMPLETED", **ev.to_dict()})
                    continue
            else:
                ev.stable_since = None
            if now - ev.started_at >= self.cfg.max_duration_s:
                ev.residual = {a: q[a] - ev.baseline.get(a, 0.0) for a in q}
                self._finish(ev, now, "TIMEOUT")
                events.append({"type": "RECOVERY_TIMEOUT", **ev.to_dict()})
        return events

    def _finish(self, ev: RecoveryEvent, t: float, status: str):
        ev.status = status
        ev.completed_at = t
        self.active.pop(ev.node, None)
        self.finished.append(ev)
        ctrl = self.controllers[ev.node]
        if ctrl.priority_owner is None:
            ctrl.mode = Mode.NORMAL
        ctrl.green_time_provider = None

    def all_events(self) -> list[RecoveryEvent]:
        return self.finished + list(self.active.values())
