"""Video analytics: tracker, analyzer, motion detector pipeline, YOLO (if installed) and the API."""
import importlib.util
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from computer_vision.video.analytics import TrafficAnalyzer, ZoneConfig
from computer_vision.video.detectors import Det, MODELS_DIR
from computer_vision.video.manager import VideoManager
from computer_vision.video.tracker import Tracker

SAMPLE = Path(__file__).resolve().parents[2] / "data" / "videos" / "samples" / "parking-lot-cars-bikes.mp4"


def synthetic_video(path: Path, seconds: float = 6, fps: int = 15) -> Path:
    """Grey road, one block driving down through the frame, one block parked (stopped)."""
    w, h = 640, 360
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (w, h))
    rng = np.random.default_rng(0)
    for i in range(int(seconds * fps)):
        img = np.full((h, w, 3), 90, np.uint8) + rng.integers(0, 6, (h, w, 3), dtype=np.uint8)
        y = int(-60 + i * (h + 120) / (seconds * fps))
        cv2.rectangle(img, (200, y), (260, y + 60), (30, 30, 200), -1)
        if i > fps:  # parked car appears after 1 s and never moves
            cv2.rectangle(img, (450, 250), (520, 300), (200, 200, 40), -1)
        vw.write(img)
    vw.release()
    return path


def test_tracker_and_line_crossing():
    tr, an = Tracker(), TrafficAnalyzer(ZoneConfig(line=[[0, 0.5], [1, 0.5]]))
    shape = (400, 600, 3)
    stats = None
    for k in range(20):
        t = k * 0.25
        y = 50 + k * 20  # moving down across y = 200
        dets = [Det((100, y, 160, y + 40), "car", 0.9), Det((400, 300, 460, 340), "bus", 0.9)]
        stats = an.update(tr.update(dets, t), t, shape)
    assert tr.total_created == 2, "each vehicle keeps one id"
    assert an.crossings_total == 1 and an.crossings_by_class["car"] == 1
    assert stats.queue == 1, "the bus that never moves is queued"
    assert stats.by_class.get("bus") == 1


def test_motion_pipeline_on_video(tmp_path):
    vid = synthetic_video(tmp_path / "synthetic.avi")
    m = VideoManager(tmp_path / "out", tmp_path / "up")
    src = m.create(kind="file", uri=str(vid), mode="batch", detector="motion", target_fps=15,
                   zone={"line": [[0, 0.5], [1, 0.5]]})
    for _ in range(200):
        if src.status in ("finished", "error"):
            break
        time.sleep(0.1)
    assert src.status == "finished", src.error
    assert src.analyzer.crossings_total >= 1
    assert (src.out_dir / src.outputs["video"]).exists() and (src.out_dir / "summary.json").exists()


@pytest.mark.skipif(importlib.util.find_spec("ultralytics") is None or not (MODELS_DIR / "yolo11n.pt").exists()
                    or not SAMPLE.exists(), reason="YOLO or sample video not available")
def test_yolo_detects_car_in_sample():
    from computer_vision.video.detectors import UltralyticsYolo

    cap = cv2.VideoCapture(str(SAMPLE))
    cap.set(cv2.CAP_PROP_POS_FRAMES, 575)
    ok, frame = cap.read()
    dets = UltralyticsYolo(conf=0.3).detect(frame)
    assert any(d.cls == "car" for d in dets)


@pytest.mark.skipif(not (MODELS_DIR / "yolo11n.onnx").exists() or not SAMPLE.exists(), reason="ONNX model not exported")
def test_opencv_dnn_yolo_detects_car():
    from computer_vision.video.detectors import OpenCvDnnYolo

    cap = cv2.VideoCapture(str(SAMPLE))
    cap.set(cv2.CAP_PROP_POS_FRAMES, 575)
    ok, frame = cap.read()
    assert any(d.cls == "car" for d in OpenCvDnnYolo(conf=0.3).detect(frame))


def test_video_api_and_controller_feed(tmp_path):
    from app.main import app

    with TestClient(app) as client:
        caps = client.get("/api/video/capabilities").json()
        assert caps["detectors"]["motion"] is True
        # upload a video for batch processing with the motion detector
        vid = synthetic_video(tmp_path / "clip.avi")
        with open(vid, "rb") as f:
            r = client.post("/api/video/upload", files={"file": ("clip.avi", f, "video/x-msvideo")},
                            data={"mode": "batch", "detector": "motion", "target_fps": "15"})
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        for _ in range(200):
            s = client.get(f"/api/video/sources/{sid}").json()
            if s["status"] in ("finished", "error"):
                break
            time.sleep(0.1)
        assert s["status"] == "finished", s["error"]
        assert client.get(f"/api/video/sources/{sid}/files/summary.json").status_code == 200
        assert client.post("/api/video/upload", files={"file": ("x.txt", b"hello", "text/plain")}).status_code == 422
        # browser-camera push source attached to an approach feeds the signal controller
        r = client.post("/api/video/sources", json={"kind": "push", "detector": "motion",
                                                    "attach": {"node": "n22", "approach": "n21_n22"}})
        assert r.status_code == 201
        pid = r.json()["id"]
        frame = np.full((240, 320, 3), 90, np.uint8)
        with client.websocket_connect(f"/ws/video/{pid}/push") as ws:
            for _ in range(3):
                ws.send_bytes(cv2.imencode(".jpg", frame)[1].tobytes())
                res = ws.receive_json()
        assert "stats" in res and "tracks" in res
        live = client.app.state.live
        assert live.coord.camera_queue("n21_n22") == 0.0
        snap = client.get("/api/simulation/snapshot?vehicles=false").json()
        assert "n21_n22" in snap["signals"]["n22"]["camera_approaches"]
        assert client.post("/api/video/sources", json={"kind": "push", "attach": {"node": "n22", "approach": "n00_n01"}}).status_code == 422
        assert client.post("/api/video/sources", json={"kind": "url", "uri": "file:///etc/passwd"}).status_code == 422
        assert client.post("/api/video/sources", json={"kind": "file", "uri": "../../secret.mp4"}).status_code == 422
        assert client.delete(f"/api/video/sources/{pid}").status_code == 200
        assert live.coord.camera_queue("n21_n22") is None
