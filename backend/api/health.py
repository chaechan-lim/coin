"""
Health check endpoint for the Coin auto-trading API.

Exposes GET /api/v1/health — returns engine registration status,
DB connectivity, and overall service readiness.
"""
from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import select

from api.dependencies import engine_registry
from db.session import get_session_factory

router = APIRouter(tags=["health"])


@router.get("/health")
async def api_health_check():
    """
    API-level health check.

    Returns:
    - ``status``: ``"ok"`` when DB is reachable, ``"degraded"`` otherwise
    - ``timestamp``: current UTC time in ISO-8601
    - ``db_connected``: whether the database is reachable
    - ``exchanges_registered``: list of exchange names with registered engines
    - ``engines``: per-exchange ``{registered, running}`` summary
    """
    # DB 연결 확인
    db_ok = True
    try:
        sf = get_session_factory()
        async with sf() as session:
            await session.execute(select(1))
    except Exception:
        db_ok = False

    exchanges = engine_registry.available_exchanges

    engines_status: dict[str, dict] = {}
    for name in exchanges:
        eng = engine_registry.get_engine(name)
        engines_status[name] = {
            "registered": eng is not None,
            "running": eng.is_running if eng is not None else False,
        }

    return {
        "status": "ok" if db_ok else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "db_connected": db_ok,
        "exchanges_registered": exchanges,
        "engines": engines_status,
    }
