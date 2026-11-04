"""ResQRoute AI backend package."""
import sys
from pathlib import Path

# make the top-level `computer_vision` package importable when running from backend/
_repo = Path(__file__).resolve().parents[2]
if str(_repo) not in sys.path:
    sys.path.append(str(_repo))
