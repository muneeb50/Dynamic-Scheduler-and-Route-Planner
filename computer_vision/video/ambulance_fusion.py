"""Combine the ambulance cues into one probability.

Cues (each 0..1):
  cls      – trained vehicle classifier: P(ambulance | vehicle crop)              (strongest, learned)
  text     – OCR found the word AMBULANCE (or mirrored) on the vehicle            (very specific)
  beacon   – flashing red/blue light bar (video) or a lit light bar (photo)
  livery   – white/yellow body with red/blue markings                             (weak)
  model    – open-vocabulary detector (YOLOE) said "ambulance"                    (weak)

The weights of a logistic model over these cues are fitted on the validation split by
computer_vision/train/fit_ambulance_fusion.py and stored in models/ambulance_fusion.json; without that
file sensible hand-set weights are used. Text and classifier evidence can each make the decision on
their own when they are confident; the colour cues only add support.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

from .detectors import MODELS_DIR

FUSION_FILE = MODELS_DIR / "ambulance_fusion.json"
DEFAULT = {"bias": -2.5, "w": {"cls_logit": 1.3, "text": 6.5, "beacon": 2.5, "livery": 0.8, "model": 1.5},
           "threshold": 0.5}
_params: Optional[dict] = None


def params() -> dict:
    global _params
    if _params is None:
        try:
            _params = json.loads(Path(FUSION_FILE).read_text()) if Path(FUSION_FILE).exists() else DEFAULT
        except Exception:
            _params = DEFAULT
    return _params


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def features(cls: Optional[float], text: float = 0.0, beacon: float = 0.0, livery: float = 0.0,
             model: float = 0.0) -> dict:
    return {"cls_logit": logit(cls) if cls is not None else 0.0, "text": text, "beacon": beacon,
            "livery": livery, "model": model}


def ambulance_probability(cls: Optional[float], text: float = 0.0, beacon: float = 0.0, livery: float = 0.0,
                          model: float = 0.0) -> tuple[float, list[str]]:
    """(probability, human-readable reasons). Without a classifier (cls=None) the old cues decide."""
    p = params()
    f = features(cls, text, beacon, livery, model)
    w = p["w"]
    if cls is None:  # no trained classifier: fall back to the original cue mix
        prob = max(min(1.0, model * 1.2), 0.8 * beacon + 0.2 * livery, 0.95 * text)
    else:
        z = p["bias"] + sum(w[k] * f[k] for k in w)
        prob = 1 / (1 + math.exp(-z))
    reasons = []
    if cls is not None and cls >= 0.35:
        reasons.append(f"classifier {cls:.2f}")
    if text >= 0.3:
        reasons.append("'AMBULANCE' text")
    if beacon > 0.3:
        reasons.append(f"light bar {beacon:.2f}")
    if livery > 0.4:
        reasons.append(f"livery {livery:.2f}")
    if model > 0.2:
        reasons.append(f"open-vocab model {model:.2f}")
    return float(prob), reasons


def threshold() -> float:
    return float(params().get("threshold", 0.5))


def decision_score(prob: float, cls: Optional[float]) -> float:
    """Re-centre a fused probability on the fitted decision threshold, so 0.5 always means "at the threshold".

    The fusion is fitted with balanced class weights and its threshold is chosen for precision (e.g. 0.91), so
    raw probabilities of ordinary cars can sit near 0.5. Callers that use fixed cut-offs (the video verifier's
    0.5 / 0.25 hysteresis and its temporal voting) must use this score, not the raw probability.
    Without a classifier the fallback cue mix is already on that scale and is returned unchanged."""
    if cls is None:
        return prob
    return 1 / (1 + math.exp(-(logit(prob) - logit(threshold()))))
