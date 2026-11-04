"""Registry of running video sources with shared detector models."""
from __future__ import annotations

import itertools
import shutil
import threading
from pathlib import Path
from typing import Callable, Optional

from .analytics import FrameStats
from .ambulance import ambulance_model_path
from .detectors import BaseDetector, MotionDetector, UltralyticsYolo, available_detectors, create_detector, ultralytics_available
from .source import SourceConfig, VideoSource


class VideoManager:
    def __init__(self, results_dir: Path, uploads_dir: Path,
                 on_queue: Optional[Callable[[str, float], None]] = None,
                 on_event: Optional[Callable[[dict], None]] = None):
        self.results_dir = Path(results_dir)
        self.uploads_dir = Path(uploads_dir)
        self.on_queue = on_queue  # (approach edge id, queued vehicles) – feeds the signal controller
        self.on_event = on_event
        self.on_field_update: Optional[Callable[[VideoSource, FrameStats], None]] = None
        self.sources: dict[str, VideoSource] = {}
        self._models: dict[tuple, BaseDetector] = {}
        self._models_lock = threading.Lock()
        self._ids = itertools.count(1)
        self.photos: dict[str, dict] = {}  # photo batches (newest last)
        self._photo_ids = itertools.count(1)
        self._load_photo_batches()

    def capabilities(self) -> dict:
        return {"detectors": available_detectors(), "kinds": ["file", "url", "webcam", "push"]}

    def _detector_for(self, cfg: SourceConfig) -> BaseDetector:
        if cfg.detector == "motion":
            return MotionDetector(cfg.conf)  # stateful background model: one per camera
        key = (cfg.detector, cfg.weights)
        with self._models_lock:
            if key not in self._models:
                det = create_detector(cfg.detector, cfg.weights, conf=0.1, keep_classes=None)
                if isinstance(det, MotionDetector):  # "auto" fell back: do not share it
                    return det
                det.keep = None  # each source filters classes / confidence itself
                self._models[key] = det
            return self._models[key]

    def _ambulance_model(self) -> Optional[BaseDetector]:
        """Shared open-vocabulary verifier (YOLOE with an 'ambulance' class), if installed."""
        path = ambulance_model_path()
        if path is None or not ultralytics_available():
            return None
        key = ("ambulance-verifier", str(path))
        with self._models_lock:
            if key not in self._models:
                det = UltralyticsYolo(str(path), conf=0.1, keep_classes=None)
                det.keep = None
                self._models[key] = det
            return self._models[key]

    def _on_update(self, src: VideoSource, stats: FrameStats):
        if src.cfg.attach and self.on_queue and src.cfg.attach.get("approach") and not src.cfg.attach.get("field"):
            self.on_queue(src.cfg.attach["approach"], float(stats.queue))
        if src.cfg.attach and src.cfg.attach.get("field") and self.on_field_update:
            self.on_field_update(src, stats)

    def _on_event(self, ev: dict):
        src = self.sources.get(ev.get("source", ""))
        if src is not None and src.cfg.attach:
            ev["attach"] = dict(src.cfg.attach)
        if self.on_event:
            self.on_event(ev)

    def create(self, sid: Optional[str] = None, **kw) -> VideoSource:
        if sid is None or sid in self.sources:
            sid = f"cam{next(self._ids)}"
            while sid in self.sources:
                sid = f"cam{next(self._ids)}"
        cfg = SourceConfig(id=sid, name=kw.pop("name", None) or sid, **kw)
        if cfg.kind not in ("file", "url", "webcam", "push"):
            raise ValueError("kind must be file | url | webcam | push")
        if cfg.kind == "file" and not Path(cfg.uri).exists():
            raise ValueError(f"video file not found: {cfg.uri}")
        if cfg.mode not in ("live", "batch"):
            raise ValueError("mode must be live | batch")
        src = VideoSource(cfg, self._detector_for, self.results_dir / sid, self._on_update, self._on_event,
                          self._ambulance_model)
        self.sources[sid] = src
        src.start()
        return src

    def get(self, sid: str) -> Optional[VideoSource]:
        return self.sources.get(sid)

    def delete(self, sid: str, remove_outputs: bool = False):
        src = self.sources.pop(sid, None)
        if src:
            src.stop()
            if remove_outputs:
                shutil.rmtree(src.out_dir, ignore_errors=True)

    def stop_all(self):
        for s in self.sources.values():
            s.stop()

    def attached(self) -> dict[str, str]:
        """approach edge -> source id for every camera feeding the controller."""
        return {s.cfg.attach["approach"]: sid for sid, s in self.sources.items()
                if s.cfg.attach and s.cfg.attach.get("approach") and s.status in ("running", "waiting for frames")}

    # ------------------------------------------------------------------ photos
    def _load_photo_batches(self):
        import json

        root = self.results_dir / "photos"
        if not root.exists():
            return
        for f in sorted(root.glob("*/results.json"), key=lambda p: p.stat().st_mtime):
            try:
                b = json.loads(f.read_text(encoding="utf-8"))
                self.photos[b["id"]] = b
            except Exception:
                continue
        nums = [int(k[1:]) for k in self.photos if k[1:].isdigit()]
        self._photo_ids = itertools.count(max(nums, default=0) + 1)

    def photo_detector(self, detector: str, quality: str):
        """'accurate': trained traffic model + larger COCO model, whole photo + tiles, fused (crowded photos).
        'fast': one model, one pass."""
        from .detectors import LARGE_WEIGHTS, MODELS_DIR, TRAFFIC_WEIGHTS, UltralyticsYolo, ultralytics_available
        from .ensemble import EnsembleDetector
        from .source import SourceConfig

        big = MODELS_DIR / LARGE_WEIGHTS
        trained = MODELS_DIR / TRAFFIC_WEIGHTS
        if quality == "accurate" and detector in ("auto", "resq", "yolo") and ultralytics_available() \
                and (big.exists() or trained.exists()):
            key = ("photo-ensemble",)
            with self._models_lock:
                if key not in self._models:
                    members = []
                    if trained.exists():
                        members.append((UltralyticsYolo(str(trained), conf=0.1), 1.0, 960, "all"))
                    if big.exists():
                        # YOLO11m, scale-aware: 640 px for close and mid-size vehicles, 1280 px (never above the
                        # photo's own size) + tiles for small, distant vehicles in crowded scenes
                        m = UltralyticsYolo(str(big), conf=0.1)
                        wt = 0.8 if members else 1.0
                        members += [(m, wt, 640, "all"), (m, wt, 1280, "small")]
                    for mem in members:
                        mem[0].keep = None
                    self._models[key] = EnsembleDetector(members, tile_grid=2, conf=0.15)
                return self._models[key]
        return self._detector_for(SourceConfig(id="photo", name="photo", kind="file", detector=detector))

    def analyze_photos(self, files: list[tuple[str, bytes]], detector: str = "auto", conf: float = 0.3,
                       detect: str = "all", ambulance: bool = True, imgsz: int = 960, name: Optional[str] = None,
                       quality: str = "accurate") -> dict:
        """Run detection + analysis on photos; writes annotated JPEGs, results.json and results.csv."""
        import csv
        import json
        import re
        import time

        import cv2
        import numpy as np

        from .photo import analyze_photo
        from .source import SourceConfig

        bid = f"P{next(self._photo_ids)}"
        out = self.results_dir / "photos" / bid
        out.mkdir(parents=True, exist_ok=True)
        det = self.photo_detector(detector, quality)
        w = str(getattr(det, "weights", ""))
        from .vehicle_classifier import classifier_path

        # the open-vocabulary model is only a fallback when the trained classifier is missing
        amb = self._ambulance_model() if ambulance and det.name != "ensemble" and "yoloe" not in w \
            and "resq-traffic" not in w and classifier_path() is None else None
        text = classifier = None
        if ambulance:
            from .ensemble import shared_text_reader

            text = shared_text_reader()
        if det.name != "motion":
            from .vehicle_classifier import shared_classifier

            classifier = shared_classifier()  # vehicle types (auto-rickshaw …) and ambulance probability
        items, t0 = [], time.time()
        for i, (fname, data) in enumerate(files):
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(fname or f"photo{i + 1}.jpg").stem)[:60] or f"photo{i + 1}"
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                items.append({"name": fname, "error": "not an image (or unsupported format)"})
                continue
            annotated, res = analyze_photo(img, det, conf, detect, amb, ambulance, imgsz, text_reader=text,
                                           classifier=classifier)
            ann = f"{i + 1:03d}_{safe}_annotated.jpg"
            cv2.imwrite(str(out / ann), annotated, [cv2.IMWRITE_JPEG_QUALITY, 88])
            orig = f"{i + 1:03d}_{safe}.jpg"
            cv2.imwrite(str(out / orig), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
            items.append({"name": fname, "annotated": ann, "original": orig, **res})
        ok = [x for x in items if "error" not in x]
        by_class: dict[str, int] = {}
        for x in ok:
            for k, v in x["vehicles_by_class"].items():
                by_class[k] = by_class.get(k, 0) + v
        batch = {
            "id": bid, "name": name or (items[0]["name"] if len(items) == 1 else f"{len(items)} photos"),
            "created": time.time(), "detector": det.name, "weights": Path(str(getattr(det, "weights", "") or "")).name or None,
            "settings": {"conf": conf, "detect": detect, "ambulance": ambulance, "imgsz": imgsz, "quality": quality,
                         "text_check": text is not None, "classifier": classifier is not None},
            "photos": len(items), "processing_s": round(time.time() - t0, 2),
            "summary": {
                "vehicles": sum(x["vehicles"] for x in ok), "people": sum(x["people"] for x in ok),
                "vehicles_by_class": by_class, "ambulances": sum(x["ambulances"] for x in ok),
                "photos_with_ambulance": sum(1 for x in ok if x["ambulances"]),
                "mean_occupancy": round(sum(x["occupancy"] for x in ok) / len(ok), 3) if ok else 0,
                "congestion": {lvl: sum(1 for x in ok if x["congestion"] == lvl) for lvl in ("free", "moderate", "heavy", "jam")},
                "mean_ms": round(sum(x["ms"] for x in ok) / len(ok)) if ok else 0,
            },
            "items": items,
        }
        with open(out / "results.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["photo", "vehicles", "people", "other_objects", "occupancy", "congestion", "ambulances",
                        "vehicles_by_class", "ms"])
            for x in ok:
                w.writerow([x["name"], x["vehicles"], x["people"], x["other_objects"], x["occupancy"], x["congestion"],
                            x["ambulances"], json.dumps(x["vehicles_by_class"]), x["ms"]])
        batch["files"] = {"csv": "results.csv", "json": "results.json"}
        (out / "results.json").write_text(json.dumps(batch, indent=1), encoding="utf-8")
        self.photos[bid] = batch
        while len(self.photos) > 50:  # keep the newest 50 batches in the list
            self.photos.pop(next(iter(self.photos)))
        return batch

    def delete_photos(self, bid: str):
        import shutil

        self.photos.pop(bid, None)
        shutil.rmtree(self.results_dir / "photos" / bid, ignore_errors=True)

