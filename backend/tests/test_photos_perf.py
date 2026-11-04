"""Photo analysis, browser-camera reconnects, background ambulance model and the DB writer."""
import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from computer_vision.video.ambulance import AmbulanceConfig, AmbulanceVerifier
from computer_vision.video.detectors import BaseDetector, Det
from computer_vision.video.photo import analyze_photo
from computer_vision.video.tracker import Tracker

from app.config import REPO_ROOT

SAMPLE = REPO_ROOT / "computer_vision" / "models" / "selftest.jpg"


class _Fake(BaseDetector):
    name = "fake"
    keep = None

    def __init__(self, dets, delay=0.0):
        super().__init__(0.1)
        self.dets, self.delay, self.calls = dets, delay, 0

    def detect(self, frame, imgsz=None):
        self.calls += 1
        time.sleep(self.delay)
        return list(self.dets)


def _van(lights=True):
    img = np.full((480, 640, 3), 90, np.uint8)
    cv2.rectangle(img, (200, 200), (420, 400), (240, 240, 240), -1)
    if lights:
        cv2.rectangle(img, (230, 202), (290, 222), (0, 0, 255), -1)
        cv2.rectangle(img, (330, 202), (390, 222), (255, 40, 0), -1)
    cv2.rectangle(img, (230, 300), (390, 320), (0, 0, 230), -1)  # red stripe
    return img


def test_photo_analysis_counts_and_ambulance_light_bar():
    det = _Fake([Det((200, 200, 420, 400), "truck", 0.8), Det((20, 20, 60, 120), "person", 0.7),
                 Det((500, 50, 540, 90), "cup", 0.5)])
    out, r = analyze_photo(_van(True), det, conf=0.3, detect="all")
    assert out.shape == (480, 640, 3)
    assert r["vehicles"] == 1 and r["people"] == 1 and r["other_objects"] == 1
    assert r["ambulances"] == 1 and r["vehicles_by_class"] == {"ambulance": 1}
    assert any("light bar" in x for x in r["ambulance_details"][0]["reasons"])
    _, r2 = analyze_photo(_van(False), det, conf=0.3, detect="vehicles")
    assert r2["ambulances"] == 0 and r2["vehicles_by_class"] == {"truck": 1}
    assert r2["congestion"] == "free"


def test_background_ambulance_model_does_not_block():
    slow = _Fake([Det((200, 200, 420, 400), "ambulance", 0.9)], delay=0.4)
    v, tr = AmbulanceVerifier(AmbulanceConfig(), slow), Tracker()
    img = _van(False)
    t0 = time.perf_counter()
    for k in range(6):
        tracks = tr.update([Det((200, 200, 420, 400), "truck", 0.8)], k * 0.1)
        v.update(img, tracks, k * 0.1)
    assert time.perf_counter() - t0 < 0.3, "frames are not held up by the 0.4 s model"
    time.sleep(0.6)
    for k in range(6, 12):
        tracks = tr.update([Det((200, 200, 420, 400), "truck", 0.8)], k * 0.1)
        v.update(img, tracks, k * 0.1)
    assert tracks[0].amb_model > 0.5, "model evidence arrives asynchronously"


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_photo_api(client):
    with open(SAMPLE, "rb") as f:
        r = client.post("/api/video/photos", files=[("files", ("street.jpg", f.read(), "image/jpeg")),
                                                    ("files", ("notes.txt", b"hello", "image/jpeg"))],
                        data={"detector": "opencv-dnn", "detect": "all", "conf": "0.3"})
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["photos"] == 2 and b["summary"]["vehicles"] >= 1 and b["summary"]["people"] >= 3
    assert b["items"][1]["error"]
    assert client.get(f"/api/video/photos/{b['id']}/files/{b['items'][0]['annotated']}").headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/video/photos/{b['id']}/files/results.csv").status_code == 200
    assert client.get(f"/api/video/photos/{b['id']}/files/../../secret").status_code == 404
    assert any(x["id"] == b["id"] for x in client.get("/api/video/photos").json())
    assert client.post("/api/video/photos", files=[("files", ("a.exe", b"x", "application/octet-stream"))]).status_code == 422
    assert client.delete(f"/api/video/photos/{b['id']}").status_code == 200


def test_browser_camera_survives_reconnect_and_restart(client):
    live = client.app.state.live
    frame = cv2.imencode(".jpg", np.full((240, 320, 3), 90, np.uint8))[1].tobytes()
    # the source does not exist (e.g. the backend restarted): it is re-created from the query parameters
    with client.websocket_connect("/ws/video/cam77/push?name=Laptop&detect=all&conf=0.3&detector=motion") as ws:
        ws.send_bytes(frame)
        assert "tracks" in ws.receive_json()
        ws.send_bytes(b"not a jpeg")
        assert "error" in ws.receive_json()
        ws.send_text("ping")
        assert ws.receive_json() == {"pong": True}
        ws.send_bytes(frame)
        assert "tracks" in ws.receive_json(), "still connected after a bad frame"
    src = live.video.get("cam77")
    assert src is not None and src.cfg.name == "Laptop" and src.cfg.detect == "all"
    with client.websocket_connect("/ws/video/cam77/push") as ws:  # reconnect to the same source
        ws.send_bytes(frame)
        assert "tracks" in ws.receive_json()
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/video/../../x/push") as ws:
            ws.receive_json()
    client.delete("/api/video/sources/cam77")


def test_events_are_persisted_off_the_event_loop(client):
    live = client.app.state.live
    live.coord.emit({"type": "PRIORITY_DEFERRED", "node": "n22", "reason": "test"})
    assert live.db.pending >= 0
    r = client.get("/api/intersections/n22/events")  # the read waits for queued writes
    assert any(e["type"] == "PRIORITY_DEFERRED" for e in r.json())


def test_ensemble_fusion_and_text_cue():
    from computer_vision.video.ensemble import remove_wrapping_boxes, text_evidence, weighted_box_fusion, tiles

    a = [Det((10, 10, 110, 90), "car", 0.8), Det((300, 50, 380, 120), "ambulance", 0.6)]
    b = [Det((12, 8, 112, 92), "truck", 0.5), Det((302, 52, 378, 118), "car", 0.9)]
    fused = weighted_box_fusion([(a, 1.0), (b, 0.8)], n_sources=2, prefer={"ambulance": 0.4})
    assert len(fused) == 2, "one box per vehicle"
    by = sorted(fused, key=lambda d: d.box[0])
    assert by[0].cls == "car" and by[1].cls == "ambulance", "class vote; a confident ambulance wins"
    # one box over two vehicles is removed
    wrap = [Det((0, 0, 200, 100), "car", 0.5), Det((0, 0, 95, 100), "car", 0.8), Det((105, 0, 200, 100), "car", 0.8)]
    assert len(remove_wrapping_boxes(wrap)) == 2
    assert text_evidence("AMBULANCE")[0] > 0.9 and text_evidence("ECNALUBMA")[0] > 0.9  # mirrored on the bonnet
    assert text_evidence("AMBULANC E")[0] > 0.8 and text_evidence("108")[0] >= 0.6
    assert text_evidence("MH.47.C.8729")[0] == 0 and text_evidence("alamy")[0] == 0
    assert len(tiles(1280, 720, 2)) == 4


def test_ensemble_is_scale_aware():
    """The high-res pass only adds small (distant) vehicles; a close-up vehicle from the native pass is kept and
    its fragments from the high-res pass do not replace it. Photos are never upsampled beyond their own size."""
    from computer_vision.video.detectors import BaseDetector
    from computer_vision.video.ensemble import EnsembleDetector

    class Fake(BaseDetector):
        def __init__(self):
            super().__init__(0.1)
            self.sizes = []

        def detect(self, frame, imgsz=None):
            self.sizes.append(imgsz)
            if imgsz == 640:  # native pass: the close bus
                return [Det((20, 20, 780, 760), "bus", 0.5)]
            if frame.shape[0] < 800:  # tiles
                return []
            # high-res pass: two big fragments of the bus + one small distant car
            return [Det((30, 30, 400, 700), "car", 0.6), Det((400, 30, 770, 700), "truck", 0.6),
                    Det((600, 100, 660, 140), "car", 0.7)]

    f = Fake()
    ens = EnsembleDetector([(f, 1.0, 640, "all"), (f, 1.0, 1280, "small")], tile_grid=2, conf=0.15)
    out = ens.detect(np.zeros((800, 800, 3), np.uint8))
    assert 800 in f.sizes and 1280 not in f.sizes, "not upsampled beyond the photo"
    classes = sorted(d.cls for d in out)
    assert classes == ["bus", "car"], classes
    assert max(d.conf for d in out if d.cls == "bus") >= 0.45, "a box only the native pass can see is not penalised"


def test_ocr_reads_ambulance_on_photo():
    pytest.importorskip("rapidocr_onnxruntime")
    from computer_vision.video.ensemble import AmbulanceText

    img = np.full((360, 640, 3), 235, np.uint8)
    cv2.putText(img, "AMBULANCE", (120, 200), cv2.FONT_HERSHEY_DUPLEX, 2.0, (30, 30, 220), 4)
    mirrored = np.ascontiguousarray(img[:, ::-1])
    t = AmbulanceText()
    for im in (img, mirrored):
        (score, word), = t.score_vehicles(im, [(80, 80, 600, 330)])
        assert score > 0.8 and word == "AMBULANCE"


def test_ambulance_fusion_combines_cues():
    from computer_vision.video.ambulance_fusion import ambulance_probability, threshold

    thr = threshold()  # the fitted decision threshold (probabilities come from a class-balanced fit)
    p_car, _ = ambulance_probability(0.03, 0.0, 0.0, 0.1)
    p_cls, r_cls = ambulance_probability(0.97, 0.0, 0.0, 0.2)
    p_txt, r_txt = ambulance_probability(0.2, 0.95, 0.0, 0.0)
    p_bar, _ = ambulance_probability(0.02, 0.0, 0.8, 0.5)  # light bar on a vehicle the classifier calls a car
    assert p_car < thr and p_bar < thr, "colour cues alone do not make an ambulance"
    assert p_car < p_bar < p_cls
    assert p_cls >= thr and any("classifier" in r for r in r_cls)
    assert p_txt >= thr and any("AMBULANCE" in r for r in r_txt), "'AMBULANCE' text outweighs a doubtful classifier"
    p_old, _ = ambulance_probability(None, 0.0, 0.9, 0.3)  # no classifier installed: previous behaviour
    assert p_old >= 0.7


def test_vehicle_classifier_on_sample_photo():
    from computer_vision.video.vehicle_classifier import VehicleClassifier, classifier_path

    if classifier_path() is None:
        pytest.skip("classifier weights not present")
    img = cv2.imread(str(SAMPLE))
    clf = VehicleClassifier()
    (bus,) = clf.classify_boxes(img, [(0, 230, 810, 740)])  # the bus in the sample photo
    assert max(bus, key=bus.get) == "bus" and bus["ambulance"] < 0.2
    onnx = classifier_path().with_suffix(".onnx")
    if onnx.exists():  # OpenCV DNN path (no PyTorch) gives the same answer
        (b2,) = VehicleClassifier(onnx).classify_boxes(img, [(0, 230, 810, 740)])
        assert max(b2, key=b2.get) == "bus"
