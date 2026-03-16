"""
Tests for BalanceGuard API endpoints (api/dashboard.py).

- GET /engine/balance-guard/status
- POST /engine/balance-guard/resume
- POST /engine/balance-guard/sync
"""
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from api.dashboard import router as dashboard_router
from api.dependencies import engine_registry
from engine.balance_guard import BalanceGuard
from exchange.data_models import Balance


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard_router)
    return app


def _mock_guard(*, paused: bool = False) -> BalanceGuard:
    """Create a BalanceGuard with a mock exchange."""
    exchange = AsyncMock()
    exchange.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=100.0, used=50.0, total=150.0),
    })
    guard = BalanceGuard(
        exchange=exchange,
        exchange_name="binance_futures",
        warn_pct=3.0,
        pause_pct=5.0,
        auto_resume_count=3,
    )
    guard._paused = paused
    return guard


def _mock_engine_with_guard(guard: BalanceGuard) -> MagicMock:
    eng = MagicMock()
    eng.is_running = True
    type(eng).balance_guard = PropertyMock(return_value=guard)
    return eng


def _save_and_clear(exchange: str):
    return {
        "engine": engine_registry._engines.get(exchange),
        "pm": engine_registry._portfolio_managers.get(exchange),
        "comb": engine_registry._combiners.get(exchange),
        "coord": engine_registry._coordinators.get(exchange),
    }


def _register(name: str, engine) -> None:
    engine_registry._engines[name] = engine
    engine_registry._portfolio_managers[name] = None
    engine_registry._combiners[name] = None
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


# ── GET /engine/balance-guard/status ──────────────────────────────────────


@pytest.mark.asyncio
async def test_balance_guard_status():
    """정상 상태 조회."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    guard = _mock_guard(paused=False)
    eng = _mock_engine_with_guard(guard)
    _register(exchange, eng)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/engine/balance-guard/status", params={"exchange": exchange})
        assert resp.status_code == 200
        data = resp.json()
        assert data["exchange"] == exchange
        assert data["paused"] is False
        assert data["auto_resume_count"] == 3
        assert data["warn_pct"] == 3.0
        assert data["pause_pct"] == 5.0
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_balance_guard_status_paused():
    """일시 정지 상태 조회."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    guard = _mock_guard(paused=True)
    eng = _mock_engine_with_guard(guard)
    _register(exchange, eng)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/engine/balance-guard/status", params={"exchange": exchange})
        assert resp.status_code == 200
        data = resp.json()
        assert data["paused"] is True
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_balance_guard_status_no_engine():
    """엔진이 없으면 500."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    engine_registry._engines.pop(exchange, None)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/engine/balance-guard/status", params={"exchange": exchange})
        assert resp.status_code == 500
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_balance_guard_status_no_guard():
    """BalanceGuard가 없는 엔진 → 404."""
    exchange = "binance_spot"
    saved = _save_and_clear(exchange)
    eng = MagicMock()
    eng.is_running = True
    # balance_guard 속성 없음
    del eng.balance_guard
    del eng._guard
    _register(exchange, eng)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/engine/balance-guard/status", params={"exchange": exchange})
        assert resp.status_code == 404
    finally:
        _restore(exchange, saved)


# ── POST /engine/balance-guard/resume ─────────────────────────────────────


@pytest.mark.asyncio
async def test_balance_guard_resume_paused():
    """일시 정지 → 수동 재개."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    guard = _mock_guard(paused=True)
    eng = _mock_engine_with_guard(guard)
    _register(exchange, eng)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/engine/balance-guard/resume", params={"exchange": exchange})
        assert resp.status_code == 200
        data = resp.json()
        assert data["was_paused"] is True
        assert data["is_paused"] is False
        assert data["status"] == "resumed"
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_balance_guard_resume_not_paused():
    """이미 실행 중이면 already_running."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    guard = _mock_guard(paused=False)
    eng = _mock_engine_with_guard(guard)
    _register(exchange, eng)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/engine/balance-guard/resume", params={"exchange": exchange})
        assert resp.status_code == 200
        data = resp.json()
        assert data["was_paused"] is False
        assert data["status"] == "already_running"
    finally:
        _restore(exchange, saved)


# ── POST /engine/balance-guard/sync ───────────────────────────────────────


@pytest.mark.asyncio
async def test_balance_guard_sync():
    """내부 현금 → 거래소 잔고 동기화."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = MagicMock()
    eng.is_running = True
    eng.sync_balance_to_exchange = AsyncMock(return_value={
        "old_cash": 223.0,
        "new_cash": 260.0,
        "exchange_balance": 260.0,
    })
    type(eng).balance_guard = PropertyMock(return_value=_mock_guard())
    _register(exchange, eng)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/engine/balance-guard/sync", params={"exchange": exchange})
        assert resp.status_code == 200
        data = resp.json()
        assert data["exchange"] == exchange
        assert data["old_cash"] == 223.0
        assert data["new_cash"] == 260.0
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_balance_guard_sync_not_available():
    """sync 미지원 엔진 → 404."""
    exchange = "binance_spot"
    saved = _save_and_clear(exchange)
    eng = MagicMock(spec=[])  # no attributes
    eng.is_running = True
    _register(exchange, eng)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/engine/balance-guard/sync", params={"exchange": exchange})
        assert resp.status_code == 404
    finally:
        _restore(exchange, saved)
