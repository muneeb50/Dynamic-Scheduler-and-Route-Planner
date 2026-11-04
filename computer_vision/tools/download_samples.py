"""Download a few openly licensed sample videos (Intel sample-videos, CC BY 4.0) into data/videos/samples."""
from __future__ import annotations

import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "https://raw.githubusercontent.com/intel-iot-devkit/sample-videos/master/"
FILES = {"car-detection.mp4": "parking-lot-cars.mp4", "person-bicycle-car-detection.mp4": "parking-lot-cars-bikes.mp4"}


def main():
    out = ROOT / "data" / "videos" / "samples"
    out.mkdir(parents=True, exist_ok=True)
    for src, name in FILES.items():
        dest = out / name
        if dest.exists():
            print(f"exists: {dest}")
            continue
        print(f"downloading {src} ...")
        urllib.request.urlretrieve(BASE + src, dest)
        print(f"saved {dest}")
    print("Videos © Intel, CC BY 4.0 (https://github.com/intel-iot-devkit/sample-videos)")


if __name__ == "__main__":
    main()
