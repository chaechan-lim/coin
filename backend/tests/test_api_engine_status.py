"""
Tests for GET /api/v1/engine/status (api/dashboard.py).

Validates that EngineStatusResponse includes min_confidence from the combiner,
and falls back to 0.55 when no combiner is registered.
"""
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from api.dashboard import router as dashboard_router
from api.dependencies import engine_registry
from core.models import Base


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard_router)
    return app


def _mock_engine(*, running: bool = False, strategies: dict | None = None) -> MagicMock:
    eng = MagicMock()
    eng.is_running = running
    eng.strategies = strategies or {}
    eng.tracked_coins = []
    eng._ec = MagicMock()
    eng._ec.mode = "paper"
    eng._ec.evaluation_interval_sec = 300
    return eng


def _mock_combiner(min_confidence: float = 0.55) -> MagicMock:
    comb = MagicMock()
    comb.min_confidence = min_confidence
    return comb


def _save_and_clear(exchange: str):
    saved = {
        "engine": engine_registry._engines.get(exchange),
        "pm": engine_registry._portfolio_managers.get(exchange),
        "comb": engine_registry._combiners.get(exchange),
        "coord": engine_registry._coordinators.get(exchange),
    }
    return saved


def _register(name: str, engine, combiner=None) -> None:
    engine_registry._engines[name] = engine
    engine_registry._portfolio_managers[name] = None
    engine_registry._combiners[name] = combiner
    engine_registry._coordinators[name] = None


def _restore(name: str, saved: dict) -> None:
    for store, key in [
        (engine_registry._engines, "engine"),
        (engine_registry._portfolio_managers, "pm"),
        (engine_registry._combiners, "comb"),
        (engine_registry._coordinators, "coord"),
    ]:
        if saved[key] is None:
            store.pop(name, None)
        else:
            store[name] = saved[key]


async def _db_override():
    """Provide an in-memory SQLite session for the endpoint."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


# ── tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_engine_status_includes_min_confidence_default():
    """Engine status returns min_confidence=0.55 as default when no combiner."""
    exchange = "bithumb"
    saved = _save_and_clear(exchange)
    eng = _mock_engine(running=True)
    _register(exchange, eng, combiner=None)
    try:
        from db.session import get_db
        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/engine/status", params={"exchange": exchange})
        assert resp.status_code == 200
        data = resp.json()
        assert "min_confidence" in data
        assert data["min_confidence"] == pytest.approx(0.55)
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_engine_status_min_confidence_from_combiner():
    """Engine status returns min_confidence from the registered combiner."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = _mock_engine(running=True)
    comb = _mock_combiner(min_confidence=0.60)
    _register(exchange, eng, combiner=comb)
    try:
        from db.session import get_db
        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/engine/status", params={"exchange": exchange})
        assert resp.status_code == 200
        data = resp.json()
        assert data["min_confidence"] == pytest.approx(0.60)
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_engine_status_no_engine_returns_default_min_confidence():
    """When no engine is registered, min_confidence defaults to 0.55."""
    exchange = "binance_spot"
    saved = _save_and_clear(exchange)
    # Remove from registry to simulate not-started engine
    engine_registry._engines.pop(exchange, None)
    engine_registry._combiners.pop(exchange, None)
    try:
        from db.session import get_db
        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/engine/status", params={"exchange": exchange})
        assert resp.status_code == 200
        data = resp.json()
        assert "min_confidence" in data
        assert data["min_confidence"] == pytest.approx(0.55)
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_engine_status_strategies_active():
    """Engine status returns the names of all registered strategies."""
    exchange = "bithumb"
    saved = _save_and_clear(exchange)
    eng = _mock_engine(
        running=True,
        strategies={"rsi": MagicMock(), "bollinger_rsi": MagicMock()},
    )
    _register(exchange, eng)
    try:
        from db.session import get_db
        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/engine/status", params={"exchange": exchange})
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["strategies_active"]) == {"rsi", "bollinger_rsi"}
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_engine_status_schema_has_all_required_fields():
    """EngineStatusResponse schema includes all expected fields including min_confidence."""
    exchange = "bithumb"
    saved = _save_and_clear(exchange)
    eng = _mock_engine(running=False)
    _register(exchange, eng)
    try:
        from db.session import get_db
        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/engine/status", params={"exchange": exchange})
        assert resp.status_code == 200
        data = resp.json()
        for field in ("exchange", "is_running", "mode", "evaluation_interval_sec",
                      "tracked_coins", "daily_trade_count", "strategies_active",
                      "min_confidence"):
            assert field in data, f"Missing field: {field}"
    finally:
        _restore(exchange, saved)


# ── COIN-17: tier1-status endpoint tests ──────────────────────────────────────

def _mock_v2_engine() -> MagicMock:
    """V2 엔진 mock with get_tier1_status."""
    eng = MagicMock()
    eng.is_running = True
    eng.get_tier1_status = MagicMock(return_value={
        "cycle_count": 42,
        "last_cycle_at": "2026-03-16T12:00:00+00:00",
        "last_action_at": "2026-03-16T11:30:00+00:00",
        "coins": ["BTC/USDT", "ETH/USDT"],
        "active_positions": 1,
        "last_decisions": {"BTC/USDT": "hold", "ETH/USDT": "opened"},
        "regime": "trending_up",
    })
    return eng


@pytest.mark.asyncio
async def test_tier1_status_endpoint():
    """COIN-17: GET /engine/v2/tier1-status returns Tier1 operational state."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = _mock_v2_engine()
    _register(exchange, eng)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/engine/v2/tier1-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cycle_count"] == 42
        assert data["active_positions"] == 1
        assert data["regime"] == "trending_up"
        assert "BTC/USDT" in data["last_decisions"]
        eng.get_tier1_status.assert_called_once()
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_tier1_status_no_engine():
    """COIN-17: No engine → 500 error."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    engine_registry._engines.pop(exchange, None)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/engine/v2/tier1-status")
        assert resp.status_code == 500
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_tier1_status_v1_engine_no_support():
    """COIN-17: V1 engine without get_tier1_status → 400 error."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = _mock_engine(running=True)
    # V1 engine doesn't have get_tier1_status
    del eng.get_tier1_status
    _register(exchange, eng)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/engine/v2/tier1-status")
        assert resp.status_code == 400
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_tier1_status_required_fields():
    """COIN-17: tier1-status response has all expected fields."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = _mock_v2_engine()
    _register(exchange, eng)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/engine/v2/tier1-status")
        assert resp.status_code == 200
        data = resp.json()
        for field in ("cycle_count", "last_cycle_at", "last_action_at", "coins",
                      "active_positions", "last_decisions", "regime"):
            assert field in data, f"Missing field: {field}"
    finally:
        _restore(exchange, saved)
