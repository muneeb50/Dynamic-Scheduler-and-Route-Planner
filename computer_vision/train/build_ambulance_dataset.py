"""Build the vehicle-crop dataset used to train the ambulance / vehicle-type classifier.

The classifier looks at one detected vehicle at a time (the crop inside its box) and decides:

    ambulance · emergency_other (fire engine, police) · auto_rickshaw · bus · motorcycle · car

Sources (all public; clone them into one folder, default ../datasets next to the repo):

  * HariPrashanna-07/Ambulance_dataset           Indian traffic scenes, YOLO boxes (ambulance, auto, bike, bus, car),
                                                 Roboflow export, Public Domain
  * shivamsingh96/Emergency-vs-Non-Emergency-Vehicle-Classification
                                                 Analytics Vidhya JanataHack dataset (emergency / non-emergency crops);
                                                 our manual ambulance labels: train/data/av_emergency_ambulance_labels.json
  * Jomeezius/yolov5_ambulance_detection         British ambulances, YOLO boxes (Roboflow, CC BY 4.0)
  * Vatsalsamarth/Ambulance-Detection            Indian ambulance video frames (Roboflow, CC BY 4.0)
  * Abhi-899/YOLOV4-Custom-Object-Detection      Open Images ambulance / car boxes

    git clone --depth 1 https://github.com/HariPrashanna-07/Ambulance_dataset.git
    ... (see docs/ambulance_model.md)
    python computer_vision/train/build_ambulance_dataset.py --src ../datasets --out ../datasets/vehicle_crops

Leakage control: images are de-duplicated with a perceptual hash across sources and splits; a validation/test
image that looks like a training image (Hamming distance <= 6 on a 64-bit dHash) is moved to training, so the
reported accuracy is measured on images the model has never seen in any form.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import shutil
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

CLASSES = ["ambulance", "emergency_other", "auto_rickshaw", "bus", "motorcycle", "car"]
HERE = Path(__file__).resolve().parent
PAD = 0.06
MIN_SIDE = 24
CAPS = {"car": 4000, "motorcycle": 2500, "bus": 2500}  # training crops per class (keeps classes balanced)


def dhash(img: np.ndarray) -> int:
    g = cv2.cvtColor(cv2.resize(img, (9, 8), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
    bits = (g[:, 1:] > g[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def ham(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def near(h: int, arr: np.ndarray, max_d: int = 6) -> bool:
    x = np.bitwise_xor(arr, np.uint64(h))
    bits = np.unpackbits(x.view(np.uint8).reshape(-1, 8), axis=1).sum(1)
    return bool((bits <= max_d).any())


def yolo_boxes(label: Path, w: int, h: int):
    if not label.exists():
        return []
    out = []
    for line in label.read_text().splitlines():
        p = line.split()
        if len(p) < 5:
            continue
        c, x, y, bw, bh = int(float(p[0])), *map(float, p[1:5])
        out.append((c, (x - bw / 2) * w, (y - bh / 2) * h, (x + bw / 2) * w, (y + bh / 2) * h))
    return out


def crop(img: np.ndarray, box) -> bytes | None:
    """Padded crop, scaled to <= 320 px and JPEG-encoded (keeps memory low for 30k+ crops)."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = box
    if x2 - x1 < MIN_SIDE or y2 - y1 < MIN_SIDE:
        return None
    px, py = (x2 - x1) * PAD, (y2 - y1) * PAD
    x1, y1, x2, y2 = int(max(0, x1 - px)), int(max(0, y1 - py)), int(min(w, x2 + px)), int(min(h, y2 + py))
    c = img[y1:y2, x1:x2]
    if not c.size:
        return None
    return encode(c)


def encode(c: np.ndarray) -> bytes:
    side = max(c.shape[:2])
    if side > 320:
        s = 320 / side
        c = cv2.resize(c, (max(1, int(c.shape[1] * s)), max(1, int(c.shape[0] * s))), interpolation=cv2.INTER_AREA)
    return cv2.imencode(".jpg", c, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()


def split_of(name: str, val=0.1, test=0.1) -> str:
    u = int(hashlib.md5(name.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "test" if u < test else "val" if u < test + val else "train"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=str(HERE.parents[2] / "datasets"))
    ap.add_argument("--out", default=str(HERE.parents[2] / "datasets" / "vehicle_crops"))
    a = ap.parse_args()
    src, out = Path(a.src), Path(a.out)
    random.seed(0)
    # (source image id, split, class, crop, image hash)
    items: list[tuple[str, str, str, bytes, int]] = []

    # ---- Indian traffic scenes (Roboflow export; train contains 3 augmented copies of each image)
    H = src / "HariPrashanna-07_Ambulance_dataset" / "Ambulance.v1i.yolov11"
    hmap = {1: "ambulance", 2: "auto_rickshaw", 3: "motorcycle", 4: "bus", 5: "car"}  # 0 = siren light, skipped
    if H.exists():
        for split, sname in (("train", "train"), ("valid", "val"), ("test", "test")):
            seen_stem = Counter()
            for f in sorted((H / split / "images").glob("*")):
                stem = re.sub(r"\.rf\..*", "", f.name)
                img = cv2.imread(str(f))
                if img is None:
                    continue
                boxes = yolo_boxes(H / split / "labels" / (f.stem + ".txt"), img.shape[1], img.shape[0])
                has_amb = any(c == 1 for c, *_ in boxes)
                seen_stem[stem] += 1
                if split == "train" and seen_stem[stem] > 1 and not has_amb:
                    continue  # one copy of ordinary images, all augmented copies of the scarce ambulance images
                hsh = dhash(img)
                for c, *b in boxes:
                    if c in hmap and (cr := crop(img, b)) is not None:
                        items.append((f"hari:{stem}", sname, hmap[c], cr, hsh))
        print("indian scenes:", Counter(i[2] for i in items))

    # ---- Analytics Vidhya emergency vs non-emergency (+ our ambulance labels)
    S = src / "shivamsingh96_Emergency-vs-Non-Emergency-Vehicle-Classification"
    if S.exists():
        amb = set(json.loads((HERE / "data" / "av_emergency_ambulance_labels.json").read_text())["ambulance"])
        n0 = len(items)
        for row in csv.DictReader(open(S / "train.csv")):
            img = cv2.imread(str(S / "images" / row["image_names"]))
            if img is None:
                continue
            cls = ("ambulance" if row["image_names"] in amb else "emergency_other") if row["emergency_or_not"] == "1" else "car"
            items.append((f"av:{row['image_names']}", split_of("av" + row["image_names"]), cls, encode(img), dhash(img)))
        print("analytics vidhya:", Counter(i[2] for i in items[n0:]))

    # ---- ambulance detection sets (boxes -> crops)
    def boxes_dir(root: Path, split_dirs: dict, cmap: dict, tag: str):
        n0 = len(items)
        for d, sname in split_dirs.items():
            imgs = root / d / "images" if (root / d / "images").exists() else root / d
            for f in sorted(imgs.glob("*")):
                if f.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                    continue
                img = cv2.imread(str(f))
                if img is None:
                    continue
                lab = (root / d / "labels" / (f.stem + ".txt")) if (root / d / "labels").exists() else f.with_suffix(".txt")
                hsh = dhash(img)
                for c, *b in yolo_boxes(lab, img.shape[1], img.shape[0]):
                    if c in cmap and (cr := crop(img, b)) is not None:
                        items.append((f"{tag}:{f.stem}", sname if sname != "hash" else split_of(tag + f.stem), cmap[c], cr, hsh))
        print(f"{tag}:", Counter(i[2] for i in items[n0:]))

    J = src / "Jomeezius_yolov5_ambulance_detection" / "british_ambulance"
    if J.exists():
        boxes_dir(J, {"train": "train", "valid": "val", "test": "test"}, {0: "ambulance"}, "uk")
    V = src / "vatsal" / "dataset"
    if V.exists():
        boxes_dir(V, {"train": "train"}, {0: "ambulance"}, "vatsal")
    A = src / "Abhi-899_YOLOV4-Custom-Object-Detection" / "Dataset" / "train"
    if A.exists():
        boxes_dir(A, {"Person_Car_Ambulance": "hash"}, {0: "ambulance", 1: "car"}, "openimages")

    # ---- leakage control: val/test images that look like any training image go to training
    train_hashes = np.array(sorted({h for _, s, _, _, h in items if s == "train"}), dtype=np.uint64)
    moved = 0
    fixed = []
    for sid, s, c, cr, h in items:
        if s != "train" and near(h, train_hashes):
            s, moved = "train", moved + 1
        fixed.append((sid, s, c, cr, h))
    print(f"moved {moved} near-duplicate val/test crops to train")

    # ---- write (cap the big classes in train)
    if out.exists():
        shutil.rmtree(out)
    counts = Counter()
    random.shuffle(fixed)
    for i, (sid, s, c, cr, _) in enumerate(fixed):
        if s == "train" and counts[(s, c)] >= CAPS.get(c, 10 ** 9):
            continue
        d = out / s / c
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', sid)}_{i}.jpg").write_bytes(cr)
        counts[(s, c)] += 1
    for s in ("train", "val", "test"):
        print(s, {c: counts[(s, c)] for c in CLASSES})
    (out / "classes.json").write_text(json.dumps(CLASSES))


if __name__ == "__main__":
    main()
