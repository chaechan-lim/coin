"""
Tests for GET /api/v1/engine/status (api/dashboard.py).

Validates that EngineStatusResponse includes min_confidence from the combiner,
and falls back to 0.55 when no combiner is registered.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from api.dashboard import router as dashboard_router
from api.dependencies import engine_registry
from core.models import Base, Order, ServerEvent
from research.stage_gate import ResearchStageGateService


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


def _save_shared(key: str):
    return engine_registry._shared.get(key)


def _restore_shared(key: str, saved):
    if saved is None:
        engine_registry._shared.pop(key, None)
    else:
        engine_registry._shared[key] = saved


async def _db_override():
    """Provide an in-memory SQLite session for the endpoint."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


async def _make_stage_service():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    service = ResearchStageGateService(factory, engine_registry=engine_registry)
    return engine, service


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
async def test_research_overview_reports_live_engine_state():
    """R&D overview exposes candidate catalog and live engine registration state."""
    exchange = "binance_donchian"
    saved = _save_and_clear(exchange)
    eng = _mock_engine(running=True)
    _register(exchange, eng)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/research/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert data["live_candidates"] >= 1
        item = next(i for i in data["items"] if i["key"] == "donchian_daily_spot")
        assert item["is_live_engine_registered"] is True
        assert item["is_live_engine_running"] is True
        assert item["stage"] == "live_rnd"
        assert item["catalog_stage"] == "live_rnd"
        assert item["execution_allowed"] is True
        assert item["stage_managed"] is True
        assert "production" in item["next_stages"]
        assert item["auto_review"]["recommended_stage"] == "live_rnd"
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_research_overview_reports_non_registered_candidate():
    """Candidates without active engine stay visible as research backlog."""
    exchange = "binance_pairs"
    saved = _save_and_clear(exchange)
    engine_registry._engines.pop(exchange, None)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/research/overview")
        assert resp.status_code == 200
        data = resp.json()
        item = next(i for i in data["items"] if i["key"] == "pairs_trading_futures")
        assert item["is_live_engine_registered"] is False
        assert item["is_live_engine_running"] is False
        assert item["stage"] == "candidate"
        assert item["catalog_stage"] == "candidate"
        assert item["execution_allowed"] is False
        assert item["stage_managed"] is True
        assert "shadow" in item["next_stages"]
        assert item["auto_review"]["recommended_stage"] in {"candidate", "shadow", "hold"}
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_research_overview_uses_bootstrap_effective_stage_for_live_rnd_candidates():
    key = "research_stage_gate_service"
    saved = _save_shared(key)
    engine, service = await _make_stage_service()
    engine_registry.set_shared(key, service)
    await service.ensure_bootstrap_states()
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/research/overview")
        assert resp.status_code == 200
        data = resp.json()
        item = next(i for i in data["items"] if i["key"] == "donchian_futures_bi")
        assert item["catalog_stage"] == "research"
        assert item["stage"] == "live_rnd"
        assert item["stage_source"] == "bootstrap"
        assert item["execution_allowed"] is True
        assert item["approved_by"] == "system_bootstrap"
    finally:
        _restore_shared(key, saved)
        await engine.dispose()


@pytest.mark.asyncio
async def test_research_auto_review_status_endpoint_returns_shared_service_status():
    key = "research_auto_review_service"
    saved = _save_shared(key)
    service = MagicMock()
    service.get_status.return_value = {
        "ready": True,
        "candidate_count": 7,
        "total_candidates": 7,
        "pending_candidates": 0,
        "last_refresh_at": "2026-04-09T10:10:40+00:00",
        "refresh_interval_sec": 900,
        "snapshot_age_sec": 5,
    }
    engine_registry._shared[key] = service
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/research/auto-review/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ready"] is True
        assert data["candidate_count"] == 7
        assert data["pending_candidates"] == 0
    finally:
        _restore_shared(key, saved)


@pytest.mark.asyncio
async def test_research_auto_review_status_endpoint_not_enabled():
    key = "research_auto_review_service"
    saved = _save_shared(key)
    engine_registry._shared.pop(key, None)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/research/auto-review/status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_enabled"
    finally:
        _restore_shared(key, saved)


@pytest.mark.asyncio
async def test_engine_start_blocked_by_stage_gate_for_non_execution_stage():
    exchange = "binance_donchian_futures"
    saved_exchange = _save_and_clear(exchange)
    saved_stage_service = _save_shared("research_stage_gate_service")
    eng = _mock_engine(running=False)
    eng.start = AsyncMock()
    _register(exchange, eng)
    stage_engine, stage_service = await _make_stage_service()
    engine_registry.set_shared("research_stage_gate_service", stage_service)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/engine/start", params={"exchange": exchange})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["error"] == "stage_gate_blocked"
        assert detail["candidate_key"] == "donchian_futures_bi"
        eng.start.assert_not_called()
    finally:
        _restore(exchange, saved_exchange)
        _restore_shared("research_stage_gate_service", saved_stage_service)
        await stage_engine.dispose()


@pytest.mark.asyncio
async def test_update_research_stage_stops_running_engine_when_execution_becomes_disallowed():
    exchange = "binance_pairs"
    saved_exchange = _save_and_clear(exchange)
    saved_stage_service = _save_shared("research_stage_gate_service")

    eng = _mock_engine(running=True)
    eng.stop = AsyncMock()
    _register(exchange, eng)

    stage_engine, stage_service = await _make_stage_service()
    engine_registry.set_shared("research_stage_gate_service", stage_service)
    await stage_service.ensure_bootstrap_states()
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put(
                "/research/candidates/pairs_trading_futures/stage",
                json={"stage": "shadow", "approved_by": "tester", "note": "demote for shadow run"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["effective_stage"] == "shadow"
        assert data["stage_source"] == "manual"
        assert data["execution_allowed"] is False
        eng.stop.assert_awaited_once()
    finally:
        _restore(exchange, saved_exchange)
        _restore_shared("research_stage_gate_service", saved_stage_service)
        await stage_engine.dispose()


@pytest.mark.asyncio
async def test_list_research_stages_returns_effective_stage_state():
    saved_stage_service = _save_shared("research_stage_gate_service")
    stage_engine, stage_service = await _make_stage_service()
    engine_registry.set_shared("research_stage_gate_service", stage_service)
    await stage_service.ensure_bootstrap_states()
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/research/stages")
        assert resp.status_code == 200
        data = resp.json()
        item = next(row for row in data if row["candidate_key"] == "pairs_trading_futures")
        assert item["catalog_stage"] == "candidate"
        assert item["effective_stage"] == "live_rnd"
        assert item["execution_allowed"] is True
        assert item["stage_source"] == "bootstrap"
    finally:
        _restore_shared("research_stage_gate_service", saved_stage_service)
        await stage_engine.dispose()


@pytest.mark.asyncio
async def test_research_stage_history_tracks_manual_transition():
    saved_stage_service = _save_shared("research_stage_gate_service")
    stage_engine, stage_service = await _make_stage_service()
    engine_registry.set_shared("research_stage_gate_service", stage_service)
    await stage_service.ensure_bootstrap_states({"pairs_trading_futures"})
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            update_resp = await client.put(
                "/research/candidates/pairs_trading_futures/stage",
                json={"stage": "shadow", "approved_by": "reviewer", "note": "pause for validation"},
            )
            assert update_resp.status_code == 200
            history_resp = await client.get(
                "/research/stage-history",
                params={"candidate_key": "pairs_trading_futures", "limit": 5},
            )
        assert history_resp.status_code == 200
        rows = history_resp.json()
        assert len(rows) == 1
        assert rows[0]["candidate_key"] == "pairs_trading_futures"
        assert rows[0]["from_stage"] == "live_rnd"
        assert rows[0]["to_stage"] == "shadow"
        assert rows[0]["approved_by"] == "reviewer"
        assert rows[0]["approval_note"] == "pause for validation"
    finally:
        _restore_shared("research_stage_gate_service", saved_stage_service)
        await stage_engine.dispose()


@pytest.mark.asyncio
async def test_pairs_auto_review_includes_live_execution_metric(monkeypatch):
    import research.evaluator as evaluator

    evaluator._review_cache.clear()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(timezone.utc)

    async with factory() as session:
        session.add_all(
            [
                Order(
                    exchange="binance_pairs",
                    symbol="BTC/USDT",
                    side="buy",
                    order_type="market",
                    status="filled",
                    requested_price=70000.0,
                    executed_price=70000.0,
                    requested_quantity=0.001,
                    executed_quantity=0.001,
                    fee=0.03,
                    is_paper=False,
                    direction="long",
                    leverage=2,
                    margin_used=25.0,
                    entry_price=70000.0,
                    strategy_name="pairs_trading_live",
                    signal_confidence=1.0,
                    signal_reason="pairs_entry:trade=t-live-1:pair_direction=long_a_short_b:leg=a:exchange=binance_pairs",
                    trade_group_id="t-live-1",
                    trade_group_type="pairs_entry",
                    created_at=now,
                    filled_at=now,
                ),
                Order(
                    exchange="binance_pairs",
                    symbol="ETH/USDT",
                    side="sell",
                    order_type="market",
                    status="filled",
                    requested_price=3000.0,
                    executed_price=3000.0,
                    requested_quantity=0.02,
                    executed_quantity=0.02,
                    fee=0.03,
                    is_paper=False,
                    direction="short",
                    leverage=2,
                    margin_used=25.0,
                    entry_price=3000.0,
                    strategy_name="pairs_trading_live",
                    signal_confidence=1.0,
                    signal_reason="pairs_entry:trade=t-live-1:pair_direction=long_a_short_b:leg=b:exchange=binance_pairs",
                    trade_group_id="t-live-1",
                    trade_group_type="pairs_entry",
                    created_at=now,
                    filled_at=now,
                ),
                Order(
                    exchange="binance_pairs",
                    symbol="BTC/USDT",
                    side="sell",
                    order_type="market",
                    status="filled",
                    requested_price=71000.0,
                    executed_price=71000.0,
                    requested_quantity=0.001,
                    executed_quantity=0.001,
                    fee=0.03,
                    is_paper=False,
                    direction="long",
                    leverage=2,
                    margin_used=25.0,
                    entry_price=70000.0,
                    realized_pnl=0.94,
                    realized_pnl_pct=1.34,
                    strategy_name="pairs_trading_live",
                    signal_confidence=1.0,
                    signal_reason="pairs_exit:trade=t-live-1:pair_direction=long_a_short_b:leg=a:exchange=binance_pairs",
                    trade_group_id="t-live-1",
                    trade_group_type="pairs_exit",
                    created_at=now,
                    filled_at=now,
                ),
                Order(
                    exchange="binance_pairs",
                    symbol="ETH/USDT",
                    side="buy",
                    order_type="market",
                    status="filled",
                    requested_price=2950.0,
                    executed_price=2950.0,
                    requested_quantity=0.02,
                    executed_quantity=0.02,
                    fee=0.03,
                    is_paper=False,
                    direction="short",
                    leverage=2,
                    margin_used=25.0,
                    entry_price=3000.0,
                    realized_pnl=0.94,
                    realized_pnl_pct=1.69,
                    strategy_name="pairs_trading_live",
                    signal_confidence=1.0,
                    signal_reason="pairs_exit:trade=t-live-1:pair_direction=long_a_short_b:leg=b:exchange=binance_pairs",
                    trade_group_id="t-live-1",
                    trade_group_type="pairs_exit",
                    created_at=now,
                    filled_at=now,
                ),
                ServerEvent(
                    level="info",
                    category="pairs_trade",
                    title="Pairs entry opened",
                    detail=None,
                    metadata_={"trade_id": "t-live-1", "exchange": "binance_pairs", "stage": "entry_opened"},
                    created_at=now,
                ),
                ServerEvent(
                    level="info",
                    category="pairs_trade",
                    title="Pairs exit closed",
                    detail=None,
                    metadata_={"trade_id": "t-live-1", "exchange": "binance_pairs", "stage": "exit_closed"},
                    created_at=now,
                ),
            ]
        )
        await session.commit()

    monkeypatch.setattr(evaluator, "get_session_factory", lambda: factory)
    try:
        review = await evaluator.get_auto_review(
            "pairs_trading_futures",
            live_context={"live_capital_usdt": 50.0},
        )
    finally:
        evaluator._review_cache.clear()
        await engine.dispose()

    live_metric = next(metric for metric in review.metrics if metric.source == "pairs_live_execution")
    assert live_metric.trade_count == 1
    assert live_metric.extra["closed_groups"] == 1
    assert live_metric.extra["failed_event_count"] == 0


@pytest.mark.asyncio
async def test_pairs_auto_review_demotes_when_live_kpi_is_bad(monkeypatch):
    import research.evaluator as evaluator

    evaluator._review_cache.clear()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(timezone.utc)

    async with factory() as session:
        for idx in range(4):
            trade_id = f"t-bad-{idx}"
            session.add_all(
                [
                    Order(
                        exchange="binance_pairs",
                        symbol="BTC/USDT",
                        side="buy",
                        order_type="market",
                        status="filled",
                        requested_price=70000.0,
                        executed_price=70000.0,
                        requested_quantity=0.001,
                        executed_quantity=0.001,
                        fee=0.03,
                        is_paper=False,
                        direction="long",
                        leverage=2,
                        margin_used=25.0,
                        entry_price=70000.0,
                        strategy_name="pairs_trading_live",
                        signal_confidence=1.0,
                        signal_reason=f"pairs_entry:trade={trade_id}:pair_direction=long_a_short_b:leg=a:exchange=binance_pairs",
                        trade_group_id=trade_id,
                        trade_group_type="pairs_entry",
                        created_at=now,
                        filled_at=now,
                    ),
                    Order(
                        exchange="binance_pairs",
                        symbol="ETH/USDT",
                        side="sell",
                        order_type="market",
                        status="filled",
                        requested_price=3000.0,
                        executed_price=3000.0,
                        requested_quantity=0.02,
                        executed_quantity=0.02,
                        fee=0.03,
                        is_paper=False,
                        direction="short",
                        leverage=2,
                        margin_used=25.0,
                        entry_price=3000.0,
                        strategy_name="pairs_trading_live",
                        signal_confidence=1.0,
                        signal_reason=f"pairs_entry:trade={trade_id}:pair_direction=long_a_short_b:leg=b:exchange=binance_pairs",
                        trade_group_id=trade_id,
                        trade_group_type="pairs_entry",
                        created_at=now,
                        filled_at=now,
                    ),
                    Order(
                        exchange="binance_pairs",
                        symbol="BTC/USDT",
                        side="sell",
                        order_type="market",
                        status="filled",
                        requested_price=69000.0,
                        executed_price=69000.0,
                        requested_quantity=0.001,
                        executed_quantity=0.001,
                        fee=0.03,
                        is_paper=False,
                        direction="long",
                        leverage=2,
                        margin_used=25.0,
                        entry_price=70000.0,
                        realized_pnl=-1.8,
                        realized_pnl_pct=-1.4,
                        strategy_name="pairs_trading_live",
                        signal_confidence=1.0,
                        signal_reason=f"pairs_exit:trade={trade_id}:pair_direction=long_a_short_b:leg=a:exchange=binance_pairs",
                        trade_group_id=trade_id,
                        trade_group_type="pairs_exit",
                        created_at=now,
                        filled_at=now,
                    ),
                    Order(
                        exchange="binance_pairs",
                        symbol="ETH/USDT",
                        side="buy",
                        order_type="market",
                        status="filled",
                        requested_price=3050.0,
                        executed_price=3050.0,
                        requested_quantity=0.02,
                        executed_quantity=0.02,
                        fee=0.03,
                        is_paper=False,
                        direction="short",
                        leverage=2,
                        margin_used=25.0,
                        entry_price=3000.0,
                        realized_pnl=-1.8,
                        realized_pnl_pct=-1.6,
                        strategy_name="pairs_trading_live",
                        signal_confidence=1.0,
                        signal_reason=f"pairs_exit:trade={trade_id}:pair_direction=long_a_short_b:leg=b:exchange=binance_pairs",
                        trade_group_id=trade_id,
                        trade_group_type="pairs_exit",
                        created_at=now,
                        filled_at=now,
                    ),
                    ServerEvent(
                        level="warning",
                        category="pairs_trade",
                        title="Pairs entry rollback",
                        detail=None,
                        metadata_={"trade_id": trade_id, "exchange": "binance_pairs", "stage": "entry_leg_rollback"},
                        created_at=now,
                    ),
                ]
            )
        await session.commit()

    monkeypatch.setattr(evaluator, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        evaluator,
        "simulate_pairs_trading",
        lambda *args, **kwargs: SimpleNamespace(return_pct=12.0, sharpe=1.4, max_drawdown=8.0, n_trades=24),
    )
    try:
        review = await evaluator.get_auto_review(
            "pairs_trading_futures",
            live_context={"live_capital_usdt": 50.0},
        )
    finally:
        evaluator._review_cache.clear()
        await engine.dispose()

    assert review.decision == "demote"
    assert review.recommended_stage == "hold"
    assert any("rollback" in blocker or "음수" in blocker for blocker in review.blockers)


@pytest.mark.asyncio
async def test_pairs_status_endpoint_returns_engine_status():
    exchange = "binance_pairs"
    saved = _save_and_clear(exchange)
    eng = _mock_engine(running=True)
    eng.get_status = MagicMock(return_value={
        "exchange": exchange,
        "is_running": True,
        "capital_usdt": 75.0,
        "last_evaluated_at": "2026-04-11T00:05:00+00:00",
        "next_evaluation_at": "2026-04-11T01:05:00+00:00",
        "recent_idle_reason": "진입 조건 대기 중 (|z|=1.42 < 2.00)",
    })
    _register(exchange, eng)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/engine/pairs/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["exchange"] == exchange
        assert data["is_running"] is True
        assert data["last_evaluated_at"] == "2026-04-11T00:05:00+00:00"
        assert data["next_evaluation_at"] == "2026-04-11T01:05:00+00:00"
        assert "진입 조건 대기" in data["recent_idle_reason"]
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_pairs_evaluate_endpoint_triggers_engine():
    exchange = "binance_pairs"
    saved = _save_and_clear(exchange)
    eng = _mock_engine(running=True)
    eng.evaluate_now = AsyncMock()
    eng.get_status = MagicMock(return_value={"exchange": exchange, "is_running": True})
    _register(exchange, eng)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/engine/pairs/evaluate")
        assert resp.status_code == 200
        assert resp.json()["status"] == "evaluated"
        eng.evaluate_now.assert_awaited_once()
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_futures_rnd_status_endpoint_returns_shared_coordinator():
    key = "futures_rnd_coordinator"
    saved = _save_shared(key)
    coordinator = MagicMock()
    coordinator.get_status = AsyncMock(return_value={
        "global_capital_usdt": 150.0,
        "entry_paused": False,
        "reserved_symbols": {"BTC/USDT": "binance_pairs"},
    })
    engine_registry._shared[key] = coordinator
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/engine/futures-rnd/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["global_capital_usdt"] == 150.0
        assert data["reserved_symbols"]["BTC/USDT"] == "binance_pairs"
        coordinator.get_status.assert_awaited_once()
    finally:
        _restore_shared(key, saved)


@pytest.mark.asyncio
async def test_futures_rnd_status_endpoint():
    saved = engine_registry.get_shared("futures_rnd_coordinator")

    coordinator = MagicMock()
    coordinator.get_status = AsyncMock(return_value={
        "global_capital_usdt": 150.0,
        "global_reserved_margin": 75.0,
        "entry_paused": False,
        "reserved_symbols": {"BTC/USDT": "binance_pairs"},
        "engines": {"binance_pairs": {"confirmed_margin": 75.0}},
    })
    engine_registry.set_shared("futures_rnd_coordinator", coordinator)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/engine/futures-rnd/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["global_capital_usdt"] == 150.0
        assert data["reserved_symbols"]["BTC/USDT"] == "binance_pairs"
    finally:
        engine_registry.set_shared("futures_rnd_coordinator", saved)


@pytest.mark.asyncio
async def test_research_overview_donchian_futures_bi_has_auto_review():
    """Bi-directional Donchian candidate should no longer be marked insufficient_data."""
    app = _make_test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/research/overview")
    assert resp.status_code == 200
    data = resp.json()
    item = next(i for i in data["items"] if i["key"] == "donchian_futures_bi")
    assert item["auto_review"]["decision"] in {"keep", "promote"}
    assert item["auto_review"]["recommended_stage"] in {"research", "candidate"}


@pytest.mark.asyncio
async def test_donchian_futures_auto_review_includes_live_execution_metric(monkeypatch):
    import research.evaluator as evaluator

    evaluator._review_cache.clear()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(timezone.utc)

    async with factory() as session:
        session.add_all(
            [
                Order(
                    exchange="binance_donchian_futures",
                    symbol="BTC/USDT",
                    side="sell",
                    order_type="market",
                    status="filled",
                    requested_price=70000.0,
                    executed_price=70000.0,
                    requested_quantity=0.001,
                    executed_quantity=0.001,
                    fee=0.03,
                    is_paper=False,
                    direction="short",
                    leverage=2,
                    margin_used=25.0,
                    entry_price=70000.0,
                    strategy_name="donchian_futures_bi",
                    signal_confidence=1.0,
                    signal_reason="donchian_futures_bi_entry:trade=d-live-1:symbol=BTC/USDT:direction=short:exchange=binance_donchian_futures",
                    trade_group_id="d-live-1",
                    trade_group_type="donchian_futures_entry",
                    created_at=now,
                    filled_at=now,
                ),
                Order(
                    exchange="binance_donchian_futures",
                    symbol="BTC/USDT",
                    side="buy",
                    order_type="market",
                    status="filled",
                    requested_price=68000.0,
                    executed_price=68000.0,
                    requested_quantity=0.001,
                    executed_quantity=0.001,
                    fee=0.03,
                    is_paper=False,
                    direction="short",
                    leverage=2,
                    margin_used=25.0,
                    entry_price=70000.0,
                    realized_pnl=1.97,
                    realized_pnl_pct=2.85,
                    strategy_name="donchian_futures_bi",
                    signal_confidence=1.0,
                    signal_reason="donchian_futures_bi_exit:trade=d-live-1:symbol=BTC/USDT:direction=short:exchange=binance_donchian_futures",
                    trade_group_id="d-live-1",
                    trade_group_type="donchian_futures_exit",
                    created_at=now,
                    filled_at=now,
                ),
                ServerEvent(
                    level="info",
                    category="donchian_futures_trade",
                    title="Donchian futures entry opened",
                    detail=None,
                    metadata_={"trade_id": "d-live-1", "exchange": "binance_donchian_futures", "stage": "entry_opened"},
                    created_at=now,
                ),
                ServerEvent(
                    level="info",
                    category="donchian_futures_trade",
                    title="Donchian futures exit closed",
                    detail=None,
                    metadata_={"trade_id": "d-live-1", "exchange": "binance_donchian_futures", "stage": "exit_closed"},
                    created_at=now,
                ),
            ]
        )
        await session.commit()

    monkeypatch.setattr(evaluator, "get_session_factory", lambda: factory)
    try:
        review = await evaluator.get_auto_review(
            "donchian_futures_bi",
            live_context={"live_capital_usdt": 100.0},
        )
    finally:
        evaluator._review_cache.clear()
        await engine.dispose()

    live_metric = next(metric for metric in review.metrics if metric.source == "donchian_futures_live_execution")
    assert live_metric.trade_count == 1
    assert live_metric.extra["short_closed_groups"] == 1
    assert live_metric.extra["failed_event_count"] == 0


@pytest.mark.asyncio
async def test_donchian_futures_live_metric_has_legacy_fallback_without_trade_group(monkeypatch):
    import research.evaluator as evaluator

    evaluator._review_cache.clear()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(timezone.utc)

    async with factory() as session:
        session.add(
            Order(
                exchange="binance_donchian_futures",
                symbol="BTC/USDT",
                side="buy",
                order_type="market",
                status="filled",
                requested_price=68000.0,
                executed_price=68000.0,
                requested_quantity=0.001,
                executed_quantity=0.001,
                fee=0.02,
                is_paper=False,
                direction="short",
                leverage=2,
                margin_used=35.0,
                entry_price=70000.0,
                realized_pnl=1.98,
                realized_pnl_pct=2.85,
                strategy_name="donchian_futures_bi",
                signal_confidence=1.0,
                signal_reason="donchian_futures_bi_exit:symbol=BTC/USDT:direction=short",
                created_at=now,
                filled_at=now,
            )
        )
        await session.commit()

    monkeypatch.setattr(evaluator, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        evaluator,
        "simulate_donchian_bi_directional",
        lambda *args, **kwargs: SimpleNamespace(
            return_pct=3.0,
            sharpe=1.0,
            max_drawdown=4.0,
            n_trades=12,
            short_trades=6,
            bh_return=-10.0,
        ),
    )
    try:
        review = await evaluator.get_auto_review(
            "donchian_futures_bi",
            live_context={"live_capital_usdt": 100.0},
        )
    finally:
        evaluator._review_cache.clear()
        await engine.dispose()

    live_metric = next(metric for metric in review.metrics if metric.source == "donchian_futures_live_execution")
    assert live_metric.trade_count == 1
    assert live_metric.extra["closed_groups"] == 1


@pytest.mark.asyncio
async def test_research_overview_hmm_has_auto_review():
    app = _make_test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/research/overview")
    assert resp.status_code == 200
    data = resp.json()
    item = next(i for i in data["items"] if i["key"] == "hmm_regime_detection")
    assert item["auto_review"]["decision"] in {"keep", "promote"}
    assert item["auto_review"]["recommended_stage"] in {"research", "candidate"}


@pytest.mark.asyncio
async def test_research_overview_volatility_adaptive_has_auto_review():
    app = _make_test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/research/overview")
    assert resp.status_code == 200
    data = resp.json()
    item = next(i for i in data["items"] if i["key"] == "volatility_adaptive_trend")
    assert item["auto_review"]["decision"] in {"keep", "promote"}
    assert item["auto_review"]["recommended_stage"] in {"research", "candidate"}


def test_binance_trading_config_has_enabled_flag():
    from config import BinanceTradingConfig

    cfg = BinanceTradingConfig()
    assert hasattr(cfg, "enabled")


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


# ── COIN-15: balance-guard endpoints tests ───────────────────────────────────

def _mock_v2_engine_with_balance_guard(*, paused: bool = False) -> MagicMock:
    """V2 엔진 mock with balance guard methods."""
    eng = MagicMock()
    eng.is_running = True
    eng.resume_balance_guard = MagicMock(return_value={
        "was_paused": paused,
        "is_paused": False,
        "guard": {
            "is_paused": False,
            "consecutive_warnings": 0,
            "consecutive_stable": 0,
            "auto_resume_stable_count": 3,
            "warn_pct": 3.0,
            "pause_pct": 5.0,
            "last_check": None,
        },
    })
    eng.get_balance_guard_status = MagicMock(return_value={
        "is_paused": paused,
        "consecutive_warnings": 0,
        "consecutive_stable": 0,
        "auto_resume_stable_count": 3,
        "warn_pct": 3.0,
        "pause_pct": 5.0,
        "last_check": None,
    })
    return eng


@pytest.mark.asyncio
async def test_balance_guard_resume_endpoint():
    """COIN-15: POST /engine/balance-guard/resume resumes the guard."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = _mock_v2_engine_with_balance_guard(paused=True)
    _register(exchange, eng)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/engine/balance-guard/resume",
                params={"exchange": exchange},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "resumed"
        assert data["exchange"] == exchange
        assert data["was_paused"] is True
        assert data["is_paused"] is False
        eng.resume_balance_guard.assert_called_once()
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_balance_guard_resume_no_engine():
    """COIN-15: No engine → 500 error."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    engine_registry._engines.pop(exchange, None)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/engine/balance-guard/resume",
                params={"exchange": exchange},
            )
        assert resp.status_code == 500
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_balance_guard_resume_unsupported_engine():
    """COIN-15: Engine without resume_balance_guard → 400 error."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = _mock_engine(running=True)
    del eng.resume_balance_guard
    _register(exchange, eng)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/engine/balance-guard/resume",
                params={"exchange": exchange},
            )
        assert resp.status_code == 400
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_balance_guard_status_endpoint():
    """COIN-15: GET /engine/balance-guard/status returns guard state."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = _mock_v2_engine_with_balance_guard(paused=False)
    _register(exchange, eng)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/engine/balance-guard/status",
                params={"exchange": exchange},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exchange"] == exchange
        assert data["is_paused"] is False
        assert "consecutive_stable" in data
        assert "auto_resume_stable_count" in data
        eng.get_balance_guard_status.assert_called_once()
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_balance_guard_status_no_engine():
    """COIN-15: No engine for status → 500 error."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    engine_registry._engines.pop(exchange, None)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/engine/balance-guard/status",
                params={"exchange": exchange},
            )
        assert resp.status_code == 500
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_balance_guard_status_unsupported_engine():
    """COIN-15: Engine without get_balance_guard_status → 400 error."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = _mock_engine(running=True)
    del eng.get_balance_guard_status
    _register(exchange, eng)
    try:
        app = _make_test_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/engine/balance-guard/status",
                params={"exchange": exchange},
            )
        assert resp.status_code == 400
    finally:
        _restore(exchange, saved)
