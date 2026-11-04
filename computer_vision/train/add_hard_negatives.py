"""Add synthetic hard negatives to the training split of the vehicle-crop dataset.

    python computer_vision/train/add_hard_negatives.py --data ../datasets/vehicle_crops

Why: after the first training run the classifier had learned a shortcut – "white body with a red band = ambulance".
Painting a red stripe across ordinary cars made 6% of them 'ambulance' (a white taxi with a red bumper reflector
was the visible failure). This script adds copies of non-ambulance training crops with random red / blue / red-blue
stripes and red reflector bars, so a stripe alone is no longer enough; the model has to use the van shape, the
light bar, the lettering and the cross. It also adds copies of *every* class (ambulances included) with drawn
detection boxes and a semi-transparent watermark, so screenshots of analysed images and stock photos with
watermarks do not change the answer. Only the training split is touched; validation and test stay original.
Files are named hn_*.jpg, so re-running replaces them.
"""
from __future__ import annotations

import argparse
import random
from pathlib import Path

import cv2
import numpy as np

COUNTS = {"car": 1600, "bus": 450, "motorcycle": 300, "auto_rickshaw": 250, "emergency_other": 150}
OVERLAY_SHARE = 0.12  # share of every class that also gets a drawn-box / watermark copy


def stripe(img: np.ndarray, rnd: random.Random) -> np.ndarray:
    im = img.copy()
    h, w = im.shape[:2]
    red = (rnd.randint(20, 70), rnd.randint(20, 60), rnd.randint(170, 240))
    blue = (rnd.randint(150, 230), rnd.randint(60, 120), rnd.randint(10, 60))
    kind = rnd.random()
    y = rnd.uniform(0.3, 0.8)
    t = rnd.uniform(0.025, 0.07)
    x1, x2 = (0, w) if rnd.random() < 0.6 else (int(w * rnd.uniform(0, 0.3)), int(w * rnd.uniform(0.7, 1.0)))
    y1, y2 = int(h * y), int(h * (y + t))
    if kind < 0.55:  # red band / reflector bar
        im[y1:y2, x1:x2] = red
    elif kind < 0.75:  # blue band
        im[y1:y2, x1:x2] = blue
    else:  # red over blue, as on many ambulances
        im[y1:y2, x1:x2] = red
        y3 = y2 + max(2, int(h * t * 0.7))
        im[y2:y3, x1:x2] = blue
    if rnd.random() < 0.35:  # a second, thinner band
        yy = int(h * rnd.uniform(0.25, 0.85))
        im[yy:yy + max(2, int(h * 0.02)), x1:x2] = red
    alpha = rnd.uniform(0.75, 1.0)
    return cv2.addWeighted(im, alpha, img, 1 - alpha, 0)


def overlay(img: np.ndarray, rnd: random.Random) -> np.ndarray:
    im = img.copy()
    h, w = im.shape[:2]
    th = max(1, int(round(min(h, w) / rnd.uniform(60, 120))))
    for _ in range(rnd.randint(1, 3)):  # detection boxes drawn by some other tool
        col = rnd.choice([(255, 170, 50), (60, 60, 230), (60, 200, 60), (230, 100, 230), (40, 220, 230)])
        x1, y1 = int(w * rnd.uniform(-0.1, 0.5)), int(h * rnd.uniform(-0.1, 0.5))
        x2, y2 = int(w * rnd.uniform(0.5, 1.1)), int(h * rnd.uniform(0.5, 1.1))
        cv2.rectangle(im, (x1, y1), (x2, y2), col, th)
        if rnd.random() < 0.5:
            cv2.rectangle(im, (max(0, x1), max(0, y1)), (max(0, x1) + w // 4, max(0, y1) + h // 14), col, -1)
    if rnd.random() < 0.6:  # stock-photo watermark
        layer = im.copy()
        word = rnd.choice(["alamy", "shutterstock", "gettyimages", "dreamstime", "a"])
        cv2.putText(layer, word, (int(w * rnd.uniform(0, 0.4)), int(h * rnd.uniform(0.3, 0.9))),
                    cv2.FONT_HERSHEY_SIMPLEX, w / rnd.uniform(150, 320), (240, 240, 240), max(1, w // 70))
        im = cv2.addWeighted(layer, 0.55, im, 0.45, 0)
    return im


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(Path(__file__).resolve().parents[3] / "datasets" / "vehicle_crops"))
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    train = Path(a.data) / "train"
    for old in train.glob("*/hn_*.jpg"):
        old.unlink()
    total = 0
    for cdir in sorted(d for d in train.iterdir() if d.is_dir()):
        src = sorted(f for f in cdir.glob("*.jpg") if not f.name.startswith("hn_"))
        jobs = []
        n = COUNTS.get(cdir.name, 0)
        jobs += [("s", f) for f in rnd.sample(src, min(n, len(src)))] if n else []
        jobs += [("o", f) for f in rnd.sample(src, int(len(src) * OVERLAY_SHARE))]
        for i, (k, f) in enumerate(jobs):
            img = cv2.imread(str(f))
            if img is None:
                continue
            out = stripe(img, rnd) if k == "s" else overlay(img, rnd)
            if k == "s" and rnd.random() < 0.15:
                out = overlay(out, rnd)
            cv2.imwrite(str(cdir / f"hn_{k}{i:05d}_{f.stem}.jpg"), out, [cv2.IMWRITE_JPEG_QUALITY, 90])
            total += 1
        print(f"{cdir.name}: +{len(jobs)}")
    print("added", total)


if __name__ == "__main__":
    main()
