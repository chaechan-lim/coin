"""COIN-38: V2 선물 엔진 에이전트 스택 통합 테스트.

V2 경로에서 에이전트 스택(회고/성과분석/전략어드바이저)이 올바르게 생성되고
코디네이터가 엔진에 연결되는지 검증.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.coordinator import AgentCoordinator
from engine.futures_engine_v2 import FuturesEngineV2
from config import AppConfig


@pytest.fixture
def app_config():
    return AppConfig()


@pytest.fixture
def mock_exchange():
    exchange = AsyncMock()
    exchange.set_leverage = AsyncMock()
    exchange.fetch_balance = AsyncMock(return_value={})
    return exchange


@pytest.fixture
def mock_market_data():
    md = AsyncMock()
    md.get_current_price = AsyncMock(return_value=80000.0)
    md.get_ohlcv_df = AsyncMock(return_value=None)
    return md


@pytest.fixture
def v2_engine(app_config, mock_exchange, mock_market_data):
    pm = MagicMock()
    pm.cash_balance = 500.0
    pm._is_paper = False
    pm._exchange_name = "binance_futures"
    om = MagicMock()
    return FuturesEngineV2(
        config=app_config,
        exchange=mock_exchange,
        market_data=mock_market_data,
        order_manager=om,
        portfolio_manager=pm,
    )


class TestCreateAgentStackForV2:
    """_create_agent_stack 결과가 V2 엔진과 호환되는지 검증."""

    def test_agent_stack_creates_all_agents(self, mock_market_data, app_config):
        """_create_agent_stack이 5개 에이전트를 포함하는 코디네이터 생성."""
        from main import _create_agent_stack
        combiner, coordinator = _create_agent_stack(
            mock_market_data, app_config, "binance_futures", market_symbol="BTC/USDT",
        )
        assert combiner is not None
        assert coordinator is not None
        assert isinstance(coordinator, AgentCoordinator)
        # 코디네이터에 에이전트들이 설정됨
        assert coordinator._trade_review_agent is not None
        assert coordinator._performance_agent is not None
        assert coordinator._strategy_advisor is not None

    def test_coordinator_connects_to_v2_engine(self, v2_engine, mock_market_data, app_config):
        """코디네이터가 V2 엔진에 연결됨."""
        from main import _create_agent_stack
        _, coordinator = _create_agent_stack(
            mock_market_data, app_config, "binance_futures", market_symbol="BTC/USDT",
        )
        coordinator.set_engine(v2_engine)
        v2_engine.set_agent_coordinator(coordinator)

        assert coordinator._engine is v2_engine
        assert v2_engine._agent_coordinator is coordinator

    def test_coordinator_exchange_name_futures(self, mock_market_data, app_config):
        """binance_futures 에이전트의 exchange_name이 올바르게 설정."""
        from main import _create_agent_stack
        _, coordinator = _create_agent_stack(
            mock_market_data, app_config, "binance_futures", market_symbol="BTC/USDT",
        )
        assert coordinator._exchange_name == "binance_futures"


class TestV2CoordinatorRunMethods:
    """V2 엔진에 연결된 코디네이터의 run 메서드가 에러 없이 실행되는지 검증."""

    @pytest.fixture
    def wired_coordinator(self, v2_engine, mock_market_data, app_config):
        """V2 엔진에 연결된 코디네이터."""
        from main import _create_agent_stack
        _, coordinator = _create_agent_stack(
            mock_market_data, app_config, "binance_futures", market_symbol="BTC/USDT",
        )
        coordinator.set_engine(v2_engine)
        v2_engine.set_agent_coordinator(coordinator)
        return coordinator

    @pytest.mark.asyncio
    async def test_run_trade_review(self, wired_coordinator, session_factory):
        """run_trade_review가 V2 엔진과 에러 없이 실행."""
        with patch("agents.coordinator.get_session_factory", return_value=session_factory):
            review = await wired_coordinator.run_trade_review()
        assert review is not None
        assert review.total_trades == 0  # 빈 DB
        assert review.exchange_name if hasattr(review, "exchange_name") else True

    @pytest.mark.asyncio
    async def test_run_performance_analysis(self, wired_coordinator, session_factory):
        """run_performance_analysis가 V2 엔진과 에러 없이 실행."""
        with patch("agents.coordinator.get_session_factory", return_value=session_factory):
            report = await wired_coordinator.run_performance_analysis()
        assert report is not None

    @pytest.mark.asyncio
    async def test_run_strategy_advice(self, wired_coordinator, session_factory):
        """run_strategy_advice가 V2 엔진과 에러 없이 실행."""
        with patch("agents.coordinator.get_session_factory", return_value=session_factory):
            advice = await wired_coordinator.run_strategy_advice()
        assert advice is not None

    @pytest.mark.asyncio
    async def test_run_market_analysis_with_v2(self, wired_coordinator, mock_market_data):
        """run_market_analysis가 V2 엔진(no _market_state)과 에러 없이 실행.

        V2 엔진은 _market_state 속성이 없지만 coordinator가 hasattr 체크를 하므로
        에러 없이 동작해야 함.
        """
        # market analysis agent needs OHLCV data
        mock_market_data.get_ohlcv_df = AsyncMock(return_value=None)
        with patch("agents.coordinator.get_session_factory") as mock_sf:
            mock_session = AsyncMock()
            mock_sf.return_value = MagicMock(
                __aenter__=AsyncMock(return_value=mock_session),
                __aexit__=AsyncMock(return_value=None),
            )
            mock_sf.return_value.__call__ = MagicMock(return_value=mock_sf.return_value)

            # MarketAnalysisAgent.analyze() 를 모킹
            with patch.object(
                wired_coordinator._market_agent, "analyze",
                new_callable=AsyncMock,
            ) as mock_analyze:
                from agents.market_analysis import MarketAnalysis
                from core.enums import MarketState
                mock_analyze.return_value = MarketAnalysis(
                    state=MarketState.SIDEWAYS,
                    confidence=0.7,
                    volatility_level="medium",
                    reasoning="test",
                    indicators={},
                    recommended_weights={},
                )
                analysis = await wired_coordinator.run_market_analysis()
            assert analysis is not None
            assert analysis.state == MarketState.SIDEWAYS

    @pytest.mark.asyncio
    async def test_run_risk_evaluation_with_v2(self, wired_coordinator, v2_engine, session_factory):
        """run_risk_evaluation이 V2 엔진의 호환 속성으로 에러 없이 실행."""
        with patch("agents.coordinator.get_session_factory", return_value=session_factory):
            alerts = await wired_coordinator.run_risk_evaluation(500.0)
        # V2 엔진은 _paused_coins, _suppressed_coins, suppress_buys 보유
        assert isinstance(alerts, list)


class TestRegistryIntegration:
    """engine_registry에 V2 엔진+코디네이터가 올바르게 등록되는지 검증."""

    def test_register_with_coordinator(self, v2_engine, mock_market_data, app_config):
        """V2 엔진이 코디네이터와 함께 레지스트리에 등록."""
        from api.dependencies import EngineRegistry
        from main import _create_agent_stack

        registry = EngineRegistry()
        combiner, coordinator = _create_agent_stack(
            mock_market_data, app_config, "binance_futures", market_symbol="BTC/USDT",
        )
        pm = MagicMock()

        registry.register(
            "binance_futures", v2_engine, pm, combiner, coordinator,
        )

        assert registry.get_coordinator("binance_futures") is coordinator
        assert registry.get_combiner("binance_futures") is combiner
        assert registry.get_engine("binance_futures") is v2_engine

    def test_old_v2_registered_none_coordinator(self):
        """이전 코드(COIN-38 이전)에서는 None으로 등록 -> get_coordinator가 None."""
        from api.dependencies import EngineRegistry
        registry = EngineRegistry()
        registry.register("binance_futures", MagicMock(), MagicMock(), None, None)
        assert registry.get_coordinator("binance_futures") is None
