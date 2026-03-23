"""FuturesEngineV2 테스트."""
import asyncio
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

    def test_get_status_includes_balance_guard_detail(self, engine):
        """COIN-15: get_status()에 balance_guard 상세 정보 포함."""
        status = engine.get_status()
        assert "balance_guard" in status
        bg = status["balance_guard"]
        assert "is_paused" in bg
        assert "consecutive_stable" in bg
        assert "auto_resume_stable_count" in bg


class TestBalanceGuardAPI:
    """COIN-15: BalanceGuard 수동 재개 및 상태 API 테스트."""

    def test_resume_balance_guard_not_paused(self, engine):
        """일시 정지되지 않은 상태에서 resume 호출."""
        result = engine.resume_balance_guard()
        assert result["was_paused"] is False
        assert result["is_paused"] is False
        assert "guard" in result

    def test_resume_balance_guard_when_paused(self, engine):
        """일시 정지 상태에서 resume 호출 → 재개."""
        engine._guard._paused = True
        result = engine.resume_balance_guard()
        assert result["was_paused"] is True
        assert result["is_paused"] is False

    def test_get_balance_guard_status(self, engine):
        """BalanceGuard 상태 조회."""
        status = engine.get_balance_guard_status()
        assert isinstance(status, dict)
        assert "is_paused" in status
        assert "consecutive_warnings" in status
        assert "consecutive_stable" in status
        assert "warn_pct" in status
        assert "pause_pct" in status


class TestAPICompatibility:
    """종목/로테이션 탭 및 전략 성과 탭 API 호환성 테스트."""

    def test_strategies_property_returns_dict(self, engine):
        """eng.strategies는 dict를 반환해야 함 (전략 성과 탭용)."""
        strats = engine.strategies
        assert isinstance(strats, dict)

    def test_strategies_contains_spot_strategy_names(self, engine):
        """SpotEvaluator의 현물 4전략 이름이 포함되어야 함 (주문에 사용되는 전략명)."""
        strats = engine.strategies
        strategy_names = set(strats.keys())
        assert "cis_momentum" in strategy_names
        assert "bnf_deviation" in strategy_names
        assert "donchian_channel" in strategy_names
        assert "larry_williams" in strategy_names

    def test_strategies_excludes_v2_regime_names(self, engine):
        """COIN-34: v2 레짐 전략 3종은 비활성이므로 제외되어야 함."""
        strats = engine.strategies
        strategy_names = set(strats.keys())
        assert "trend_follower" not in strategy_names
        assert "mean_reversion" not in strategy_names
        assert "vol_breakout" not in strategy_names

    def test_strategies_total_count(self, engine):
        """COIN-34: 현물 4전략만 반환 (레짐 전략 제외)."""
        strats = engine.strategies
        assert len(strats) == 4

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


class TestTier1Status:
    """COIN-17: get_tier1_status 테스트."""

    def test_get_tier1_status(self, engine):
        """get_tier1_status가 Tier1Manager 상태를 반환."""
        status = engine.get_tier1_status()
        assert isinstance(status, dict)
        assert "cycle_count" in status
        assert "last_cycle_at" in status
        assert "last_action_at" in status
        assert "coins" in status
        assert "active_positions" in status
        assert "last_decisions" in status
        assert "regime" in status

    def test_tier1_status_initial(self, engine):
        """초기 상태: cycle_count=0, last_cycle_at=None."""
        status = engine.get_tier1_status()
        assert status["cycle_count"] == 0
        assert status["last_cycle_at"] is None
        assert status["last_action_at"] is None
        assert status["active_positions"] == 0
        assert status["last_decisions"] == {}


class TestHealthMonitorCompat:
    """Bug COIN-13: FuturesEngineV2 health_monitor 호환성 테스트."""

    def test_has_eval_error_counts(self, engine):
        """_eval_error_counts 속성 존재."""
        assert hasattr(engine, '_eval_error_counts')
        assert engine._eval_error_counts == {}

    def test_has_position_trackers(self, engine):
        """_position_trackers 속성 존재."""
        assert hasattr(engine, '_position_trackers')
        assert engine._position_trackers == {}

    def test_eval_error_counts_is_dict(self, engine):
        """_eval_error_counts는 dict 타입이고 값 설정 가능."""
        assert isinstance(engine._eval_error_counts, dict)
        engine._eval_error_counts["BTC/USDT"] = 1
        assert engine._eval_error_counts["BTC/USDT"] == 1

    def test_has_pause_buying(self, engine):
        """pause_buying 메서드 존재하고 호출 시 에러 없음."""
        assert hasattr(engine, 'pause_buying')
        assert callable(engine.pause_buying)
        engine.pause_buying(["BTC/USDT"])

    def test_has_resume_buying(self, engine):
        """resume_buying 메서드 존재하고 호출 시 에러 없음."""
        assert hasattr(engine, 'resume_buying')
        assert callable(engine.resume_buying)
        engine.resume_buying()

    def test_health_monitor_error_rate_check(self, engine):
        """HealthMonitor._check_error_rate_trend()가 v2 엔진에서 에러 없이 동작."""
        from engine.health_monitor import HealthMonitor
        hm = HealthMonitor(
            engine=engine,
            portfolio_manager=MagicMock(cash_balance=500.0),
            exchange_adapter=AsyncMock(),
            market_data=AsyncMock(),
            exchange_name="binance_futures",
            tracked_coins=["BTC/USDT"],
        )
        result = hm._check_error_rate_trend()
        assert result.healthy is True

    def test_health_monitor_error_rate_with_errors(self, engine):
        """v2 엔진에 에러 기록 후 HealthMonitor가 감지."""
        from engine.health_monitor import HealthMonitor
        engine._eval_error_counts = {"BTC/USDT": 3, "ETH/USDT": 2}
        hm = HealthMonitor(
            engine=engine,
            portfolio_manager=MagicMock(cash_balance=500.0),
            exchange_adapter=AsyncMock(),
            market_data=AsyncMock(),
            exchange_name="binance_futures",
            tracked_coins=["BTC/USDT"],
        )
        result = hm._check_error_rate_trend()
        assert result.healthy is False
        assert "BTC/USDT" in result.detail


class TestSnapshotInPersistLoop:
    """COIN-21: _persist_loop에서 포트폴리오 스냅샷 저장 테스트."""

    @pytest.mark.asyncio
    async def test_persist_loop_calls_take_snapshot(self, engine, mock_pm, session_factory):
        """_persist_loop가 take_snapshot을 호출."""
        mock_pm.take_snapshot = AsyncMock(return_value=MagicMock(
            total_value_krw=500.0,
            cash_balance_krw=300.0,
        ))
        mock_pm.get_portfolio_summary = AsyncMock(return_value={
            "total_value_krw": 500.0,
            "cash_balance_krw": 300.0,
        })

        engine._is_running = True

        # _persist_loop를 한 번만 실행하도록 시뮬레이션
        with patch("engine.futures_engine_v2.get_session_factory", return_value=session_factory):
            call_count = 0
            async def mock_sleep(seconds):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    # 첫 sleep (120초 초기 대기)은 스킵
                    return
                # 두 번째 sleep (300초)에서 루프 중지
                engine._is_running = False

            with patch("asyncio.sleep", side_effect=mock_sleep):
                await engine._persist_loop()

        mock_pm.take_snapshot.assert_called_once()

    @pytest.mark.asyncio
    async def test_persist_loop_broadcasts_on_snapshot(self, engine, mock_pm, session_factory):
        """스냅샷 성공 시 브로드캐스트 콜백이 호출됨."""
        mock_pm.take_snapshot = AsyncMock(return_value=MagicMock(
            total_value_krw=500.0,
            cash_balance_krw=300.0,
        ))
        mock_pm.get_portfolio_summary = AsyncMock(return_value={
            "total_value_krw": 500.0,
        })

        broadcast_cb = AsyncMock()
        engine.set_broadcast_callback(broadcast_cb)
        engine._is_running = True

        with patch("engine.futures_engine_v2.get_session_factory", return_value=session_factory):
            call_count = 0
            async def mock_sleep(seconds):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return
                engine._is_running = False

            with patch("asyncio.sleep", side_effect=mock_sleep):
                await engine._persist_loop()

        broadcast_cb.assert_called_once()
        call_args = broadcast_cb.call_args[0][0]
        assert call_args["event"] == "portfolio_update"
        assert call_args["exchange"] == "binance_futures"

    @pytest.mark.asyncio
    async def test_persist_loop_no_broadcast_when_snapshot_none(self, engine, mock_pm, session_factory):
        """스냅샷이 None(스파이크)이면 브로드캐스트 미호출."""
        mock_pm.take_snapshot = AsyncMock(return_value=None)

        broadcast_cb = AsyncMock()
        engine.set_broadcast_callback(broadcast_cb)
        engine._is_running = True

        with patch("engine.futures_engine_v2.get_session_factory", return_value=session_factory):
            call_count = 0
            async def mock_sleep(seconds):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return
                engine._is_running = False

            with patch("asyncio.sleep", side_effect=mock_sleep):
                await engine._persist_loop()

        broadcast_cb.assert_not_called()

    @pytest.mark.asyncio
    async def test_persist_loop_handles_snapshot_error(self, engine, mock_pm, session_factory):
        """스냅샷 에러 시 루프가 중단되지 않음."""
        mock_pm.take_snapshot = AsyncMock(side_effect=Exception("DB error"))

        engine._is_running = True

        with patch("engine.futures_engine_v2.get_session_factory", return_value=session_factory):
            call_count = 0
            async def mock_sleep(seconds):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return
                engine._is_running = False

            with patch("asyncio.sleep", side_effect=mock_sleep):
                # Should not raise
                await engine._persist_loop()

        mock_pm.take_snapshot.assert_called_once()

    def test_exchange_name_passed_to_tier1(self, engine):
        """FuturesEngineV2가 Tier1Manager에 exchange_name을 전달."""
        assert engine._tier1._exchange_name == "binance_futures"


class TestAgentCoordinatorCompat:
    """COIN-38: V2 에이전트 코디네이터 호환성 테스트."""

    def test_has_paused_coins(self, engine):
        """_paused_coins 속성 존재 (coordinator 호환)."""
        assert hasattr(engine, '_paused_coins')
        assert isinstance(engine._paused_coins, set)

    def test_has_suppressed_coins(self, engine):
        """_suppressed_coins 속성 존재 (coordinator 호환)."""
        assert hasattr(engine, '_suppressed_coins')
        assert isinstance(engine._suppressed_coins, set)

    def test_suppress_buys_no_error(self, engine):
        """suppress_buys 메서드 호출 시 에러 없음."""
        assert hasattr(engine, 'suppress_buys')
        assert callable(engine.suppress_buys)
        engine.suppress_buys(["BTC/USDT"])  # no-op, should not raise

    def test_set_agent_coordinator(self, engine):
        """에이전트 코디네이터 설정."""
        mock_coord = MagicMock()
        engine.set_agent_coordinator(mock_coord)
        assert engine._agent_coordinator is mock_coord

    def test_has_sells_since_review(self, engine):
        """_sells_since_review 카운터 존재."""
        assert hasattr(engine, '_sells_since_review')
        assert engine._sells_since_review == 0

    @pytest.mark.asyncio
    async def test_on_sell_completed_increments_counter(self, engine):
        """_on_sell_completed가 카운터를 증가."""
        assert engine._sells_since_review == 0
        await engine._on_sell_completed()
        assert engine._sells_since_review == 1

    @pytest.mark.asyncio
    async def test_on_sell_completed_triggers_review_at_threshold(self, engine):
        """매도 N회 도달 시 trade_review 트리거."""
        mock_coord = MagicMock()
        mock_coord.run_trade_review = AsyncMock()
        engine.set_agent_coordinator(mock_coord)

        # 트리거 직전까지 카운터 설정
        engine._sells_since_review = engine._REVIEW_TRIGGER_SELLS - 1
        await engine._on_sell_completed()

        # 카운터 리셋 확인
        assert engine._sells_since_review == 0

    @pytest.mark.asyncio
    async def test_on_sell_completed_no_trigger_without_coordinator(self, engine):
        """코디네이터 미설정 시 에러 없이 카운터만 증가."""
        engine._agent_coordinator = None
        engine._sells_since_review = engine._REVIEW_TRIGGER_SELLS - 1
        await engine._on_sell_completed()
        # 코디네이터 없으면 트리거 안 됨, 카운터는 threshold에 도달
        assert engine._sells_since_review == engine._REVIEW_TRIGGER_SELLS

    @pytest.mark.asyncio
    async def test_on_sell_completed_no_trigger_below_threshold(self, engine):
        """매도 횟수 미달 시 트리거 안 됨."""
        mock_coord = MagicMock()
        mock_coord.run_trade_review = AsyncMock()
        engine.set_agent_coordinator(mock_coord)

        engine._sells_since_review = 0
        await engine._on_sell_completed()
        assert engine._sells_since_review == 1  # just incremented

    def test_tier1_has_on_close_callback(self, engine):
        """Tier1Manager에 on_close_callback이 연결됨."""
        assert engine._tier1._on_close_callback is not None
        assert engine._tier1._on_close_callback == engine._on_sell_completed
