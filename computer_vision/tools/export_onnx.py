"""Export the YOLO weights to ONNX so OpenCV's DNN module can run them (detector "opencv-dnn").

    python computer_vision/tools/export_onnx.py                    # yolo11n.pt -> computer_vision/models/yolo11n.onnx
    python computer_vision/tools/export_onnx.py --weights my_indian_traffic.pt

Needs `pip install ultralytics onnx` once; afterwards the ONNX file runs with plain OpenCV.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from computer_vision.video.detectors import MODELS_DIR, resolve_weights  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default="yolo11n.pt")
    ap.add_argument("--imgsz", type=int, default=640)
    args = ap.parse_args()
    from ultralytics import YOLO

    weights = resolve_weights(args.weights)
    model = YOLO(weights)
    out = Path(model.export(format="onnx", imgsz=args.imgsz, opset=12, simplify=False, dynamic=False))
    target = MODELS_DIR / out.name
    if out.resolve() != target.resolve():
        shutil.move(str(out), target)
    target.with_suffix(".names").write_text("\n".join(model.names[i] for i in sorted(model.names)), encoding="utf-8")
    print(f"wrote {target} (+ class names) – use detector 'opencv-dnn'")


if __name__ == "__main__":
    main()
