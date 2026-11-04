from __future__ import annotations

from fastapi import Header, HTTPException, Request

from ..config import settings
from ..services.live import LiveSimulation


def get_live(request: Request) -> LiveSimulation:
    return request.app.state.live


def require_admin(x_admin_token: str = Header(default="")):
    """Admin endpoints are open when ADMIN_TOKEN is unset (local demo); otherwise the header must match."""
    if settings.admin_token and x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="admin token required (X-Admin-Token header)")
