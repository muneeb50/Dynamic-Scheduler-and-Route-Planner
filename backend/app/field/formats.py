"""Open traffic-signal data formats: parsing and producing.

* SAE J2735 SPaT (Signal Phase and Timing) in its common JSON rendering
  (as produced by J2735 ASN.1 → JSON decoders and V2X/RSU tool chains):
      {"intersections": [{"id": {"id": 101}, "states": [
          {"signalGroup": 2, "state-time-speed": [{"eventState": "protected-Movement-Allowed",
                                                   "timing": {"minEndTime": 34000}}]}]}]}
  `eventState` may be the J2735 enumeration name or its integer code.
* OGC SensorThings API observations (e.g. city IoT platforms publishing traffic-light
  states as Datastreams): {"result": <value>, "phenomenonTime": "..."}.
* Simple JSON: {"approaches": {"N": "green", "E": "red"}} or {"N": "G", "E": "R"}.

All states are normalised to "G" (go), "Y" (clearance/amber), "R" (stop) or "X" (dark/unknown).
"""
from __future__ import annotations

import time
from typing import Any, Optional

# J2735 MovementPhaseState
J2735_CODES = {
    0: "unavailable", 1: "dark", 2: "stop-Then-Proceed", 3: "stop-And-Remain", 4: "pre-Movement",
    5: "permissive-Movement-Allowed", 6: "protected-Movement-Allowed", 7: "permissive-clearance",
    8: "protected-clearance", 9: "caution-Conflicting-Traffic",
}
J2735_TO_STATE = {
    "unavailable": "X", "dark": "X", "stop-then-proceed": "R", "stop-and-remain": "R", "pre-movement": "R",
    "permissive-movement-allowed": "G", "protected-movement-allowed": "G", "permissive-clearance": "Y",
    "protected-clearance": "Y", "caution-conflicting-traffic": "Y",
}
STATE_TO_J2735 = {"G": "protected-Movement-Allowed", "Y": "protected-clearance", "R": "stop-And-Remain",
                  "X": "dark"}
WORDS = {
    "g": "G", "green": "G", "go": "G", "grün": "G", "gruen": "G", "allowed": "G", "proceed": "G",
    "y": "Y", "yellow": "Y", "amber": "Y", "orange": "Y", "gelb": "Y", "clearance": "Y", "caution": "Y",
    "r": "R", "red": "R", "stop": "R", "rot": "R", "red-amber": "R", "redamber": "R", "rotgelb": "R",
    "x": "X", "off": "X", "dark": "X", "unknown": "X", "unavailable": "X",
}


def normalize_state(value: Any, value_map: Optional[dict] = None) -> str:
    """Map any reported signal value to G / Y / R / X (value_map wins for source-specific codes)."""
    if value_map:
        for key in (value, str(value), str(value).lower()):
            if key in value_map:
                return normalize_state(value_map[key])
    if isinstance(value, bool):
        return "G" if value else "R"
    if isinstance(value, (int, float)) and int(value) in J2735_CODES:
        return J2735_TO_STATE[J2735_CODES[int(value)].lower()]
    s = str(value).strip().lower().replace("_", "-")
    if s in J2735_TO_STATE:
        return J2735_TO_STATE[s]
    if s.replace("-", "") in WORDS:
        return WORDS[s.replace("-", "")]
    return WORDS.get(s, "X")


def parse_spat(doc: Any, intersection_id: Optional[Any] = None, group_map: Optional[dict] = None) -> dict[str, dict]:
    """J2735 SPaT JSON -> {approach or 'sg<n>': {"state": G/Y/R/X, "raw": eventState, "min_end_s": float|None}}."""
    if isinstance(doc, dict) and "spat" in doc:
        doc = doc["spat"]
    inters = doc.get("intersections", doc.get("IntersectionStateList", [])) if isinstance(doc, dict) else doc
    if isinstance(inters, dict):
        inters = [inters]
    out: dict[str, dict] = {}
    for inter in inters or []:
        iid = inter.get("id")
        if isinstance(iid, dict):
            iid = iid.get("id")
        if intersection_id is not None and str(iid) != str(intersection_id):
            continue
        for st in inter.get("states", inter.get("MovementList", [])):
            sg = st.get("signalGroup")
            events = st.get("state-time-speed") or st.get("stateTimeSpeed") or st.get("state_time_speed") or []
            if isinstance(events, dict):
                events = [events]
            if not events:
                continue
            ev = events[0]
            raw = ev.get("eventState")
            timing = ev.get("timing") or {}
            min_end = timing.get("minEndTime")
            key = (group_map or {}).get(str(sg)) or (group_map or {}).get(sg) or f"sg{sg}"
            out[key] = {"state": normalize_state(raw), "raw": raw,
                        "min_end_s": None if min_end is None else _time_mark_to_s(min_end)}
        if intersection_id is None:
            break  # first intersection only
    return out


def _time_mark_to_s(mark: Any) -> Optional[float]:
    """J2735 TimeMark: tenths of a second past the current UTC hour (0..35999); returns seconds from now."""
    try:
        m = float(mark)
    except (TypeError, ValueError):
        return None
    if m > 36000:  # absolute timestamps or ms – leave as is
        return None
    now = time.time() % 3600
    return round((m / 10.0 - now) % 3600, 1)


def build_spat(intersection_id: Any, name: str, approaches: dict[str, str], group_numbers: dict[str, int],
               remaining_s: dict[str, float]) -> dict:
    """Produce a J2735-style SPaT JSON for a controlled junction (consumable by V2X tools / other apps)."""
    now_tenths = (time.time() % 3600) * 10
    states = []
    for a, st in approaches.items():
        states.append({
            "signalGroup": group_numbers[a],
            "movementName": a,
            "state-time-speed": [{
                "eventState": STATE_TO_J2735.get(st, "dark"),
                "timing": {"minEndTime": int((now_tenths + 10 * remaining_s.get(a, 0)) % 36000)},
            }],
        })
    return {"timeStamp": int(time.time() % 60 * 1000), "name": name,
            "intersections": [{"id": {"id": intersection_id}, "name": name, "revision": 1,
                               "status": "0000000000000000", "moy": None, "timeStamp": None, "states": states}]}


def parse_simple(doc: Any, value_map: Optional[dict] = None) -> dict[str, dict]:
    data = doc.get("approaches", doc) if isinstance(doc, dict) else {}
    return {str(k): {"state": normalize_state(v, value_map), "raw": v, "min_end_s": None}
            for k, v in data.items() if not isinstance(v, (dict, list))}


def parse_sensorthings_observation(doc: Any, value_map: Optional[dict] = None) -> Optional[dict]:
    """One Observation (or a {'value': [obs…]} page) -> {"state", "raw", "time"}."""
    if isinstance(doc, dict) and "value" in doc and isinstance(doc["value"], list):
        if not doc["value"]:
            return None
        doc = doc["value"][0]
    if not isinstance(doc, dict) or "result" not in doc:
        return None
    return {"state": normalize_state(doc["result"], value_map), "raw": doc["result"],
            "time": doc.get("phenomenonTime") or doc.get("resultTime"), "min_end_s": None}
