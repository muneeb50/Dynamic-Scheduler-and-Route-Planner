"""Signal controller: one instance per signalised intersection.

Each intersection has up to two *phase groups* of approaches that never conflict
(see RoadNetwork.signal_groups). The controller cycles

    GREEN(group k) -> YELLOW(group k) -> ALL_RED -> GREEN(group k+1) -> ...

and these are the ONLY transitions it can make, so a conflicting green is
impossible by construction. Every tick also re-validates that invariant and counts
violations (expected to stay at 0) so the safety claim is measured, not assumed.

Emergency priority never skips amber or all-red, and only truncates the
current conflicting green after its minimum green time has elapsed.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class Stage(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    ALL_RED = "ALL_RED"


class Mode(str, Enum):
    NORMAL = "NORMAL"
    PRIORITY = "PRIORITY"
    RECOVERY = "RECOVERY"


ALLOWED = {
    (Stage.GREEN, Stage.YELLOW),
    (Stage.YELLOW, Stage.ALL_RED),
    (Stage.ALL_RED, Stage.GREEN),
}


@dataclass
class SignalPlan:
    green: float = 25.0
    yellow: float = 3.0
    all_red: float = 2.0
    min_green: float = 7.0
    max_green: float = 60.0

    @property
    def cycle(self) -> float:
        return 2 * (self.green + self.yellow + self.all_red)


@dataclass
class SignalController:
    node_id: str
    groups: list[list[str]]
    plan: SignalPlan = field(default_factory=SignalPlan)
    current: int = 0
    stage: Stage = Stage.GREEN
    elapsed: float = 0.0
    mode: Mode = Mode.NORMAL
    priority_group: Optional[int] = None
    priority_owner: Optional[str] = None
    green_time_provider: Optional[Callable[[int], Optional[float]]] = None
    current_green: float = 0.0
    violations: int = 0
    truncated_greens: int = 0
    cycles: int = 0

    def __post_init__(self):
        self.group_of = {a: i for i, g in enumerate(self.groups) for a in g}
        self.current_green = self.plan.green
        if len(self.groups) > 1:
            # deterministic offset per intersection so signals are not all in phase
            off = zlib.crc32(self.node_id.encode()) % int(self.plan.cycle)
            self._advance(float(off))

    # ------------------------------------------------------------------ state
    @property
    def n_groups(self) -> int:
        return len(self.groups)

    def approach_states(self) -> dict[str, str]:
        out = {}
        for i, g in enumerate(self.groups):
            if self.n_groups == 1:
                s = "G"
            elif i == self.current and self.stage == Stage.GREEN:
                s = "G"
            elif i == self.current and self.stage == Stage.YELLOW:
                s = "Y"
            else:
                s = "R"
            for a in g:
                out[a] = s
        return out

    def is_green_for(self, group: int) -> bool:
        return self.n_groups == 1 or (self.current == group and self.stage == Stage.GREEN)

    def time_to_priority_green(self, group: int) -> float:
        """Estimated seconds until `group` is green if priority were requested now."""
        p = self.plan
        if self.is_green_for(group):
            return 0.0
        if self.stage == Stage.GREEN:
            return max(0.0, p.min_green - self.elapsed) + p.yellow + p.all_red
        if self.stage == Stage.YELLOW:
            return max(0.0, p.yellow - self.elapsed) + p.all_red
        return max(0.0, p.all_red - self.elapsed)

    # --------------------------------------------------------------- priority
    def request_priority(self, group: int, owner: str) -> bool:
        if self.priority_owner is not None and self.priority_owner != owner:
            return False
        self.priority_group = group
        self.priority_owner = owner
        self.mode = Mode.PRIORITY
        return True

    def release_priority(self, owner: str, recovery: bool = True) -> None:
        if self.priority_owner != owner:
            return
        self.priority_group = None
        self.priority_owner = None
        self.mode = Mode.RECOVERY if recovery else Mode.NORMAL

    # ------------------------------------------------------------------- tick
    def _set_stage(self, new: Stage, events: list):
        if (self.stage, new) not in ALLOWED:
            self.violations += 1
            events.append({"type": "SAFETY_VIOLATION", "node": self.node_id, "from": self.stage, "to": new})
        self.stage = new
        self.elapsed = 0.0

    def _green_duration(self, group: int) -> float:
        if self.green_time_provider is not None:
            g = self.green_time_provider(group)
            if g is not None:
                return min(max(g, self.plan.min_green), self.plan.max_green)
        return self.plan.green

    def _advance(self, dt: float) -> list[dict]:
        events: list[dict] = []
        if self.n_groups <= 1:
            return events
        remaining = dt
        while remaining > 1e-9:
            step = min(remaining, 0.5)
            remaining -= step
            self.elapsed += step
            p = self.plan
            if self.stage == Stage.GREEN:
                if self.priority_group is not None:
                    if self.current != self.priority_group and self.elapsed >= p.min_green:
                        if self.elapsed < self.current_green:
                            self.truncated_greens += 1
                        self._set_stage(Stage.YELLOW, events)
                        events.append({"type": "SIGNAL_TRANSITION", "node": self.node_id, "stage": "YELLOW",
                                       "group": self.current, "reason": "priority"})
                elif self.elapsed >= self.current_green:
                    self._set_stage(Stage.YELLOW, events)
            elif self.stage == Stage.YELLOW and self.elapsed >= p.yellow:
                self._set_stage(Stage.ALL_RED, events)
            elif self.stage == Stage.ALL_RED and self.elapsed >= p.all_red:
                nxt = self.priority_group if self.priority_group is not None else (self.current + 1) % self.n_groups
                if nxt == 0 and self.current != 0:
                    self.cycles += 1
                self.current = nxt
                self._set_stage(Stage.GREEN, events)
                self.current_green = self._green_duration(nxt)
                if self.priority_group is not None:
                    events.append({"type": "SIGNAL_TRANSITION", "node": self.node_id, "stage": "GREEN",
                                   "group": nxt, "reason": "priority"})
            self._validate(events)
        return events

    def _validate(self, events: list):
        states = self.approach_states()
        active_groups = {self.group_of[a] for a, s in states.items() if s in ("G", "Y")}
        if len(active_groups) > 1:
            self.violations += 1
            events.append({"type": "SAFETY_VIOLATION", "node": self.node_id, "detail": "conflicting groups active"})

    def tick(self, dt: float) -> list[dict]:
        return self._advance(dt)

    def to_dict(self) -> dict:
        return {
            "node": self.node_id,
            "mode": self.mode.value,
            "stage": self.stage.value,
            "current_group": self.current,
            "elapsed": round(self.elapsed, 1),
            "current_green": round(self.current_green, 1),
            "groups": self.groups,
            "approaches": self.approach_states(),
            "priority_owner": self.priority_owner,
            "violations": self.violations,
        }
