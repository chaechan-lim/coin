"""
Tests for agent timestamp improvements (COIN-37).

Validates:
- trade-review/latest DB fallback includes analyzed_at
- performance/latest DB fallback includes generated_at
- strategy-advice/latest DB fallback includes generated_at
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from api.dashboard import router as dashboard_router
from api.dependencies import engine_registry
from core.models import Base, AgentAnalysisLog


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard_router)
    return app


def _save_and_clear(exchange: str):
    return {
        "engine": engine_registry._engines.get(exchange),
        "pm": engine_registry._portfolio_managers.get(exchange),
        "comb": engine_registry._combiners.get(exchange),
        "coord": engine_registry._coordinators.get(exchange),
    }


def _register_no_coordinator(exchange: str) -> None:
    """Register a dummy engine with no coordinator (forces DB fallback)."""
    eng = MagicMock()
    eng.is_running = False
    engine_registry._engines[exchange] = eng
    engine_registry._portfolio_managers[exchange] = None
    engine_registry._combiners[exchange] = None
    engine_registry._coordinators[exchange] = None


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


async def _create_db():
    """Create an in-memory DB engine with all tables."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


# ── trade-review DB fallback ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trade_review_db_fallback_includes_analyzed_at():
    """trade-review/latest DB fallback injects analyzed_at from the log row."""
    exchange = "bithumb"
    saved = _save_and_clear(exchange)
    _register_no_coordinator(exchange)
    db_engine = await _create_db()
    try:
        factory = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)

        # Seed a trade_review log without analyzed_at in result JSON
        ts = datetime(2025, 3, 22, 0, 30, 0, tzinfo=timezone.utc)
        async with factory() as sess:
            log = AgentAnalysisLog(
                exchange=exchange,
                agent_name="trade_review",
                analysis_type="trade_review",
                result={
                    "total_trades": 5,
                    "insights": ["good"],
                    "recommendations": ["more"],
                },
                analyzed_at=ts,
            )
            sess.add(log)
            await sess.commit()

        async def _db_override():
            async with factory() as sess:
                yield sess

        from db.session import get_db
        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_override

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/agents/trade-review/latest", params={"exchange": exchange})

        assert resp.status_code == 200
        data = resp.json()
        assert "analyzed_at" in data
        # SQLite drops timezone; check prefix matches
        assert data["analyzed_at"].startswith("2025-03-22T00:30:00")
        assert data["total_trades"] == 5
    finally:
        _restore(exchange, saved)
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_trade_review_db_fallback_preserves_existing_analyzed_at():
    """If result JSON already has analyzed_at, it is not overwritten."""
    exchange = "bithumb"
    saved = _save_and_clear(exchange)
    _register_no_coordinator(exchange)
    db_engine = await _create_db()
    try:
        factory = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)

        original_at = "2025-03-21T12:00:00"
        ts = datetime(2025, 3, 22, 0, 30, 0, tzinfo=timezone.utc)
        async with factory() as sess:
            log = AgentAnalysisLog(
                exchange=exchange,
                agent_name="trade_review",
                analysis_type="trade_review",
                result={
                    "total_trades": 3,
                    "analyzed_at": original_at,
                    "insights": [],
                    "recommendations": [],
                },
                analyzed_at=ts,
            )
            sess.add(log)
            await sess.commit()

        async def _db_override():
            async with factory() as sess:
                yield sess

        from db.session import get_db
        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_override

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/agents/trade-review/latest", params={"exchange": exchange})

        assert resp.status_code == 200
        data = resp.json()
        # Original analyzed_at in result JSON preserved, not overwritten
        assert data["analyzed_at"] == original_at
    finally:
        _restore(exchange, saved)
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_trade_review_no_data_returns_fallback_message():
    """trade-review/latest with no data returns fallback message."""
    exchange = "bithumb"
    saved = _save_and_clear(exchange)
    _register_no_coordinator(exchange)
    db_engine = await _create_db()
    try:
        factory = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)

        async def _db_override():
            async with factory() as sess:
                yield sess

        from db.session import get_db
        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_override

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/agents/trade-review/latest", params={"exchange": exchange})

        assert resp.status_code == 200
        data = resp.json()
        assert "message" in data
        assert data["insights"] == []
    finally:
        _restore(exchange, saved)
        await db_engine.dispose()


# ── performance DB fallback ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_performance_db_fallback_includes_generated_at():
    """performance/latest DB fallback includes generated_at from analyzed_at."""
    exchange = "bithumb"
    saved = _save_and_clear(exchange)
    _register_no_coordinator(exchange)
    db_engine = await _create_db()
    try:
        factory = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)

        ts = datetime(2025, 3, 22, 21, 30, 0, tzinfo=timezone.utc)
        async with factory() as sess:
            log = AgentAnalysisLog(
                exchange=exchange,
                agent_name="performance_analytics",
                analysis_type="performance",
                result={"windows": {}, "insights": ["test"]},
                analyzed_at=ts,
            )
            sess.add(log)
            await sess.commit()

        async def _db_override():
            async with factory() as sess:
                yield sess

        from db.session import get_db
        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_override

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/agents/performance/latest", params={"exchange": exchange})

        assert resp.status_code == 200
        data = resp.json()
        assert "generated_at" in data
        assert data["generated_at"].startswith("2025-03-22T21:30:00")
        assert data["insights"] == ["test"]
    finally:
        _restore(exchange, saved)
        await db_engine.dispose()


# ── strategy-advice DB fallback ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_strategy_advice_db_fallback_includes_generated_at():
    """strategy-advice/latest DB fallback includes generated_at from analyzed_at."""
    exchange = "bithumb"
    saved = _save_and_clear(exchange)
    _register_no_coordinator(exchange)
    db_engine = await _create_db()
    try:
        factory = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)

        ts = datetime(2025, 3, 22, 22, 0, 0, tzinfo=timezone.utc)
        async with factory() as sess:
            log = AgentAnalysisLog(
                exchange=exchange,
                agent_name="strategy_advisor",
                analysis_type="strategy_advice",
                result={"suggestions": ["tune SL"]},
                analyzed_at=ts,
            )
            sess.add(log)
            await sess.commit()

        async def _db_override():
            async with factory() as sess:
                yield sess

        from db.session import get_db
        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_override

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/agents/strategy-advice/latest", params={"exchange": exchange})

        assert resp.status_code == 200
        data = resp.json()
        assert "generated_at" in data
        assert data["generated_at"].startswith("2025-03-22T22:00:00")
        assert data["suggestions"] == ["tune SL"]
    finally:
        _restore(exchange, saved)
        await db_engine.dispose()
