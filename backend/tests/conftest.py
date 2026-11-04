import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="resq_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_tmp, 'test.db').as_posix()}"
os.environ["SIM_AUTOSTART"] = "false"  # tests advance the simulation explicitly
os.environ["NETWORK_FILE"] = str(ROOT / "data" / "network.demo.json")
os.environ["SIM_ENGINE"] = "builtin"
os.environ["RESULTS_DIR"] = str(Path(_tmp, "results"))
os.environ["FIELD_STORE"] = str(Path(_tmp, "field_junctions.json"))
os.environ["MAPS_DIR"] = str(Path(_tmp, "maps"))
os.environ["MAP_AUTO_FETCH"] = "false"  # tests never download maps
os.environ.pop("ADMIN_TOKEN", None)

from app.core.network import RoadNetwork  # noqa: E402


@pytest.fixture(scope="session")
def net() -> RoadNetwork:
    return RoadNetwork.load(ROOT / "data" / "network.demo.json")
