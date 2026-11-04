"""Train the ambulance / vehicle-type classifier (second stage after the detector).

    python computer_vision/train/build_ambulance_dataset.py --src ../datasets --out ../datasets/vehicle_crops
    python computer_vision/train/train_vehicle_classifier.py --data ../datasets/vehicle_crops --model yolo11n-cls.pt
    python computer_vision/train/fit_ambulance_fusion.py --data ../datasets/vehicle_crops

Copies the best weights to computer_vision/models/vehicle_cls.pt (+ .onnx for OpenCV DNN) – the backend picks
them up automatically. A GPU (Colab/Kaggle) trains in minutes; on a laptop CPU allow ~5 min per epoch.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "computer_vision" / "models"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(ROOT.parent / "datasets" / "vehicle_crops"))
    ap.add_argument("--model", default="yolo11n-cls.pt", help="ImageNet-pretrained start (yolo11n-cls.pt / yolo11s-cls.pt)")
    ap.add_argument("--epochs", type=int, default=35)
    ap.add_argument("--imgsz", type=int, default=224)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--device", default=None, help="0 for a GPU, cpu")
    ap.add_argument("--name", default="vehicle_cls")
    a = ap.parse_args()
    from ultralytics import YOLO

    m = YOLO(a.model)
    m.train(data=a.data, imgsz=a.imgsz, epochs=a.epochs, batch=a.batch, device=a.device, project=str(ROOT / "runs" / "classify"),
            name=a.name, exist_ok=True, patience=12, seed=0, optimizer="AdamW", lr0=0.001, cos_lr=True, warmup_epochs=2,
            hsv_h=0.015, hsv_s=0.5, hsv_v=0.4, fliplr=0.5, erasing=0.3, scale=0.4, dropout=0.1)
    best = Path(m.trainer.best)
    MODELS.mkdir(parents=True, exist_ok=True)
    shutil.copy(best, MODELS / "vehicle_cls.pt")
    trained = YOLO(str(MODELS / "vehicle_cls.pt"))
    names = [trained.names[i] for i in range(len(trained.names))]
    (MODELS / "vehicle_cls.json").write_text(json.dumps({"classes": names, "imgsz": a.imgsz}))
    onnx = trained.export(format="onnx", imgsz=a.imgsz, opset=12, simplify=True)
    shutil.move(onnx, MODELS / "vehicle_cls.onnx")
    print("saved", MODELS / "vehicle_cls.pt", "classes:", names)


if __name__ == "__main__":
    main()
