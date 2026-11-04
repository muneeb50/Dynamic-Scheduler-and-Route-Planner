#!/usr/bin/env bash
# One-time setup (macOS / Linux). Run from the repo root: bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r backend/requirements.txt
if [ "${SKIP_YOLO:-0}" != "1" ]; then
  echo "Installing YOLO (Ultralytics + PyTorch CPU) for video analytics – this is the large download…"
  if [ "$(uname -s)" = "Linux" ]; then  # avoid the multi-GB CUDA build on Linux
    pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu || true
  fi
  pip install -r computer_vision/requirements-yolo.txt || echo "YOLO install failed – video analytics will use OpenCV DNN / motion detection instead."
fi
if [ "${WITH_SUMO:-0}" = "1" ]; then pip install -r backend/requirements-sumo.txt; fi
python simulation/tools/make_demo_network.py
if command -v npm >/dev/null 2>&1; then
  (cd admin_dashboard && npm install && npm run build)
else
  echo "npm not found – skipping dashboard build (a prebuilt copy may already be in admin_dashboard/dist)"
fi
echo "Setup complete. Start with: bash scripts/run.sh"
