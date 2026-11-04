"""Opening cameras and streams reliably on Windows, macOS and Linux.

`cv2.VideoCapture(0)` with the default backend is slow or fails on many Windows
laptops (MSMF quirks) and needs the right backend on macOS. We try the platform's
preferred backends in order and verify that a frame can actually be read.
"""
from __future__ import annotations

import logging
import platform
import time
from typing import Union

import cv2

log = logging.getLogger("resqroute.video")


def _webcam_backends() -> list[tuple[str, int]]:
    system = platform.system()
    if system == "Windows":
        return [("DirectShow", cv2.CAP_DSHOW), ("Media Foundation", cv2.CAP_MSMF), ("default", cv2.CAP_ANY)]
    if system == "Darwin":
        return [("AVFoundation", cv2.CAP_AVFOUNDATION), ("default", cv2.CAP_ANY)]
    return [("V4L2", cv2.CAP_V4L2), ("default", cv2.CAP_ANY)]


def open_webcam(index: int, width: int = 1280, height: int = 720) -> tuple[cv2.VideoCapture, str]:
    """Open a local camera, trying several backends. Returns (capture, backend name)."""
    errors = []
    for name, api in _webcam_backends():
        cap = cv2.VideoCapture(index, api)
        if not cap.isOpened():
            cap.release()
            errors.append(f"{name}: not opened")
            continue
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ok = False
        for _ in range(15):  # some cameras deliver a few empty frames while warming up
            ok, frame = cap.read()
            if ok and frame is not None and frame.size:
                break
            time.sleep(0.05)
        if ok:
            log.info("webcam %s opened with %s", index, name)
            return cap, name
        cap.release()
        errors.append(f"{name}: no frames")
    hint = {
        "Darwin": "Allow camera access for your terminal in System Settings > Privacy & Security > Camera.",
        "Windows": "Close other apps using the camera (Zoom, Teams, Camera app) and check Settings > Privacy > Camera.",
    }.get(platform.system(), "Check the camera is connected and not used by another program.")
    raise RuntimeError(f"cannot open webcam {index} ({'; '.join(errors)}). {hint}")


def open_capture(source: Union[int, str]) -> tuple[cv2.VideoCapture, str]:
    """Webcam index, file path or stream URL."""
    if isinstance(source, int) or (isinstance(source, str) and source.isdigit()):
        return open_webcam(int(source))
    if isinstance(source, str) and source.lower().startswith(("rtsp://", "rtsps://")):
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    else:
        cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open '{source}'")
    return cap, "ffmpeg"


def probe_webcams(max_index: int = 4) -> list[dict]:
    """List local cameras that deliver frames (index, resolution, backend)."""
    found = []
    for i in range(max_index + 1):
        try:
            cap, backend = open_webcam(i, 640, 480)
        except RuntimeError:
            continue
        ok, frame = cap.read()
        h, w = (frame.shape[:2] if ok and frame is not None else (0, 0))
        found.append({"index": i, "backend": backend, "width": int(w), "height": int(h)})
        cap.release()
    return found
