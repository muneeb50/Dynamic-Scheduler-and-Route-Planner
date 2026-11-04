"""Fit the weights that combine the ambulance cues, on the validation split, and report test accuracy.

    python computer_vision/train/fit_ambulance_fusion.py --data ../datasets/vehicle_crops

For every validation crop it computes the cues – trained classifier P(ambulance), 'AMBULANCE' text (OCR,
normal + mirrored), lit light bar and livery – fits a logistic regression (ambulance vs every other vehicle)
and picks the decision threshold with the best F1 among those with precision >= 0.9 (a false ambulance turns
a signal green, so precision matters). Saves computer_vision/models/ambulance_fusion.json and writes
results/ambulance_classifier_eval.json with test-set numbers for each cue alone and for the fusion.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from computer_vision.video.ambulance import livery_score  # noqa: E402
from computer_vision.video.ambulance_fusion import FUSION_FILE, features  # noqa: E402
from computer_vision.video.ensemble import AmbulanceText  # noqa: E402
from computer_vision.video.photo import static_beacon_score  # noqa: E402
from computer_vision.video.vehicle_classifier import VehicleClassifier  # noqa: E402

KEYS = ["cls_logit", "text", "beacon", "livery"]


def cues(files: list[Path], clf: VehicleClassifier, ocr: AmbulanceText, use_ocr: bool) -> tuple[np.ndarray, list[dict]]:
    rows, probs_all = [], []
    imgs = [cv2.imread(str(f)) for f in files]
    probs = []
    for i in range(0, len(imgs), 64):
        probs += clf.classify(imgs[i:i + 64])
    for img, p in zip(imgs, probs):
        h, w = img.shape[:2]
        box = (0, 0, w, h)
        txt = ocr.score_vehicles(img, [box])[0][0] if use_ocr else 0.0
        f = features(p["ambulance"], txt, static_beacon_score(img, box), livery_score(img, box))
        rows.append([f[k] for k in KEYS])
        probs_all.append(p)
    return np.array(rows), probs_all


def metrics(y: np.ndarray, pred: np.ndarray) -> dict:
    tp, fp = int(((pred == 1) & (y == 1)).sum()), int(((pred == 1) & (y == 0)).sum())
    fn, tn = int(((pred == 0) & (y == 1)).sum()), int(((pred == 0) & (y == 0)).sum())
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0,
            "false_alarm_rate": round(fp / (fp + tn), 4) if fp + tn else 0.0, "tp": tp, "fp": fp, "fn": fn, "tn": tn}


def sample(split_dir: Path, n_other: int, seed: int) -> tuple[list[Path], np.ndarray, list[str]]:
    rnd = random.Random(seed)
    amb = sorted((split_dir / "ambulance").glob("*.jpg"))
    other = [f for c in sorted(split_dir.iterdir()) if c.is_dir() and c.name != "ambulance" for f in sorted(c.glob("*.jpg"))]
    rnd.shuffle(other)
    other = other[:n_other] if n_other else other
    files = amb + other
    return files, np.array([1] * len(amb) + [0] * len(other)), [f.parent.name for f in files]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(ROOT.parent / "datasets" / "vehicle_crops"))
    ap.add_argument("--weights", default=None)
    ap.add_argument("--val-other", type=int, default=700, help="non-ambulance validation crops used (OCR is slow)")
    ap.add_argument("--no-ocr", action="store_true")
    a = ap.parse_args()
    from sklearn.linear_model import LogisticRegression

    data = Path(a.data)
    clf = VehicleClassifier(Path(a.weights) if a.weights else None)
    ocr = AmbulanceText()
    use_ocr = not a.no_ocr and ocr._engine() is not None

    vf, vy, _ = sample(data / "val", a.val_other, 1)
    Xv, _ = cues(vf, clf, ocr, use_ocr)
    lr = LogisticRegression(C=1.0, class_weight="balanced", max_iter=2000).fit(Xv, vy)
    pv = lr.predict_proba(Xv)[:, 1]
    best = (0.5, -1.0)
    for thr in np.linspace(0.2, 0.95, 76):
        m = metrics(vy, (pv >= thr).astype(int))
        if m["precision"] >= 0.9 and m["f1"] > best[1]:
            best = (float(thr), m["f1"])
    params = {"bias": float(lr.intercept_[0]), "w": {k: float(v) for k, v in zip(KEYS, lr.coef_[0])},
              "threshold": round(best[0], 3), "fitted_on": f"{len(vy)} validation crops ({int(vy.sum())} ambulances)",
              "ocr": use_ocr}
    params["w"]["model"] = 1.5  # open-vocabulary model: not available on crops, keep a modest hand-set weight
    Path(FUSION_FILE).write_text(json.dumps(params, indent=1))
    print("fusion:", params)

    # ---- test set: every crop
    tf, ty, tcls = sample(data / "test", 0, 2)
    Xt, tprobs = cues(tf, clf, ocr, use_ocr)
    z = params["bias"] + Xt @ np.array([params["w"][k] for k in KEYS])
    pt = 1 / (1 + np.exp(-z))
    res = {
        "test_crops": len(ty), "test_ambulances": int(ty.sum()),
        "classifier_only": metrics(ty, np.array([p["ambulance"] >= 0.5 for p in tprobs]).astype(int)),
        "text_only": metrics(ty, (Xt[:, 1] >= 0.3).astype(int)),
        "light_bar_livery_only (previous cues)": metrics(ty, ((0.6 * Xt[:, 2] + 0.4 * Xt[:, 3]) >= 0.5).astype(int)),
        "fusion": metrics(ty, (pt >= params["threshold"]).astype(int)),
    }
    # vehicle-type accuracy of the classifier on the test set
    names = sorted({c for c in tcls})
    conf = {c: {d: 0 for d in names} for c in names}
    for c, p in zip(tcls, tprobs):
        conf[c][max(p, key=p.get)] += 1
    res["type_accuracy"] = round(sum(conf[c][c] for c in names) / len(tcls), 4)
    res["per_class_recall"] = {c: round(conf[c][c] / max(1, sum(conf[c].values())), 3) for c in names}
    res["confusion"] = conf
    # shortcut check: non-ambulance test vehicles with a painted red / blue stripe must not become ambulances
    from computer_vision.train.add_hard_negatives import stripe

    rnd = random.Random(5)
    neg = [f for f, y in zip(tf, ty) if y == 0]
    imgs = [cv2.imread(str(f)) for f in neg]
    plain = [p["ambulance"] for i in range(0, len(imgs), 64) for p in clf.classify(imgs[i:i + 64])]
    striped_imgs = [stripe(im, rnd) for im in imgs]
    striped = [p["ambulance"] for i in range(0, len(striped_imgs), 64) for p in clf.classify(striped_imgs[i:i + 64])]
    res["stripe_shortcut_check"] = {
        "non_ambulance_crops": len(neg),
        "called_ambulance_plain": int(sum(p >= 0.5 for p in plain)),
        "called_ambulance_with_painted_stripe": int(sum(p >= 0.5 for p in striped)),
    }
    res["fusion_params"] = params
    # the previous open-vocabulary check (YOLOE with an "ambulance" prompt) on the same test crops, for comparison
    yoloe = ROOT / "computer_vision" / "models" / "yoloe-11s-traffic.pt"
    if yoloe.exists():
        from computer_vision.video.detectors import UltralyticsYolo

        m = UltralyticsYolo(str(yoloe), conf=0.1)
        m.keep = None
        ev = []
        for f in tf:
            img = cv2.imread(str(f))
            h, w = img.shape[:2]
            ev.append(max([d.conf for d in m.detect(img, imgsz=320) if d.cls == "ambulance"
                           and (d.box[2] - d.box[0]) * (d.box[3] - d.box[1]) > 0.2 * w * h] or [0.0]))
        res["previous_open_vocab_model"] = metrics(ty, (np.array(ev) * 1.2 >= 0.5).astype(int))
    out = ROOT / "results" / "ambulance_classifier_eval.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(res, indent=1))
    out.write_text(json.dumps(res, indent=1))
    print(json.dumps({k: v for k, v in res.items() if k not in ("confusion", "fusion_params")}, indent=1))


if __name__ == "__main__":
    main()
