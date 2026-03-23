"""V2 선물 엔진 WS 실시간 모니터링 테스트 (COIN-39).

테스트 대상:
  - _ws_price_monitor_loop: WS 가격 수신 → SL/TP 체크
  - _realtime_stop_check: 인메모리 빠른 SL/TP 필터
  - _fast_stop_check_loop: WS 실패 시 30초 폴링 폴백
  - _ws_balance_loop: 잔고 실시간 감사
  - _ws_position_loop: 포지션 실시간 DB 동기화
  - _ws_reconnect: 지수 백오프 재연결
  - start/stop 통합: WS 태스크 생성/정리
  - PositionStateTracker ATR 캐싱
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from config import AppConfig
from core.enums import Direction
from core.models import Position
from engine.futures_engine_v2 import FuturesEngineV2
from engine.position_state_tracker import PositionState, PositionStateTracker
from exchange.data_models import Balance


# ── Fixtures ─────────────────────────────────────────


@pytest.fixture
def mock_exchange():
    exchange = AsyncMock()
    exchange.set_leverage = AsyncMock()
    exchange.fetch_balance = AsyncMock(
        return_value={
            "USDT": Balance(currency="USDT", free=500.0, used=0.0, total=500.0),
        }
    )
    exchange.close_ws = AsyncMock()
    exchange.create_ws_exchange = AsyncMock()
    exchange.watch_tickers = AsyncMock(return_value={})
    exchange.watch_balance = AsyncMock(return_value={})
    exchange.watch_positions = AsyncMock(return_value=[])
    exchange.fetch_ticker = AsyncMock(
        return_value=MagicMock(last=80000.0)
    )
    return exchange


@pytest.fixture
def mock_market_data():
    md = AsyncMock()
    md.get_current_price = AsyncMock(return_value=80000.0)
    md.get_ohlcv_df = AsyncMock(return_value=None)
    return md


@pytest.fixture
def mock_pm():
    pm = MagicMock()
    pm.cash_balance = 500.0
    pm._is_paper = False
    pm._exchange_name = "binance_futures"
    pm.apply_income = AsyncMock()
    pm.take_snapshot = AsyncMock(return_value=None)
    pm.get_portfolio_summary = AsyncMock(return_value={})
    return pm


@pytest.fixture
def mock_om():
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


def _make_position_state(
    symbol="BTC/USDT",
    direction=Direction.LONG,
    entry_price=80000.0,
    quantity=0.01,
    tier="tier1",
    sl_atr=1.5,
    tp_atr=3.0,
    trail_act_atr=2.0,
    trail_stop_atr=1.0,
    trailing_active=False,
    strategy_name="spot_eval",
) -> PositionState:
    return PositionState(
        symbol=symbol,
        direction=direction,
        quantity=quantity,
        entry_price=entry_price,
        margin=100.0,
        leverage=3,
        extreme_price=entry_price,
        stop_loss_atr=sl_atr,
        take_profit_atr=tp_atr,
        trailing_activation_atr=trail_act_atr,
        trailing_stop_atr=trail_stop_atr,
        trailing_active=trailing_active,
        tier=tier,
        strategy_name=strategy_name,
    )


# ── PositionStateTracker ATR 캐싱 테스트 ──────────


class TestATRCaching:
    def test_update_and_get_atr(self):
        tracker = PositionStateTracker()
        tracker.update_atr("BTC/USDT", 500.0)
        assert tracker.get_atr("BTC/USDT") == 500.0

    def test_get_atr_missing_returns_zero(self):
        tracker = PositionStateTracker()
        assert tracker.get_atr("UNKNOWN") == 0.0

    def test_update_atr_ignores_zero(self):
        tracker = PositionStateTracker()
        tracker.update_atr("BTC/USDT", 500.0)
        tracker.update_atr("BTC/USDT", 0.0)
        assert tracker.get_atr("BTC/USDT") == 500.0

    def test_update_atr_ignores_negative(self):
        tracker = PositionStateTracker()
        tracker.update_atr("BTC/USDT", 500.0)
        tracker.update_atr("BTC/USDT", -10.0)
        assert tracker.get_atr("BTC/USDT") == 500.0

    def test_close_position_cleans_atr_cache(self):
        """포지션 종료 시 ATR 캐시도 제거."""
        tracker = PositionStateTracker()
        state = _make_position_state()
        tracker.open_position(state)
        tracker.update_atr("BTC/USDT", 500.0)
        assert tracker.get_atr("BTC/USDT") == 500.0

        tracker.close_position("BTC/USDT")
        assert tracker.get_atr("BTC/USDT") == 0.0


# ── WS Reconnect 테스트 ──────────────────────────


class TestWSReconnect:
    @pytest.mark.asyncio
    async def test_reconnect_success_returns_min(self, engine, mock_exchange):
        """재연결 성공 시 최소 backoff 반환."""
        engine._last_reconnect_at = 0.0  # 오래 전 재연결 (freshness 통과)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await engine._ws_reconnect(10.0)
        assert result == engine._WS_RECONNECT_MIN
        mock_exchange.close_ws.assert_called_once()
        mock_exchange.create_ws_exchange.assert_called_once()

    @pytest.mark.asyncio
    async def test_reconnect_failure_doubles_backoff(self, engine, mock_exchange):
        """재연결 실패 시 backoff 2배."""
        engine._last_reconnect_at = 0.0
        mock_exchange.create_ws_exchange.side_effect = Exception("connection failed")
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await engine._ws_reconnect(10.0)
        assert result == 20.0

    @pytest.mark.asyncio
    async def test_reconnect_caps_at_max(self, engine, mock_exchange):
        """backoff 최대값 초과 방지."""
        engine._last_reconnect_at = 0.0
        mock_exchange.create_ws_exchange.side_effect = Exception("fail")
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await engine._ws_reconnect(200.0)
        assert result == engine._WS_RECONNECT_MAX

    @pytest.mark.asyncio
    async def test_reconnect_skips_when_fresh(self, engine, mock_exchange):
        """최근 재연결된 경우 중복 재연결 스킵."""
        import time
        engine._last_reconnect_at = time.monotonic()  # 방금 재연결
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await engine._ws_reconnect(10.0)
        assert result == engine._WS_RECONNECT_MIN
        # close_ws/create_ws_exchange 호출 없어야 함
        mock_exchange.close_ws.assert_not_called()
        mock_exchange.create_ws_exchange.assert_not_called()


# ── Realtime Stop Check 테스트 ────────────────────


class TestRealtimeStopCheck:
    @pytest.mark.asyncio
    async def test_no_position_does_nothing(self, engine):
        """포지션 없으면 아무것도 안 함."""
        await engine._realtime_stop_check("BTC/USDT", 80000.0)
        # No exception → pass

    @pytest.mark.asyncio
    async def test_tier1_sl_hit_long(self, engine):
        """Tier1 LONG SL 히트 시 _execute_ws_close 호출."""
        state = _make_position_state(entry_price=80000.0, sl_atr=1.5)
        engine._positions.open_position(state)
        engine._positions.update_atr("BTC/USDT", 1000.0)  # SL = 80000 - 1500 = 78500
        engine._execute_ws_close = AsyncMock()

        await engine._realtime_stop_check("BTC/USDT", 78000.0)
        engine._execute_ws_close.assert_called_once()
        args = engine._execute_ws_close.call_args
        assert args[0][0] == "BTC/USDT"
        assert "[WS] SL hit" in args[0][3]

    @pytest.mark.asyncio
    async def test_tier1_tp_hit_long(self, engine):
        """Tier1 LONG TP 히트 시 _execute_ws_close 호출."""
        state = _make_position_state(entry_price=80000.0, tp_atr=3.0)
        engine._positions.open_position(state)
        engine._positions.update_atr("BTC/USDT", 1000.0)  # TP = 80000 + 3000 = 83000
        engine._execute_ws_close = AsyncMock()

        await engine._realtime_stop_check("BTC/USDT", 84000.0)
        engine._execute_ws_close.assert_called_once()
        assert "[WS] TP hit" in engine._execute_ws_close.call_args[0][3]

    @pytest.mark.asyncio
    async def test_tier1_trailing_stop_hit(self, engine):
        """Tier1 트레일링 스탑 활성화 후 히트."""
        state = _make_position_state(
            entry_price=80000.0, trail_act_atr=2.0, trail_stop_atr=1.0,
            trailing_active=True,
        )
        state.extreme_price = 85000.0
        engine._positions.open_position(state)
        engine._positions.update_atr("BTC/USDT", 1000.0)  # trail = extreme - 1000 = 84000
        engine._execute_ws_close = AsyncMock()

        await engine._realtime_stop_check("BTC/USDT", 83500.0)
        engine._execute_ws_close.assert_called_once()
        assert "[WS] Trailing stop" in engine._execute_ws_close.call_args[0][3]

    @pytest.mark.asyncio
    async def test_tier1_no_atr_skips_check(self, engine):
        """ATR 미캐시 시 SL/TP 체크 스킵."""
        state = _make_position_state(entry_price=80000.0)
        engine._positions.open_position(state)
        engine._execute_ws_close = AsyncMock()

        await engine._realtime_stop_check("BTC/USDT", 70000.0)
        engine._execute_ws_close.assert_not_called()
        assert engine._positions.has_position("BTC/USDT")

    @pytest.mark.asyncio
    async def test_tier1_price_in_range_no_close(self, engine):
        """가격이 SL/TP 범위 내이면 청산 안 함."""
        state = _make_position_state(entry_price=80000.0)
        engine._positions.open_position(state)
        engine._positions.update_atr("BTC/USDT", 1000.0)
        engine._execute_ws_close = AsyncMock()

        await engine._realtime_stop_check("BTC/USDT", 80500.0)
        engine._execute_ws_close.assert_not_called()

    @pytest.mark.asyncio
    async def test_tier1_short_sl_hit(self, engine):
        """Tier1 SHORT SL 히트."""
        state = _make_position_state(
            direction=Direction.SHORT, entry_price=80000.0, sl_atr=1.5,
        )
        engine._positions.open_position(state)
        engine._positions.update_atr("BTC/USDT", 1000.0)  # SL = 80000 + 1500 = 81500
        engine._execute_ws_close = AsyncMock()

        await engine._realtime_stop_check("BTC/USDT", 82000.0)
        engine._execute_ws_close.assert_called_once()
        assert "[WS] SL hit" in engine._execute_ws_close.call_args[0][3]

    @pytest.mark.asyncio
    async def test_tier2_sl_hit_pct_based(self, engine):
        """Tier2 퍼센트 기반 SL."""
        state = _make_position_state(entry_price=100.0, tier="tier2")
        engine._positions.open_position(state)
        engine._execute_ws_close = AsyncMock()

        # leverage=3, SL default=3.5%
        # pnl_pct = (95 - 100) / 100 * 100 * 3 = -15.0% < -3.5%
        await engine._realtime_stop_check("BTC/USDT", 95.0)
        engine._execute_ws_close.assert_called_once()
        assert "[WS] Tier2 SL" in engine._execute_ws_close.call_args[0][3]

    @pytest.mark.asyncio
    async def test_tier2_tp_hit_pct_based(self, engine):
        """Tier2 퍼센트 기반 TP."""
        state = _make_position_state(entry_price=100.0, tier="tier2")
        engine._positions.open_position(state)
        engine._execute_ws_close = AsyncMock()

        # leverage=3, TP default=4.5%
        # pnl_pct = (102 - 100) / 100 * 100 * 3 = 6.0% > 4.5%
        await engine._realtime_stop_check("BTC/USDT", 102.0)
        engine._execute_ws_close.assert_called_once()
        assert "[WS] Tier2 TP" in engine._execute_ws_close.call_args[0][3]

    @pytest.mark.asyncio
    async def test_tier2_in_range_no_close(self, engine):
        """Tier2 가격 범위 내 — 청산 안 함."""
        state = _make_position_state(entry_price=100.0, tier="tier2")
        engine._positions.open_position(state)
        engine._execute_ws_close = AsyncMock()

        await engine._realtime_stop_check("BTC/USDT", 100.5)
        engine._execute_ws_close.assert_not_called()

    @pytest.mark.asyncio
    async def test_updates_extreme_price(self, engine):
        """WS 체크마다 extreme_price 업데이트."""
        state = _make_position_state(entry_price=80000.0)
        engine._positions.open_position(state)
        engine._positions.update_atr("BTC/USDT", 1000.0)
        engine._execute_ws_close = AsyncMock()

        await engine._realtime_stop_check("BTC/USDT", 82000.0)
        assert engine._positions.get("BTC/USDT").extreme_price == 82000.0

    @pytest.mark.asyncio
    async def test_zero_entry_price_skips(self, engine):
        """entry_price=0이면 스킵."""
        state = _make_position_state(entry_price=0.0)
        engine._positions.open_position(state)
        engine._positions.update_atr("BTC/USDT", 1000.0)
        engine._execute_ws_close = AsyncMock()

        await engine._realtime_stop_check("BTC/USDT", 80000.0)
        engine._execute_ws_close.assert_not_called()


# ── Execute WS Close 테스트 ──────────────────────


class TestExecuteWSClose:
    @pytest.mark.asyncio
    async def test_close_creates_order_and_removes_position(
        self, engine, session, session_factory
    ):
        """WS 청산 성공 시 포지션 제거."""
        # DB 포지션 생성
        db_pos = Position(
            symbol="BTC/USDT", exchange="binance_futures",
            quantity=0.01, average_buy_price=80000.0,
            total_invested=100.0, current_value=100.0,
        )
        session.add(db_pos)
        await session.commit()

        state = _make_position_state()
        engine._positions.open_position(state)

        mock_resp = MagicMock(success=True)
        engine._safe_order.execute_order = AsyncMock(return_value=mock_resp)

        with patch(
            "engine.futures_engine_v2.get_session_factory",
            return_value=session_factory,
        ):
            await engine._execute_ws_close(
                "BTC/USDT", state, 78000.0, "[WS] SL hit"
            )

        assert not engine._positions.has_position("BTC/USDT")
        engine._safe_order.execute_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_failure_keeps_position(
        self, engine, session, session_factory
    ):
        """주문 실패 시 포지션 유지."""
        db_pos = Position(
            symbol="BTC/USDT", exchange="binance_futures",
            quantity=0.01, average_buy_price=80000.0,
            total_invested=100.0, current_value=100.0,
        )
        session.add(db_pos)
        await session.commit()

        state = _make_position_state()
        engine._positions.open_position(state)

        mock_resp = MagicMock(success=False)
        engine._safe_order.execute_order = AsyncMock(return_value=mock_resp)

        with patch(
            "engine.futures_engine_v2.get_session_factory",
            return_value=session_factory,
        ):
            await engine._execute_ws_close(
                "BTC/USDT", state, 78000.0, "[WS] SL hit"
            )

        assert engine._positions.has_position("BTC/USDT")

    @pytest.mark.asyncio
    async def test_no_db_position_skips(self, engine, session_factory):
        """DB 포지션 없으면 (이미 청산) 스킵."""
        state = _make_position_state()
        engine._positions.open_position(state)
        engine._safe_order.execute_order = AsyncMock()

        with patch(
            "engine.futures_engine_v2.get_session_factory",
            return_value=session_factory,
        ):
            await engine._execute_ws_close(
                "BTC/USDT", state, 78000.0, "[WS] SL hit"
            )

        engine._safe_order.execute_order.assert_not_called()
        # 인메모리 포지션은 유지 (DB에서 이미 청산된 것으로 판단)
        assert engine._positions.has_position("BTC/USDT")


# ── WS Price Monitor Loop 테스트 ──────────────────


class TestWSPriceMonitorLoop:
    @pytest.mark.asyncio
    async def test_exits_on_cancel(self, engine, mock_exchange):
        """CancelledError 시 정상 종료."""
        mock_exchange.watch_tickers.side_effect = asyncio.CancelledError()
        engine._is_running = True
        # 포지션이 있어야 watch_tickers 호출
        state = _make_position_state()
        engine._positions.open_position(state)

        await engine._ws_price_monitor_loop()
        # 정상 종료 확인

    @pytest.mark.asyncio
    async def test_skips_when_no_positions(self, engine, mock_exchange):
        """포지션 없으면 5초 대기."""
        engine._is_running = True
        call_count = 0

        async def stop_after_one(secs):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                engine._is_running = False

        with patch("asyncio.sleep", side_effect=stop_after_one):
            await engine._ws_price_monitor_loop()

        mock_exchange.watch_tickers.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_realtime_check_on_tick(self, engine, mock_exchange):
        """WS 틱 수신 시 _realtime_stop_check 호출."""
        state = _make_position_state()
        engine._positions.open_position(state)
        engine._positions.update_atr("BTC/USDT", 1000.0)

        tick_count = 0

        async def mock_watch(symbols):
            nonlocal tick_count
            tick_count += 1
            if tick_count > 1:
                raise asyncio.CancelledError()
            return {"BTC/USDT": {"last": 80500.0}}

        mock_exchange.watch_tickers = AsyncMock(side_effect=mock_watch)
        engine._is_running = True
        engine._realtime_stop_check = AsyncMock()

        await engine._ws_price_monitor_loop()
        engine._realtime_stop_check.assert_called_once_with("BTC/USDT", 80500.0)

    @pytest.mark.asyncio
    async def test_fallback_activated_after_3_errors(self, engine, mock_exchange):
        """3회 연속 에러 → 폴백 태스크 시작."""
        state = _make_position_state()
        engine._positions.open_position(state)

        error_count = 0

        async def mock_watch(symbols):
            nonlocal error_count
            error_count += 1
            if error_count > 3:
                raise asyncio.CancelledError()
            raise RuntimeError("WS error")

        mock_exchange.watch_tickers = AsyncMock(side_effect=mock_watch)
        mock_exchange.create_ws_exchange = AsyncMock(
            side_effect=Exception("reconnect fail")
        )
        engine._is_running = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await engine._ws_price_monitor_loop()

        # 폴백 태스크가 생성되었는지 확인
        assert engine._fast_sl_task is not None

        # 정리
        if engine._fast_sl_task and not engine._fast_sl_task.done():
            engine._fast_sl_task.cancel()
            try:
                await engine._fast_sl_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_fallback_cancelled_after_consecutive_successes(self, engine, mock_exchange):
        """WS 3회 연속 성공 후 폴백 태스크 취소 (대칭 히스테리시스)."""
        # 미리 폴백 태스크 생성
        async def dummy():
            await asyncio.sleep(3600)

        engine._fast_sl_task = asyncio.create_task(dummy())

        state = _make_position_state()
        engine._positions.open_position(state)

        tick_count = 0

        async def mock_watch(symbols):
            nonlocal tick_count
            tick_count += 1
            if tick_count > 3:
                raise asyncio.CancelledError()
            return {"BTC/USDT": {"last": 80000.0}}

        mock_exchange.watch_tickers = AsyncMock(side_effect=mock_watch)
        engine._is_running = True
        engine._realtime_stop_check = AsyncMock()
        engine._ws_consecutive_successes = 0

        await engine._ws_price_monitor_loop()

        # 3회 연속 성공 후 폴백 취소 확인
        assert engine._fast_sl_task is None

    @pytest.mark.asyncio
    async def test_fallback_not_cancelled_on_single_success(self, engine, mock_exchange):
        """WS 1회 성공으로는 폴백 취소 안 함."""
        async def dummy():
            await asyncio.sleep(3600)

        engine._fast_sl_task = asyncio.create_task(dummy())

        state = _make_position_state()
        engine._positions.open_position(state)

        tick_count = 0

        async def mock_watch(symbols):
            nonlocal tick_count
            tick_count += 1
            if tick_count > 1:
                raise asyncio.CancelledError()
            return {"BTC/USDT": {"last": 80000.0}}

        mock_exchange.watch_tickers = AsyncMock(side_effect=mock_watch)
        engine._is_running = True
        engine._realtime_stop_check = AsyncMock()
        engine._ws_consecutive_successes = 0

        await engine._ws_price_monitor_loop()

        # 1회 성공으로는 폴백 유지
        assert engine._fast_sl_task is not None

        # cleanup
        engine._fast_sl_task.cancel()
        try:
            await engine._fast_sl_task
        except asyncio.CancelledError:
            pass


# ── Fast SL Fallback Loop 테스트 ──────────────────


class TestFastStopCheckLoop:
    @pytest.mark.asyncio
    async def test_fetches_ticker_and_checks(self, engine, mock_exchange):
        """폴백 루프가 ticker 조회 후 stop check 호출."""
        state = _make_position_state()
        engine._positions.open_position(state)
        engine._positions.update_atr("BTC/USDT", 1000.0)
        engine._is_running = True

        call_count = 0

        async def mock_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                engine._is_running = False

        engine._realtime_stop_check = AsyncMock()

        with patch("asyncio.sleep", side_effect=mock_sleep):
            await engine._fast_stop_check_loop()

        mock_exchange.fetch_ticker.assert_called()
        engine._realtime_stop_check.assert_called()


# ── WS Balance Loop 테스트 ────────────────────────


class TestWSBalanceLoop:
    @pytest.mark.asyncio
    async def test_logs_discrepancy(self, engine, mock_exchange, mock_pm):
        """>2% 괴리 시 경고 로그."""
        call_count = 0

        async def mock_watch():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()
            return {"USDT": {"total": 600.0, "used": 50.0}}

        mock_exchange.watch_balance = AsyncMock(side_effect=mock_watch)
        # unrealized PnL이 있는 상황: exchange_cash = 600 - 0(unrealized) - 50 = 550
        engine._ws_unrealized_pnl = {}
        mock_pm.cash_balance = 100.0  # 내부 장부 = 100, 거래소 = 550 → 큰 괴리
        engine._is_running = True

        with patch("engine.futures_engine_v2.logger") as mock_logger:
            await engine._ws_balance_loop()
            mock_logger.warning.assert_any_call(
                "v2_ws_balance_discrepancy",
                internal=100.0,
                exchange=550.0,
                diff=450.0,
            )

    @pytest.mark.asyncio
    async def test_no_log_when_balanced(self, engine, mock_exchange, mock_pm):
        """괴리 없으면 경고 없음."""
        call_count = 0

        async def mock_watch():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()
            return {"USDT": {"total": 500.0, "used": 0.0}}

        mock_exchange.watch_balance = AsyncMock(side_effect=mock_watch)
        engine._ws_unrealized_pnl = {}
        mock_pm.cash_balance = 500.0
        engine._is_running = True

        with patch("engine.futures_engine_v2.logger") as mock_logger:
            await engine._ws_balance_loop()
            # warning은 호출되지 않아야 함 (reconnect/error 경고 제외)
            for call in mock_logger.warning.call_args_list:
                assert call.args[0] != "v2_ws_balance_discrepancy"

    @pytest.mark.asyncio
    async def test_subtracts_unrealized_pnl(self, engine, mock_exchange, mock_pm):
        """미실현 PnL 차감하여 정확한 잔고 비교."""
        call_count = 0

        async def mock_watch():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()
            return {"USDT": {"total": 600.0, "used": 50.0}}

        mock_exchange.watch_balance = AsyncMock(side_effect=mock_watch)
        # unrealized PnL = 50 → exchange_cash = 600 - 50 - 50 = 500
        engine._ws_unrealized_pnl = {"BTC/USDT": 50.0}
        mock_pm.cash_balance = 500.0  # 일치 → 경고 없음
        engine._is_running = True

        with patch("engine.futures_engine_v2.logger") as mock_logger:
            await engine._ws_balance_loop()
            for call in mock_logger.warning.call_args_list:
                assert call.args[0] != "v2_ws_balance_discrepancy"

    @pytest.mark.asyncio
    async def test_timeout_continues(self, engine, mock_exchange):
        """TimeoutError는 정상 — 계속 진행."""
        call_count = 0

        async def mock_watch():
            nonlocal call_count
            call_count += 1
            if call_count > 2:
                raise asyncio.CancelledError()
            raise asyncio.TimeoutError()

        mock_exchange.watch_balance = AsyncMock(side_effect=mock_watch)
        engine._is_running = True

        await engine._ws_balance_loop()
        assert call_count == 3  # 2 timeouts + 1 cancel


# ── WS Position Loop 테스트 ───────────────────────


class TestWSPositionLoop:
    @pytest.mark.asyncio
    async def test_updates_db_position_fields(
        self, engine, mock_exchange, session, session_factory
    ):
        """WS 포지션 데이터 → DB Position 필드 업데이트."""
        # DB에 포지션 생성
        db_pos = Position(
            symbol="BTC/USDT",
            exchange="binance_futures",
            quantity=0.01,
            average_buy_price=80000.0,
            total_invested=100.0,
            current_value=100.0,
        )
        session.add(db_pos)
        await session.commit()

        call_count = 0

        async def mock_watch():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()
            return [
                {
                    "symbol": "BTC/USDT:USDT",
                    "contracts": 0.015,
                    "initialMargin": 300.0,
                    "entryPrice": 81000.0,
                    "liquidationPrice": 51000.0,
                    "unrealizedPnl": 5.0,
                    "markPrice": 81500.0,
                }
            ]

        mock_exchange.watch_positions = AsyncMock(side_effect=mock_watch)
        engine._is_running = True

        with patch(
            "engine.futures_engine_v2.get_session_factory",
            return_value=session_factory,
        ):
            await engine._ws_position_loop()

        # DB 확인
        await session.refresh(db_pos)
        assert db_pos.quantity == 0.015
        assert db_pos.margin_used == 300.0
        assert abs(db_pos.average_buy_price - 81000.0) < 0.01
        assert db_pos.liquidation_price == 51000.0

    @pytest.mark.asyncio
    async def test_updates_inmemory_extreme(
        self, engine, mock_exchange, session, session_factory
    ):
        """WS 포지션 mark price → 인메모리 extreme 업데이트."""
        # DB 포지션
        db_pos = Position(
            symbol="BTC/USDT",
            exchange="binance_futures",
            quantity=0.01,
            average_buy_price=80000.0,
            total_invested=100.0,
            current_value=100.0,
        )
        session.add(db_pos)
        await session.commit()

        # 인메모리 포지션
        state = _make_position_state(entry_price=80000.0)
        engine._positions.open_position(state)

        call_count = 0

        async def mock_watch():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()
            return [
                {
                    "symbol": "BTC/USDT:USDT",
                    "contracts": 0.01,
                    "initialMargin": 100.0,
                    "entryPrice": 80000.0,
                    "markPrice": 82000.0,
                    "unrealizedPnl": 20.0,
                }
            ]

        mock_exchange.watch_positions = AsyncMock(side_effect=mock_watch)
        engine._is_running = True

        with patch(
            "engine.futures_engine_v2.get_session_factory",
            return_value=session_factory,
        ):
            await engine._ws_position_loop()

        assert engine._positions.get("BTC/USDT").extreme_price == 82000.0

    @pytest.mark.asyncio
    async def test_timeout_continues(self, engine, mock_exchange):
        """TimeoutError는 정상 — 계속 진행."""
        call_count = 0

        async def mock_watch():
            nonlocal call_count
            call_count += 1
            if call_count > 2:
                raise asyncio.CancelledError()
            raise asyncio.TimeoutError()

        mock_exchange.watch_positions = AsyncMock(side_effect=mock_watch)
        engine._is_running = True

        await engine._ws_position_loop()
        assert call_count == 3


# ── Start/Stop 통합 테스트 ────────────────────────


class TestStartStop:
    @pytest.mark.asyncio
    async def test_start_creates_ws_tasks(self, engine, mock_exchange):
        """start()가 WS 태스크를 생성."""
        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine.start()

        assert engine._ws_monitor_task is not None
        assert engine._ws_bp_task is not None
        assert engine._ws_pos_task is not None
        assert engine._fast_sl_task is None  # WS 성공이므로 폴백 없음
        assert len(engine._tasks) >= 9  # 6 기본 + 3 WS

        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine.stop()

    @pytest.mark.asyncio
    async def test_start_ws_failure_activates_fallback(self, engine, mock_exchange):
        """WS 초기화 실패 시 폴백 태스크 생성."""
        mock_exchange.create_ws_exchange.side_effect = Exception("WS init failed")

        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine.start()

        assert engine._ws_monitor_task is None
        assert engine._ws_bp_task is None
        assert engine._ws_pos_task is None
        assert engine._fast_sl_task is not None
        assert len(engine._tasks) >= 7  # 6 기본 + 1 폴백

        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine.stop()

    @pytest.mark.asyncio
    async def test_stop_cleans_up_ws(self, engine, mock_exchange):
        """stop()이 WS 태스크 취소 + 연결 해제."""
        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine.start()
            await engine.stop()

        assert engine._ws_monitor_task is None
        assert engine._ws_bp_task is None
        assert engine._ws_pos_task is None
        assert engine._fast_sl_task is None
        mock_exchange.close_ws.assert_called()

    @pytest.mark.asyncio
    async def test_stop_when_ws_disabled(self, engine, mock_exchange):
        """WS 비활성 상태에서도 stop() 정상 동작."""
        engine._ws_enabled = False
        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine.start()
            await engine.stop()
        # close_ws는 항상 호출 (에러 무시)
        mock_exchange.close_ws.assert_called()


# ── get_status WS 상태 표시 테스트 ─────────────────


class TestGetStatusWS:
    def test_status_includes_ws_fields(self, engine):
        """get_status에 WS 상태 필드 포함."""
        status = engine.get_status()
        assert "ws_price_monitor" in status
        assert "ws_balance_position" in status
        assert "ws_position_sync" in status
        assert "fast_sl_fallback" in status

    @pytest.mark.asyncio
    async def test_ws_active_after_start(self, engine, mock_exchange):
        """start 후 WS 상태 = True."""
        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine.start()
        status = engine.get_status()
        assert status["ws_price_monitor"] is True
        assert status["ws_balance_position"] is True
        assert status["ws_position_sync"] is True
        assert status["fast_sl_fallback"] is False
        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine.stop()

    @pytest.mark.asyncio
    async def test_fallback_active_when_ws_fails(self, engine, mock_exchange):
        """WS 실패 시 폴백 상태 = True."""
        mock_exchange.create_ws_exchange.side_effect = Exception("fail")
        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine.start()
        status = engine.get_status()
        assert status["ws_price_monitor"] is False
        assert status["ws_balance_position"] is False
        assert status["ws_position_sync"] is False
        assert status["fast_sl_fallback"] is True
        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine.stop()


# ── close_lock 동시성 테스트 ──────────────────────


class TestCloseLock:
    @pytest.mark.asyncio
    async def test_close_lock_serializes_concurrent_closes(self, engine):
        """close_lock이 동시 청산을 직렬화 — 비중첩 실행 확인."""
        import time

        state = _make_position_state()
        engine._positions.open_position(state)
        engine._positions.update_atr("BTC/USDT", 1000.0)

        timestamps = []  # (start, end) pairs

        async def mock_close(symbol, state, price, reason):
            start = time.monotonic()
            await asyncio.sleep(0.05)
            end = time.monotonic()
            timestamps.append((start, end))

        engine._execute_ws_close = AsyncMock(side_effect=mock_close)

        # 동시에 두 개의 stop check 시도
        await asyncio.gather(
            engine._realtime_stop_check("BTC/USDT", 77000.0),  # SL hit
            engine._realtime_stop_check("BTC/USDT", 76000.0),  # SL hit
        )

        assert len(timestamps) == 2
        # 두 번째 호출이 첫 번째 완료 후에 시작됨 (직렬화 확인)
        timestamps.sort(key=lambda t: t[0])
        assert timestamps[1][0] >= timestamps[0][1]

    @pytest.mark.asyncio
    async def test_close_lock_exists(self, engine):
        """close_lock이 asyncio.Lock 인스턴스."""
        assert isinstance(engine._close_lock, asyncio.Lock)

    @pytest.mark.asyncio
    async def test_reconnect_lock_exists(self, engine):
        """reconnect_lock이 asyncio.Lock 인스턴스."""
        assert isinstance(engine._ws_reconnect_lock, asyncio.Lock)


class TestReconnectStorm:
    @pytest.mark.asyncio
    async def test_concurrent_reconnects_only_execute_once(self, engine, mock_exchange):
        """3개 루프가 동시에 _ws_reconnect 호출 → freshness로 1회만 실제 재연결."""
        engine._last_reconnect_at = 0.0  # 오래 전

        with patch("asyncio.sleep", new_callable=AsyncMock):
            results = await asyncio.gather(
                engine._ws_reconnect(5.0),
                engine._ws_reconnect(5.0),
                engine._ws_reconnect(5.0),
            )

        # 첫 번째만 실제 재연결, 나머지는 freshness로 스킵
        assert all(r == engine._WS_RECONNECT_MIN for r in results)
        # create_ws_exchange는 1회만 호출 (첫 번째가 재연결, 나머지 스킵)
        assert mock_exchange.create_ws_exchange.call_count == 1


class TestWSPositionLoopExternalClose:
    @pytest.mark.asyncio
    async def test_contracts_zero_detects_external_close(
        self, engine, mock_exchange, session, session_factory
    ):
        """contracts=0 수신 시 외부 청산 감지 → DB 커밋 후 인메모리 제거."""
        db_pos = Position(
            symbol="BTC/USDT",
            exchange="binance_futures",
            quantity=0.01,
            average_buy_price=80000.0,
            total_invested=100.0,
            current_value=100.0,
        )
        session.add(db_pos)
        await session.commit()

        # 인메모리 포지션 + ATR 캐시 등록
        state = _make_position_state()
        engine._positions.open_position(state)
        engine._positions.update_atr("BTC/USDT", 500.0)

        call_count = 0

        async def mock_watch():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()
            return [
                {
                    "symbol": "BTC/USDT:USDT",
                    "contracts": 0,
                    "initialMargin": 0,
                    "entryPrice": 80000.0,
                    "markPrice": 80000.0,
                    "unrealizedPnl": 0,
                }
            ]

        mock_exchange.watch_positions = AsyncMock(side_effect=mock_watch)
        engine._is_running = True

        with patch(
            "engine.futures_engine_v2.get_session_factory",
            return_value=session_factory,
        ):
            await engine._ws_position_loop()

        # DB 확인: 수량 0으로 업데이트
        await session.refresh(db_pos)
        assert db_pos.quantity == 0
        assert db_pos.current_value == 0

        # 인메모리 포지션 제거 확인 (commit 후)
        assert not engine._positions.has_position("BTC/USDT")
        # ATR 캐시도 제거됨
        assert engine._positions.get_atr("BTC/USDT") == 0.0
