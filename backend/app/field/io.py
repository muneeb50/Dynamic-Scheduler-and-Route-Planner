"""Signal data inputs (sources) and signal outputs (drivers).

Sources (read live signal states):
  spat         – J2735 SPaT JSON from an HTTP URL or a local file, polled
  sensorthings – OGC SensorThings API: latest Observation of one Datastream per approach
  json         – simple JSON {"approaches": {"N": "green", …}} from a URL or file
  mqtt         – MQTT topic carrying SPaT JSON, simple JSON or SensorThings observations

Drivers (send our controller's states to a signal):
  virtual – dashboard only
  serial  – Arduino / ESP32 LED traffic-light model over USB serial ("S N=G E=R S=G W=R")
  http    – POST JSON to a device or gateway (e.g. the ESP32 sketch in hardware/)
  mqtt    – publish simple JSON or SPaT JSON

Drivers only ever receive states produced by the safety-checked SignalController.
They are meant for model / demonstration signals: controlling a real street signal
requires a certified controller and the traffic authority's approval.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .formats import build_spat, parse_sensorthings_observation, parse_simple, parse_spat

log = logging.getLogger("resqroute.field")


def _fetch_json(url_or_path: str, timeout: float = 5.0) -> Any:
    if url_or_path.startswith(("http://", "https://")):
        req = urllib.request.Request(url_or_path, headers={"Accept": "application/json",
                                                            "User-Agent": "ResQRouteAI/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    return json.loads(Path(url_or_path).read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------ sources
class SignalSource:
    """Polls / subscribes in a background thread; `latest` holds {approach: {state, raw, min_end_s}}."""

    kind = "none"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.latest: dict[str, dict] = {}
        self.updated_at: Optional[float] = None
        self.error: Optional[str] = None
        self.messages = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"signal-source-{self.kind}")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _set(self, states: dict[str, dict]):
        if states:
            self.latest = states
            self.updated_at = time.time()
            self.messages += 1
            self.error = None

    def _run(self):  # polling sources
        interval = float(self.cfg.get("poll_s", 1.0))
        while not self._stop.is_set():
            try:
                self._set(self.poll())
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(interval)

    def poll(self) -> dict[str, dict]:  # pragma: no cover - abstract
        return {}

    @property
    def fresh(self) -> bool:
        return self.updated_at is not None and time.time() - self.updated_at <= float(self.cfg.get("stale_s", 10))

    def status(self) -> dict:
        return {"type": self.kind, "fresh": self.fresh, "error": self.error, "messages": self.messages,
                "updated_at": self.updated_at, "latest": self.latest}


class SpatSource(SignalSource):
    kind = "spat"

    def poll(self):
        return parse_spat(_fetch_json(self.cfg["url"]), self.cfg.get("intersection_id"),
                          self.cfg.get("group_map"))


class JsonSource(SignalSource):
    kind = "json"

    def poll(self):
        return parse_simple(_fetch_json(self.cfg["url"]), self.cfg.get("value_map"))


class SensorThingsSource(SignalSource):
    """cfg: base_url (…/v1.1), datastreams {approach: datastream id}, value_map {raw: state}."""

    kind = "sensorthings"

    def poll(self):
        base = self.cfg["base_url"].rstrip("/")
        out = {}
        for approach, ds in self.cfg.get("datastreams", {}).items():
            q = urllib.parse.quote("phenomenonTime desc")
            obs = parse_sensorthings_observation(
                _fetch_json(f"{base}/Datastreams({ds})/Observations?$orderby={q}&$top=1"), self.cfg.get("value_map"))
            if obs:
                out[approach] = obs
        return out


class MqttSource(SignalSource):
    """cfg: host, port, topics {topic: approach | "*"}, format: spat | json | sensorthings."""

    kind = "mqtt"

    def _run(self):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.error = "paho-mqtt not installed (pip install paho-mqtt)"
            return
        topics: dict = self.cfg.get("topics") or {self.cfg.get("topic", "#"): "*"}
        fmt = self.cfg.get("format", "json")

        def on_message(_c, _u, msg):
            try:
                doc = json.loads(msg.payload.decode("utf-8"))
                target = next((a for t, a in topics.items() if mqtt.topic_matches_sub(t, msg.topic)), "*")
                if fmt == "spat":
                    states = parse_spat(doc, self.cfg.get("intersection_id"), self.cfg.get("group_map"))
                elif fmt == "sensorthings":
                    obs = parse_sensorthings_observation(doc, self.cfg.get("value_map"))
                    states = {target: obs} if obs and target != "*" else {}
                else:
                    states = parse_simple(doc, self.cfg.get("value_map"))
                self._set({**self.latest, **states})
            except Exception as exc:
                self.error = f"bad message on {msg.topic}: {exc}"

        def on_connect(c, _u, _flags, reason_code, _props=None):
            # (re)subscribe on every (re)connect – paho reconnects by itself after a drop
            if not getattr(reason_code, "is_failure", False):
                for t in topics:
                    c.subscribe(t)
                self.error = None
            else:
                self.error = f"MQTT connect refused: {reason_code}"

        def on_disconnect(_c, _u, _flags, reason_code, _props=None):
            if not self._stop.is_set():
                self.error = "MQTT disconnected – reconnecting"

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.on_message = on_message
        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.reconnect_delay_set(min_delay=1, max_delay=10)
        if self.cfg.get("username"):
            client.username_pw_set(self.cfg["username"], self.cfg.get("password"))
        while not self._stop.is_set():  # first connection; afterwards paho's loop reconnects
            try:
                client.connect(self.cfg.get("host", "localhost"), int(self.cfg.get("port", 1883)), keepalive=30)
                break
            except Exception as exc:
                self.error = f"MQTT: {exc}"
                self._stop.wait(3.0)
        if self._stop.is_set():
            return
        client.loop_start()
        self._stop.wait()
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass


SOURCES = {"spat": SpatSource, "json": JsonSource, "sensorthings": SensorThingsSource, "mqtt": MqttSource}


def make_source(cfg: Optional[dict]) -> Optional[SignalSource]:
    if not cfg or cfg.get("type", "none") == "none":
        return None
    if cfg["type"] not in SOURCES:
        raise ValueError(f"unknown signal source '{cfg['type']}' ({', '.join(SOURCES)})")
    return SOURCES[cfg["type"]](cfg)


# ------------------------------------------------------------------------------ drivers
class SignalDriver:
    kind = "virtual"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.error: Optional[str] = None
        self.sent = 0
        self.last_payload: Optional[str] = None

    def apply(self, junction: dict):
        """junction: {"id", "name", "approaches": {a: G/Y/R}, "mode", "remaining": {a: s}, "groups": {a: n}}"""
        self.sent += 1

    def close(self):
        pass

    def status(self) -> dict:
        return {"type": self.kind, "error": self.error, "sent": self.sent, "last": self.last_payload}


class _AsyncDriver(SignalDriver):
    """Sends in a worker thread so a slow device never blocks the controller; only the newest state matters."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._q: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        self._t = threading.Thread(target=self._worker, daemon=True)
        self._closed = False
        self._t.start()

    def apply(self, junction: dict):
        try:
            self._q.get_nowait()
        except queue.Empty:
            pass
        self._q.put_nowait(junction)

    def _worker(self):
        while not self._closed:
            try:
                j = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.send(j)
                self.sent += 1
                self.error = None
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"

    def send(self, junction: dict):  # pragma: no cover - abstract
        raise NotImplementedError

    def close(self):
        self._closed = True


def serial_line(junction: dict) -> str:
    """'S N=G E=R S=G W=R M=PRIORITY' – parsed by hardware/arduino_traffic_light."""
    parts = " ".join(f"{a}={s}" for a, s in junction["approaches"].items())
    return f"S {parts} M={junction.get('mode', 'NORMAL')}\n"


class SerialDriver(_AsyncDriver):
    kind = "serial"

    def __init__(self, cfg: dict):
        import serial  # pyserial

        self._ser = serial.serial_for_url(cfg["port"], baudrate=int(cfg.get("baud", 115200)), timeout=0.5)
        super().__init__(cfg)

    def send(self, junction: dict):
        line = serial_line(junction)
        self._ser.write(line.encode("ascii"))
        self._ser.flush()
        self.last_payload = line.strip()

    def close(self):
        super().close()
        try:
            self._ser.close()
        except Exception:
            pass


class HttpDriver(_AsyncDriver):
    kind = "http"

    def send(self, junction: dict):
        payload = self._payload(junction)
        req = urllib.request.Request(self.cfg["url"], data=payload.encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=float(self.cfg.get("timeout", 2.0))) as r:
            r.read()
        self.last_payload = payload

    def _payload(self, j: dict) -> str:
        if self.cfg.get("format") == "spat":
            return json.dumps(build_spat(j["id"], j["name"], j["approaches"], j["groups"], j.get("remaining", {})))
        return json.dumps({"junction": j["id"], "approaches": j["approaches"], "mode": j.get("mode"),
                           "t": time.time()})


class MqttDriver(_AsyncDriver):
    kind = "mqtt"

    def __init__(self, cfg: dict):
        import paho.mqtt.client as mqtt

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if cfg.get("username"):
            self._client.username_pw_set(cfg["username"], cfg.get("password"))
        self._client.connect_async(cfg.get("host", "localhost"), int(cfg.get("port", 1883)), keepalive=30)
        self._client.loop_start()
        super().__init__(cfg)

    def send(self, junction: dict):
        if self.cfg.get("format") == "spat":
            payload = json.dumps(build_spat(junction["id"], junction["name"], junction["approaches"],
                                            junction["groups"], junction.get("remaining", {})))
        else:
            payload = json.dumps({"junction": junction["id"], "approaches": junction["approaches"],
                                  "mode": junction.get("mode"), "t": time.time()})
        info = self._client.publish(self.cfg.get("topic", f"resqroute/signals/{junction['id']}"), payload, qos=1,
                                    retain=True)
        if info.rc != 0:
            raise RuntimeError(f"publish failed (rc={info.rc})")
        self.last_payload = payload

    def close(self):
        super().close()
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass


DRIVERS = {"virtual": SignalDriver, "serial": SerialDriver, "http": HttpDriver, "mqtt": MqttDriver}


def make_driver(cfg: Optional[dict]) -> SignalDriver:
    cfg = cfg or {"type": "virtual"}
    kind = cfg.get("type", "virtual")
    if kind not in DRIVERS:
        raise ValueError(f"unknown signal driver '{kind}' ({', '.join(DRIVERS)})")
    return DRIVERS[kind](cfg)
