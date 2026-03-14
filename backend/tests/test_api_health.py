"""
Tests for GET /api/v1/health (api/health.py).

All tests are unit-level: they mount the health router onto a minimal FastAPI
app and call it via httpx's ASGI transport — no real exchange connections.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from api.health import router as health_router
from api.dependencies import engine_registry


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_test_app() -> FastAPI:
    """Minimal FastAPI app with only the health router attached."""
    app = FastAPI()
    app.include_router(health_router)
    return app


def _mock_engine(*, running: bool = False) -> MagicMock:
    eng = MagicMock()
    eng.is_running = running
    return eng


def _register(name: str, engine) -> None:
    engine_registry._engines[name] = engine
    engine_registry._portfolio_managers[name] = None
    engine_registry._combiners[name] = None
    engine_registry._coordinators[name] = None


def _unregister(name: str) -> None:
    for store in (
        engine_registry._engines,
        engine_registry._portfolio_managers,
        engine_registry._combiners,
        engine_registry._coordinators,
    ):
        store.pop(name, None)


# ── tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_returns_200():
    """GET /health always responds with HTTP 200."""
    app = _make_test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_health_status_ok():
    """Response body contains status='ok'."""
    app = _make_test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    data = resp.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_health_has_required_keys():
    """Response body includes all expected top-level keys."""
    app = _make_test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    data = resp.json()
    assert "status" in data
    assert "timestamp" in data
    assert "exchanges_registered" in data
    assert "engines" in data


@pytest.mark.asyncio
async def test_health_timestamp_is_utc_iso():
    """Timestamp is a valid ISO-8601 string with timezone info."""
    app = _make_test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    ts_str = resp.json()["timestamp"]
    ts = datetime.fromisoformat(ts_str)
    assert ts.tzinfo is not None, "timestamp must carry timezone info"


@pytest.mark.asyncio
async def test_health_empty_registry():
    """Works correctly when no engines are registered."""
    saved = {
        k: dict(getattr(engine_registry, f"_{k}"))
        for k in ("engines", "portfolio_managers", "combiners", "coordinators")
    }
    engine_registry._engines.clear()
    engine_registry._portfolio_managers.clear()
    engine_registry._combiners.clear()
    engine_registry._coordinators.clear()

    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")
        data = resp.json()
        assert data["status"] == "ok"
        assert data["exchanges_registered"] == []
        assert data["engines"] == {}
    finally:
        engine_registry._engines.update(saved["engines"])
        engine_registry._portfolio_managers.update(saved["portfolio_managers"])
        engine_registry._combiners.update(saved["combiners"])
        engine_registry._coordinators.update(saved["coordinators"])


@pytest.mark.asyncio
async def test_health_running_engine_reflected():
    """A running engine is reported as running=True in response."""
    _register("test_running", _mock_engine(running=True))
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")
        data = resp.json()
        assert "test_running" in data["exchanges_registered"]
        assert data["engines"]["test_running"]["registered"] is True
        assert data["engines"]["test_running"]["running"] is True
    finally:
        _unregister("test_running")


@pytest.mark.asyncio
async def test_health_stopped_engine_reflected():
    """A stopped engine is reported as running=False in response."""
    _register("test_stopped", _mock_engine(running=False))
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")
        data = resp.json()
        assert data["engines"]["test_stopped"]["running"] is False
    finally:
        _unregister("test_stopped")


@pytest.mark.asyncio
async def test_health_multiple_engines():
    """All registered engines appear in the response."""
    _register("ex_a", _mock_engine(running=True))
    _register("ex_b", _mock_engine(running=False))
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")
        data = resp.json()
        for name in ("ex_a", "ex_b"):
            assert name in data["exchanges_registered"]
            assert name in data["engines"]
    finally:
        _unregister("ex_a")
        _unregister("ex_b")
