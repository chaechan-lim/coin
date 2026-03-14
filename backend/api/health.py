"""
Health check endpoint for the Coin auto-trading API.

Exposes GET /api/v1/health — returns engine registration status and
overall service readiness without requiring DB access.
"""
from datetime import datetime, timezone

from fastapi import APIRouter

from api.dependencies import engine_registry

router = APIRouter(tags=["health"])


@router.get("/health")
async def api_health_check():
    """
    API-level health check.

    Returns:
    - ``status``: always ``"ok"`` (HTTP 200 means the service is reachable)
    - ``timestamp``: current UTC time in ISO-8601
    - ``exchanges_registered``: list of exchange names with registered engines
    - ``engines``: per-exchange ``{registered, running}`` summary
    """
    exchanges = engine_registry.available_exchanges

    engines_status: dict[str, dict] = {}
    for name in exchanges:
        eng = engine_registry.get_engine(name)
        engines_status[name] = {
            "registered": eng is not None,
            "running": eng.is_running if eng is not None else False,
        }

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "exchanges_registered": exchanges,
        "engines": engines_status,
    }
