#!/usr/bin/env bash
# Start the backend (which also serves the built dashboard) on http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
cd backend
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
