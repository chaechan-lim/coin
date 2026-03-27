"""FuturesEngineV2 테스트."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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
        assert len(engine._tasks) >= 6  # 6 기본 + WS 태스크 (최대 8)

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

    def test_get_status_includes_strategy_mode(self, engine):
        """COIN-46: get_status()에 strategy_mode 포함."""
        status = engine.get_status()
        assert "strategy_mode" in status
        assert status["strategy_mode"] == "regime"

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

    def test_strategies_regime_mode_contains_regime_names(self, engine):
        """COIN-46: 기본(regime) 모드에서 레짐 3전략이 반환되어야 함."""
        strats = engine.strategies
        strategy_names = set(strats.keys())
        assert "trend_follower" in strategy_names
        assert "mean_reversion" in strategy_names
        assert "vol_breakout" in strategy_names

    def test_strategies_regime_mode_excludes_spot_names(self, engine):
        """COIN-46: regime 모드에서 현물 4전략은 제외."""
        strats = engine.strategies
        strategy_names = set(strats.keys())
        assert "cis_momentum" not in strategy_names
        assert "bnf_deviation" not in strategy_names
        assert "donchian_channel" not in strategy_names
        assert "larry_williams" not in strategy_names

    def test_strategies_regime_mode_total_count(self, engine):
        """COIN-46: regime 모드 → 3전략 (trend_follower 중복 제거)."""
        strats = engine.strategies
        assert len(strats) == 3

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

        # run_trade_review가 실제로 호출됐는지 확인
        # create_task로 스케줄됐으므로 이벤트 루프 한 틱 실행
        await asyncio.sleep(0)
        mock_coord.run_trade_review.assert_called_once()

        # 태스크가 background_tasks에 등록됐다가 완료 후 제거됨
        await asyncio.sleep(0)
        assert len(engine._background_tasks) == 0

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


class TestStrategyMode:
    """COIN-46: strategy_mode 전환 테스트."""

    @pytest.fixture
    def spot_config(self):
        """strategy_mode=spot 설정."""
        config = AppConfig()
        config.futures_v2.strategy_mode = "spot"
        return config

    @pytest.fixture
    def regime_config(self):
        """strategy_mode=regime 설정 (기본값)."""
        config = AppConfig()
        config.futures_v2.strategy_mode = "regime"
        return config

    @pytest.fixture
    def spot_engine(self, spot_config, mock_exchange, mock_market_data, mock_om, mock_pm):
        return FuturesEngineV2(
            config=spot_config,
            exchange=mock_exchange,
            market_data=mock_market_data,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
        )

    @pytest.fixture
    def regime_engine(self, regime_config, mock_exchange, mock_market_data, mock_om, mock_pm):
        return FuturesEngineV2(
            config=regime_config,
            exchange=mock_exchange,
            market_data=mock_market_data,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
        )

    # ── Default mode ──

    def test_default_strategy_mode_is_regime(self):
        """기본 strategy_mode는 regime."""
        from config import FuturesV2Config
        cfg = FuturesV2Config()
        assert cfg.strategy_mode == "regime"

    # ── Regime mode ──

    def test_regime_mode_uses_regime_evaluators(self, regime_engine):
        """regime 모드에서 RegimeLongEvaluator/RegimeShortEvaluator 사용."""
        from engine.regime_evaluators import RegimeLongEvaluator, RegimeShortEvaluator
        assert isinstance(regime_engine._long_evaluator, RegimeLongEvaluator)
        assert isinstance(regime_engine._short_evaluator, RegimeShortEvaluator)

    def test_regime_mode_evaluators_are_different_instances(self, regime_engine):
        """regime 모드에서 long/short evaluator는 별도 인스턴스 (SAR 작동)."""
        assert regime_engine._long_evaluator is not regime_engine._short_evaluator

    def test_regime_mode_strategies_returns_regime_strategies(self, regime_engine):
        """regime 모드 strategies 프로퍼티: 3전략 반환."""
        strats = regime_engine.strategies
        assert len(strats) == 3
        assert "trend_follower" in strats
        assert "mean_reversion" in strats
        assert "vol_breakout" in strats

    def test_regime_mode_eval_interval(self, regime_engine):
        """regime 모드 eval_interval이 백테스트 최적 4h로 적용."""
        assert regime_engine._long_evaluator.eval_interval_sec == 14400
        assert regime_engine._short_evaluator.eval_interval_sec == 14400

    def test_regime_mode_status(self, regime_engine):
        """regime 모드 get_status()에 strategy_mode 포함."""
        status = regime_engine.get_status()
        assert status["strategy_mode"] == "regime"

    # ── Spot mode (폴백) ──

    def test_spot_mode_uses_spot_evaluator(self, spot_engine):
        """spot 모드에서 SpotEvaluator 사용."""
        from engine.spot_evaluator import SpotEvaluator
        assert isinstance(spot_engine._long_evaluator, SpotEvaluator)
        assert isinstance(spot_engine._short_evaluator, SpotEvaluator)

    def test_spot_mode_evaluators_are_same_instance(self, spot_engine):
        """spot 모드에서 long/short evaluator는 동일 인스턴스."""
        assert spot_engine._long_evaluator is spot_engine._short_evaluator

    def test_spot_mode_strategies_returns_spot_strategies(self, spot_engine):
        """spot 모드 strategies 프로퍼티: 현물 4전략 반환."""
        strats = spot_engine.strategies
        assert len(strats) == 4
        assert "cis_momentum" in strats
        assert "bnf_deviation" in strats
        assert "donchian_channel" in strats
        assert "larry_williams" in strats

    def test_spot_mode_strategies_excludes_regime(self, spot_engine):
        """spot 모드에서 레짐 전략은 제외."""
        strats = spot_engine.strategies
        assert "trend_follower" not in strats
        assert "mean_reversion" not in strats
        assert "vol_breakout" not in strats

    def test_spot_mode_status(self, spot_engine):
        """spot 모드 get_status()."""
        status = spot_engine.get_status()
        assert status["strategy_mode"] == "spot"

    # ── Config validation ──

    def test_strategy_mode_config_regime(self):
        """strategy_mode=regime config 유효."""
        from config import FuturesV2Config
        cfg = FuturesV2Config(strategy_mode="regime")
        assert cfg.strategy_mode == "regime"

    def test_strategy_mode_config_spot(self):
        """strategy_mode=spot config 유효."""
        from config import FuturesV2Config
        cfg = FuturesV2Config(strategy_mode="spot")
        assert cfg.strategy_mode == "spot"

    def test_strategy_mode_config_invalid_rejected(self):
        """잘못된 strategy_mode는 거부."""
        from config import FuturesV2Config
        with pytest.raises(Exception):
            FuturesV2Config(strategy_mode="invalid")

    # ── Regime config params ──

    def test_regime_eval_interval_default(self):
        """기본 regime eval_interval은 14400초 (4h) — 백테스트 최적값."""
        from config import FuturesV2Config
        cfg = FuturesV2Config()
        assert cfg.tier1_regime_eval_interval_sec == 14400

    def test_regime_cooldown_default(self):
        """기본 regime cooldown은 26h."""
        from config import FuturesV2Config
        cfg = FuturesV2Config()
        assert cfg.tier1_regime_cooldown_hours == 26.0

    # ── Tier1Manager integration ──

    def test_regime_mode_tier1_has_evaluators(self, regime_engine):
        """regime 모드에서 Tier1Manager에 올바른 evaluator 주입."""
        from engine.regime_evaluators import RegimeLongEvaluator, RegimeShortEvaluator
        assert isinstance(regime_engine._tier1._long_evaluator, RegimeLongEvaluator)
        assert isinstance(regime_engine._tier1._short_evaluator, RegimeShortEvaluator)

    def test_spot_mode_tier1_has_spot_evaluator(self, spot_engine):
        """spot 모드에서 Tier1Manager에 SpotEvaluator 주입."""
        from engine.spot_evaluator import SpotEvaluator
        assert isinstance(spot_engine._tier1._long_evaluator, SpotEvaluator)
        assert isinstance(spot_engine._tier1._short_evaluator, SpotEvaluator)


class TestRegimeChangeTrigger:
    """COIN-50: 레짐 변경 → Tier1 즉시 재평가 트리거 테스트."""

    @pytest.fixture
    def regime_engine(self, app_config, mock_exchange, mock_market_data, mock_om, mock_pm):
        app_config.futures_v2.strategy_mode = "regime"
        return FuturesEngineV2(
            config=app_config,
            exchange=mock_exchange,
            market_data=mock_market_data,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
        )

    def test_regime_changed_event_exists(self, regime_engine):
        """엔진에 _regime_changed_event asyncio.Event 존재."""
        import asyncio
        assert hasattr(regime_engine, "_regime_changed_event")
        assert isinstance(regime_engine._regime_changed_event, asyncio.Event)

    def test_regime_changed_event_initially_clear(self, regime_engine):
        """초기에 _regime_changed_event는 미설정 상태."""
        assert not regime_engine._regime_changed_event.is_set()

    def test_on_regime_change_sets_event(self, regime_engine):
        """_on_regime_change 호출 시 _regime_changed_event가 설정됨."""
        from core.enums import Regime
        regime_engine._on_regime_change(Regime.RANGING, Regime.TRENDING_UP)
        assert regime_engine._regime_changed_event.is_set()

    def test_regime_detector_has_callback(self, regime_engine):
        """RegimeDetector에 on_regime_change 콜백이 등록됨."""
        assert regime_engine._regime._on_regime_change is not None

    @pytest.mark.asyncio
    async def test_regime_transition_triggers_event(self):
        """RegimeDetector 레짐 전환 확정 시 콜백이 호출됨."""
        import pandas as pd
        import numpy as np
        from engine.regime_detector import RegimeDetector
        from core.enums import Regime

        fired = []

        def callback(prev, new):
            fired.append((prev, new))

        detector = RegimeDetector(confirm_count=1, min_duration_h=0, on_regime_change=callback)

        def _make_df(adx, ema_20, ema_50, ema_slope_dir):
            n = 100
            ema_values = [ema_20 * (1 + ema_slope_dir * 0.002 * (i - (n - 1))) for i in range(n)]
            return pd.DataFrame({
                "close": [80000.0] * n,
                "adx_14": [adx] * n,
                "atr_14": [1000.0] * n,
                "ema_20": ema_values,
                "ema_50": [ema_50] * n,
                "bb_upper_20": [82000.0] * n,
                "bb_lower_20": [78000.0] * n,
                "bb_mid_20": [80000.0] * n,
                "volume": [1000.0] * n,
            })

        # 초기 레짐: TRENDING_UP
        df_up = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        await detector.update(df_up, "BTC/USDT")
        assert len(fired) == 0  # 첫 감지는 콜백 없음

        # 레짐 전환: RANGING (1회 확인으로 전환)
        df_range = _make_df(adx=15, ema_20=80000, ema_50=80000, ema_slope_dir=0)
        df_range["bb_upper_20"] = 81000.0
        df_range["bb_lower_20"] = 79000.0
        await detector.update(df_range, "BTC/USDT")
        assert len(fired) == 1
        assert fired[0][0] == Regime.TRENDING_UP
        assert fired[0][1] == Regime.RANGING

    @pytest.mark.asyncio
    async def test_callback_not_fired_on_same_regime(self):
        """같은 레짐 업데이트에서는 콜백 미호출."""
        import pandas as pd
        from engine.regime_detector import RegimeDetector

        fired = []

        def callback(prev, new):
            fired.append((prev, new))

        detector = RegimeDetector(confirm_count=1, min_duration_h=0, on_regime_change=callback)

        n = 100
        df = pd.DataFrame({
            "close": [80000.0] * n,
            "adx_14": [30.0] * n,
            "atr_14": [1000.0] * n,
            "ema_20": [81000.0 * (1 + 0.002 * (i - (n - 1))) for i in range(n)],
            "ema_50": [79000.0] * n,
            "bb_upper_20": [82000.0] * n,
            "bb_lower_20": [78000.0] * n,
            "bb_mid_20": [80000.0] * n,
            "volume": [1000.0] * n,
        })

        await detector.update(df, "BTC/USDT")  # 첫 감지
        await detector.update(df, "BTC/USDT")  # 동일 레짐
        await detector.update(df, "BTC/USDT")  # 동일 레짐

        assert len(fired) == 0  # 전환 없음 → 콜백 없음

    @pytest.mark.asyncio
    async def test_tier1_loop_wakes_on_regime_change(self, regime_engine):
        """_tier1_loop이 레짐 변경 이벤트 감지 시 즉시 평가 재실행.

        interval=60s, 레짐 변경은 50ms 후 — 두 번째 eval이 ~50ms 만에
        실행되면 60s 대기 없이 즉시 깨어난 것임을 증명한다.
        """
        eval_calls = []

        async def mock_eval_cycle(session):
            eval_calls.append(1)
            # 두 번째 호출 이후 엔진 종료
            if len(eval_calls) >= 2:
                regime_engine._is_running = False

        regime_engine._tier1.evaluation_cycle = mock_eval_cycle
        regime_engine._is_running = True

        # 레짐 변경 이벤트를 50ms 후 설정하는 태스크
        async def trigger_regime_change():
            await asyncio.sleep(0.05)
            regime_engine._on_regime_change(None, None)

        # interval=60s이지만 outer timeout=1s 이내에 2회 eval이 완료되어야 함
        # (regime change wakeup 없으면 60s 후에야 2번째 eval → timeout 발생)
        with patch.object(regime_engine._config.futures_v2, "tier1_eval_interval_sec", 60):
            trigger_task = asyncio.create_task(trigger_regime_change())
            # _tier1_loop의 초기 sleep(5)만 우회
            with patch("engine.futures_engine_v2.asyncio.sleep", return_value=None):
                try:
                    await asyncio.wait_for(regime_engine._tier1_loop(), timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            trigger_task.cancel()

        # 레짐 변경 이벤트로 즉시 2회 평가가 트리거되어야 함
        assert len(eval_calls) >= 2, (
            f"regime change did not trigger immediate re-eval: only {len(eval_calls)} eval(s)"
        )

    @pytest.mark.asyncio
    async def test_tier1_loop_no_race_condition_on_regime_change_during_eval(self, regime_engine):
        """평가 중 레짐 변경이 발생해도 이벤트가 소실되지 않음.

        clear()를 eval 전에 호출하므로, eval 도중 set()된 이벤트는
        다음 wait_for에서 즉시 반환된다 (소실 없음).
        """
        eval_calls = []
        regime_change_fired_during_eval = False

        async def mock_eval_cycle_fires_regime_change(session):
            nonlocal regime_change_fired_during_eval
            eval_calls.append(len(eval_calls) + 1)
            if len(eval_calls) == 1:
                # 첫 번째 eval 도중 제어권 양보 후 레짐 변경 발생
                await asyncio.sleep(0)
                regime_engine._on_regime_change(None, None)
                regime_change_fired_during_eval = True
            elif len(eval_calls) >= 2:
                regime_engine._is_running = False

        regime_engine._tier1.evaluation_cycle = mock_eval_cycle_fires_regime_change
        regime_engine._is_running = True

        with patch.object(regime_engine._config.futures_v2, "tier1_eval_interval_sec", 60):
            with patch("engine.futures_engine_v2.asyncio.sleep", return_value=None):
                try:
                    await asyncio.wait_for(regime_engine._tier1_loop(), timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

        assert regime_change_fired_during_eval, "test setup: regime change never fired during eval"
        # eval 도중 set된 이벤트가 소실되지 않아 즉시 2회 eval이 실행되어야 함
        assert len(eval_calls) >= 2, (
            f"regime change during eval was lost (race condition): only {len(eval_calls)} eval(s)"
        )
