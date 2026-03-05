"""매수 실행 흐름 전체 테스트.

시그널 → combiner → _process_decision → create_order → position_opened
까지의 전체 경로를 검증한다. 13개 관문을 모두 테스트:

1. HOLD 시그널 → 매수 안 됨
2. BUY 시그널 + can_trade=True → 매수 실행
3. can_trade=False (일일 한도/쿨다운/washout) → 매수 차단
4. 비대칭 모드: crash/downtrend → 매수 차단
5. 신뢰도 미달 → 매수 안 됨
6. 이미 포지션 있음 → 추가 매수 안 됨
7. 교차 거래소 충돌 → 매수 차단
8. 현금 부족 → 매수 안 됨
9. 최소 주문금액 미달 → 매수 안 됨
10. 거래소 주문 실패 → 에러 핸들링
11. 주문 미체결 → 취소 처리
12. 매수 성공 후 포지션 트래커 생성
13. 매도 시그널 → 매도 실행
"""
import pytest
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from dataclasses import dataclass

from config import AppConfig
from core.enums import SignalType, MarketState
from core.models import Position, Order
from strategies.base import Signal
from strategies.combiner import CombinedDecision
from engine.trading_engine import TradingEngine, PositionTracker


# ── 공통 픽스처 ──────────────────────────────────────────────────

@pytest.fixture
def config():
    cfg = AppConfig()
    cfg.trading.mode = "live"
    cfg.trading.asymmetric_mode = True
    cfg.trading.min_combined_confidence = 0.50
    cfg.trading.daily_buy_limit = 20
    cfg.trading.max_daily_coin_buys = 3
    cfg.trading.min_trade_interval_sec = 3600
    cfg.trading.cooldown_after_sell_sec = 14400
    cfg.risk.max_trade_size_pct = 0.20
    return cfg


@pytest.fixture
def mock_exchange():
    ex = AsyncMock()
    ex.fetch_ticker = AsyncMock(return_value=MagicMock(last=50000, ask=50100))
    return ex


@pytest.fixture
def mock_market_data():
    md = AsyncMock()
    md.get_ticker = AsyncMock(return_value=MagicMock(last=50000))
    md.get_current_price = AsyncMock(return_value=50000)
    # get_candles: 200행 DataFrame mock
    import pandas as pd
    import numpy as np
    idx = pd.date_range("2026-01-01", periods=200, freq="4h")
    df = pd.DataFrame({
        "close": np.linspace(48000, 52000, 200),
        "high": np.linspace(48500, 52500, 200),
        "low": np.linspace(47500, 51500, 200),
        "volume": np.random.uniform(100, 1000, 200),
        "sma_20": np.linspace(48000, 51000, 200),
        "sma_50": np.linspace(47000, 50000, 200),
        "rsi_14": np.linspace(45, 65, 200),
        "volume_sma_20": np.full(200, 500),
        "atr_14": np.full(200, 1000),
    }, index=idx)
    md.get_candles = AsyncMock(return_value=df)
    return md


@pytest.fixture
def mock_order_mgr():
    om = AsyncMock()
    om.log_signal_only = AsyncMock()
    # 기본: 체결 성공
    filled_order = MagicMock()
    filled_order.status = "filled"
    filled_order.fee = 50
    filled_order.id = 1
    filled_order.exchange_order_id = "ex-123"
    om.create_order = AsyncMock(return_value=filled_order)
    om.cancel_order_by_id = AsyncMock()
    return om


@pytest.fixture
def mock_pm():
    pm = MagicMock()
    pm.cash_balance = 500000  # 50만원 현금
    pm._sync_lock = asyncio.Lock()
    pm._is_paper = False
    pm._exchange_name = "bithumb"
    pm.update_position_on_buy = AsyncMock()
    pm.update_position_on_sell = AsyncMock()
    pm.reconcile_cash_from_db = AsyncMock()
    pm.get_portfolio_summary = AsyncMock(return_value={"total_value_krw": 500000, "cash_balance_krw": 500000})
    pm.take_snapshot = AsyncMock(return_value=None)
    return pm


@pytest.fixture
def mock_combiner():
    return MagicMock()




def _make_buy_signal(confidence=0.65, strategy="bollinger_rsi"):
    return Signal(
        signal_type=SignalType.BUY,
        confidence=confidence,
        strategy_name=strategy,
        reason="test buy signal",
        indicators={},
    )


def _make_sell_signal(confidence=0.60, strategy="rsi"):
    return Signal(
        signal_type=SignalType.SELL,
        confidence=confidence,
        strategy_name=strategy,
        reason="test sell signal",
        indicators={},
    )


def _make_buy_decision(confidence=0.65, strategy="bollinger_rsi"):
    sig = _make_buy_signal(confidence, strategy)
    return CombinedDecision(
        action=SignalType.BUY,
        combined_confidence=confidence,
        contributing_signals=[sig],
        final_reason="test buy",
    )


def _make_sell_decision(confidence=0.60, strategy="rsi"):
    sig = _make_sell_signal(confidence, strategy)
    return CombinedDecision(
        action=SignalType.SELL,
        combined_confidence=confidence,
        contributing_signals=[sig],
        final_reason="test sell",
    )


def _make_hold_decision():
    return CombinedDecision(
        action=SignalType.HOLD,
        combined_confidence=0.0,
        contributing_signals=[],
        final_reason="no signal",
    )


def _make_session_no_position():
    """포지션 없음 + 교차 충돌 없음 + 기타 쿼리 all-None 반환하는 session mock."""
    session = AsyncMock()

    def _execute_side_effect(*args, **kwargs):
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        result.scalars = MagicMock(return_value=MagicMock(
            first=MagicMock(return_value=None),
            all=MagicMock(return_value=[]),
        ))
        result.all = MagicMock(return_value=[])
        return result

    session.execute = AsyncMock(side_effect=_execute_side_effect)
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner, exchange_name="bithumb"):
    engine = TradingEngine(
        config=config,
        exchange=mock_exchange,
        market_data=mock_market_data,
        order_manager=mock_order_mgr,
        portfolio_manager=mock_pm,
        combiner=mock_combiner,
        exchange_name=exchange_name,
    )
    engine._market_state = "sideways"
    engine._market_confidence = 0.5
    return engine


# ── 테스트: 매수 성공 Happy Path ──────────────────────────────

class TestBuyHappyPath:
    """시그널 → 매수 성공까지 전체 경로 검증."""

    @pytest.mark.asyncio
    async def test_buy_signal_executes_order(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """BUY 시그널 + 충분한 현금 → 매수 주문 실행 + 포지션 업데이트."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        decision = _make_buy_decision(confidence=0.65)
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "ETH/KRW", decision)

        mock_order_mgr.create_order.assert_called_once()
        call_args = mock_order_mgr.create_order.call_args
        assert call_args[0][1] == "ETH/KRW"
        assert call_args[0][2] == "buy"
        mock_pm.update_position_on_buy.assert_called_once()

    @pytest.mark.asyncio
    async def test_buy_creates_position_tracker(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """매수 성공 후 PositionTracker가 생성됨."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        decision = _make_buy_decision(confidence=0.65)
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "ETH/KRW", decision)

        assert "ETH/KRW" in engine._position_trackers
        tracker = engine._position_trackers["ETH/KRW"]
        assert tracker.entry_price == 50000
        assert tracker.stop_loss_pct > 0

    @pytest.mark.asyncio
    async def test_buy_amount_calculation(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """매수 금액 = cash × size_pct / fee_margin."""
        mock_pm.cash_balance = 500000
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        engine._market_state = "sideways"
        decision = _make_buy_decision(confidence=0.65)
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "ETH/KRW", decision)

        call_args = mock_order_mgr.create_order.call_args
        amount = call_args[0][3]
        # sideways: size_pct = 0.20 * 0.50 = 0.10
        # amount_krw = 500000 * 0.10 / 1.003 ≈ 49850
        # amount = 49850 / 50000 ≈ 0.997
        assert 0.9 < amount < 1.1

    @pytest.mark.asyncio
    async def test_buy_emits_trade_event(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """매수 성공 시 trade 이벤트 emit."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        decision = _make_buy_decision(confidence=0.65)
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock) as mock_emit:
            await engine._process_decision(session, "ETH/KRW", decision)

        trade_calls = [c for c in mock_emit.call_args_list if "매수:" in str(c)]
        assert len(trade_calls) >= 1


# ── 테스트: 매수 차단 조건들 ──────────────────────────────────

class TestBuyBlocking:
    """매수를 차단하는 각 관문 테스트."""

    @pytest.mark.asyncio
    async def test_hold_signal_no_action(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """HOLD 결정 → 아무 액션 없음."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        decision = _make_hold_decision()
        session = _make_session_no_position()

        await engine._process_decision(session, "ETH/KRW", decision)
        mock_order_mgr.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_suppressed_coin_blocked(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """_suppressed_coins에 포함된 코인 → 매수 차단."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        engine._suppressed_coins.add("ETH/KRW")
        decision = _make_buy_decision()
        session = _make_session_no_position()

        await engine._process_decision(session, "ETH/KRW", decision)
        mock_order_mgr.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_asymmetric_crash_blocked(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """비대칭 모드 + crash 시장 → 매수 차단."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        engine._market_state = "crash"
        decision = _make_buy_decision()
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "ETH/KRW", decision)
        mock_order_mgr.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_asymmetric_downtrend_blocked(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """비대칭 모드 + downtrend → 매수 차단."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        engine._market_state = "downtrend"
        decision = _make_buy_decision()
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "ETH/KRW", decision)
        mock_order_mgr.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_confidence_blocked(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """신뢰도 미달 → 매수 차단. sideways min_conf = 0.50 + 0.05 = 0.55."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        engine._market_state = "sideways"
        decision = _make_buy_decision(confidence=0.52)
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "ETH/KRW", decision)
        mock_order_mgr.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_position_blocked(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """이미 포지션 보유 → 추가 매수 안 됨."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        decision = _make_buy_decision(confidence=0.65)

        existing_pos = MagicMock()
        existing_pos.quantity = 0.5
        existing_pos.exchange = "bithumb"

        session = _make_session_no_position()
        original = session.execute.side_effect
        call_count = [0]

        async def pos_execute(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # 첫 번째: 기존 포지션 있음
                result = MagicMock()
                result.scalar_one_or_none = MagicMock(return_value=existing_pos)
                return result
            return original(*args, **kwargs)

        session.execute = AsyncMock(side_effect=pos_execute)

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "ETH/KRW", decision)
        mock_order_mgr.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_cross_exchange_conflict_blocked(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """교차 거래소 숏 보유 → 현물 매수 차단."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        decision = _make_buy_decision(confidence=0.65)

        cross_pos = MagicMock()
        cross_pos.exchange = "binance_futures"
        cross_pos.direction = "short"
        cross_pos.quantity = 0.5

        session = _make_session_no_position()
        call_count = [0]

        async def cross_execute(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                # 두 번째: 교차 거래소 숏 포지션 있음
                result = MagicMock()
                result.scalars = MagicMock(return_value=MagicMock(first=MagicMock(return_value=cross_pos)))
                return result
            result = MagicMock()
            result.scalar_one_or_none = MagicMock(return_value=None)
            return result

        session.execute = AsyncMock(side_effect=cross_execute)

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "ETH/KRW", decision)
        mock_order_mgr.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_cash_blocked(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """현금 0원 → 매수 차단 (바로 return)."""
        mock_pm.cash_balance = 0
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        decision = _make_buy_decision(confidence=0.65)
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "ETH/KRW", decision)
        mock_order_mgr.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_small_cash_blocked(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """현금 3000원 → 최소 주문금액(5000) 미달로 차단."""
        mock_pm.cash_balance = 3000
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        decision = _make_buy_decision(confidence=0.65)
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "ETH/KRW", decision)
        mock_order_mgr.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_min_order_amount_bithumb_5000(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """빗썸 최소 주문금액 = 5000 KRW."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner, exchange_name="bithumb")
        assert engine._min_order_amount == 5000

    @pytest.mark.asyncio
    async def test_min_order_amount_binance_5(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """바이낸스 최소 주문금액 = 5 USDT."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner, exchange_name="binance_futures")
        assert engine._min_order_amount == 5.0


# ── 테스트: 거래소 주문 에러 핸들링 ──────────────────────────

class TestOrderErrorHandling:
    """거래소 에러 시 graceful 처리 검증."""

    @pytest.mark.asyncio
    async def test_buy_order_exception_handled(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """거래소 주문 실패 → ExchangeError → 에러 로그 + 이벤트 emit, crash 안 됨."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        decision = _make_buy_decision(confidence=0.65)
        mock_order_mgr.create_order = AsyncMock(side_effect=Exception("주문 금액 부족: 0 KRW < 5000 KRW"))
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock) as mock_emit:
            await engine._process_decision(session, "ETH/KRW", decision)

        mock_pm.update_position_on_buy.assert_not_called()
        error_calls = [c for c in mock_emit.call_args_list if "매수 주문 실패" in str(c)]
        assert len(error_calls) >= 1

    @pytest.mark.asyncio
    async def test_sell_order_exception_handled(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """매도 주문 실패 → graceful 처리."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        decision = _make_sell_decision()
        mock_order_mgr.create_order = AsyncMock(side_effect=Exception("Exchange error"))

        pos = MagicMock()
        pos.quantity = 0.5
        session = _make_session_no_position()

        async def sell_execute(*args, **kwargs):
            result = MagicMock()
            result.scalar_one_or_none = MagicMock(return_value=pos)
            return result

        session.execute = AsyncMock(side_effect=sell_execute)

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "ETH/KRW", decision)

        mock_pm.update_position_on_sell.assert_not_called()

    @pytest.mark.asyncio
    async def test_buy_order_not_filled_cancelled(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """주문 미체결 → 취소 시도."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        decision = _make_buy_decision(confidence=0.65)

        unfilled = MagicMock()
        unfilled.status = "open"
        unfilled.id = 99
        unfilled.exchange_order_id = "ex-unfilled"
        mock_order_mgr.create_order = AsyncMock(return_value=unfilled)
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "ETH/KRW", decision)

        mock_order_mgr.cancel_order_by_id.assert_called_once()
        mock_pm.update_position_on_buy.assert_not_called()


# ── 테스트: can_trade 매수 제한 ───────────────────────────────

class TestCanTrade:
    """_can_trade 매수 제한 조건 검증."""

    def test_daily_buy_limit(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """일일 매수 한도 초과 → 차단."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        engine._daily_buy_count = 20

        can, reason = engine._can_trade("ETH/KRW", "buy")
        assert not can
        assert "Daily buy limit" in reason

    def test_coin_daily_limit(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """코인별 일일 매수 한도 초과 → 차단."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        engine._daily_coin_buy_count["ETH/KRW"] = 3

        can, reason = engine._can_trade("ETH/KRW", "buy")
        assert not can
        assert "Coin daily buy limit" in reason

    def test_cooldown_blocks(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """코인 쿨다운 (1시간) 내 → 차단."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        engine._last_trade_time["ETH/KRW"] = datetime.now(timezone.utc) - timedelta(minutes=30)

        can, reason = engine._can_trade("ETH/KRW", "buy")
        assert not can
        assert "cooldown" in reason.lower()

    def test_post_sell_washout_blocks(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """매도 후 워시아웃 (4시간) 내 → 차단."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        engine._last_sell_time["ETH/KRW"] = datetime.now(timezone.utc) - timedelta(hours=2)

        can, reason = engine._can_trade("ETH/KRW", "buy")
        assert not can
        assert "washout" in reason.lower()

    def test_paused_coin_blocks(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """리스크 에이전트 매수 중지 → 차단."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        engine._paused_coins.add("ETH/KRW")

        can, reason = engine._can_trade("ETH/KRW", "buy")
        assert not can
        assert "risk agent" in reason.lower()

    def test_sell_always_allowed(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """매도는 항상 허용 (일일 한도/쿨다운 무시)."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        engine._daily_buy_count = 20
        engine._last_trade_time["ETH/KRW"] = datetime.now(timezone.utc)
        engine._paused_coins.add("ETH/KRW")

        can, reason = engine._can_trade("ETH/KRW", "sell")
        assert can
        assert reason == "OK"

    def test_no_restrictions_passes(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """제한 없으면 매수 허용."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)

        can, reason = engine._can_trade("ETH/KRW", "buy")
        assert can
        assert reason == "OK"


# ── 테스트: 매도 실행 ────────────────────────────────────────

class TestSellExecution:
    """매도 시그널 → 매도 실행 검증."""

    @pytest.mark.asyncio
    async def test_sell_with_position(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """SELL 시그널 + 포지션 보유 → 매도 실행."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        decision = _make_sell_decision()

        pos = MagicMock()
        pos.quantity = 0.5
        pos.average_buy_price = 48000
        pos.total_invested = 24000
        session = _make_session_no_position()

        async def sell_execute(*args, **kwargs):
            result = MagicMock()
            result.scalar_one_or_none = MagicMock(return_value=pos)
            result.scalars = MagicMock(return_value=MagicMock(
                first=MagicMock(return_value=None),
                all=MagicMock(return_value=[]),
            ))
            return result

        session.execute = AsyncMock(side_effect=sell_execute)

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "ETH/KRW", decision)

        mock_order_mgr.create_order.assert_called_once()
        call_args = mock_order_mgr.create_order.call_args
        assert call_args[0][2] == "sell"
        assert call_args[0][3] == 0.5

    @pytest.mark.asyncio
    async def test_sell_without_position_noop(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """SELL 시그널 + 포지션 없음 → 아무 액션 없음."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        decision = _make_sell_decision()
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "ETH/KRW", decision)

        mock_order_mgr.create_order.assert_not_called()


# ── 테스트: 비대칭 모드 세부 ──────────────────────────────────

class TestAsymmetricMode:
    """비대칭 모드 시장 상태별 동작 검증."""

    @pytest.mark.asyncio
    async def test_uptrend_lower_threshold(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """uptrend → 신뢰도 임계값 = base(0.50) - 0.10 = 0.40. 신뢰도 0.45면 통과."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        engine._market_state = "uptrend"
        decision = _make_buy_decision(confidence=0.45)
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "ETH/KRW", decision)

        mock_order_mgr.create_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_strong_uptrend_full_size(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """strong_uptrend → 풀 사이즈 (max_trade_size_pct)."""
        mock_pm.cash_balance = 1000000
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        engine._market_state = "strong_uptrend"
        decision = _make_buy_decision(confidence=0.65)
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "ETH/KRW", decision)

        call_args = mock_order_mgr.create_order.call_args
        amount = call_args[0][3]
        # strong_uptrend: size_pct = 0.20 (full)
        # amount_krw = 1000000 * 0.20 / 1.003 ≈ 199402
        # amount = 199402 / 50000 ≈ 3.99
        assert amount > 3.5

    @pytest.mark.asyncio
    async def test_asymmetric_off_no_crash_block(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """비대칭 모드 OFF + crash → 축소 매수 (차단 안 됨, 25%)."""
        config.trading.asymmetric_mode = False
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        engine._market_state = "crash"
        engine._market_confidence = 0.3
        decision = _make_buy_decision(confidence=0.65)
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "ETH/KRW", decision)

        # 비대칭 OFF + crash → 매수 허용 (25% 축소)
        mock_order_mgr.create_order.assert_called_once()


# ── 테스트: evaluate_coin 전체 흐름 ──────────────────────────

class TestEvaluateCoin:
    """_evaluate_coin: 시그널 수집 → combiner → _process_decision 전체 흐름."""

    @pytest.mark.asyncio
    async def test_evaluate_coin_buy_signal_executes(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """전략 → BUY 시그널 → combiner → 매수 실행까지."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        engine._market_state = "uptrend"

        mock_strategy = AsyncMock()
        mock_strategy.required_timeframe = "4h"
        mock_strategy.min_candles_required = 50
        mock_strategy.analyze = AsyncMock(return_value=_make_buy_signal(0.70))
        engine._strategies = {"test_strat": mock_strategy}

        mock_combiner.combine = MagicMock(return_value=_make_buy_decision(0.70))
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._evaluate_coin(session, "ETH/KRW")

        mock_order_mgr.create_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_evaluate_coin_can_trade_false_blocks(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """can_trade=False → BUY 시그널이지만 매수 차단."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        engine._daily_buy_count = 20  # 일일 한도 초과

        mock_strategy = AsyncMock()
        mock_strategy.required_timeframe = "4h"
        mock_strategy.min_candles_required = 50
        mock_strategy.analyze = AsyncMock(return_value=_make_buy_signal(0.70))
        engine._strategies = {"test_strat": mock_strategy}

        mock_combiner.combine = MagicMock(return_value=_make_buy_decision(0.70))
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._evaluate_coin(session, "ETH/KRW")

        mock_order_mgr.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_evaluate_coin_hold_signal_no_action(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """모든 전략 HOLD → combiner HOLD → 아무 액션 없음."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)

        mock_strategy = AsyncMock()
        mock_strategy.required_timeframe = "4h"
        mock_strategy.min_candles_required = 50
        hold_signal = Signal(signal_type=SignalType.HOLD, confidence=0.3, strategy_name="test", reason="hold", indicators={})
        mock_strategy.analyze = AsyncMock(return_value=hold_signal)
        engine._strategies = {"test_strat": mock_strategy}

        mock_combiner.combine = MagicMock(return_value=_make_hold_decision())
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._evaluate_coin(session, "ETH/KRW")

        mock_order_mgr.create_order.assert_not_called()


# ── 테스트: 바이낸스 선물 매수 ────────────────────────────────

class TestFuturesBuy:
    """선물 엔진에서의 매수 (USDT 기반)."""

    @pytest.mark.asyncio
    async def test_futures_min_order_amount(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """선물 최소 주문금액 = 5 USDT."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner, exchange_name="binance_futures")
        assert engine._min_order_amount == 5.0
        assert engine._fee_margin == 1.002
        assert engine._min_fallback_amount == 10.0

    @pytest.mark.asyncio
    async def test_futures_buy_executes(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """선물 매수 성공 (USDT 기반)."""
        mock_pm.cash_balance = 300  # 300 USDT
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner, exchange_name="binance_futures")
        engine._market_state = "uptrend"
        decision = _make_buy_decision(confidence=0.65)
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "BTC/USDT", decision)

        mock_order_mgr.create_order.assert_called_once()


# ── 테스트: 빗썸 bid/ask=None 티커 폴백 ───────────────────────

class TestBithumbTickerFallback:
    """빗썸 ccxt에서 bid/ask가 None 반환 시 last로 폴백."""

    @pytest.mark.asyncio
    async def test_fetch_ticker_ask_none_fallback_last(self):
        """ask=None → ask=last 폴백."""
        from exchange.bithumb_adapter import BithumbAdapter

        adapter = BithumbAdapter.__new__(BithumbAdapter)
        mock_ccxt = AsyncMock()
        adapter._exchange = MagicMock()

        # ccxt fetch_ticker가 ask=None 반환하는 상황 시뮬레이션
        ticker_data = {
            "last": 2947.0, "bid": None, "ask": None,
            "high": 3100.0, "low": 2800.0,
            "baseVolume": 100000.0,
            "timestamp": 1741200000000,
        }

        async def fake_call(fn, *args, **kwargs):
            return ticker_data

        adapter._call = fake_call

        result = await adapter.fetch_ticker("RPL/KRW")
        assert result.last == 2947.0
        assert result.ask == 2947.0  # None → last 폴백
        assert result.bid == 2947.0  # None → last 폴백

    @pytest.mark.asyncio
    async def test_fetch_ticker_ask_zero_fallback_last(self):
        """ask=0 → ask=last 폴백."""
        from exchange.bithumb_adapter import BithumbAdapter

        adapter = BithumbAdapter.__new__(BithumbAdapter)
        adapter._exchange = MagicMock()

        ticker_data = {
            "last": 50000.0, "bid": 0, "ask": 0,
            "high": 51000.0, "low": 49000.0,
            "baseVolume": 500.0,
            "timestamp": 1741200000000,
        }

        async def fake_call(fn, *args, **kwargs):
            return ticker_data

        adapter._call = fake_call

        result = await adapter.fetch_ticker("TEST/KRW")
        assert result.ask == 50000.0
        assert result.bid == 50000.0

    @pytest.mark.asyncio
    async def test_fetch_ticker_normal_bid_ask_preserved(self):
        """bid/ask 정상이면 그대로 유지."""
        from exchange.bithumb_adapter import BithumbAdapter

        adapter = BithumbAdapter.__new__(BithumbAdapter)
        adapter._exchange = MagicMock()

        ticker_data = {
            "last": 50000.0, "bid": 49900.0, "ask": 50100.0,
            "high": 51000.0, "low": 49000.0,
            "baseVolume": 500.0,
            "timestamp": 1741200000000,
        }

        async def fake_call(fn, *args, **kwargs):
            return ticker_data

        adapter._call = fake_call

        result = await adapter.fetch_ticker("BTC/KRW")
        assert result.bid == 49900.0
        assert result.ask == 50100.0

    @pytest.mark.asyncio
    async def test_create_market_buy_with_ask_none(self):
        """bid/ask=None이어도 create_market_buy가 last 기반으로 정상 동작."""
        from exchange.bithumb_v2_adapter import BithumbV2Adapter
        from exchange.data_models import Ticker
        from datetime import datetime, timezone

        adapter = BithumbV2Adapter.__new__(BithumbV2Adapter)

        # fetch_ticker가 ask=0 반환 (ccxt None → BithumbAdapter에서 0)
        # 하지만 BithumbAdapter.fetch_ticker 폴백이 적용되어 ask=last
        ticker = Ticker(
            symbol="RPL/KRW", last=2947.0,
            bid=2947.0, ask=2947.0,  # 폴백 적용 후
            high=3100.0, low=2800.0,
            volume=100000.0,
            timestamp=datetime.now(timezone.utc),
        )
        adapter.fetch_ticker = AsyncMock(return_value=ticker)

        # amount=16 RPL (≈47,152 KRW)
        # 빗썸 V2 API 호출은 mock
        adapter._v2 = AsyncMock(return_value={"uuid": "test-uuid", "state": "done",
                                               "side": "bid", "volume": "16",
                                               "executed_volume": "16", "price": "2947",
                                               "paid_fee": "50", "created_at": "2026-03-05T12:00:00+09:00"})
        adapter._poll_fill = AsyncMock(return_value=MagicMock(
            order_id="test-uuid", status="closed", price=2947.0,
            amount=16.0, filled=16.0, cost=47152.0, fee=50.0))

        amount = 16.0  # 16 RPL
        result = await adapter.create_market_buy("RPL/KRW", amount)
        assert result.status == "closed"

    @pytest.mark.asyncio
    async def test_create_market_buy_ref_price_zero_raises(self):
        """ask=0, last=0 → 에러 발생 (주문 불가)."""
        from exchange.bithumb_v2_adapter import BithumbV2Adapter
        from exchange.data_models import Ticker
        from core.exceptions import ExchangeError
        from datetime import datetime, timezone

        adapter = BithumbV2Adapter.__new__(BithumbV2Adapter)

        # ask=0, last=0 (완전 무효 티커)
        ticker = Ticker(
            symbol="DEAD/KRW", last=0, bid=0, ask=0,
            high=0, low=0, volume=0,
            timestamp=datetime.now(timezone.utc),
        )
        adapter.fetch_ticker = AsyncMock(return_value=ticker)

        with pytest.raises(ExchangeError, match="유효한 가격 없음"):
            await adapter.create_market_buy("DEAD/KRW", 100.0)

    @pytest.mark.asyncio
    async def test_paper_adapter_ask_none_uses_last(self):
        """Paper adapter: ask=0 → last 사용하여 create_limit_buy 호출."""
        from exchange.paper_adapter import PaperAdapter
        from exchange.data_models import Ticker
        from datetime import datetime, timezone

        adapter = PaperAdapter.__new__(PaperAdapter)

        # ask=0 (None에서 변환), last=5000
        ticker = Ticker(
            symbol="TEST/KRW", last=5000.0,
            bid=0, ask=0,
            high=5100.0, low=4900.0,
            volume=1000.0,
            timestamp=datetime.now(timezone.utc),
        )
        adapter.fetch_ticker = AsyncMock(return_value=ticker)
        adapter.create_limit_buy = AsyncMock(return_value=MagicMock(
            status="closed", price=5000.0))

        result = await adapter.create_market_buy("TEST/KRW", 1.0)
        # create_limit_buy가 last=5000으로 호출되었는지 확인
        adapter.create_limit_buy.assert_called_once_with("TEST/KRW", 1.0, 5000.0)

    @pytest.mark.asyncio
    async def test_rotation_buy_with_none_ticker(self, config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner):
        """로테이션 매수: bid/ask=None이어도 정상 동작 (last 폴백)."""
        engine = _make_engine(config, mock_exchange, mock_market_data, mock_order_mgr, mock_pm, mock_combiner)
        mock_pm.cash_balance = 315000

        # market_data.get_ticker는 last 반환 (ask=0)
        mock_market_data.get_ticker = AsyncMock(return_value=MagicMock(last=2947, ask=0, bid=0))
        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._execute_rotation_buy(session, "RPL/KRW", 5.6, 0.71)

        # create_order가 호출되었는지 확인 (amount > 0)
        mock_order_mgr.create_order.assert_called_once()
        call_args = mock_order_mgr.create_order.call_args
        amount = call_args[0][3]
        assert amount > 0  # amount = amount_krw / price > 0
