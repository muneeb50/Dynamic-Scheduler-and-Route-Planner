"""Video sources processed in background threads.

kinds
  file    – a video file. mode "live": played back in real time (frames are skipped when
            processing is slower than the video), optionally looping; mode "batch": every
            frame (or every n-th) is processed as fast as possible and an annotated video,
            a per-second CSV timeline and a JSON summary are written.
  url     – RTSP / HTTP(S) camera stream (IP cameras, the Android "IP Webcam" app,
            MJPEG URLs …). Reconnects automatically.
  webcam  – a camera attached to the machine running the backend (index 0, 1 …).
  push    – frames pushed by a client (the dashboard's browser-camera mode) over a WebSocket.
"""
from __future__ import annotations

import csv
import json
import logging
import threading
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from .ambulance import AmbulanceConfig, AmbulanceVerifier
from .analytics import FrameStats, TrafficAnalyzer, ZoneConfig
from .annotate import annotate
from .capture import open_capture
from .detectors import VEHICLE_CLASSES, BaseDetector, Det
from .tracker import Tracker

log = logging.getLogger("resqroute.video")
STREAM_MAX_WIDTH = 960
# frames wider than this are scaled down before processing: the detector works at 640 px anyway, and
# drawing, JPEG encoding and the ambulance colour checks get 2–4× cheaper on HD/Full-HD cameras
PROCESS_MAX_WIDTH = 1280


@dataclass
class SourceConfig:
    id: str
    name: str
    kind: str  # file | url | webcam | push
    uri: str = ""
    mode: str = "live"  # live | batch (files only)
    loop: bool = True
    detector: str = "auto"
    weights: Optional[str] = None
    conf: float = 0.35
    target_fps: float = 8.0
    classes: Optional[list[str]] = None  # None = road vehicles
    # what to draw: "vehicles" (road vehicles only), "road" (vehicles + people), "all" (every object the
    # model knows – useful to check a webcam works indoors). Traffic numbers always use road users only.
    detect: str = "vehicles"
    imgsz: int = 640  # detector input size: 640 = accurate, 480 / 416 / 320 = faster on a CPU
    zone: dict = field(default_factory=dict)
    # {"node": "n22", "approach": "n21_n22"} -> simulated junction, or {"field": "J1", "approach": "N"} -> real/demo signal
    attach: Optional[dict] = None
    ambulance: dict = field(default_factory=dict)  # AmbulanceConfig overrides

    def public(self) -> dict:
        d = asdict(self)
        if self.kind == "file":
            d["uri"] = Path(self.uri).name
        if self.kind == "url" and "@" in self.uri:  # hide credentials embedded in RTSP URLs
            scheme, rest = self.uri.split("://", 1) if "://" in self.uri else ("", self.uri)
            d["uri"] = f"{scheme}://***@{rest.split('@', 1)[1]}"
        return d


class _LatestFrameReader(threading.Thread):
    """Keeps only the newest frame of a live stream so processing never lags behind."""

    def __init__(self, cap: cv2.VideoCapture):
        super().__init__(daemon=True)
        self.cap = cap
        self.frame = None
        self.ok = True
        self.count = 0
        self.lock = threading.Lock()
        self.stopped = False

    def run(self):
        fails = 0
        while not self.stopped:
            ok, frame = self.cap.read()
            if not ok or frame is None or not frame.size:
                # laptop webcams (Windows MSMF/DirectShow especially) drop single frames now and then:
                # tolerate short glitches instead of tearing the camera down
                fails += 1
                if fails >= 25:
                    self.ok = False
                    break
                time.sleep(0.04)
                continue
            fails = 0
            with self.lock:
                self.frame = frame
                self.count += 1

    def latest(self):
        with self.lock:
            f, self.frame = self.frame, None
            return f


class VideoSource:
    def __init__(self, cfg: SourceConfig, detector_factory: Callable[[SourceConfig], BaseDetector], out_dir: Path,
                 on_update: Optional[Callable[["VideoSource", FrameStats], None]] = None,
                 on_event: Optional[Callable[[dict], None]] = None,
                 ambulance_model_factory: Optional[Callable[[], Optional[BaseDetector]]] = None):
        self.cfg = cfg
        self.ambulance_model_factory = ambulance_model_factory
        self.verifier: Optional[AmbulanceVerifier] = None
        self.on_event = on_event
        self.detector_factory = detector_factory
        self.detector: Optional[BaseDetector] = None
        self.out_dir = out_dir
        self.on_update = on_update
        self.tracker = Tracker()
        self.analyzer = TrafficAnalyzer(ZoneConfig.from_dict(cfg.zone))
        self.status = "created"
        self.error: Optional[str] = None
        self.stats: Optional[FrameStats] = None
        self.timeline: deque = deque(maxlen=3600)
        self.events: deque = deque(maxlen=200)
        self.outputs: dict[str, str] = {}
        self.progress: Optional[float] = None
        self.proc_fps = 0.0
        self.video_fps: Optional[float] = None
        self.frame_size: Optional[tuple[int, int]] = None
        self.frames_processed = 0
        self.frame_id = 0
        self.latest_jpeg: Optional[bytes] = None
        self.latest_raw_jpeg: Optional[bytes] = None
        self.last_tracks: list = []
        self.last_road_ids: set = set()
        self.objects_now: Counter = Counter()
        self.last_raw = 0
        self.class_totals: Counter = Counter()
        self._seen_tracks: set[int] = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_timeline_t = -1e9
        self._proc_times: deque = deque(maxlen=20)
        self.started_at = time.time()
        self.backend: Optional[str] = None
        self.reconnects = 0

    # ---------------------------------------------------------------- control
    def start(self):
        if self.cfg.kind == "push":
            self._ensure_detector()
            self.status = "waiting for frames"
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"video-{self.cfg.id}", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self.status not in ("finished", "error"):
            self.status = "stopped"

    def set_zone(self, zone: dict):
        self.cfg.zone = {**self.cfg.zone, **zone}
        with self._lock:
            self.analyzer.set_zone(ZoneConfig.from_dict(self.cfg.zone))

    def road_classes(self) -> set:
        keep = set(self.cfg.classes) | {"ambulance"} if self.cfg.classes else set(VEHICLE_CLASSES)
        if self.cfg.detect in ("road", "all") and not self.cfg.classes:
            keep |= {"person"} if self.cfg.detect == "road" else set()
        return keep

    def _ensure_detector(self):
        if self.detector is None:
            self.status = "loading model"
            self.detector = self.detector_factory(self.cfg)
        self._imgsz_ok = "imgsz" in self.detector.detect.__code__.co_varnames
        if self.verifier is None:
            acfg = AmbulanceConfig.from_dict(self.cfg.ambulance)
            model = None
            # the open-vocabulary detector already reports "ambulance" itself; otherwise add the verifier model
            w = str(getattr(self.detector, "weights", ""))
            if acfg.enabled and acfg.use_model and self.detector.name != "motion" and self.ambulance_model_factory \
                    and "yoloe" not in w and "resq-traffic" not in w:
                try:
                    model = self.ambulance_model_factory()
                except Exception:
                    log.exception("ambulance model unavailable; using beacon/livery cues only")
            # live sources run the slow verifier model in the background; batch keeps it in step with the frames
            text = None
            if acfg.enabled and acfg.use_model and self.detector.name != "motion" and acfg.use_text:
                from .ensemble import shared_text_reader

                text = shared_text_reader()  # reads "AMBULANCE" / "108" on vehicles, in the background
            classifier = None
            if acfg.enabled and acfg.use_model and self.detector.name != "motion":
                from .vehicle_classifier import shared_classifier

                classifier = shared_classifier()  # trained ambulance / vehicle-type classifier
                if classifier is not None:
                    model = None  # the classifier replaces the slower, less accurate open-vocabulary check
            batch = self.cfg.kind == "file" and self.cfg.mode == "batch"
            self.verifier = AmbulanceVerifier(acfg, model, background=not batch, text_reader=None if batch else text,
                                              classifier=classifier)

    # -------------------------------------------------------------- pipeline
    def process(self, frame: np.ndarray, t: float, encode: bool = True) -> tuple[np.ndarray, FrameStats]:
        t0 = time.perf_counter()
        self._ensure_detector()
        if frame.shape[1] > PROCESS_MAX_WIDTH:
            frame = cv2.resize(frame, (PROCESS_MAX_WIDTH, int(frame.shape[0] * PROCESS_MAX_WIDTH / frame.shape[1])),
                               interpolation=cv2.INTER_AREA)
        all_dets: list[Det] = self.detector.detect(frame, imgsz=self.cfg.imgsz) if self._imgsz_ok else self.detector.detect(frame)
        keep = self.road_classes()
        motion = self.detector.name == "motion"
        if self.cfg.detect == "all" or motion:
            dets = [d for d in all_dets if d.conf >= self.cfg.conf]
        else:
            dets = [d for d in all_dets if d.conf >= self.cfg.conf and d.cls in keep]
        self.last_raw = len(all_dets)
        with self._lock:
            tracks = self.tracker.update(dets, t)
            road = [tr for tr in tracks if motion or tr.base_cls in keep or tr.is_ambulance]
            self.verifier.update(frame, road, t, all_dets)
            for gone in self.tracker.removed:
                self.analyzer.track_lost(gone, t)
            stats = self.analyzer.update(road, t, frame.shape)
            road_ids = {tr.id for tr in road}
        stats.tracks_total = self.tracker.total_created
        for tr in tracks:
            if tr.confirmed and tr.id not in self._seen_tracks and tr.id in road_ids:
                self._seen_tracks.add(tr.id)
                self.class_totals[tr.cls] += 1
        self.objects_now = Counter(tr.cls for tr in tracks if tr.confirmed)
        while self.analyzer.events:
            ev = self.analyzer.events.pop(0)
            ev["source"] = self.cfg.id
            ev["source_name"] = self.cfg.name
            self.events.append(ev)
            if self.on_event:
                try:
                    self.on_event(dict(ev))
                except Exception:
                    log.exception("video on_event failed")
        self._proc_times.append(time.perf_counter() - t0)
        self.proc_fps = len(self._proc_times) / max(1e-6, sum(self._proc_times))
        info = f"{self.detector.name} | {self.proc_fps:.1f} fps"
        img = annotate(frame, tracks, stats, self.analyzer, info, road_ids=road_ids)
        self.stats = stats
        self.last_tracks = tracks
        self.last_road_ids = road_ids
        self.frames_processed += 1
        self.frame_size = (frame.shape[1], frame.shape[0])
        if t - self._last_timeline_t >= 1.0:
            self._last_timeline_t = t
            self.timeline.append({"t": round(t, 1), "vehicles": stats.vehicles, "queue": stats.queue,
                                  "flow_per_min": round(stats.flow_per_min, 1), "occupancy": round(stats.occupancy, 3),
                                  "mean_speed": None if stats.mean_speed is None else round(stats.mean_speed, 1),
                                  "crossings": stats.crossings_total, "congestion": stats.congestion})
        if encode:
            self.latest_jpeg = self._jpeg(img)
            self.latest_raw_jpeg = None
            self.frame_id += 1
        if self.on_update:
            try:
                self.on_update(self, stats)
            except Exception:  # a broken consumer must not stop the camera
                log.exception("video on_update failed")
        return img, stats

    def _jpeg(self, img: np.ndarray, quality: int = 72) -> bytes:
        h, w = img.shape[:2]
        if w > STREAM_MAX_WIDTH:
            img = cv2.resize(img, (STREAM_MAX_WIDTH, int(h * STREAM_MAX_WIDTH / w)))
        return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])[1].tobytes()

    def push_frame(self, frame: np.ndarray) -> dict:
        """Browser-camera mode: process one frame now and return detections for client-side drawing."""
        t = time.time() - self.started_at
        _, stats = self.process(frame, t)
        self.status = "running"
        h, w = frame.shape[:2]
        return {
            "stats": stats.to_dict(),
            "tracks": [{"id": tr.id, "cls": tr.cls, "conf": round(tr.conf, 2), "stopped": tr.stopped,
                        "road": tr.id in self.last_road_ids,
                        "ambulance": tr.is_ambulance, "amb_score": round(tr.amb_score, 2), "reasons": tr.amb_reasons,
                        "box": [round(tr.box[0] / w, 4), round(tr.box[1] / h, 4), round(tr.box[2] / w, 4),
                                round(tr.box[3] / h, 4)]}
                       for tr in self.last_tracks if tr.confirmed],
            "proc_fps": round(self.proc_fps, 1),
        }

    # -------------------------------------------------------------- runners
    def _open(self) -> cv2.VideoCapture:
        if self.cfg.kind == "file":
            cap = cv2.VideoCapture(self.cfg.uri)
            if not cap.isOpened():
                raise RuntimeError(f"cannot open file '{Path(self.cfg.uri).name}'")
            return cap
        source = int(self.cfg.uri or 0) if self.cfg.kind == "webcam" else self.cfg.uri
        cap, backend = open_capture(source)
        self.backend = backend
        return cap

    def _run(self):
        try:
            self._ensure_detector()
            if self.cfg.kind == "file" and self.cfg.mode == "batch":
                self._run_batch()
            elif self.cfg.kind == "file":
                self._run_file_live()
            else:
                self._run_stream()
        except Exception as exc:
            log.exception("video source %s failed", self.cfg.id)
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"

    def _pace(self, t_start: float):
        wait = 1.0 / max(self.cfg.target_fps, 0.5) - (time.perf_counter() - t_start)
        if wait > 0:
            self._stop.wait(wait)

    def _run_file_live(self):
        cap = self._open()
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        fps = fps if 1 <= fps <= 240 else 25.0
        self.video_fps = fps
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        idx, offset = 0, 0.0
        wall0 = time.perf_counter()
        self.status = "running"
        while not self._stop.is_set():
            t_loop = time.perf_counter()
            due = int((t_loop - wall0) * fps)
            while idx < due - 1:  # skip frames we have no time to process (real-time playback)
                if not cap.grab():
                    break
                idx += 1
            ok, frame = cap.read()
            idx += 1
            if not ok:
                if self.cfg.loop and idx > 1:
                    cap.release()
                    cap = self._open()
                    offset += idx / fps + 1.0
                    idx = 0
                    wall0 = time.perf_counter()
                    with self._lock:
                        self.tracker.reset()
                    continue
                self.status = "finished"
                break
            self.progress = idx / n if n else None
            self.process(frame, offset + idx / fps)
            self._pace(t_loop)
        cap.release()

    def _run_stream(self):
        attempts = 0
        while not self._stop.is_set():
            try:
                cap = self._open()
            except RuntimeError as exc:
                # keep trying (camera unplugged, used by another app, laptop resumed from sleep …)
                attempts += 1
                self.status = "reconnecting"
                self.error = f"{exc} – retrying (attempt {attempts})"
                self.reconnects += 1
                self._stop.wait(min(5.0, 0.5 * attempts))
                continue
            attempts = 0
            self.error = None
            reader = _LatestFrameReader(cap)
            reader.start()
            self.status = "running"
            t0 = time.perf_counter()
            last_frame_at = time.perf_counter()
            while not self._stop.is_set():
                t_loop = time.perf_counter()
                frame = reader.latest()
                if frame is None:
                    if not reader.ok or t_loop - last_frame_at > (5.0 if self.cfg.kind == "webcam" else 10.0):
                        break  # stream ended or stalled -> reconnect
                    self._stop.wait(0.01)
                    continue
                last_frame_at = t_loop
                try:
                    self.process(frame, t_loop - t0)
                except Exception as exc:  # one bad frame must not stop a live camera
                    log.exception("frame processing failed")
                    self.error = f"frame skipped: {type(exc).__name__}: {exc}"
                self._pace(t_loop)
            reader.stopped = True
            reader.join(timeout=1.0)
            cap.release()
            if not self._stop.is_set():
                self.status = "reconnecting"
                self.error = "camera stream interrupted – reconnecting"
                self.reconnects += 1
                self._stop.wait(0.5 if self.cfg.kind == "webcam" else 2.0)

    def _open_writer(self, path_base: Path, fps: float, size: tuple[int, int]):
        for fourcc, ext in (("VP80", ".webm"), ("avc1", ".mp4"), ("mp4v", ".mp4")):  # webm plays in browsers
            path = path_base.with_suffix(ext)
            w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*fourcc), fps, size)
            if w.isOpened():
                return w, path
            w.release()
            path.unlink(missing_ok=True)
        raise RuntimeError("no working video encoder in this OpenCV build")

    def _run_batch(self):
        cap = self._open()
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        fps = fps if 1 <= fps <= 240 else 25.0
        self.video_fps = fps
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        stride = max(1, int(round(fps / max(self.cfg.target_fps, 1.0))))
        self.out_dir.mkdir(parents=True, exist_ok=True)
        writer, video_path = None, None
        rows = []
        idx = 0
        self.status = "processing"
        t_start = time.time()
        while not self._stop.is_set():
            ok, frame = cap.read()
            if not ok:
                break
            idx += 1
            if (idx - 1) % stride:
                continue
            img, stats = self.process(frame, idx / fps, encode=(idx // stride) % 3 == 0)
            if writer is None:
                h, w = img.shape[:2]
                writer, video_path = self._open_writer(self.out_dir / "annotated", fps / stride, (w, h))
            writer.write(img)
            rows.append({"frame": idx, **{k: v for k, v in stats.to_dict().items() if k not in ("by_class", "crossings_by_dir")},
                         "by_class": json.dumps(stats.by_class)})
            self.progress = idx / n if n else None
        cap.release()
        if writer:
            writer.release()
        if self._stop.is_set():
            self.status = "stopped"
            return
        csv_path = self.out_dir / "frames.csv"
        if rows:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
        tl_path = self.out_dir / "timeline.csv"
        if self.timeline:
            with open(tl_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(self.timeline[0]))
                w.writeheader()
                w.writerows(self.timeline)
        summary = self.summary()
        summary["processing_time_s"] = round(time.time() - t_start, 1)
        summary["frames_in_video"] = n
        summary["stride"] = stride
        (self.out_dir / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        self.outputs = {"video": video_path.name if video_path else None, "frames_csv": csv_path.name,
                        "timeline_csv": tl_path.name, "summary": "summary.json"}
        self.progress = 1.0
        self.status = "finished"

    # ------------------------------------------------------------------ views
    def summary(self) -> dict:
        tl = list(self.timeline)
        return {
            "source": self.cfg.public(),
            "detector": self.detector.info() if self.detector else None,
            "frames_processed": self.frames_processed,
            "duration_s": tl[-1]["t"] if tl else 0,
            "vehicles_tracked": sum(self.class_totals.values()),
            "vehicles_by_class": dict(self.class_totals),
            "line_crossings": self.analyzer.crossings_total,
            "crossings_by_direction": dict(self.analyzer.crossings_by_dir),
            "crossings_by_class": dict(self.analyzer.crossings_by_class),
            "max_queue": max((r["queue"] for r in tl), default=0),
            "mean_vehicles_in_zone": round(sum(r["vehicles"] for r in tl) / len(tl), 2) if tl else 0,
            "peak_flow_per_min": max((r["flow_per_min"] for r in tl), default=0),
            "congestion_share": {k: round(v / len(tl), 3) for k, v in Counter(r["congestion"] for r in tl).items()} if tl else {},
            "emergency_events": [e for e in self.events if e["type"] == "EMERGENCY_VEHICLE"],
        }

    def to_dict(self) -> dict:
        return {
            **self.cfg.public(),
            "status": self.status,
            "error": self.error,
            "progress": None if self.progress is None else round(self.progress, 3),
            "proc_fps": round(self.proc_fps, 1),
            "video_fps": self.video_fps,
            "frame_size": self.frame_size,
            "frames_processed": self.frames_processed,
            "detector_info": self.detector.info() if self.detector else None,
            "ambulance_model": bool(self.verifier and self.verifier.model) or "yoloe" in str(getattr(self.detector, "weights", "")),
            "backend": self.backend,
            "reconnects": self.reconnects,
            "stats": self.stats.to_dict() if self.stats else None,
            "ambulances_now": [{"track": tr.id, "score": round(tr.amb_score, 2), "reasons": tr.amb_reasons,
                                "in_zone": tr.in_zone, "passed": tr.passed}
                               for tr in self.last_tracks if tr.is_ambulance],
            "vehicles_by_class_total": dict(self.class_totals),
            "objects_now": dict(self.objects_now),
            "detections_raw": self.last_raw,
            "events": list(self.events)[-20:],
            "outputs": self.outputs,
        }
