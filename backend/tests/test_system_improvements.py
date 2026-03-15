"""
시스템 개선 사항 테스트
======================
- Exchange API timeout + circuit breaker
- Scheduler job timeout
- Strategy loop consecutive error tracking
- Market data retry + LRU cache
- Config validation
- API exchange parameter validation
- Engine shutdown task cleanup
"""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from exchange.binance_usdm_adapter import BinanceUSDMAdapter
from exchange.binance_spot_adapter import BinanceSpotAdapter
from services.market_data import MarketDataService, _LRUCache
from engine.scheduler import _wrap, _JOB_TIMEOUT_SEC


# ── Exchange API Timeout + Circuit Breaker Tests ─────────────

class TestExchangeAPITimeout:
    """API 타임아웃 + 서킷브레이커 테스트."""

    @pytest.mark.asyncio
    async def test_api_call_timeout(self):
        """30초 타임아웃 초과 시 ExchangeConnectionError."""
        from core.exceptions import ExchangeConnectionError
        adapter = BinanceUSDMAdapter()

        async def slow_method():
            await asyncio.sleep(100)

        with pytest.raises(ExchangeConnectionError, match="timed out"):
            adapter._API_TIMEOUT = 0.01  # 10ms for test
            await adapter._call(slow_method)

    @pytest.mark.asyncio
    async def test_successful_call_resets_circuit_breaker(self):
        """성공 호출 시 서킷브레이커 카운터 리셋."""
        adapter = BinanceUSDMAdapter()
        adapter._cb_failures = 3

        async def ok_method():
            return "ok"

        result = await adapter._call(ok_method)
        assert result == "ok"
        assert adapter._cb_failures == 0

    @pytest.mark.asyncio
    async def test_circuit_breaker_trips_after_threshold(self):
        """연속 실패 시 서킷브레이커 작동."""
        from core.exceptions import ExchangeConnectionError
        adapter = BinanceUSDMAdapter()
        adapter._cb_failures = 5
        adapter._cb_open_until = time.monotonic() + 60

        async def any_method():
            return "ok"

        with pytest.raises(ExchangeConnectionError, match="Circuit breaker"):
            await adapter._call(any_method)

    @pytest.mark.asyncio
    async def test_circuit_breaker_resets_after_cooldown(self):
        """쿨다운 후 서킷브레이커 리셋."""
        adapter = BinanceUSDMAdapter()
        adapter._cb_failures = 5
        adapter._cb_open_until = time.monotonic() - 1  # Already expired

        async def ok_method():
            return "ok"

        result = await adapter._call(ok_method)
        assert result == "ok"
        assert adapter._cb_failures == 0

    @pytest.mark.asyncio
    async def test_spot_adapter_timeout(self):
        """바이낸스 현물 어댑터도 타임아웃 지원."""
        from core.exceptions import ExchangeConnectionError
        adapter = BinanceSpotAdapter()

        async def slow_method():
            await asyncio.sleep(100)

        with pytest.raises(ExchangeConnectionError, match="timed out"):
            adapter._API_TIMEOUT = 0.01
            await adapter._call(slow_method)

    @pytest.mark.asyncio
    async def test_network_error_increments_circuit_breaker(self):
        """NetworkError 시 서킷브레이커 카운터 증가."""
        import ccxt.async_support as ccxt
        from core.exceptions import ExchangeConnectionError
        adapter = BinanceUSDMAdapter()

        async def network_fail():
            raise ccxt.NetworkError("connection reset")

        with pytest.raises(ExchangeConnectionError):
            await adapter._call(network_fail)

        assert adapter._cb_failures == 1


# ── Scheduler Job Timeout Tests ──────────────────────────────

class TestSchedulerJobTimeout:
    """스케줄러 작업 타임아웃 테스트."""

    @pytest.mark.asyncio
    async def test_job_timeout_logged(self):
        """타임아웃 시 에러 로그."""
        async def slow_job():
            await asyncio.sleep(100)

        wrapped = _wrap(slow_job)

        with patch("engine.scheduler._JOB_TIMEOUT_SEC", 0.01):
            # Should not raise, just log
            await wrapped()

    @pytest.mark.asyncio
    async def test_normal_job_completes(self):
        """정상 작업은 완료."""
        called = False

        async def normal_job():
            nonlocal called
            called = True

        wrapped = _wrap(normal_job)
        await wrapped()
        assert called

    @pytest.mark.asyncio
    async def test_job_error_caught(self):
        """예외 발생 시 에러 처리 (전파 안 함)."""
        async def failing_job():
            raise RuntimeError("test error")

        wrapped = _wrap(failing_job)
        await wrapped()  # Should not raise


# ── Strategy Loop Consecutive Error Tests ────────────────────

class TestStrategyLoopErrors:
    """전략 루프 연속 에러 처리 테스트."""

    @pytest.mark.asyncio
    async def test_consecutive_errors_trigger_pause(self):
        """연속 5회 에러 후 일시 중지."""
        from engine.trading_engine import TradingEngine

        config = MagicMock()
        config.trading.mode = "paper"
        config.trading.evaluation_interval_sec = 300
        config.trading.tracked_coins = ["BTC/KRW"]
        config.trading.min_combined_confidence = 0.50
        config.trading.daily_buy_limit = 20
        config.trading.max_daily_coin_buys = 3
        config.trading.min_trade_interval_sec = 3600
        config.trading.rotation_enabled = False
        config.risk.max_trade_size_pct = 0.30

        engine = TradingEngine(
            config=config,
            exchange=AsyncMock(),
            market_data=AsyncMock(),
            order_manager=MagicMock(),
            portfolio_manager=MagicMock(),
            combiner=MagicMock(),
            exchange_name="bithumb",
        )
        engine._is_running = True

        error_count = 0
        sleep_values = []

        async def failing_cycle():
            nonlocal error_count
            error_count += 1
            if error_count > 6:
                engine._is_running = False
                return
            raise RuntimeError("test error")

        original_sleep = asyncio.sleep

        async def mock_sleep(sec):
            sleep_values.append(sec)
            await original_sleep(0)

        with patch.object(engine, '_evaluation_cycle', failing_cycle), \
             patch("asyncio.sleep", mock_sleep), \
             patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._strategy_loop()

        # After 5 errors, should have paused (60 seconds sleep)
        assert engine._ERROR_PAUSE_SEC in sleep_values


# ── Market Data Retry + LRU Cache Tests ──────────────────────

class TestMarketDataRetry:
    """마켓 데이터 재시도 + LRU 캐시 테스트."""

    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        """실패 시 지수 백오프 재시도."""
        exchange = AsyncMock()
        call_count = 0

        async def flaky_fetch(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("transient error")
            return MagicMock(last=100.0)

        exchange.fetch_ticker = flaky_fetch
        md = MarketDataService(exchange)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            ticker = await md.get_ticker("BTC/USDT")

        assert ticker.last == 100.0
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retry_exhausted_raises(self):
        """재시도 횟수 초과 시 예외 전파."""
        exchange = AsyncMock()
        exchange.fetch_ticker = AsyncMock(side_effect=Exception("persistent error"))
        md = MarketDataService(exchange)

        with patch("asyncio.sleep", new_callable=AsyncMock), \
             pytest.raises(Exception, match="persistent error"):
            await md.get_ticker("BTC/USDT")

    def test_lru_cache_eviction(self):
        """LRU 캐시 초과 시 가장 오래된 항목 제거."""
        cache = _LRUCache(max_size=3, ttl_sec=60)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)
        cache.put("d", 4)  # Should evict "a"

        assert cache.get("a") is None
        assert cache.get("d") == 4

    def test_lru_cache_ttl_expiry(self):
        """TTL 만료 시 캐시 미스."""
        cache = _LRUCache(max_size=10, ttl_sec=0.01)
        cache.put("key", "value")
        import time
        time.sleep(0.02)
        assert cache.get("key") is None

    def test_lru_cache_hit(self):
        """TTL 내 캐시 히트."""
        cache = _LRUCache(max_size=10, ttl_sec=60)
        cache.put("key", "value")
        assert cache.get("key") == "value"

    def test_cache_stats(self):
        """캐시 통계."""
        exchange = AsyncMock()
        md = MarketDataService(exchange)
        md._ohlcv_cache.put("BTC:4h", "data")
        md._ticker_cache.put("BTC", "tick")
        stats = md.cache_stats
        assert stats["ohlcv_entries"] == 1
        assert stats["ticker_entries"] == 1


# ── Config Validation Tests ──────────────────────────────────

class TestConfigValidation:
    """설정값 검증 테스트."""

    def test_trading_mode_invalid(self):
        """잘못된 mode 값 거부."""
        from config import TradingConfig
        with pytest.raises(Exception):
            TradingConfig(mode="invalid")

    def test_trading_mode_valid(self):
        """올바른 mode 값 허용."""
        from config import TradingConfig
        config = TradingConfig(mode="paper")
        assert config.mode == "paper"

    def test_confidence_out_of_range(self):
        """confidence 범위 초과 거부."""
        from config import TradingConfig
        with pytest.raises(Exception):
            TradingConfig(min_combined_confidence=1.5)

    def test_negative_daily_buy_limit(self):
        """음수 daily_buy_limit 거부."""
        from config import TradingConfig
        with pytest.raises(Exception):
            TradingConfig(daily_buy_limit=0)

    def test_risk_pct_out_of_range(self):
        """비율 범위 초과 거부."""
        from config import RiskConfig
        with pytest.raises(Exception):
            RiskConfig(max_single_coin_pct=1.5)

    def test_risk_pct_zero(self):
        """비율 0 거부."""
        from config import RiskConfig
        with pytest.raises(Exception):
            RiskConfig(max_drawdown_pct=0.0)

    def test_binance_trading_mode_invalid(self):
        """바이낸스 트레이딩 잘못된 mode 거부."""
        from config import BinanceTradingConfig
        with pytest.raises(Exception):
            BinanceTradingConfig(mode="test")

    def test_binance_spot_confidence_valid(self):
        """바이낸스 현물 confidence 범위 내 허용."""
        from config import BinanceSpotTradingConfig
        config = BinanceSpotTradingConfig(min_combined_confidence=0.55)
        assert config.min_combined_confidence == 0.55


# ── API Exchange Parameter Validation Tests ──────────────────

class TestAPIExchangeValidation:
    """API 거래소 파라미터 검증 테스트."""

    def test_valid_exchange_names(self):
        """유효한 거래소 이름 통과."""
        from api.dependencies import validate_exchange
        assert validate_exchange("bithumb") == "bithumb"
        assert validate_exchange("binance_futures") == "binance_futures"
        assert validate_exchange("binance_spot") == "binance_spot"

    def test_invalid_exchange_name(self):
        """잘못된 거래소 이름 거부."""
        from api.dependencies import validate_exchange
        with pytest.raises(ValueError, match="Invalid exchange"):
            validate_exchange("invalid_exchange")


# ── Engine Shutdown Tests ────────────────────────────────────

class TestEngineShutdown:
    """엔진 종료 시 태스크 정리 테스트."""

    @pytest.mark.asyncio
    async def test_stop_cancels_tasks(self):
        """stop() 시 실행 중인 태스크 취소."""
        from engine.trading_engine import TradingEngine

        config = MagicMock()
        config.trading.mode = "paper"
        config.trading.evaluation_interval_sec = 300
        config.trading.tracked_coins = ["BTC/KRW"]
        config.trading.min_combined_confidence = 0.50
        config.trading.daily_buy_limit = 20
        config.trading.max_daily_coin_buys = 3
        config.trading.min_trade_interval_sec = 3600
        config.trading.rotation_enabled = False
        config.risk.max_trade_size_pct = 0.30

        engine = TradingEngine(
            config=config,
            exchange=AsyncMock(),
            market_data=AsyncMock(),
            order_manager=MagicMock(),
            portfolio_manager=MagicMock(),
            combiner=MagicMock(),
            exchange_name="bithumb",
        )

        # Simulate running tasks
        async def long_running():
            await asyncio.sleep(1000)

        engine._tasks = [
            asyncio.create_task(long_running(), name="test_task1"),
            asyncio.create_task(long_running(), name="test_task2"),
        ]

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine.stop()

        assert engine._is_running is False
        assert len(engine._tasks) == 0


# ── Fire-and-Forget Task Names Tests ─────────────────────────

class TestTaskNames:
    """asyncio.create_task에 name 파라미터 부여 테스트."""

    def test_engine_start_creates_named_tasks(self):
        """엔진 start() 시 name 파라미터 사용."""
        import inspect
        from engine.trading_engine import TradingEngine
        source = inspect.getsource(TradingEngine.start)
        assert "name=" in source

    def test_dashboard_start_uses_named_task(self):
        """API 엔진 시작 시 name 파라미터 사용."""
        import inspect
        from api import dashboard
        source = inspect.getsource(dashboard.start_engine)
        assert "name=" in source


# ── Scheduler Setup Tests ─────────────────────────────────────

class TestSchedulerSetup:
    """setup_scheduler 잡 등록 검증."""

    def test_setup_scheduler_registers_performance_analytics_for_coordinator(self):
        """coordinator가 있을 때 performance_analytics 크론잡이 등록됨."""
        from engine.scheduler import setup_scheduler

        mock_coord = MagicMock()
        mock_coord.run_market_analysis = AsyncMock()
        mock_coord.run_risk_evaluation = AsyncMock()
        mock_coord.run_performance_analysis = AsyncMock()
        mock_coord.run_strategy_advice = AsyncMock()

        mock_config = MagicMock()
        mock_config.llm.enabled = False
        mock_config.llm.daily_review_enabled = False
        mock_config.llm.api_key = ""

        with patch("config.get_config", return_value=mock_config):
            with patch("apscheduler.schedulers.asyncio.AsyncIOScheduler.start"):
                scheduler = setup_scheduler(
                    config=mock_config,
                    session_factory=MagicMock(),
                    coordinator=mock_coord,
                    portfolio_manager=None,
                )

        assert "performance_analytics" in scheduler._jobs
        assert "strategy_advice" in scheduler._jobs

    def test_setup_scheduler_no_coordinator_skips_agent_jobs(self):
        """coordinator가 None이면 에이전트 잡 등록 안 됨."""
        from engine.scheduler import setup_scheduler

        mock_config = MagicMock()
        mock_config.llm.enabled = False

        # coordinator=None이면 get_config 호출 없으므로 패치 불필요
        scheduler = setup_scheduler(
            config=mock_config,
            session_factory=MagicMock(),
            coordinator=None,
            portfolio_manager=None,
        )

        assert "performance_analytics" not in scheduler._jobs
        assert "strategy_advice" not in scheduler._jobs
        assert "market_analysis" not in scheduler._jobs
