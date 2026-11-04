"""Real-video traffic analytics: YOLO / OpenCV detection on uploaded videos, live camera streams,
the server's webcam and the browser's camera."""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ..config import REPO_ROOT
from ..services.live import LiveSimulation
from .deps import get_live, require_admin

router = APIRouter(tags=["video"])
log = logging.getLogger("resqroute.video")
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GB
VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg", ".wmv", ".ts"}
SAMPLES_DIR = REPO_ROOT / "data" / "videos" / "samples"
DETECT_MODES = ("vehicles", "road", "all")
SELFTEST_IMAGE = REPO_ROOT / "computer_vision" / "models" / "selftest.jpg"


class SourceCreate(BaseModel):
    kind: str = Field(description="url | webcam | file | push")
    name: Optional[str] = None
    uri: str = ""
    mode: str = "live"
    loop: bool = True
    detector: str = "auto"
    weights: Optional[str] = None
    conf: float = Field(default=0.35, ge=0.05, le=0.95)
    target_fps: float = Field(default=8, ge=0.5, le=30)
    classes: Optional[list[str]] = None
    detect: Optional[str] = Field(default=None, description="vehicles | road | all (default: all for cameras, vehicles for videos)")
    imgsz: Optional[int] = Field(default=None, description="detector input size 320–1280 (default 480 for live cameras, 640 for videos)")
    zone: dict = {}
    attach: Optional[dict] = None
    ambulance: dict = {}


class SourceUpdate(BaseModel):
    zone: Optional[dict] = None
    attach: Optional[dict] = None
    conf: Optional[float] = Field(default=None, ge=0.05, le=0.95)
    classes: Optional[list[str]] = None
    clear_attach: bool = False
    ambulance: Optional[dict] = None
    detect: Optional[str] = None
    imgsz: Optional[int] = Field(default=None, ge=256, le=1280)
    target_fps: Optional[float] = Field(default=None, ge=0.5, le=30)


def _src(live: LiveSimulation, sid: str):
    s = live.video.get(sid)
    if s is None:
        raise HTTPException(404, "video source not found")
    return s


def _check_attach(live: LiveSimulation, attach: Optional[dict]):
    if not attach:
        return
    if attach.get("field"):
        j = live.field.junctions.get(attach["field"])
        if j is None or attach.get("approach") not in j.approach:
            raise HTTPException(422, "attach.approach must be an approach of field junction attach.field")
        return
    node, approach = attach.get("node"), attach.get("approach")
    if node not in live.net.nodes or approach not in live.net.nodes[node].incoming:
        raise HTTPException(422, "attach.approach must be an incoming edge of attach.node")


@router.get("/api/video/capabilities")
def capabilities(live: LiveSimulation = Depends(get_live)):
    caps = live.video.capabilities()
    caps["samples"] = sorted(p.name for p in SAMPLES_DIR.glob("*") if p.suffix.lower() in VIDEO_EXT) \
        if SAMPLES_DIR.exists() else []
    return caps


@router.get("/api/video/selftest")
def selftest(detector: str = "auto", live: LiveSimulation = Depends(get_live)):
    """Run the detector on a bundled test photo (a bus and people in a street) – checks the model works."""
    import time as _time

    import cv2

    from computer_vision.video.source import SourceConfig

    img = cv2.imread(str(SELFTEST_IMAGE)) if SELFTEST_IMAGE.exists() else None
    if img is None:
        raise HTTPException(500, "self-test image missing")
    try:
        det = live.video._detector_for(SourceConfig(id="selftest", name="selftest", kind="file", detector=detector))
    except Exception as exc:
        return {"ok": False, "detector": detector, "error": f"{type(exc).__name__}: {exc}",
                "hint": "Install YOLO: pip install -r computer_vision/requirements-yolo.txt (or re-run the setup script)."}
    if det.name == "motion":
        return {"ok": False, "detector": "motion", "detections": [],
                "error": ("OpenCV motion detection has no object model: it only finds things that move and cannot name them."
                          if detector == "motion" else
                          "No object-detection model is available, so only OpenCV motion detection can run. It needs a "
                          "moving scene and cannot name objects."),
                "hint": "Re-run the setup script (installs YOLO) or keep computer_vision/models/yolo11n.onnx for OpenCV DNN."}
    det.detect(img)  # warm-up (first call loads weights onto the device)
    t0 = _time.perf_counter()
    dets = det.detect(img)
    ms = (_time.perf_counter() - t0) * 1000
    found = sorted(({"cls": d.cls, "conf": round(d.conf, 2)} for d in dets if d.conf >= 0.25), key=lambda d: -d["conf"])
    return {"ok": bool(found), "detector": det.name, "info": det.info(), "ms": round(ms), "detections": found[:12],
            "image": "bus and pedestrians (Ultralytics sample image)"}


IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
MAX_PHOTOS = 100
MAX_PHOTO_BYTES = 40 * 1024 * 1024


@router.post("/api/video/photos", status_code=201, dependencies=[Depends(require_admin)])
async def analyze_photos(files: list[UploadFile] = File(...), detector: str = Form("auto"), conf: float = Form(0.3),
                         detect: str = Form("all"), ambulance: bool = Form(True), imgsz: int = Form(960),
                         name: Optional[str] = Form(None), quality: str = Form("accurate"),
                         live: LiveSimulation = Depends(get_live)):
    """Detection and traffic analysis on one or more photos (annotated images + CSV/JSON)."""
    if not files or len(files) > MAX_PHOTOS:
        raise HTTPException(422, f"send between 1 and {MAX_PHOTOS} photos")
    if detect not in DETECT_MODES:
        raise HTTPException(422, f"detect must be one of {DETECT_MODES}")
    if not 0.05 <= conf <= 0.95:
        raise HTTPException(422, "conf must be between 0.05 and 0.95")
    data = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext and ext not in IMAGE_EXT and not (f.content_type or "").startswith("image/"):
            raise HTTPException(422, f"{f.filename}: not an image (jpg, png, bmp, webp, tif)")
        b = await f.read()
        if len(b) > MAX_PHOTO_BYTES:
            raise HTTPException(413, f"{f.filename}: larger than 40 MB")
        data.append((f.filename or "photo.jpg", b))
    try:
        if quality not in ("accurate", "fast"):
            raise HTTPException(422, "quality must be accurate or fast")
        return await asyncio.to_thread(live.video.analyze_photos, data, detector, conf, detect, ambulance,
                                       int(min(1280, max(320, imgsz)) // 32 * 32), name, quality)
    except (ValueError, FileNotFoundError, ImportError) as exc:
        raise HTTPException(422, str(exc))


@router.post("/api/video/photos/sample", status_code=201, dependencies=[Depends(require_admin)])
async def analyze_sample_photo(live: LiveSimulation = Depends(get_live)):
    """The bundled street photo, to try photo analysis without your own image."""
    return await asyncio.to_thread(live.video.analyze_photos, [("sample-street.jpg", SELFTEST_IMAGE.read_bytes())],
                                   "auto", 0.3, "all", True, 960, "Sample street photo")


@router.get("/api/video/photos")
def list_photo_batches(live: LiveSimulation = Depends(get_live)):
    return [{k: v for k, v in b.items() if k != "items"} | {"thumb": next((i.get("annotated") for i in b["items"] if i.get("annotated")), None)}
            for b in reversed(list(live.video.photos.values()))]


@router.get("/api/video/photos/{bid}")
def get_photo_batch(bid: str, live: LiveSimulation = Depends(get_live)):
    b = live.video.photos.get(bid)
    if b is None:
        raise HTTPException(404, "photo batch not found")
    return b


@router.get("/api/video/photos/{bid}/files/{name}")
def photo_file(bid: str, name: str, live: LiveSimulation = Depends(get_live)):
    b = live.video.photos.get(bid)
    allowed = set((b or {}).get("files", {}).values()) | {i.get(k) for i in (b or {}).get("items", []) for k in ("annotated", "original")}
    if b is None or name not in allowed:
        raise HTTPException(404, "file not found")
    path = live.video.results_dir / "photos" / bid / name
    media = {".jpg": "image/jpeg", ".csv": "text/csv", ".json": "application/json"}
    return FileResponse(path, media_type=media.get(path.suffix, "application/octet-stream"), filename=f"{bid}_{name}",
                        content_disposition_type="inline" if path.suffix == ".jpg" else "attachment")


@router.delete("/api/video/photos/{bid}", dependencies=[Depends(require_admin)])
def delete_photo_batch(bid: str, live: LiveSimulation = Depends(get_live)):
    if bid not in live.video.photos:
        raise HTTPException(404, "photo batch not found")
    live.video.delete_photos(bid)
    return {"deleted": bid}


@router.get("/api/video/webcams")
def webcams(live: LiveSimulation = Depends(get_live)):
    """Cameras attached to the machine running the backend (indices in use by a running source are reported, not probed)."""
    from computer_vision.video.capture import probe_webcams

    busy = {int(s.cfg.uri or 0): sid for sid, s in live.video.sources.items()
            if s.cfg.kind == "webcam" and s.status not in ("stopped", "error", "finished")}
    found = [c for c in probe_webcams() if c["index"] not in busy]
    found += [{"index": i, "in_use_by": sid} for i, sid in busy.items()]
    return sorted(found, key=lambda c: c["index"])


@router.get("/api/video/sources")
def list_sources(live: LiveSimulation = Depends(get_live)):
    return [s.to_dict() for s in live.video.sources.values()]


@router.post("/api/video/sources", status_code=201, dependencies=[Depends(require_admin)])
def create_source(req: SourceCreate, live: LiveSimulation = Depends(get_live)):
    _check_attach(live, req.attach)
    data = req.model_dump()
    if req.kind == "file":  # only bundled samples or previously uploaded files can be opened by name
        name = Path(req.uri).name
        cands = [SAMPLES_DIR / name, live.video.uploads_dir / name]
        path = next((c for c in cands if c.exists()), None)
        if path is None:
            raise HTTPException(422, f"video '{name}' not found in samples or uploads")
        data["uri"] = str(path)
    if req.kind == "url" and not re.match(r"^(rtsp|rtsps|rtmp|http|https|udp|tcp)://", req.uri.strip()):
        raise HTTPException(422, "stream URL must start with rtsp://, http(s)://, rtmp://, udp:// or tcp://")
    if req.kind == "webcam":
        data["uri"] = str(int(req.uri or 0)) if (req.uri or "0").isdigit() else "0"
    if data.get("detect") is None:
        data["detect"] = "all" if req.kind in ("webcam", "push") else "vehicles"
    if data["detect"] not in DETECT_MODES:
        raise HTTPException(422, f"detect must be one of {DETECT_MODES}")
    if data.get("imgsz") is None:
        data["imgsz"] = 480 if req.kind in ("webcam", "push", "url") else 640
    data["imgsz"] = int(min(1280, max(256, data["imgsz"])) // 32 * 32)
    try:
        src = live.video.create(**data)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return src.to_dict()


@router.post("/api/video/upload", status_code=201, dependencies=[Depends(require_admin)])
async def upload_video(file: UploadFile = File(...), name: Optional[str] = Form(None), mode: str = Form("batch"),
                       detector: str = Form("auto"), conf: float = Form(0.35), target_fps: float = Form(8.0),
                       loop: bool = Form(True), live: LiveSimulation = Depends(get_live)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in VIDEO_EXT:
        raise HTTPException(422, f"unsupported video type '{ext}'")
    live.video.uploads_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename).stem)[:60]
    dest = live.video.uploads_dir / f"{uuid.uuid4().hex[:8]}_{safe}{ext}"
    size = 0
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "video larger than 1 GB")
            f.write(chunk)
    cap = cv2.VideoCapture(str(dest))
    ok = cap.isOpened() and cap.read()[0]
    cap.release()
    if not ok:
        dest.unlink(missing_ok=True)
        raise HTTPException(422, "OpenCV could not decode this video – try MP4 (H.264)")
    try:
        src = live.video.create(kind="file", uri=str(dest), name=name or file.filename, mode=mode, detector=detector,
                                conf=conf, target_fps=target_fps, loop=loop)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return src.to_dict()


@router.get("/api/video/sources/{sid}")
def get_source(sid: str, live: LiveSimulation = Depends(get_live)):
    s = _src(live, sid)
    return {**s.to_dict(), "timeline": list(s.timeline)[-600:], "summary": s.summary()}


@router.patch("/api/video/sources/{sid}", dependencies=[Depends(require_admin)])
def update_source(sid: str, req: SourceUpdate, live: LiveSimulation = Depends(get_live)):
    s = _src(live, sid)
    if req.zone is not None:
        for key in ("roi", "line"):
            pts = req.zone.get(key)
            if pts is not None and (not isinstance(pts, list) or len(pts) < (3 if key == "roi" else 2)
                                    or any(len(p) != 2 or not all(0 <= v <= 1 for v in p) for p in pts)):
                raise HTTPException(422, f"zone.{key} must be a list of [x, y] fractions between 0 and 1")
        s.set_zone(req.zone)
    if req.clear_attach:
        if s.cfg.attach:
            live.external_queues.pop(s.cfg.attach.get("approach"), None)
        s.cfg.attach = None
    elif req.attach is not None:
        _check_attach(live, req.attach)
        if s.cfg.attach:
            live.external_queues.pop(s.cfg.attach.get("approach"), None)
        s.cfg.attach = req.attach
    if req.conf is not None:
        s.cfg.conf = req.conf
    if req.classes is not None:
        s.cfg.classes = req.classes or None
    if req.imgsz is not None:
        s.cfg.imgsz = int(req.imgsz // 32 * 32)
    if req.target_fps is not None:
        s.cfg.target_fps = req.target_fps
    if req.detect is not None:
        if req.detect not in DETECT_MODES:
            raise HTTPException(422, f"detect must be one of {DETECT_MODES}")
        s.cfg.detect = req.detect
    if req.ambulance is not None and s.verifier is not None:
        s.cfg.ambulance = {**s.cfg.ambulance, **req.ambulance}
        for k, v in req.ambulance.items():
            if hasattr(s.verifier.cfg, k):
                setattr(s.verifier.cfg, k, v)
    return s.to_dict()


@router.post("/api/video/sources/{sid}/stop", dependencies=[Depends(require_admin)])
def stop_source(sid: str, live: LiveSimulation = Depends(get_live)):
    s = _src(live, sid)
    s.stop()
    if s.cfg.attach:
        live.external_queues.pop(s.cfg.attach.get("approach"), None)
    return s.to_dict()


@router.delete("/api/video/sources/{sid}", dependencies=[Depends(require_admin)])
def delete_source(sid: str, live: LiveSimulation = Depends(get_live)):
    s = _src(live, sid)
    if s.cfg.attach:
        live.external_queues.pop(s.cfg.attach.get("approach"), None)
    live.video.delete(sid)
    return {"deleted": sid}


@router.get("/api/video/sources/{sid}/frame.jpg")
def latest_frame(sid: str, live: LiveSimulation = Depends(get_live)):
    s = _src(live, sid)
    if not s.latest_jpeg:
        raise HTTPException(404, "no frame yet")
    return Response(s.latest_jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/api/video/sources/{sid}/stream.mjpg")
async def mjpeg(sid: str, live: LiveSimulation = Depends(get_live)):
    """Annotated live video as Motion-JPEG – usable directly in an <img> tag or VLC."""
    s = _src(live, sid)

    async def gen():
        last = -1
        idle = 0.0
        sent_at = 0.0
        loop = asyncio.get_running_loop()
        while live.video.get(sid) is s:
            # send new frames as they come; resend the last one every second so browsers
            # (which render an MJPEG part only when the next one starts) always show it
            if s.latest_jpeg and (s.frame_id != last or loop.time() - sent_at > 1.0):
                if s.frame_id != last:
                    idle = 0.0
                last = s.frame_id
                sent_at = loop.time()
                frame = s.latest_jpeg
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode()
                       + b"\r\n\r\n" + frame + b"\r\n")
            else:
                idle += 0.04
                if idle > 30 and s.status in ("finished", "stopped", "error"):
                    break
            await asyncio.sleep(0.04)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


@router.get("/api/video/sources/{sid}/files/{name}")
def download(sid: str, name: str, live: LiveSimulation = Depends(get_live)):
    s = _src(live, sid)
    if name not in (s.outputs or {}).values():
        raise HTTPException(404, "file not found")
    path = s.out_dir / name
    media = {".webm": "video/webm", ".mp4": "video/mp4", ".csv": "text/csv", ".json": "application/json"}
    return FileResponse(path, media_type=media.get(path.suffix, "application/octet-stream"), filename=f"{sid}_{name}")


@router.websocket("/ws/video/{sid}")
async def ws_video(ws: WebSocket, sid: str):
    live = ws.app.state.live
    channel = f"video:{sid}"
    await live.hub.connect(channel, ws)
    try:
        src = live.video.get(sid)
        if src is not None:
            await ws.send_json({"type": "video", **src.to_dict(), "timeline": list(src.timeline)[-120:]})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        live.hub.disconnect(channel, ws)


@router.websocket("/ws/video/{sid}/push")
async def ws_push(ws: WebSocket, sid: str):
    """Browser camera: the client sends JPEG frames (binary), the server answers with detections + stats.

    Robust against reconnects: if the source no longer exists (backend restarted, source removed) it is
    re-created with the same id from the query parameters, so the browser simply carries on."""
    live = ws.app.state.live
    src = live.video.get(sid)
    await ws.accept()
    if src is None and re.fullmatch(r"cam\d{1,6}", sid):
        q = ws.query_params
        try:
            src = live.video.create(sid=sid, kind="push", name=q.get("name") or "Browser camera",
                                    detector=q.get("detector") or "auto", detect=q.get("detect") or "all",
                                    conf=float(q.get("conf") or 0.25), imgsz=int(q.get("imgsz") or 480))
            log.info("re-created browser camera source %s after reconnect", sid)
        except Exception as exc:
            log.warning("could not re-create push source %s: %s", sid, exc)
            src = None
    if src is None or src.cfg.kind != "push":
        await ws.close(code=4404)
        return
    src.status = "running"
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is None:  # text keep-alive ("ping")
                if msg.get("text") == "ping":
                    await ws.send_json({"pong": True})
                continue
            frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                await ws.send_json({"error": "could not decode frame"})
                continue
            try:
                result = await asyncio.to_thread(src.push_frame, frame)
            except Exception as exc:  # one bad frame must not drop the camera
                log.exception("push frame failed")
                result = {"error": f"{type(exc).__name__}: {exc}"}
            await ws.send_json(result)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # network hiccup while sending: the client reconnects
        log.info("browser camera %s connection ended: %s", sid, exc)
    finally:
        if src.status == "running":
            src.status = "waiting for frames"
