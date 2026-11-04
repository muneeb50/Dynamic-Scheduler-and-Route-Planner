"""Fine-tune YOLO on your own traffic images (e.g. Indian vehicle classes, ambulances).

Why: the stock COCO model has no "auto_rickshaw" or "ambulance" class and is trained mostly
on non-Indian streets. A few hundred labelled frames from Nerul/Seawoods improve it a lot.

1. Collect frames from your videos:
       python computer_vision/train/train_yolo.py extract --video my_clip.mp4 --every 2 --out data/train/images
2. Label them in YOLO format (free tools: CVAT, Label Studio, Roboflow, makesense.ai) with classes such as
       car, motorcycle, auto_rickshaw, bus, truck, bicycle, ambulance
   and arrange:  data/train/{images,labels}/{train,val}/…
3. Write data/train/dataset.yaml:
       path: data/train
       train: images/train
       val: images/val
       names: {0: car, 1: motorcycle, 2: auto_rickshaw, 3: bus, 4: truck, 5: bicycle, 6: ambulance}
4. Train (CPU works for a small set; a free Colab/Kaggle GPU is much faster):
       python computer_vision/train/train_yolo.py train --data data/train/dataset.yaml --epochs 50
5. Use it: copy runs/detect/train/weights/best.pt to computer_vision/models/indian_traffic.pt and choose
   weights "indian_traffic.pt" (API/CLI) – "ambulance" detections then raise EMERGENCY_VEHICLE alerts.
   Report precision/recall/mAP from the validation output in your project report.

Use images you are allowed to use (your own recordings, or datasets whose licence permits it, e.g. the
India Driving Dataset (IDD) for research use).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2


def extract(video: str, every: float, out: str):
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise SystemExit(f"cannot open {video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    step = max(1, int(round(fps * every)))
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    i = saved = 0
    stem = Path(video).stem
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % step == 0:
            cv2.imwrite(str(out_dir / f"{stem}_{i:06d}.jpg"), frame)
            saved += 1
        i += 1
    print(f"saved {saved} frames to {out_dir}")


def train(data: str, epochs: int, model: str, imgsz: int, batch: int):
    from ultralytics import YOLO

    m = YOLO(model)
    m.train(data=data, epochs=epochs, imgsz=imgsz, batch=batch, patience=15)
    metrics = m.val()
    print("validation mAP50-95:", metrics.box.map, "mAP50:", metrics.box.map50)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("--video", required=True)
    e.add_argument("--every", type=float, default=2.0, help="seconds between saved frames")
    e.add_argument("--out", default="data/train/images/all")
    t = sub.add_parser("train")
    t.add_argument("--data", required=True)
    t.add_argument("--epochs", type=int, default=50)
    t.add_argument("--model", default="yolo11n.pt")
    t.add_argument("--imgsz", type=int, default=640)
    t.add_argument("--batch", type=int, default=8)
    args = ap.parse_args()
    if args.cmd == "extract":
        extract(args.video, args.every, args.out)
    else:
        train(args.data, args.epochs, args.model, args.imgsz, args.batch)


if __name__ == "__main__":
    main()
