"""
COIN-53: MarketAnalysisAgent 비활성화 테스트.

검증 항목:
1. MARKET_ANALYSIS_ENABLED=False 시 coordinator.run_market_analysis()가 즉시 반환
2. API /agents/market-analysis/latest 가 disabled=True 포함해서 반환
3. scheduler.setup_scheduler() 가 market_analysis job을 추가하지 않음
"""

import os

os.environ.setdefault("DB_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("EXCHANGE_API_KEY", "test")
os.environ.setdefault("EXCHANGE_API_SECRET", "test")
os.environ.setdefault("TRADING_MODE", "paper")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from api.dashboard import router as dashboard_router
from api.dependencies import engine_registry
from core.models import Base
from core.enums import MarketState


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard_router)
    return app


async def _db_override():
    """In-memory SQLite for endpoint tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as sess:
        yield sess
    await engine.dispose()


def _save_registry(exchange: str):
    return {
        "engine": engine_registry._engines.get(exchange),
        "pm": engine_registry._portfolio_managers.get(exchange),
        "comb": engine_registry._combiners.get(exchange),
        "coord": engine_registry._coordinators.get(exchange),
    }


def _restore_registry(exchange: str, saved: dict) -> None:
    for store, key in [
        (engine_registry._engines, "engine"),
        (engine_registry._portfolio_managers, "pm"),
        (engine_registry._combiners, "comb"),
        (engine_registry._coordinators, "coord"),
    ]:
        if saved[key] is None:
            store.pop(exchange, None)
        else:
            store[exchange] = saved[key]


def _make_coordinator(with_analysis: bool = False):
    """Make a mock coordinator with or without cached analysis."""
    from agents.market_analysis import MarketAnalysis

    coord = MagicMock()
    if with_analysis:
        coord.last_market_analysis = MarketAnalysis(
            state=MarketState.UPTREND,
            confidence=0.75,
            volatility_level="medium",
            reasoning="Test reasoning",
            indicators={"rsi": 55.0},
            recommended_weights={"bollinger_rsi": 0.26, "rsi": 0.21},
        )
    else:
        coord.last_market_analysis = None
    return coord


# ── Test 1: Coordinator disabled early return ─────────────────────────────────


class TestCoordinatorMarketAnalysisDisabled:
    """COIN-53: MARKET_ANALYSIS_ENABLED=False 시 run_market_analysis가 즉시 반환."""

    @pytest.mark.asyncio
    async def test_disabled_returns_none_when_no_cache(self):
        """비활성 + 캐시 없음 → None 반환, market_agent.analyze() 호출 안 함."""
        import agents.coordinator as coord_module

        from agents.coordinator import AgentCoordinator
        from agents.risk_management import RiskManagementAgent
        from strategies.combiner import SignalCombiner

        market_agent = MagicMock()
        market_agent.analyze = AsyncMock()
        risk_agent = MagicMock(spec=RiskManagementAgent)
        combiner = MagicMock(spec=SignalCombiner)

        coord = AgentCoordinator(
            market_agent=market_agent,
            risk_agent=risk_agent,
            combiner=combiner,
            exchange_name="binance_futures",
        )

        # Patch the flag to False (already False by default, but explicit)
        with patch.object(coord_module, "MARKET_ANALYSIS_ENABLED", False):
            result = await coord.run_market_analysis()

        assert result is None
        market_agent.analyze.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_returns_cached_when_cache_exists(self):
        """비활성 + 캐시 있음 → 캐시 반환, market_agent.analyze() 호출 안 함."""
        import agents.coordinator as coord_module
        from agents.coordinator import AgentCoordinator
        from agents.market_analysis import MarketAnalysis
        from agents.risk_management import RiskManagementAgent
        from strategies.combiner import SignalCombiner

        market_agent = MagicMock()
        market_agent.analyze = AsyncMock()
        risk_agent = MagicMock(spec=RiskManagementAgent)
        combiner = MagicMock(spec=SignalCombiner)

        coord = AgentCoordinator(
            market_agent=market_agent,
            risk_agent=risk_agent,
            combiner=combiner,
            exchange_name="bithumb",
        )
        # Inject cached analysis
        cached = MarketAnalysis(
            state=MarketState.SIDEWAYS,
            confidence=0.6,
            volatility_level="low",
            reasoning="cached",
            indicators={},
            recommended_weights={},
        )
        coord._last_market_analysis = cached

        with patch.object(coord_module, "MARKET_ANALYSIS_ENABLED", False):
            result = await coord.run_market_analysis()

        assert result is cached
        assert result.state == MarketState.SIDEWAYS
        market_agent.analyze.assert_not_called()

    @pytest.mark.asyncio
    async def test_enabled_calls_analyze(self):
        """MARKET_ANALYSIS_ENABLED=True 시 market_agent.analyze() 호출됨."""
        import agents.coordinator as coord_module
        from agents.coordinator import AgentCoordinator
        from agents.market_analysis import MarketAnalysis
        from agents.risk_management import RiskManagementAgent
        from strategies.combiner import SignalCombiner

        expected = MarketAnalysis(
            state=MarketState.UPTREND,
            confidence=0.8,
            volatility_level="medium",
            reasoning="ok",
            indicators={},
            recommended_weights={},
        )

        market_agent = MagicMock()
        market_agent.analyze = AsyncMock(return_value=expected)
        risk_agent = MagicMock(spec=RiskManagementAgent)
        combiner = MagicMock(spec=SignalCombiner)

        coord = AgentCoordinator(
            market_agent=market_agent,
            risk_agent=risk_agent,
            combiner=combiner,
            exchange_name="bithumb",
        )

        # Patch session factory to avoid DB calls
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_factory = MagicMock(return_value=mock_session)

        with patch.object(coord_module, "MARKET_ANALYSIS_ENABLED", True), \
             patch("agents.coordinator.get_session_factory", return_value=mock_factory), \
             patch("agents.coordinator.emit_event", new_callable=AsyncMock):
            result = await coord.run_market_analysis()

        market_agent.analyze.assert_called_once()
        assert result is not None
        assert result.state == MarketState.UPTREND


# ── Test 2: API returns disabled=True ────────────────────────────────────────


class TestMarketAnalysisAPIDisabledFlag:
    """COIN-53: /agents/market-analysis/latest 가 disabled=True 반환."""

    @pytest.mark.asyncio
    async def test_api_returns_disabled_true_with_coord_cache(self):
        """코디네이터 캐시 있고 비활성 → disabled=True 반환."""
        import agents.coordinator as coord_module

        exchange = "binance_futures"
        saved = _save_registry(exchange)
        coord = _make_coordinator(with_analysis=True)
        engine_registry._engines[exchange] = None
        engine_registry._portfolio_managers[exchange] = None
        engine_registry._combiners[exchange] = None
        engine_registry._coordinators[exchange] = coord

        try:
            from db.session import get_db

            app = _make_test_app()
            app.dependency_overrides[get_db] = _db_override
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with patch.object(coord_module, "MARKET_ANALYSIS_ENABLED", False):
                    resp = await client.get(
                        "/agents/market-analysis/latest",
                        params={"exchange": exchange},
                    )
            assert resp.status_code == 200
            data = resp.json()
            assert data["disabled"] is True
            assert "disabled_reason" in data
            assert data["disabled_reason"]  # non-empty string
        finally:
            _restore_registry(exchange, saved)

    @pytest.mark.asyncio
    async def test_api_returns_disabled_false_when_enabled(self):
        """MARKET_ANALYSIS_ENABLED=True 시 disabled=False 반환."""
        import agents.coordinator as coord_module

        exchange = "binance_futures"
        saved = _save_registry(exchange)
        coord = _make_coordinator(with_analysis=True)
        engine_registry._engines[exchange] = None
        engine_registry._portfolio_managers[exchange] = None
        engine_registry._combiners[exchange] = None
        engine_registry._coordinators[exchange] = coord

        try:
            from db.session import get_db

            app = _make_test_app()
            app.dependency_overrides[get_db] = _db_override
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with patch.object(coord_module, "MARKET_ANALYSIS_ENABLED", True):
                    resp = await client.get(
                        "/agents/market-analysis/latest",
                        params={"exchange": exchange},
                    )
            assert resp.status_code == 200
            data = resp.json()
            assert data["disabled"] is False
        finally:
            _restore_registry(exchange, saved)

    @pytest.mark.asyncio
    async def test_api_returns_disabled_true_no_coord_no_db(self):
        """코디네이터/DB 모두 없을 때도 disabled=True 포함 반환."""
        import agents.coordinator as coord_module

        exchange = "bithumb"
        saved = _save_registry(exchange)
        # No coordinator
        engine_registry._coordinators.pop(exchange, None)

        try:
            from db.session import get_db

            app = _make_test_app()
            app.dependency_overrides[get_db] = _db_override
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with patch.object(coord_module, "MARKET_ANALYSIS_ENABLED", False):
                    resp = await client.get(
                        "/agents/market-analysis/latest",
                        params={"exchange": exchange},
                    )
            assert resp.status_code == 200
            data = resp.json()
            assert data["disabled"] is True
            assert data.get("state") == "unknown"
        finally:
            _restore_registry(exchange, saved)


# ── Test 3: Scheduler skips market_analysis job ───────────────────────────────


class TestSchedulerSkipsMarketAnalysisJob:
    """COIN-53: setup_scheduler() 가 MARKET_ANALYSIS_ENABLED=False 시 market_analysis job 미추가."""

    def _count_market_analysis_jobs(self, scheduler) -> int:
        """Count jobs with 'market_analysis' in name."""
        return sum(
            1
            for name in scheduler._jobs
            if "market_analysis" in name
        )

    def test_scheduler_skips_market_analysis_when_disabled(self):
        """MARKET_ANALYSIS_ENABLED=False → market_analysis job 미추가."""
        import agents.coordinator as coord_module
        from engine.scheduler import setup_scheduler

        coord = MagicMock()
        coord.run_market_analysis = AsyncMock()
        coord.run_risk_evaluation = AsyncMock()
        coord.run_performance_analysis = AsyncMock()
        coord.run_strategy_advice = AsyncMock()

        pm = MagicMock()
        pm.cash_balance = 1000.0

        # patch both locations: coord module flag + config import inside scheduler
        with patch.object(coord_module, "MARKET_ANALYSIS_ENABLED", False), \
             patch("config.get_config") as mock_cfg:
            llm = MagicMock()
            llm.enabled = False
            llm.daily_review_enabled = False
            llm.api_key = None
            mock_cfg.return_value.llm = llm

            scheduler = setup_scheduler(
                config=None,
                session_factory=None,
                coordinator=coord,
                portfolio_manager=pm,
            )

        job_names = list(scheduler._jobs.keys())
        assert "market_analysis" not in job_names, (
            f"market_analysis job was added but should be disabled. Jobs: {job_names}"
        )

    def test_scheduler_adds_market_analysis_when_enabled(self):
        """MARKET_ANALYSIS_ENABLED=True → market_analysis job 추가됨."""
        import agents.coordinator as coord_module
        from engine.scheduler import setup_scheduler

        coord = MagicMock()
        coord.run_market_analysis = AsyncMock()
        coord.run_risk_evaluation = AsyncMock()
        coord.run_performance_analysis = AsyncMock()
        coord.run_strategy_advice = AsyncMock()

        pm = MagicMock()
        pm.cash_balance = 1000.0

        with patch.object(coord_module, "MARKET_ANALYSIS_ENABLED", True), \
             patch("config.get_config") as mock_cfg:
            llm = MagicMock()
            llm.enabled = False
            llm.daily_review_enabled = False
            llm.api_key = None
            mock_cfg.return_value.llm = llm

            scheduler = setup_scheduler(
                config=None,
                session_factory=None,
                coordinator=coord,
                portfolio_manager=pm,
            )

        job_names = list(scheduler._jobs.keys())
        assert "market_analysis" in job_names, (
            f"market_analysis job should be present when enabled. Jobs: {job_names}"
        )


# ── Test 4: MARKET_ANALYSIS_ENABLED constant value ───────────────────────────


class TestMarketAnalysisEnabledConstant:
    """Verify MARKET_ANALYSIS_ENABLED is False by default (COIN-53)."""

    def test_constant_is_false(self):
        """MARKET_ANALYSIS_ENABLED デフォルトはFalseであること."""
        from agents.coordinator import MARKET_ANALYSIS_ENABLED
        assert MARKET_ANALYSIS_ENABLED is False, (
            "MARKET_ANALYSIS_ENABLED must be False (COIN-53: agent disabled)"
        )
