"""
Tests for COIN-66:
- Bug 1: ExchangeNameType validation on capital.py, dashboard.py, strategies.py
- Bug 2: WebSocket idle timeout with asyncio.wait_for
"""
import asyncio
import json
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from api.capital import router as capital_router
from api.dashboard import router as dashboard_router
from api.strategies import router as strategies_router
from api.dependencies import engine_registry
from core.models import Base


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_app(*routers) -> FastAPI:
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    return app


async def _db_override():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as sess:
        yield sess
    await engine.dispose()


def _save_and_clear(exchange: str):
    return {
        "engine": engine_registry._engines.get(exchange),
        "pm": engine_registry._portfolio_managers.get(exchange),
        "comb": engine_registry._combiners.get(exchange),
        "coord": engine_registry._coordinators.get(exchange),
    }


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


# ── Bug 1: capital.py exchange validation ─────────────────────────────────────


@pytest.mark.asyncio
async def test_capital_list_transactions_invalid_exchange_returns_422():
    """Invalid exchange → 422 Unprocessable Entity (not 500 KeyError)."""
    from db.session import get_db

    app = _make_app(capital_router)
    app.dependency_overrides[get_db] = _db_override
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/capital/transactions", params={"exchange": "invalid_exchange"}
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_capital_list_transactions_valid_exchange_accepted():
    """Valid exchange values are accepted (200 or other non-422)."""
    from db.session import get_db

    app = _make_app(capital_router)
    app.dependency_overrides[get_db] = _db_override
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/capital/transactions", params={"exchange": "binance_futures"}
        )
    assert resp.status_code != 422


@pytest.mark.asyncio
async def test_capital_summary_invalid_exchange_returns_422():
    """GET /capital/summary with invalid exchange → 422."""
    from db.session import get_db

    app = _make_app(capital_router)
    app.dependency_overrides[get_db] = _db_override
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/capital/summary", params={"exchange": "not_a_real_exchange"}
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_capital_summary_valid_exchanges_accepted():
    """All valid exchange values are accepted for /capital/summary."""
    from db.session import get_db

    app = _make_app(capital_router)
    app.dependency_overrides[get_db] = _db_override
    valid_exchanges = ["bithumb", "binance_futures", "binance_spot", "binance_surge"]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        for exchange in valid_exchanges:
            resp = await client.get(
                "/capital/summary", params={"exchange": exchange}
            )
            assert resp.status_code != 422, (
                f"Expected valid exchange '{exchange}' to be accepted, got 422"
            )


# ── Bug 1: dashboard.py exchange validation ───────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_engine_status_invalid_exchange_returns_422():
    """GET /engine/status with invalid exchange → 422."""
    from db.session import get_db

    app = _make_app(dashboard_router)
    app.dependency_overrides[get_db] = _db_override
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/engine/status", params={"exchange": "totally_fake"}
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_dashboard_engine_start_invalid_exchange_returns_422():
    """POST /engine/start with invalid exchange → 422."""
    app = _make_app(dashboard_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/engine/start", params={"exchange": "xyzexchange"}
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_dashboard_engine_stop_invalid_exchange_returns_422():
    """POST /engine/stop with invalid exchange → 422."""
    app = _make_app(dashboard_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/engine/stop", params={"exchange": "bad_exchange"}
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_dashboard_valid_exchange_not_422():
    """GET /engine/status with valid exchange is accepted (not 422)."""
    from db.session import get_db

    exchange = "bithumb"
    saved = _save_and_clear(exchange)
    engine_registry._engines.pop(exchange, None)
    engine_registry._combiners.pop(exchange, None)
    try:
        app = _make_app(dashboard_router)
        app.dependency_overrides[get_db] = _db_override
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/engine/status", params={"exchange": exchange}
            )
        assert resp.status_code != 422
    finally:
        _restore(exchange, saved)


# ── Bug 1: strategies.py exchange validation ──────────────────────────────────


@pytest.mark.asyncio
async def test_strategies_update_params_invalid_exchange_returns_422():
    """PUT /strategies/{name}/params with invalid exchange → 422."""
    app = _make_app(strategies_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/strategies/rsi/params",
            params={"exchange": "not_valid"},
            json={"params": {}},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_strategies_update_weight_invalid_exchange_returns_422():
    """PUT /strategies/{name}/weight with invalid exchange → 422."""
    app = _make_app(strategies_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/strategies/rsi/weight",
            params={"exchange": "garbage_exchange"},
            json={"weight": 0.5},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_strategies_update_params_valid_exchange_accepted():
    """PUT /strategies/{name}/params with valid exchange is not rejected by type validation."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    mock_strategy = MagicMock()
    mock_strategy.set_params = MagicMock()
    mock_strategy.get_params = MagicMock(return_value={})
    mock_engine = MagicMock()
    mock_engine.strategies = {"rsi": mock_strategy}
    engine_registry._engines[exchange] = mock_engine
    engine_registry._portfolio_managers[exchange] = None
    engine_registry._combiners[exchange] = None
    engine_registry._coordinators[exchange] = None
    try:
        app = _make_app(strategies_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/strategies/rsi/params",
                params={"exchange": exchange},
                json={"params": {}},
            )
        # Should not be 422 — valid exchange passes type check
        assert resp.status_code != 422
    finally:
        _restore(exchange, saved)


# ── Bug 2: WebSocket idle timeout ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_websocket_timeout_disconnects_idle_connection():
    """WebSocket handler disconnects after asyncio.TimeoutError (idle client)."""
    from api.websocket import router as ws_router, ws_manager

    app = _make_app(ws_router)

    # Track whether disconnect was called
    disconnect_called = False
    original_disconnect = ws_manager.disconnect

    async def _mock_disconnect(websocket):
        nonlocal disconnect_called
        disconnect_called = True
        await original_disconnect(websocket)

    ws_manager.disconnect = _mock_disconnect

    # Simulate timeout by patching asyncio.wait_for to raise TimeoutError immediately
    original_wf = asyncio.wait_for

    async def patched_wait_for(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError()

    asyncio.wait_for = patched_wait_for

    try:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            try:
                with client.websocket_connect("/ws/dashboard"):
                    pass
            except Exception:
                pass
    finally:
        asyncio.wait_for = original_wf
        ws_manager.disconnect = original_disconnect

    assert disconnect_called, "ws_manager.disconnect should have been called on timeout"


@pytest.mark.asyncio
async def test_websocket_timeout_constant_is_set():
    """_WS_RECEIVE_TIMEOUT is defined and is a positive number."""
    from api.websocket import _WS_RECEIVE_TIMEOUT

    assert isinstance(_WS_RECEIVE_TIMEOUT, (int, float))
    assert _WS_RECEIVE_TIMEOUT > 0


@pytest.mark.asyncio
async def test_websocket_ping_pong_handled_before_timeout():
    """A ping message gets a pong response without triggering disconnect."""
    from api.websocket import router as ws_router

    app = _make_app(ws_router)

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        with client.websocket_connect("/ws/dashboard") as ws:
            ws.send_text("ping")
            data = ws.receive_text()
            msg = json.loads(data)
            assert msg.get("event") == "pong"
