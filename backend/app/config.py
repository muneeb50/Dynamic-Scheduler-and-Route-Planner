"""Runtime configuration from environment variables (and an optional .env file at the repo root)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv():
    env = REPO_ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0].strip().strip('"').strip("'")  # allow trailing comments
        if v:
            os.environ.setdefault(k.strip(), v)


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _default_network() -> str:
    """NETWORK_FILE > data/network.json (manual import) > last real map loaded in the dashboard > demo grid."""
    real = REPO_ROOT / "data" / "network.json"
    if real.exists():
        return str(real)
    cur = Path(os.environ.get("MAPS_DIR", REPO_ROOT / "data" / "maps")) / "current.json"
    if cur.exists():
        try:
            import json

            p = Path(json.loads(cur.read_text(encoding="utf-8"))["network"])
            if p.exists():
                return str(p)
        except Exception:
            pass
    return str(REPO_ROOT / "data" / "network.demo.json")


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get(
        "DATABASE_URL", f"sqlite:///{(REPO_ROOT / 'backend' / 'resqroute.db').as_posix()}"))
    network_file: str = field(default_factory=lambda: os.environ.get("NETWORK_FILE") or _default_network())
    sim_engine: str = field(default_factory=lambda: os.environ.get("SIM_ENGINE", "builtin"))
    sumo_net_file: str = field(default_factory=lambda: os.environ.get(
        "SUMO_NET_FILE", str(REPO_ROOT / "simulation" / "network" / "resq.net.xml")))
    sumo_gui: bool = field(default_factory=lambda: _bool("SUMO_GUI", False))
    sim_speed: float = field(default_factory=lambda: float(os.environ.get("SIM_SPEED", "3")))
    sim_dt: float = field(default_factory=lambda: float(os.environ.get("SIM_DT", "0.5")))
    demand: str = field(default_factory=lambda: os.environ.get("DEMAND", "medium"))
    seed: int = field(default_factory=lambda: int(os.environ.get("SEED", "42")))
    priority_enabled: bool = field(default_factory=lambda: _bool("PRIORITY_ENABLED", True))
    recovery_enabled: bool = field(default_factory=lambda: _bool("RECOVERY_ENABLED", True))
    queue_source: str = field(default_factory=lambda: os.environ.get("QUEUE_SOURCE", "sim"))
    autostart: bool = field(default_factory=lambda: _bool("SIM_AUTOSTART", True))
    admin_token: str = field(default_factory=lambda: os.environ.get("ADMIN_TOKEN", ""))
    cors_origins: list[str] = field(default_factory=lambda: os.environ.get("CORS_ORIGINS", "*").split(","))
    results_dir: str = field(default_factory=lambda: os.environ.get("RESULTS_DIR", str(REPO_ROOT / "results")))
    field_store: str = field(default_factory=lambda: os.environ.get(
        "FIELD_STORE", str(REPO_ROOT / "data" / "field_junctions.json")))
    # real maps (OpenStreetMap via the Overpass API)
    maps_dir: str = field(default_factory=lambda: os.environ.get("MAPS_DIR", str(REPO_ROOT / "data" / "maps")))
    map_auto_fetch: bool = field(default_factory=lambda: _bool("MAP_AUTO_FETCH", True) and not os.environ.get("NETWORK_FILE"))
    map_area: str = field(default_factory=lambda: os.environ.get("MAP_AREA", "nerul-seawoods"))
    # random emergency calls per hour of simulated time (0 = only bookings made by people)
    auto_emergencies_per_hour: float = field(default_factory=lambda: float(os.environ.get("AUTO_EMERGENCIES_PER_HOUR", "0")))


settings = Settings()
