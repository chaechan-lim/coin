"""FuturesEngineV2 테스트."""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from engine.futures_engine_v2 import FuturesEngineV2
from config import AppConfig
from exchange.data_models import Balance


@pytest.fixture
def mock_exchange():
    exchange = AsyncMock()
    exchange.set_leverage = AsyncMock()
    exchange.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=500.0, used=0.0, total=500.0),
    })
    exchange.close_ws = AsyncMock()
    return exchange


@pytest.fixture
def mock_market_data():
    md = AsyncMock()
    md.get_current_price = AsyncMock(return_value=80000.0)
    md.get_ohlcv_df = AsyncMock(return_value=None)
    return md


@pytest.fixture
def mock_pm(mock_market_data):
    pm = MagicMock()
    pm.cash_balance = 500.0
    pm._is_paper = False
    pm._exchange_name = "binance_futures"
    pm.apply_income = AsyncMock()
    return pm


@pytest.fixture
def mock_om(mock_exchange):
    return MagicMock()


@pytest.fixture
def app_config():
    return AppConfig()


@pytest.fixture
def engine(app_config, mock_exchange, mock_market_data, mock_om, mock_pm):
    e = FuturesEngineV2(
        config=app_config,
        exchange=mock_exchange,
        market_data=mock_market_data,
        order_manager=mock_om,
        portfolio_manager=mock_pm,
    )
    return e


class TestInit:
    def test_exchange_name(self, engine):
        assert engine.exchange_name == "binance_futures"

    def test_not_running(self, engine):
        assert engine.is_running is False

    def test_tracked_coins(self, engine):
        assert "BTC/USDT" in engine.tracked_coins

    def test_has_all_components(self, engine):
        assert engine._regime is not None
        assert engine._strategies is not None
        assert engine._positions is not None
        assert engine._guard is not None
        assert engine._safe_order is not None
        assert engine._tier1 is not None
        assert engine._tier2 is not None


class TestRegistryInterface:
    def test_set_engine_registry(self, engine):
        mock_reg = MagicMock()
        engine.set_engine_registry(mock_reg)
        assert engine._engine_registry is mock_reg

    def test_set_recovery_manager(self, engine):
        mock_rm = MagicMock()
        engine.set_recovery_manager(mock_rm)
        assert engine._recovery_manager is mock_rm

    def test_set_broadcast(self, engine):
        cb = AsyncMock()
        engine.set_broadcast_callback(cb)
        assert engine._broadcast_callback is cb


class TestInitialize:
    @pytest.mark.asyncio
    async def test_initialize_sets_leverage(self, engine, mock_exchange, session_factory):
        with patch("engine.futures_engine_v2.get_session_factory", return_value=session_factory):
            await engine.initialize()
        assert mock_exchange.set_leverage.call_count > 0


class TestStartStop:
    @pytest.mark.asyncio
    async def test_start(self, engine):
        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine.start()
        assert engine.is_running is True
        assert len(engine._tasks) == 6  # 6 loops

        await engine.stop()
        assert engine.is_running is False
        assert len(engine._tasks) == 0

    @pytest.mark.asyncio
    async def test_double_start(self, engine):
        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine.start()
            task_count = len(engine._tasks)
            await engine.start()  # should not add more tasks
            assert len(engine._tasks) == task_count

            await engine.stop()


class TestStatus:
    def test_get_status(self, engine):
        status = engine.get_status()
        assert status["engine"] == "futures_v2"
        assert status["is_running"] is False
        assert "regime" in status
        assert "tier1_positions" in status
        assert "tier2_positions" in status
        assert "balance_guard_paused" in status


class TestAPICompatibility:
    """종목/로테이션 탭 및 전략 성과 탭 API 호환성 테스트."""

    def test_strategies_property_returns_dict(self, engine):
        """eng.strategies는 dict를 반환해야 함 (전략 성과 탭용)."""
        strats = engine.strategies
        assert isinstance(strats, dict)

    def test_strategies_contains_v2_strategy_names(self, engine):
        """v2 레짐 전략 3종 이름이 포함되어야 함."""
        strats = engine.strategies
        strategy_names = set(strats.keys())
        # TrendFollower는 TRENDING_UP과 TRENDING_DOWN에 동일 인스턴스가 매핑되므로
        # deduplicate 후 3종
        assert "trend_follower" in strategy_names
        assert "mean_reversion" in strategy_names
        assert "vol_breakout" in strategy_names
        assert len(strategy_names) == 3

    def test_strategies_values_have_name_attr(self, engine):
        """각 전략 객체에는 name 속성이 있어야 함."""
        for name, strategy in engine.strategies.items():
            assert hasattr(strategy, 'name')
            assert strategy.name == name

    def test_rotation_status_property_exists(self, engine):
        """rotation_status 프로퍼티가 존재해야 함 (종목/로테이션 탭용)."""
        rs = engine.rotation_status
        assert isinstance(rs, dict)

    def test_rotation_status_required_keys(self, engine):
        """rotation_status는 RotationStatusResponse에 필요한 모든 키를 포함해야 함."""
        rs = engine.rotation_status
        required_keys = [
            "rotation_enabled", "surge_threshold", "market_state",
            "current_surge_symbol", "last_rotation_time", "last_scan_time",
            "rotation_cooldown_sec", "tracked_coins", "rotation_coins",
            "all_surge_scores",
        ]
        for key in required_keys:
            assert key in rs, f"rotation_status is missing key: {key}"

    def test_rotation_status_tracked_coins(self, engine):
        """rotation_status.tracked_coins는 엔진 tracked_coins와 일치해야 함."""
        rs = engine.rotation_status
        assert rs["tracked_coins"] == engine.tracked_coins

    def test_rotation_status_market_state_on_init(self, engine):
        """초기화 시 market_state는 유효한 문자열이어야 함."""
        rs = engine.rotation_status
        assert isinstance(rs["market_state"], str)
        # 레짐이 없으면 'sideways' 폴백
        assert rs["market_state"] == "sideways"

    def test_rotation_status_futures_flags(self, engine):
        """선물 v2 엔진은 rotation 비활성 + surge 없음."""
        rs = engine.rotation_status
        assert rs["rotation_enabled"] is False
        assert rs["surge_threshold"] == 0.0
        assert rs["current_surge_symbol"] is None
        assert rs["all_surge_scores"] == {}
