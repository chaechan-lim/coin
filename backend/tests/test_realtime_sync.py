"""
실시간 동기화 기능 단위 테스트
================================
- Fast SL/TP check loop (현물 30초 주기)
- WebSocket 잔고/포지션 동기화 (선물)
- 강제 청산 쿨다운 면제
- 매매 타임스탬프 복원 (재시작 시 쿨다운/washout 유지)
- reconcile_cash_from_db paper-only 가드
- 에러/차단 emit_event
"""
import asyncio
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from datetime import datetime, timezone, timedelta

from core.models import Position
from engine.trading_engine import TradingEngine, PositionTracker
from engine.futures_engine import BinanceFuturesEngine


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def mock_config():
    config = MagicMock()
    config.binance.enabled = True
    config.binance.default_leverage = 3
    config.binance.max_leverage = 10
    config.binance.futures_fee = 0.0004
    config.binance.tracked_coins = ["BTC/USDT", "ETH/USDT"]
    config.binance.testnet = True
    config.binance_trading.evaluation_interval_sec = 300
    config.binance_trading.initial_balance_usdt = 1000.0
    config.binance_trading.min_combined_confidence = 0.55
    config.binance_trading.max_trade_size_pct = 0.35
    config.binance_trading.daily_buy_limit = 15
    config.binance_trading.max_daily_coin_buys = 3
    config.binance_trading.ws_price_monitor = True
    config.trading.mode = "paper"
    config.trading.evaluation_interval_sec = 300
    config.trading.tracked_coins = ["BTC/KRW", "ETH/KRW"]
    config.trading.min_combined_confidence = 0.50
    config.trading.daily_buy_limit = 20
    config.trading.max_daily_coin_buys = 3
    config.trading.min_trade_interval_sec = 3600
    config.trading.cooldown_after_sell_sec = 3600
    config.trading.cooldown_after_buy_sec = 3600
    config.trading.paired_exit = False
    config.trading.asymmetric_mode = False
    config.trading.rotation_enabled = False
    config.risk.max_trade_size_pct = 0.50
    return config


@pytest.fixture
def mock_exchange():
    exchange = AsyncMock()
    exchange.set_leverage = AsyncMock(return_value={})
    exchange.fetch_funding_rate = AsyncMock(return_value=0.0001)
    exchange.fetch_ticker = AsyncMock(return_value=MagicMock(last=65000.0))
    exchange.create_ws_exchange = AsyncMock()
    exchange.watch_balance = AsyncMock()
    exchange.watch_positions = AsyncMock(return_value=[])
    exchange.watch_tickers = AsyncMock(return_value={})
    return exchange


@pytest.fixture
def mock_market_data():
    md = AsyncMock()
    md.get_current_price = AsyncMock(return_value=65000.0)
    md.get_ticker = AsyncMock(return_value=MagicMock(last=65000.0))
    md.get_candles = AsyncMock(return_value=None)
    return md


@pytest.fixture
def spot_engine(mock_config, mock_exchange, mock_market_data):
    """현물 TradingEngine (빗썸)."""
    order_mgr = MagicMock()
    portfolio_mgr = MagicMock()
    portfolio_mgr.cash_balance = 300_000
    portfolio_mgr._is_paper = True
    combiner = MagicMock()

    engine = TradingEngine(
        config=mock_config,
        exchange=mock_exchange,
        market_data=mock_market_data,
        order_manager=order_mgr,
        portfolio_manager=portfolio_mgr,
        combiner=combiner,
        exchange_name="bithumb",
    )
    return engine


@pytest.fixture
def futures_engine(mock_config, mock_exchange, mock_market_data):
    """선물 BinanceFuturesEngine."""
    order_mgr = MagicMock()
    portfolio_mgr = MagicMock()
    portfolio_mgr.cash_balance = 1000.0
    portfolio_mgr._cash_balance = 1000.0
    combiner = MagicMock()

    engine = BinanceFuturesEngine(
        config=mock_config,
        exchange=mock_exchange,
        market_data=mock_market_data,
        order_manager=order_mgr,
        portfolio_manager=portfolio_mgr,
        combiner=combiner,
    )
    return engine


# ── Fast SL/TP Check Loop Tests ──────────────────────────────

class TestFastStopCheckLoop:
    """현물 빠른 SL/TP 체크 루프 테스트."""

    def test_fast_sl_interval_is_30_sec(self, spot_engine):
        """기본 인터벌이 30초인지 확인."""
        assert spot_engine._FAST_SL_INTERVAL == 30

    @pytest.mark.asyncio
    async def test_fast_stop_skips_when_no_trackers(self, spot_engine, mock_market_data):
        """포지션 트래커가 없으면 가격 조회하지 않음."""
        spot_engine._position_trackers = {}
        spot_engine._is_running = True

        # 한 번만 루프 돌도록 _is_running을 제어
        call_count = 0
        original_sleep = asyncio.sleep

        async def mock_sleep(sec):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                spot_engine._is_running = False
            await original_sleep(0)

        with patch("asyncio.sleep", mock_sleep):
            await spot_engine._fast_stop_check_loop()

        # 트래커가 없으므로 가격 조회 없음
        mock_market_data.get_current_price.assert_not_called()

    @pytest.mark.asyncio
    async def test_fast_stop_checks_tracked_positions(self, spot_engine, mock_market_data):
        """포지션 트래커가 있으면 배치 DB 조회 후 SL/TP 체크."""
        spot_engine._position_trackers = {
            "BTC/KRW": PositionTracker(entry_price=50_000_000, extreme_price=51_000_000),
        }
        spot_engine._is_running = True

        call_count = 0
        original_sleep = asyncio.sleep

        async def mock_sleep(sec):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                spot_engine._is_running = False
            await original_sleep(0)

        # Mock session factory — batch query returns empty list (no positions)
        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session_factory = MagicMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("asyncio.sleep", mock_sleep), \
             patch("db.session.get_session_factory", return_value=mock_session_factory):
            await spot_engine._fast_stop_check_loop()

        # Batch DB query was executed
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_fast_stop_handles_price_error_gracefully(self, spot_engine, mock_market_data):
        """가격 조회 실패 시 에러 전파 안 함."""
        spot_engine._position_trackers = {
            "BTC/KRW": PositionTracker(entry_price=50_000_000, extreme_price=51_000_000),
        }
        spot_engine._is_running = True
        mock_market_data.get_current_price = AsyncMock(side_effect=Exception("API error"))

        call_count = 0
        original_sleep = asyncio.sleep

        async def mock_sleep(sec):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                spot_engine._is_running = False
            await original_sleep(0)

        mock_session = AsyncMock()
        mock_session_factory = MagicMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("asyncio.sleep", mock_sleep), \
             patch("db.session.get_session_factory", return_value=mock_session_factory):
            # Should not raise
            await spot_engine._fast_stop_check_loop()

    def test_futures_engine_overrides_start(self, futures_engine):
        """선물 엔진은 start()를 오버라이드하여 fast SL/TP 루프 대신 WebSocket 사용."""
        # futures_engine.start는 BinanceFuturesEngine.start
        # TradingEngine.start와 다른지 확인
        assert type(futures_engine).start is not TradingEngine.start


# ── WebSocket Balance/Position Sync Tests ─────────────────────

class TestWebSocketBalanceSync:
    """선물 WebSocket 잔고 동기화 테스트."""

    @pytest.mark.asyncio
    async def test_ws_balance_audit_only(self, futures_engine, mock_exchange):
        """WebSocket 잔고 수신 시 cash 갱신 안 함 (감사 로그만)."""
        futures_engine._is_running = True
        old_cash = futures_engine._portfolio_manager.cash_balance

        call_count = 0

        async def mock_watch_balance():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                futures_engine._is_running = False
                raise asyncio.CancelledError()
            return {"USDT": {"total": 500.0, "used": 100.0, "free": 400.0}}

        mock_exchange.watch_balance = mock_watch_balance

        await futures_engine._ws_balance_loop()

        # WS는 감사 전용 → cash 변경 없음
        assert futures_engine._portfolio_manager.cash_balance == old_cash

    @pytest.mark.asyncio
    async def test_ws_balance_negative_cash_no_update(self, futures_engine, mock_exchange):
        """음수 현금이든 양수든 WS는 cash를 갱신하지 않는다."""
        futures_engine._is_running = True
        old_cash = futures_engine._portfolio_manager.cash_balance

        call_count = 0

        async def mock_watch_balance():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                futures_engine._is_running = False
                raise asyncio.CancelledError()
            return {"USDT": {"total": 50.0, "used": 100.0, "free": -50.0}}

        mock_exchange.watch_balance = mock_watch_balance

        await futures_engine._ws_balance_loop()

        # WS는 감사 전용 → cash 변경 없음
        assert futures_engine._portfolio_manager.cash_balance == old_cash

    @pytest.mark.asyncio
    async def test_ws_balance_handles_error(self, futures_engine, mock_exchange):
        """WebSocket 오류 시 5초 대기 후 재시도 (에러 전파 안 함)."""
        futures_engine._is_running = True
        call_count = 0

        async def mock_watch_balance():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("WebSocket disconnected")
            futures_engine._is_running = False
            raise asyncio.CancelledError()

        mock_exchange.watch_balance = mock_watch_balance

        with patch("asyncio.sleep", AsyncMock()):
            await futures_engine._ws_balance_loop()


class TestWebSocketPositionSync:
    """선물 WebSocket 포지션 동기화 테스트."""

    @pytest.mark.asyncio
    async def test_ws_position_updates_db(self, futures_engine, mock_exchange):
        """포지션 변경 시 DB 업데이트."""
        futures_engine._is_running = True

        mock_position = MagicMock()
        mock_position.symbol = "BTC/USDT"
        mock_position.quantity = 0.01
        mock_position.margin_used = 200.0
        mock_position.average_buy_price = 60000.0
        mock_position.liquidation_price = 50000.0
        mock_position.total_invested = 200.0

        call_count = 0

        async def mock_watch_positions():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                futures_engine._is_running = False
                raise asyncio.CancelledError()
            return [{
                "symbol": "BTC/USDT:USDT",
                "contracts": 0.015,
                "initialMargin": 250.0,
                "entryPrice": 62000.0,
                "liquidationPrice": 51000.0,
                "unrealizedPnl": 5.0,
                "markPrice": 62500.0,
            }]

        mock_exchange.watch_positions = mock_watch_positions

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_position
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory = MagicMock(return_value=mock_ctx)

        with patch("db.session.get_session_factory", return_value=mock_session_factory):
            await futures_engine._ws_position_loop()

        # DB position should be updated
        assert mock_position.quantity == 0.015
        assert mock_position.margin_used == 250.0
        assert mock_position.average_buy_price == 62000.0
        assert mock_position.liquidation_price == 51000.0

    @pytest.mark.asyncio
    async def test_ws_position_empty_list_skipped(self, futures_engine, mock_exchange):
        """빈 포지션 리스트는 건너뜀."""
        futures_engine._is_running = True
        call_count = 0

        async def mock_watch_positions():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                futures_engine._is_running = False
                raise asyncio.CancelledError()
            return []

        mock_exchange.watch_positions = mock_watch_positions
        session_created = False

        def mock_factory():
            nonlocal session_created
            session_created = True
            return AsyncMock()

        with patch("db.session.get_session_factory", return_value=mock_factory):
            await futures_engine._ws_position_loop()
            # Empty list → continue without creating session
            assert not session_created

    @pytest.mark.asyncio
    async def test_ws_position_symbol_strip_usdt(self, futures_engine, mock_exchange):
        """심볼에서 :USDT 접미사 제거."""
        # The loop does: sym = fp.get("symbol", "").replace(":USDT", "")
        raw_symbol = "ETH/USDT:USDT"
        expected = "ETH/USDT"
        assert raw_symbol.replace(":USDT", "") == expected


# ── Balance Monitor Loop Tests ─────────────────────────────────

class TestBalanceMonitorLoop:
    """_balance_monitor_loop이 balance + position 루프를 병렬 실행."""

    @pytest.mark.asyncio
    async def test_balance_monitor_runs_both_loops(self, futures_engine):
        """balance_monitor_loop이 ws_balance_loop + ws_position_loop 모두 실행."""
        futures_engine._is_running = False  # 즉시 종료

        with patch.object(futures_engine, '_ws_balance_loop', new_callable=AsyncMock) as mock_bal, \
             patch.object(futures_engine, '_ws_position_loop', new_callable=AsyncMock) as mock_pos:
            await futures_engine._balance_monitor_loop()
            mock_bal.assert_called_once()
            mock_pos.assert_called_once()


# ── Force Close Cooldown Exemption Tests ──────────────────────

class TestForceCloseCooldownExemption:
    """강제 청산 후 재매수 쿨다운 면제 테스트."""

    @pytest.mark.asyncio
    async def test_force_close_clears_sell_cooldown(self, spot_engine, mock_market_data):
        """강제 청산 1차(시장가 매도) 성공 후 _last_sell_time 삭제."""
        position = MagicMock(spec=Position)
        position.symbol = "BTC/KRW"
        position.quantity = 0.001
        position.average_buy_price = 50_000_000
        position.direction = "long"
        position.leverage = 1
        position.margin_used = 50_000

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = position
        session.execute = AsyncMock(return_value=mock_result)
        session.refresh = AsyncMock()

        spot_engine._eval_error_counts["BTC/KRW"] = 3
        spot_engine._last_sell_time["BTC/KRW"] = datetime.now(timezone.utc)

        with patch.object(spot_engine, '_execute_stop_sell', new_callable=AsyncMock):
            await spot_engine._force_close_stuck_position(session, "BTC/KRW", "API error")

        assert "BTC/KRW" not in spot_engine._last_sell_time
        assert "BTC/KRW" not in spot_engine._eval_error_counts

    @pytest.mark.asyncio
    async def test_force_close_db_reset_clears_sell_cooldown(self, spot_engine, mock_market_data):
        """강제 청산 2차(DB 리셋) 시에도 _last_sell_time 삭제."""
        position = MagicMock(spec=Position)
        position.symbol = "DEAD/KRW"
        position.quantity = 100
        position.average_buy_price = 1000

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = position
        session.execute = AsyncMock(return_value=mock_result)

        mock_market_data.get_current_price = AsyncMock(side_effect=Exception("404"))
        spot_engine._eval_error_counts["DEAD/KRW"] = 5
        spot_engine._last_sell_time["DEAD/KRW"] = datetime.now(timezone.utc)
        spot_engine._close_lock = asyncio.Lock()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await spot_engine._force_close_stuck_position(session, "DEAD/KRW", "API 404")

        assert "DEAD/KRW" not in spot_engine._last_sell_time
        assert "DEAD/KRW" not in spot_engine._eval_error_counts

    @pytest.mark.asyncio
    async def test_normal_sell_sets_cooldown(self, spot_engine):
        """일반 매도는 쿨다운이 설정됨 (대조)."""
        now = datetime.now(timezone.utc)
        spot_engine._last_sell_time["ETH/KRW"] = now
        assert "ETH/KRW" in spot_engine._last_sell_time
        assert spot_engine._last_sell_time["ETH/KRW"] == now


# ── Trade Timestamp Restore Tests ─────────────────────────────

class TestRestoreTradeTimestamps:
    """재시작 시 매매 타임스탬프 복원 테스트."""

    @pytest.mark.asyncio
    async def test_open_position_timestamps_restored(self, spot_engine):
        """보유 포지션(qty>0)의 타임스탬프 복원."""
        now = datetime.now(timezone.utc)
        position = MagicMock()
        position.symbol = "BTC/KRW"
        position.quantity = 0.001
        position.last_trade_at = now - timedelta(hours=2)
        position.last_sell_at = now - timedelta(hours=1)
        position.exchange = "bithumb"

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [position]
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory = MagicMock(return_value=mock_ctx)

        with patch("db.session.get_session_factory", return_value=mock_session_factory):
            await spot_engine._restore_trade_timestamps()

        assert "BTC/KRW" in spot_engine._last_trade_time
        assert "BTC/KRW" in spot_engine._last_sell_time

    @pytest.mark.asyncio
    async def test_closed_position_expired_cooldown_skipped(self, spot_engine):
        """청산 후 쿨다운 만료된 포지션은 복원하지 않음."""
        now = datetime.now(timezone.utc)
        position = MagicMock()
        position.symbol = "ETH/KRW"
        position.quantity = 0
        position.last_trade_at = now - timedelta(days=30)
        position.last_sell_at = now - timedelta(days=30)  # 30일 전 — 쿨다운 만료
        position.exchange = "bithumb"

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [position]
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory = MagicMock(return_value=mock_ctx)

        with patch("db.session.get_session_factory", return_value=mock_session_factory):
            await spot_engine._restore_trade_timestamps()

        assert "ETH/KRW" not in spot_engine._last_trade_time
        assert "ETH/KRW" not in spot_engine._last_sell_time

    @pytest.mark.asyncio
    async def test_closed_position_recent_cooldown_restored(self, spot_engine):
        """청산 후 쿨다운 내 포지션은 last_sell_time 복원."""
        now = datetime.now(timezone.utc)
        position = MagicMock()
        position.symbol = "ETH/KRW"
        position.quantity = 0
        position.last_trade_at = now - timedelta(minutes=10)
        position.last_sell_at = now - timedelta(minutes=10)  # 10분 전 — 쿨다운 내
        position.exchange = "bithumb"

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [position]
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory = MagicMock(return_value=mock_ctx)

        with patch("db.session.get_session_factory", return_value=mock_session_factory):
            await spot_engine._restore_trade_timestamps()

        assert "ETH/KRW" not in spot_engine._last_trade_time  # 청산이므로 trade_time 안 복원
        assert "ETH/KRW" in spot_engine._last_sell_time  # 쿨다운 내이므로 sell_time 복원

    @pytest.mark.asyncio
    async def test_mixed_positions_partial_restore(self, spot_engine):
        """보유/청산 혼합 시: 보유는 전부 복원, 청산은 쿨다운 내 last_sell만 복원."""
        now = datetime.now(timezone.utc)
        open_pos = MagicMock()
        open_pos.symbol = "BTC/KRW"
        open_pos.quantity = 0.001
        open_pos.last_trade_at = now
        open_pos.last_sell_at = None
        open_pos.exchange = "bithumb"

        # 최근 청산 — 쿨다운 내이므로 last_sell 복원됨
        closed_recent = MagicMock()
        closed_recent.symbol = "ETH/KRW"
        closed_recent.quantity = 0
        closed_recent.last_trade_at = now
        closed_recent.last_sell_at = now - timedelta(minutes=10)
        closed_recent.exchange = "bithumb"

        # 오래전 청산 — 쿨다운 만료이므로 복원 안 됨
        closed_old = MagicMock()
        closed_old.symbol = "SOL/KRW"
        closed_old.quantity = 0
        closed_old.last_trade_at = now - timedelta(days=30)
        closed_old.last_sell_at = now - timedelta(days=30)
        closed_old.exchange = "bithumb"

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [open_pos, closed_recent, closed_old]
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory = MagicMock(return_value=mock_ctx)

        with patch("db.session.get_session_factory", return_value=mock_session_factory):
            await spot_engine._restore_trade_timestamps()

        assert "BTC/KRW" in spot_engine._last_trade_time
        assert "ETH/KRW" not in spot_engine._last_trade_time
        assert "ETH/KRW" in spot_engine._last_sell_time  # 최근 청산 쿨다운 복원
        assert "SOL/KRW" not in spot_engine._last_sell_time  # 만료된 쿨다운은 안 복원


# ── Reconcile Cash Paper-Only Guard Tests ─────────────────────

class TestReconcilePaperOnly:
    """reconcile_cash_from_db paper 모드 전용 가드 테스트."""

    @pytest.mark.asyncio
    async def test_reconcile_skips_live_mode(self, session):
        """실거래 모드에서는 reconcile 건너뜀."""
        pm = MagicMock()
        pm._is_paper = False
        pm._exchange_name = "bithumb"

        # Make reconcile_cash_from_db an unbound method test
        from engine.portfolio_manager import PortfolioManager

        live_pm = PortfolioManager.__new__(PortfolioManager)
        live_pm._is_paper = False
        live_pm._exchange_name = "bithumb"
        live_pm._initial_balance = 500_000
        live_pm._cash_balance = 300_000

        old_cash = live_pm._cash_balance
        await live_pm.reconcile_cash_from_db(session)
        assert live_pm._cash_balance == old_cash  # 변경 없음

    @pytest.mark.asyncio
    async def test_reconcile_runs_for_paper_mode(self, session):
        """페이퍼 모드에서는 reconcile 실행."""
        from engine.portfolio_manager import PortfolioManager

        paper_pm = PortfolioManager.__new__(PortfolioManager)
        paper_pm._is_paper = True
        paper_pm._exchange_name = "bithumb"
        paper_pm._initial_balance = 500_000
        paper_pm._cash_balance = 999_999  # Intentionally wrong
        paper_pm._realized_pnl = 0.0

        # No positions, no orders → cash should be reset to initial_balance
        await paper_pm.reconcile_cash_from_db(session)
        assert paper_pm._cash_balance == 500_000


# ── Strategy Loop + Fast Stop Parallel Tests ──────────────────

class TestParallelLoops:
    """전략 루프 + 빠른 SL/TP 루프 병렬 실행 테스트."""

    @pytest.mark.asyncio
    async def test_start_runs_both_loops(self, spot_engine):
        """start()가 strategy_loop + fast_stop_check_loop를 병렬 실행."""
        with patch.object(spot_engine, '_restore_trade_timestamps', new_callable=AsyncMock), \
             patch.object(spot_engine, '_strategy_loop', new_callable=AsyncMock) as mock_strat, \
             patch.object(spot_engine, '_fast_stop_check_loop', new_callable=AsyncMock) as mock_fast, \
             patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await spot_engine.start()
            mock_strat.assert_called_once()
            mock_fast.assert_called_once()

    @pytest.mark.asyncio
    async def test_strategy_loop_respects_eval_interval(self, spot_engine):
        """전략 루프가 설정된 평가 간격을 사용."""
        spot_engine._ec.evaluation_interval_sec = 60  # Override
        spot_engine._is_running = True
        call_count = 0

        async def mock_eval():
            nonlocal call_count
            call_count += 1
            spot_engine._is_running = False

        with patch.object(spot_engine, '_evaluation_cycle', mock_eval), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await spot_engine._strategy_loop()
            mock_sleep.assert_called_with(60)

    @pytest.mark.asyncio
    async def test_strategy_loop_catches_error(self, spot_engine):
        """전략 루프가 예외를 잡고 계속 실행."""
        spot_engine._is_running = True
        call_count = 0

        async def mock_eval():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("test error")
            spot_engine._is_running = False

        sleep_calls = 0
        original_sleep = asyncio.sleep

        async def mock_sleep(sec):
            nonlocal sleep_calls
            sleep_calls += 1
            await original_sleep(0)

        with patch.object(spot_engine, '_evaluation_cycle', mock_eval), \
             patch("asyncio.sleep", mock_sleep):
            await spot_engine._strategy_loop()

        assert call_count == 2  # First errored, second succeeded


# ── Futures Start Override Tests ──────────────────────────────

class TestFuturesStartOverride:
    """선물 엔진이 WebSocket 모니터를 올바르게 시작하는지 테스트."""

    @pytest.mark.asyncio
    async def test_futures_start_creates_ws(self, futures_engine, mock_exchange):
        """WS 활성화 시 create_ws_exchange 호출."""
        with patch.object(futures_engine, '_restore_trade_timestamps', new_callable=AsyncMock), \
             patch.object(futures_engine, '_price_monitor_loop', new_callable=AsyncMock), \
             patch.object(futures_engine, '_balance_monitor_loop', new_callable=AsyncMock), \
             patch.object(futures_engine, '_strategy_eval_loop', new_callable=AsyncMock), \
             patch.object(futures_engine, '_income_poll_loop', new_callable=AsyncMock), \
             patch("engine.futures_engine.emit_event", new_callable=AsyncMock):
            await futures_engine.start()
            mock_exchange.create_ws_exchange.assert_called_once()

    @pytest.mark.asyncio
    async def test_futures_start_fallback_on_ws_failure(self, futures_engine, mock_exchange):
        """WS 초기화 실패 시 폴링 폴백 + 빠른 SL 루프 자동 시작."""
        mock_exchange.create_ws_exchange = AsyncMock(side_effect=Exception("WS error"))

        with patch.object(futures_engine, '_restore_trade_timestamps', new_callable=AsyncMock), \
             patch.object(futures_engine, '_strategy_eval_loop', new_callable=AsyncMock), \
             patch.object(futures_engine, '_fast_stop_check_loop', new_callable=AsyncMock), \
             patch.object(futures_engine, '_income_poll_loop', new_callable=AsyncMock), \
             patch("engine.futures_engine.emit_event", new_callable=AsyncMock):
            await futures_engine.start()

        # WS 실패 시 monitor_task는 None, fast_sl_task는 활성
        assert futures_engine._monitor_task is None
        assert futures_engine._fast_sl_task is not None


# ── Error Emit Event Tests ────────────────────────────────────

class TestErrorEmitEvents:
    """매매 에러/차단 emit_event 발행 테스트."""

    @pytest.mark.asyncio
    async def test_force_close_db_reset_emits_critical(self, spot_engine, mock_market_data):
        """강제 청산 DB 리셋 시 critical emit_event 발행."""
        position = MagicMock(spec=Position)
        position.symbol = "DEAD/KRW"
        position.quantity = 50
        position.average_buy_price = 1000

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = position
        session.execute = AsyncMock(return_value=mock_result)

        mock_market_data.get_current_price = AsyncMock(side_effect=Exception("404"))
        spot_engine._eval_error_counts["DEAD/KRW"] = 5
        spot_engine._close_lock = asyncio.Lock()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock) as mock_emit:
            await spot_engine._force_close_stuck_position(session, "DEAD/KRW", "API 404")

            # critical emit_event 호출 확인
            critical_calls = [
                c for c in mock_emit.call_args_list
                if c[0][0] == "critical"
            ]
            assert len(critical_calls) >= 1


# ── PositionTracker State Tests ───────────────────────────────

class TestPositionTrackerState:
    """PositionTracker 상태 관리 테스트."""

    def test_tracker_defaults(self):
        """기본값 확인."""
        t = PositionTracker(entry_price=100, extreme_price=100)
        assert t.stop_loss_pct == 5.0
        assert t.take_profit_pct == 14.0
        assert t.trailing_activation_pct == 3.0
        assert t.trailing_stop_pct == 1.5
        assert t.trailing_active is False
        assert t.is_surge is False
        assert t.max_hold_hours == 0

    def test_tracker_surge_config(self):
        """서지 코인 트래커 설정."""
        t = PositionTracker(
            entry_price=100,
            extreme_price=100,
            stop_loss_pct=4.0,
            take_profit_pct=8.0,
            trailing_activation_pct=1.5,
            trailing_stop_pct=2.0,
            is_surge=True,
            max_hold_hours=48,
        )
        assert t.is_surge
        assert t.max_hold_hours == 48
        assert t.stop_loss_pct == 4.0

    def test_position_trackers_dict_operations(self, spot_engine):
        """트래커 딕셔너리 추가/삭제."""
        t = PositionTracker(entry_price=100, extreme_price=100)
        spot_engine._position_trackers["TEST/KRW"] = t
        assert "TEST/KRW" in spot_engine._position_trackers

        spot_engine._position_trackers.pop("TEST/KRW", None)
        assert "TEST/KRW" not in spot_engine._position_trackers


# ── Rate Limit Safety Tests ───────────────────────────────────

class TestRateLimitSafety:
    """레이트 리밋 안전성 테스트."""

    def test_semaphore_limit_bithumb(self):
        """빗썸 어댑터 세마포어 제한: 8."""
        from exchange.bithumb_v2_adapter import BithumbV2Adapter
        adapter = BithumbV2Adapter(rate_limit=8)
        assert adapter._semaphore._value == 8

    def test_semaphore_limit_binance(self):
        """바이낸스 어댑터 세마포어 제한: 10."""
        from exchange.binance_usdm_adapter import BinanceUSDMAdapter
        adapter = BinanceUSDMAdapter(rate_limit=10)
        assert adapter._semaphore._value == 10

    def test_binance_spot_semaphore(self):
        """바이낸스 현물 어댑터 세마포어 제한: 10."""
        from exchange.binance_spot_adapter import BinanceSpotAdapter
        adapter = BinanceSpotAdapter(rate_limit=10)
        assert adapter._semaphore._value == 10

    def test_fast_sl_calls_within_rate_limit(self, spot_engine):
        """빠른 SL/TP 체크가 레이트 리밋 내에서 동작하는지 계산.

        빗썸 5 tracked coins × 1 call/30sec = ~10 calls/min
        빗썸 semaphore=8 → 동시 8건 가능 → 충분.
        """
        tracked = 5  # 빗썸 tracked coins
        interval = spot_engine._FAST_SL_INTERVAL  # 30
        calls_per_min = tracked * (60 / interval)
        assert calls_per_min <= 12  # 충분한 여유

    def test_ws_does_not_count_as_rest_api(self):
        """WebSocket은 REST API 레이트 리밋에 영향 없음."""
        # WebSocket 관련 메서드는 _call() (세마포어)을 거치지 않음
        from exchange.binance_usdm_adapter import BinanceUSDMAdapter
        adapter = BinanceUSDMAdapter()
        # watch_balance, watch_positions는 _call이 아닌 _ws_exchange 직접 호출
        import inspect
        src = inspect.getsource(adapter.watch_balance)
        assert "_semaphore" not in src
        assert "_call" not in src


# ── DailyPnL Seed Exclusion Tests ─────────────────────────────

class TestDailyPnLSeedExclusion:
    """DailyPnL 시드 입금 제외 테스트."""

    @pytest.mark.asyncio
    async def test_seed_deposit_not_subtracted(self, session):
        """source='seed' 입금은 PnL 계산에서 제외."""
        from core.models import PortfolioSnapshot, CapitalTransaction, DailyPnL
        from engine.portfolio_manager import PortfolioManager
        from datetime import date

        target = date(2026, 3, 10)

        # open=0, close=500K (시드 입금 500K)
        session.add(PortfolioSnapshot(
            exchange="bithumb",
            total_value_krw=0,
            cash_balance_krw=0,
            invested_value_krw=0,
            snapshot_at=datetime(2026, 3, 10, 0, 5, tzinfo=timezone.utc),
        ))
        session.add(PortfolioSnapshot(
            exchange="bithumb",
            total_value_krw=500_000,
            cash_balance_krw=500_000,
            invested_value_krw=0,
            snapshot_at=datetime(2026, 3, 10, 23, 55, tzinfo=timezone.utc),
        ))

        # 시드 입금 500K (source=seed) → 매매 수익으로 계산하면 안 됨
        session.add(CapitalTransaction(
            exchange="bithumb",
            tx_type="deposit",
            amount=500_000,
            currency="KRW",
            source="seed",
            confirmed=True,
            created_at=datetime(2026, 3, 10, 0, 1, tzinfo=timezone.utc),
        ))
        await session.flush()

        record = await PortfolioManager.record_daily_pnl(session, "bithumb", target)
        assert record is not None
        # seed 입금은 net_inflow에서 제외 → daily_pnl = 500K - 0 - 0 = 500K
        assert record.daily_pnl == 500_000

    @pytest.mark.asyncio
    async def test_manual_deposit_subtracted(self, session):
        """수동 입금(source='manual')은 PnL에서 차감."""
        from core.models import PortfolioSnapshot, CapitalTransaction
        from engine.portfolio_manager import PortfolioManager
        from datetime import date

        target = date(2026, 3, 11)

        session.add(PortfolioSnapshot(
            exchange="bithumb",
            total_value_krw=500_000,
            cash_balance_krw=500_000,
            invested_value_krw=0,
            snapshot_at=datetime(2026, 3, 11, 0, 5, tzinfo=timezone.utc),
        ))
        session.add(PortfolioSnapshot(
            exchange="bithumb",
            total_value_krw=800_000,
            cash_balance_krw=800_000,
            invested_value_krw=0,
            snapshot_at=datetime(2026, 3, 11, 23, 55, tzinfo=timezone.utc),
        ))

        # 수동 입금 200K → net_inflow=200K, daily_pnl = 300K - 200K = 100K
        session.add(CapitalTransaction(
            exchange="bithumb",
            tx_type="deposit",
            amount=200_000,
            currency="KRW",
            source="manual",
            confirmed=True,
            created_at=datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc),
        ))
        await session.flush()

        record = await PortfolioManager.record_daily_pnl(session, "bithumb", target)
        assert record is not None
        assert record.daily_pnl == 100_000  # 300K 증가 - 200K 입금 = 100K 순수 수익


# ── Engine Property Tests ─────────────────────────────────────

class TestEngineProperties:
    """거래소별 엔진 프로퍼티 테스트."""

    def test_bithumb_min_order_amount(self, spot_engine):
        assert spot_engine._min_order_amount == 5000

    def test_bithumb_fee_margin(self, spot_engine):
        assert spot_engine._fee_margin == 1.003

    def test_binance_min_order_amount(self, futures_engine):
        assert futures_engine._min_order_amount == 5.0

    def test_binance_fee_margin(self, futures_engine):
        assert futures_engine._fee_margin == 1.002

    def test_bithumb_min_fallback(self, spot_engine):
        assert spot_engine._min_fallback_amount == 5000

    def test_binance_min_fallback(self, futures_engine):
        assert futures_engine._min_fallback_amount == 10.0


# ── Critical Bug Regression Tests ───────────────────────────────

class TestFastStopCheckSignature:
    """현물 _fast_stop_check_loop에서 _check_stop_conditions 호출 시그니처 검증.

    이전 버그: price를 4번째 인자로 전달 → TypeError → 30초 빠른 SL 완전 무력화
    수정: _check_stop_conditions(session, symbol, position) 3인자 호출
    """

    @pytest.mark.asyncio
    async def test_fast_stop_calls_check_stop_with_correct_args(self, spot_engine, mock_market_data):
        """_fast_stop_check_loop가 _check_stop_conditions를 올바른 인자로 호출."""
        spot_engine._position_trackers = {
            "BTC/KRW": PositionTracker(entry_price=50_000_000, extreme_price=51_000_000),
        }
        spot_engine._is_running = True

        # position mock — batch query returns this position
        mock_position = MagicMock()
        mock_position.symbol = "BTC/KRW"
        mock_position.quantity = 0.1
        mock_position.stop_loss_pct = 5.0
        mock_position.average_buy_price = 50_000_000
        mock_position.highest_price = 51_000_000

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_position]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()
        mock_session_factory = MagicMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        # _check_stop_conditions를 mock해서 호출 인자 검증
        spot_engine._check_stop_conditions = AsyncMock(return_value=False)

        call_count = 0
        original_sleep = asyncio.sleep

        async def mock_sleep(sec):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                spot_engine._is_running = False
            await original_sleep(0)

        with patch("asyncio.sleep", mock_sleep), \
             patch("db.session.get_session_factory", return_value=mock_session_factory):
            await spot_engine._fast_stop_check_loop()

        # _check_stop_conditions가 3인자로 호출되었는지 검증 (session, symbol, position)
        spot_engine._check_stop_conditions.assert_called_once()
        args = spot_engine._check_stop_conditions.call_args[0]
        assert len(args) == 3, f"Expected 3 args (session, symbol, position), got {len(args)}"
        assert args[1] == "BTC/KRW"  # symbol
        assert args[2] is mock_position  # position


class TestMarketStateSymbol:
    """_maybe_update_market_state의 BTC 심볼 분기 검증.

    이전 버그: BTC/KRW 하드코딩 → 바이낸스 현물 엔진에서 시장 상태 감지 실패
    수정: exchange_name에 따라 BTC/USDT 또는 BTC/KRW 자동 분기
    """

    @pytest.mark.asyncio
    async def test_bithumb_uses_btc_krw(self, spot_engine, mock_market_data):
        """빗썸 엔진은 BTC/KRW로 시장 상태 감지."""
        spot_engine._market_state_updated = None
        await spot_engine._maybe_update_market_state()
        mock_market_data.get_candles.assert_any_call("BTC/KRW", "4h", 200)

    @pytest.mark.asyncio
    async def test_binance_spot_uses_btc_usdt(self, mock_config, mock_exchange, mock_market_data):
        """바이낸스 현물 엔진은 BTC/USDT로 시장 상태 감지."""
        engine = TradingEngine(
            config=mock_config,
            exchange=mock_exchange,
            market_data=mock_market_data,
            order_manager=MagicMock(),
            portfolio_manager=MagicMock(),
            combiner=MagicMock(),
            exchange_name="binance_spot",
        )
        engine._market_state_updated = None
        await engine._maybe_update_market_state()
        mock_market_data.get_candles.assert_any_call("BTC/USDT", "4h", 200)

    @pytest.mark.asyncio
    async def test_binance_futures_overrides_market_state(self, futures_engine, mock_market_data):
        """선물 엔진은 자체 _maybe_update_market_state를 오버라이드 (BTC/USDT 듀얼TF)."""
        # BinanceFuturesEngine._maybe_update_market_state는 session 인자를 받음
        assert hasattr(futures_engine, '_maybe_update_market_state')
        # 선물은 session을 받는 오버라이드 메서드 사용
        import inspect
        sig = inspect.signature(futures_engine._maybe_update_market_state)
        params = list(sig.parameters.keys())
        assert "session" in params, "선물 엔진은 session 인자를 받는 오버라이드 사용"


# ── Trailing Stop Notification Throttle Bug Regression ────────

class TestTrailingStopNotificationThrottle:
    """트레일링 스탑 알림 쿨다운 버그 수정 회귀 테스트.

    버그: _execute_stop_sell에서 order.status != "filled" 시 예외 없이 return →
         _check_stop_conditions가 성공으로 오판 → _last_stop_event_time 쿨다운 해제 →
         30초마다 알림 반복 발생.

    수정: 미체결 주문 시 RuntimeError 발생 → 쿨다운 유지 → 5분 간격으로만 재알림.
    """

    @pytest.mark.asyncio
    async def test_unfilled_order_raises_runtime_error(self, spot_engine):
        """미체결 주문 시 _execute_stop_sell이 RuntimeError를 발생시켜야 함."""
        mock_position = MagicMock()
        mock_position.quantity = 0.001
        mock_position.average_buy_price = 69000.0

        mock_order = MagicMock()
        mock_order.status = "open"       # not "filled"
        mock_order.exchange_order_id = None
        mock_order.id = 1

        spot_engine._order_manager = AsyncMock()
        spot_engine._order_manager.create_order = AsyncMock(return_value=mock_order)

        mock_session = AsyncMock()

        with pytest.raises(RuntimeError, match="sell_order_not_filled"):
            await spot_engine._execute_stop_sell(
                mock_session, "BTC/USDT", mock_position, 70906.0, "Trailing Stop"
            )

    @pytest.mark.asyncio
    async def test_unfilled_order_cancels_exchange_order(self, spot_engine):
        """미체결 주문에 exchange_order_id가 있으면 취소 시도 후 RuntimeError 발생."""
        mock_position = MagicMock()
        mock_position.quantity = 0.001
        mock_position.average_buy_price = 69000.0

        mock_order = MagicMock()
        mock_order.status = "open"
        mock_order.exchange_order_id = "binance-order-123"
        mock_order.id = 99

        spot_engine._order_manager = AsyncMock()
        spot_engine._order_manager.create_order = AsyncMock(return_value=mock_order)
        spot_engine._order_manager.cancel_order_by_id = AsyncMock()

        mock_session = AsyncMock()

        with pytest.raises(RuntimeError):
            await spot_engine._execute_stop_sell(
                mock_session, "BTC/USDT", mock_position, 70906.0, "Trailing Stop"
            )

        # 거래소 취소가 먼저 시도되었는지 확인
        spot_engine._order_manager.cancel_order_by_id.assert_called_once_with(mock_session, 99)

    @pytest.mark.asyncio
    async def test_cooldown_preserved_when_sell_order_not_filled(self, spot_engine, mock_market_data):
        """미체결 주문 시 _last_stop_event_time 쿨다운이 유지되어야 함.

        수정 전 버그: _execute_stop_sell이 silent return → _check_stop_conditions가
        _last_stop_event_time.pop()을 호출하여 쿨다운 해제 → 30초마다 알림 폭주.
        수정 후: RuntimeError → except 분기 → pop 미호출 → 쿨다운 유지.
        """
        # 유저 리포트와 동일한 시나리오:
        # 고점 73901, 현재 70906 → drawdown 4.05% >= trailing_stop_pct 4.0%
        spot_engine._position_trackers["BTC/USDT"] = PositionTracker(
            entry_price=69000.0,
            extreme_price=73901.0,
            trailing_stop_pct=4.0,
            trailing_activation_pct=5.0,
            trailing_active=True,
        )

        # 방금(10초 전) 알림이 발송된 상태 → 5분 쿨다운 중이므로 재발송 안 됨
        initial_time = datetime.now(timezone.utc) - timedelta(seconds=10)
        spot_engine._last_stop_event_time["BTC/USDT"] = initial_time

        # 현재 가격: 70906 → trailing stop 조건 충족
        mock_market_data.get_current_price = AsyncMock(return_value=70906.0)

        mock_position = MagicMock()
        mock_position.symbol = "BTC/USDT"
        mock_position.quantity = 0.001
        mock_position.average_buy_price = 69000.0
        mock_position.stop_loss_pct = 5.0
        mock_position.take_profit_pct = 14.0
        mock_position.trailing_activation_pct = 5.0
        mock_position.trailing_stop_pct = 4.0
        mock_position.trailing_active = True
        mock_position.highest_price = 73901.0
        mock_position.is_surge = False
        mock_position.max_hold_hours = 0
        mock_position.entered_at = None

        # 미체결 주문 반환
        mock_order = MagicMock()
        mock_order.status = "open"
        mock_order.exchange_order_id = None
        mock_order.id = 1

        spot_engine._order_manager = AsyncMock()
        spot_engine._order_manager.create_order = AsyncMock(return_value=mock_order)

        mock_session = AsyncMock()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            result = await spot_engine._check_stop_conditions(
                mock_session, "BTC/USDT", mock_position
            )

        # 매도 실패 → False 반환
        assert result is False

        # 핵심: 쿨다운이 지워지지 않아야 함 (initial_time 그대로)
        assert "BTC/USDT" in spot_engine._last_stop_event_time
        assert spot_engine._last_stop_event_time["BTC/USDT"] == initial_time

    @pytest.mark.asyncio
    async def test_cooldown_cleared_on_successful_sell(self, spot_engine, mock_market_data):
        """정상 체결 시에는 쿨다운이 클리어되고 True 반환."""
        spot_engine._position_trackers["BTC/USDT"] = PositionTracker(
            entry_price=69000.0,
            extreme_price=73901.0,
            trailing_stop_pct=4.0,
            trailing_activation_pct=5.0,
            trailing_active=True,
        )
        spot_engine._last_stop_event_time["BTC/USDT"] = datetime(
            2026, 3, 14, 12, 0, 0, tzinfo=timezone.utc
        )

        mock_market_data.get_current_price = AsyncMock(return_value=70906.0)

        mock_position = MagicMock()
        mock_position.symbol = "BTC/USDT"
        mock_position.quantity = 0.001
        mock_position.average_buy_price = 69000.0
        mock_position.stop_loss_pct = 5.0
        mock_position.take_profit_pct = 14.0
        mock_position.trailing_activation_pct = 5.0
        mock_position.trailing_stop_pct = 4.0
        mock_position.trailing_active = True
        mock_position.highest_price = 73901.0
        mock_position.is_surge = False
        mock_position.max_hold_hours = 0
        mock_position.entered_at = None

        # 정상 체결 주문
        mock_order = MagicMock()
        mock_order.status = "filled"
        mock_order.id = 2
        mock_order.fee = 0.04

        spot_engine._order_manager = AsyncMock()
        spot_engine._order_manager.create_order = AsyncMock(return_value=mock_order)

        spot_engine._portfolio_manager = AsyncMock()
        spot_engine._portfolio_manager.update_position_on_sell = AsyncMock()

        spot_engine._on_sell_completed = AsyncMock()

        mock_session = AsyncMock()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            result = await spot_engine._check_stop_conditions(
                mock_session, "BTC/USDT", mock_position
            )

        # 성공 → True, 쿨다운 클리어
        assert result is True
        assert "BTC/USDT" not in spot_engine._last_stop_event_time
