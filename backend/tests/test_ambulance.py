"""Ambulance recognition (beacon cue), corridor events from video, and camera -> field signal priority."""
import time
from pathlib import Path

import cv2
import numpy as np

from app.field.manager import FieldManager
from computer_vision.video.ambulance import AmbulanceConfig, AmbulanceVerifier
from computer_vision.video.analytics import TrafficAnalyzer, ZoneConfig
from computer_vision.video.detectors import BaseDetector, Det
from computer_vision.video.manager import VideoManager
from computer_vision.video.tracker import Tracker


def scene(k: int, amb_y: int):
    """Frame k: a white van with an alternating red/blue light bar driving down; a red car parked."""
    img = np.full((360, 640, 3), 100, np.uint8)
    y = amb_y
    cv2.rectangle(img, (100, y), (220, y + 110), (240, 240, 240), -1)
    on = (k // 2) % 2 == 0
    cv2.rectangle(img, (120, y + 2), (155, y + 14), (0, 0, 255) if on else (60, 60, 60), -1)
    cv2.rectangle(img, (165, y + 2), (200, y + 14), (255, 40, 0) if not on else (60, 60, 60), -1)
    cv2.rectangle(img, (380, 160), (540, 270), (20, 20, 210), -1)
    return img, [Det((100, y, 220, y + 110), "truck", 0.8), Det((380, 160, 540, 270), "car", 0.9)]


def test_beacon_cue_distinguishes_ambulance_from_red_car():
    v, tr = AmbulanceVerifier(AmbulanceConfig(use_model=False)), Tracker()
    for k in range(30):
        img, dets = scene(k, 120)
        v.update(img, tr.update(dets, k * 0.125), k * 0.125, dets)
    by = {t.base_cls: t for t in tr.tracks.values()}
    assert by["truck"].is_ambulance and by["truck"].cls == "ambulance"
    assert any("beacon" in r for r in by["truck"].amb_reasons)
    assert not by["car"].is_ambulance


def test_corridor_events_in_zone_then_passed():
    v, tr = AmbulanceVerifier(AmbulanceConfig(use_model=False)), Tracker()
    an = TrafficAnalyzer(ZoneConfig(roi=[[0, 0], [1, 0], [1, 1], [0, 1]], line=[[0, 0.8], [1, 0.8]]))
    types = []
    for k in range(70):
        t = k * 0.125
        img, dets = scene(k, 10 + k * 4)
        tracks = tr.update(dets, t)
        v.update(img, tracks, t, dets)
        an.update(tracks, t, img.shape)
        types += [e["type"] for e in an.events]
        an.events.clear()
    assert "EMERGENCY_VEHICLE" in types
    assert types.index("AMBULANCE_IN_ZONE") < types.index("AMBULANCE_PASSED")


class StubClassifier:
    """Plays the trained vehicle classifier: confident about the van, says 'car' for the car."""

    def classify_boxes(self, img, boxes):
        return [{"ambulance": 0.98, "car": 0.02} if b[2] - b[0] < 140 else {"ambulance": 0.05, "car": 0.95}
                for b in boxes]


def test_video_verifier_uses_the_fitted_fusion_threshold():
    """With the classifier, a car the classifier rejects (P=0.05) must never become an ambulance in video, even
    though its raw fused probability can sit near 0.5 (class-balanced fit, precision-oriented threshold)."""
    v, tr = AmbulanceVerifier(AmbulanceConfig(use_beacon=False, use_livery=False), background=False,
                              classifier=StubClassifier()), Tracker()
    for k in range(40):
        img = np.full((360, 640, 3), 100, np.uint8)
        dets = [Det((100, 120, 220, 230), "truck", 0.8), Det((380, 160, 540, 270), "car", 0.9)]
        v.update(img, tr.update(dets, k * 0.125), k * 0.125, dets)
    by = {t.base_cls: t for t in tr.tracks.values()}
    assert by["truck"].is_ambulance, "classifier-confirmed van becomes an ambulance"
    assert not by["car"].is_ambulance and by["car"].amb_score < 0.25, by["car"].amb_score


class FakeDetector(BaseDetector):
    name = "fake"
    keep = None

    def __init__(self):
        super().__init__(0.1)
        self.k = 0
        self.frames = {}

    def detect(self, frame):
        return self.frames.get(frame[0, 0, 0], [])


def test_camera_ambulance_turns_field_signal_green(tmp_path):
    fm = FieldManager(tmp_path / "j.json")
    fm.load((19.03, 73.024))
    j = fm.junctions["J1"]
    events = []
    vm = VideoManager(tmp_path / "out", tmp_path / "up", on_event=events.append)
    vm.on_field_update = lambda src, stats: fm.camera_stats(src.cfg.attach, stats.queue)
    det = FakeDetector()
    vm._detector_for = lambda cfg: det
    requested = None
    src = vm.create(kind="push", detector="fake", ambulance={"use_model": False},
                    zone={"roi": [[0, 0], [1, 0], [1, 1], [0, 1]], "line": [[0, 0.8], [1, 0.8]]},
                    attach={"field": "J1", "approach": "W"})
    for k in range(70):
        img, dets = scene(k, 10 + k * 4)
        img[0, 0, 0] = k % 256  # frame id for the fake detector
        det.frames[k % 256] = dets
        src.started_at = time.time() - k * 0.125  # deterministic timestamps
        src.push_frame(img)
        while events:
            fm.camera_event(events.pop(0))
        if j.active:
            requested = j.active
            j.tick(0.25)
    assert requested.approach == "W" and requested.trigger == "camera"
    assert fm._lost_timers, "ambulance passed the stop line -> release scheduled"
    fm._lost_timers = {k: (0, r) for k, (_, r) in fm._lost_timers.items()}
    fm.process_timers()
    assert j.active is None and j.history[-1].status == "RELEASED"
    fm.shutdown()
