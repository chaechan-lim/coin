"""COIN-41: V2 선물 엔진 거래 안전장치 테스트.

Tests for:
1. Daily buy limit
2. Consecutive error force close
3. Cooldown DB persistence/restoration
4. Downtime SL/TP check
"""

import time
import pytest
import pandas as pd
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from core.enums import Direction, Regime
from core.models import Order, Position
from engine.tier1_manager import Tier1Manager
from engine.direction_evaluator import DirectionDecision
from engine.regime_detector import RegimeDetector, RegimeState
from engine.safe_order_pipeline import SafeOrderPipeline, OrderResponse
from engine.position_state_tracker import PositionStateTracker, PositionState
from engine.portfolio_manager import PortfolioManager


# ── Fixtures ─────────────────────────────────────


@pytest.fixture(autouse=True)
def _safe_time():
    """Patch datetime.now in tier1_manager to dodge the US-open filter (KST 22-23).

    Uses the real time but shifts the hour if it falls in the danger zone
    (UTC 13-14 → KST 22-23), preserving date and relative time for cooldown tests.
    """
    def _safe_now(*args, **kwargs):
        real = _original_now(*args, **kwargs) if args else _original_now(timezone.utc)
        if real.hour in (13, 14):
            return real.replace(hour=15)
        return real

    _original_now = datetime.now
    with patch("engine.tier1_manager.datetime", wraps=datetime) as mock_dt:
        mock_dt.now.side_effect = _safe_now
        yield mock_dt


# ── Helpers ──────────────────────────────────────


def _regime_state(regime=Regime.TRENDING_UP):
    return RegimeState(
        regime=regime,
        confidence=0.8,
        adx=30,
        bb_width=3.0,
        atr_pct=1.5,
        volume_ratio=1.2,
        trend_direction=1,
        timestamp=datetime.now(timezone.utc),
    )


def _make_df(n=50, close=80000.0, atr=1000.0):
    return pd.DataFrame(
        {
            "close": [close] * n,
            "ema_9": [81000.0] * n,
            "ema_21": [80000.0] * n,
            "rsi_14": [40.0] * n,
            "atr_14": [atr] * n,
            "ema_20": [80000.0] * n,
            "ema_50": [79000.0] * n,
            "bb_upper_20": [82000.0] * n,
            "bb_lower_20": [78000.0] * n,
            "bb_mid_20": [80000.0] * n,
            "volume": [1000.0] * n,
        }
    )


def _long_open_decision(confidence=0.8, sizing_factor=0.7):
    return DirectionDecision(
        action="open",
        direction=Direction.LONG,
        confidence=confidence,
        sizing_factor=sizing_factor,
        stop_loss_atr=1.5,
        take_profit_atr=3.0,
        reason="long_signal",
        strategy_name="test_long",
        indicators={"close": 80000.0, "atr": 1000.0},
    )


def _hold_decision(name="test"):
    return DirectionDecision(
        action="hold",
        direction=None,
        confidence=0.0,
        sizing_factor=0.0,
        stop_loss_atr=0.0,
        take_profit_atr=0.0,
        reason="no_signal",
        strategy_name=name,
    )


class MockEvaluator:
    """Configurable mock evaluator."""

    def __init__(self, default_decision=None):
        self._default = default_decision or _hold_decision()
        self._decisions: dict[str, DirectionDecision] = {}
        self.call_count = 0

    @property
    def eval_interval_sec(self) -> int:
        return 60

    async def evaluate(self, symbol, current_position, **kwargs):
        self.call_count += 1
        return self._decisions.get(symbol, self._default)

    def set_decision(self, symbol, decision):
        self._decisions[symbol] = decision


@pytest.fixture(autouse=True)
def _pin_kst_hour():
    """Force _kst_hour() to return 19 (KST, safe from the 22-23 US-open filter).

    Avoids time-of-day test flakes when tests run during KST 22-23.
    Tests that explicitly test US-open behaviour override _kst_hour locally.
    """
    with patch.object(Tier1Manager, "_kst_hour", return_value=19):
        yield


@pytest.fixture
def mock_deps():
    regime = RegimeDetector()
    regime._current = _regime_state()

    safe_order = AsyncMock(spec=SafeOrderPipeline)
    safe_order.execute_order = AsyncMock(
        return_value=OrderResponse(
            success=True,
            order_id=1,
            executed_price=80000.0,
            executed_quantity=0.01,
            fee=0.32,
        )
    )

    tracker = PositionStateTracker()

    pm = MagicMock(spec=PortfolioManager)
    pm.cash_balance = 500.0

    market_data = AsyncMock()
    market_data.get_ohlcv_df = AsyncMock(return_value=_make_df())
    market_data.get_current_price = AsyncMock(return_value=80000.0)

    long_eval = MockEvaluator()
    short_eval = MockEvaluator()

    return {
        "regime": regime,
        "safe_order": safe_order,
        "tracker": tracker,
        "pm": pm,
        "market_data": market_data,
        "long_eval": long_eval,
        "short_eval": short_eval,
    }


def _make_tier1(mock_deps, **overrides):
    """Create Tier1Manager with test defaults + overrides."""
    kwargs = dict(
        coins=["BTC/USDT", "ETH/USDT"],
        safe_order=mock_deps["safe_order"],
        position_tracker=mock_deps["tracker"],
        regime_detector=mock_deps["regime"],
        portfolio_manager=mock_deps["pm"],
        market_data=mock_deps["market_data"],
        long_evaluator=mock_deps["long_eval"],
        short_evaluator=mock_deps["short_eval"],
        leverage=3,
        max_position_pct=0.15,
        daily_buy_limit=20,
        max_daily_coin_buys=3,
        max_eval_errors=3,
    )
    kwargs.update(overrides)
    return Tier1Manager(**kwargs)


# ═══════════════════════════════════════════════════
# 1. Daily Buy Limit Tests
# ═══════════════════════════════════════════════════


class TestDailyBuyLimit:
    """COIN-41: 일일 매수 한도 테스트."""

    def test_initial_state(self, mock_deps):
        """초기화 시 일일 카운터는 0."""
        t1 = _make_tier1(mock_deps)
        assert t1._daily_buy_count == 0
        assert t1._daily_coin_buy_count == {}

    def test_can_trade_within_limit(self, mock_deps):
        """한도 내 매수 허용."""
        t1 = _make_tier1(mock_deps, daily_buy_limit=5, max_daily_coin_buys=2)
        ok, reason = t1._can_trade("BTC/USDT")
        assert ok is True
        assert reason == "OK"

    def test_can_trade_daily_limit_reached(self, mock_deps):
        """일일 총 한도 도달 시 거부."""
        t1 = _make_tier1(mock_deps, daily_buy_limit=2)
        t1._daily_buy_count = 2
        ok, reason = t1._can_trade("BTC/USDT")
        assert ok is False
        assert "Daily buy limit" in reason

    def test_can_trade_coin_limit_reached(self, mock_deps):
        """코인당 한도 도달 시 거부."""
        t1 = _make_tier1(mock_deps, max_daily_coin_buys=1)
        t1._daily_coin_buy_count["BTC/USDT"] = 1
        ok, reason = t1._can_trade("BTC/USDT")
        assert ok is False
        assert "Coin daily limit" in reason

    def test_can_trade_other_coin_still_allowed(self, mock_deps):
        """한 코인 한도 도달해도 다른 코인은 허용."""
        t1 = _make_tier1(mock_deps, max_daily_coin_buys=1)
        t1._daily_coin_buy_count["BTC/USDT"] = 1
        ok, reason = t1._can_trade("ETH/USDT")
        assert ok is True

    def test_reset_daily_counter(self, mock_deps):
        """자정에 일일 카운터 리셋."""
        t1 = _make_tier1(mock_deps)
        t1._daily_buy_count = 10
        t1._daily_coin_buy_count = {"BTC/USDT": 3}
        t1._daily_reset_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        t1._reset_daily_counter()
        assert t1._daily_buy_count == 0
        assert t1._daily_coin_buy_count == {}

    def test_reset_daily_counter_same_day_noop(self, mock_deps):
        """같은 날에는 리셋하지 않음."""
        t1 = _make_tier1(mock_deps)
        t1._daily_buy_count = 5
        t1._daily_reset_date = datetime.now(timezone.utc).date()

        t1._reset_daily_counter()
        assert t1._daily_buy_count == 5

    @pytest.mark.asyncio
    async def test_daily_limit_blocks_entry(self, mock_deps, session):
        """일일 한도 도달 시 진입 차단."""
        t1 = _make_tier1(mock_deps, daily_buy_limit=0)
        mock_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision())
        mock_deps["short_eval"].set_decision("BTC/USDT", _hold_decision())

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            outcome = await t1._evaluate_coin(session, "BTC/USDT", _regime_state())
        assert outcome == "daily_limit"

    @pytest.mark.asyncio
    async def test_successful_open_increments_counter(self, mock_deps, session):
        """진입 성공 시 카운터 증가."""
        t1 = _make_tier1(mock_deps)
        mock_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision())
        mock_deps["short_eval"].set_decision("BTC/USDT", _hold_decision())

        assert t1._daily_buy_count == 0
        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            outcome = await t1._evaluate_coin(session, "BTC/USDT", _regime_state())
        assert outcome == "opened"
        assert t1._daily_buy_count == 1
        assert t1._daily_coin_buy_count["BTC/USDT"] == 1

    @pytest.mark.asyncio
    async def test_restore_daily_buy_count(self, mock_deps, session):
        """DB에서 오늘 매수 카운터 복원 — margin_used 기반으로 open 주문만 집계."""
        now = datetime.now(timezone.utc)
        # 3 long-open orders (side="buy", margin_used set)
        for i in range(3):
            order = Order(
                exchange="binance_futures",
                symbol="BTC/USDT",
                side="buy",
                order_type="market",
                status="filled",
                requested_price=80000.0,
                executed_price=80000.0,
                requested_quantity=0.01,
                executed_quantity=0.01,
                fee=0.32,
                is_paper=False,
                strategy_name="test",
                margin_used=266.67,
                created_at=now - timedelta(minutes=i),
            )
            session.add(order)

        # 1 ETH long-open order
        session.add(
            Order(
                exchange="binance_futures",
                symbol="ETH/USDT",
                side="buy",
                order_type="market",
                status="filled",
                requested_price=3000.0,
                executed_price=3000.0,
                requested_quantity=0.1,
                executed_quantity=0.1,
                fee=0.12,
                is_paper=False,
                strategy_name="test",
                margin_used=100.0,
                created_at=now,
            )
        )

        # 1 short-open order (side="sell", margin_used set) — SHOULD be counted
        session.add(
            Order(
                exchange="binance_futures",
                symbol="SOL/USDT",
                side="sell",
                order_type="market",
                status="filled",
                requested_price=150.0,
                executed_price=150.0,
                requested_quantity=1.0,
                executed_quantity=1.0,
                fee=0.06,
                is_paper=False,
                strategy_name="test",
                margin_used=50.0,
                direction="short",
                created_at=now,
            )
        )

        # 1 close-long order (side="sell", no margin_used) — should NOT be counted
        session.add(
            Order(
                exchange="binance_futures",
                symbol="BTC/USDT",
                side="sell",
                order_type="market",
                status="filled",
                requested_price=82000.0,
                executed_price=82000.0,
                requested_quantity=0.01,
                executed_quantity=0.01,
                fee=0.33,
                is_paper=False,
                strategy_name="test",
                margin_used=None,
                created_at=now,
            )
        )

        # 1 close-short order (side="buy", no margin_used) — should NOT be counted
        session.add(
            Order(
                exchange="binance_futures",
                symbol="SOL/USDT",
                side="buy",
                order_type="market",
                status="filled",
                requested_price=148.0,
                executed_price=148.0,
                requested_quantity=1.0,
                executed_quantity=1.0,
                fee=0.06,
                is_paper=False,
                strategy_name="test",
                margin_used=None,
                created_at=now,
            )
        )
        await session.commit()

        t1 = _make_tier1(mock_deps)
        await t1.restore_daily_buy_count(session)

        # 3 BTC long + 1 ETH long + 1 SOL short = 5 opens total
        assert t1._daily_buy_count == 5
        assert t1._daily_coin_buy_count["BTC/USDT"] == 3
        assert t1._daily_coin_buy_count["ETH/USDT"] == 1
        assert t1._daily_coin_buy_count["SOL/USDT"] == 1

    @pytest.mark.asyncio
    async def test_restore_daily_buy_count_ignores_cancelled(self, mock_deps, session):
        """취소/실패 주문은 일일 카운터에 포함하지 않음."""
        now = datetime.now(timezone.utc)

        # 1 filled open order (margin_used set)
        session.add(
            Order(
                exchange="binance_futures",
                symbol="BTC/USDT",
                side="buy",
                order_type="market",
                status="filled",
                requested_price=80000.0,
                executed_price=80000.0,
                requested_quantity=0.01,
                executed_quantity=0.01,
                fee=0.32,
                is_paper=False,
                strategy_name="test",
                margin_used=266.67,
                created_at=now,
            )
        )
        # 1 cancelled order (should be excluded — not filled)
        session.add(
            Order(
                exchange="binance_futures",
                symbol="BTC/USDT",
                side="buy",
                order_type="market",
                status="cancelled",
                requested_price=80000.0,
                executed_price=0.0,
                requested_quantity=0.01,
                executed_quantity=0.0,
                fee=0.0,
                is_paper=False,
                strategy_name="test",
                margin_used=266.67,
                created_at=now,
            )
        )
        # 1 failed order (should be excluded — not filled)
        session.add(
            Order(
                exchange="binance_futures",
                symbol="BTC/USDT",
                side="buy",
                order_type="market",
                status="failed",
                requested_price=80000.0,
                executed_price=0.0,
                requested_quantity=0.01,
                executed_quantity=0.0,
                fee=0.0,
                is_paper=False,
                strategy_name="test",
                margin_used=266.67,
                created_at=now,
            )
        )
        await session.commit()

        t1 = _make_tier1(mock_deps)
        await t1.restore_daily_buy_count(session)

        # Only the filled order should be counted
        assert t1._daily_buy_count == 1
        assert t1._daily_coin_buy_count["BTC/USDT"] == 1


# ═══════════════════════════════════════════════════
# 2. Consecutive Error Force Close Tests
# ═══════════════════════════════════════════════════


class TestConsecutiveErrorForceClose:
    """COIN-41: 연속 에러 강제 청산 테스트."""

    def test_initial_error_counts(self, mock_deps):
        """초기화 시 에러 카운터는 비어있음."""
        t1 = _make_tier1(mock_deps)
        assert t1._eval_error_counts == {}

    @pytest.mark.asyncio
    async def test_error_increments_counter(self, mock_deps, session):
        """평가 에러 시 카운터 증가."""
        t1 = _make_tier1(mock_deps)

        # Make evaluator raise an exception (not candle fetch, which is caught)
        async def failing_eval(symbol, pos, **kwargs):
            raise RuntimeError("Evaluator crashed")

        mock_deps["long_eval"].evaluate = failing_eval
        mock_deps["short_eval"].evaluate = failing_eval

        # We need candles to succeed so the error happens in the evaluator
        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            stats = await t1.evaluation_cycle(session)

        assert stats.error_count == 2  # BTC + ETH both fail
        assert t1._eval_error_counts.get("BTC/USDT") == 1
        assert t1._eval_error_counts.get("ETH/USDT") == 1

    @pytest.mark.asyncio
    async def test_success_resets_counter(self, mock_deps, session):
        """성공 시 에러 카운터 리셋."""
        t1 = _make_tier1(mock_deps)
        t1._eval_error_counts["BTC/USDT"] = 2  # 이전 에러
        # Default evaluators return HOLD → _evaluate_coin succeeds

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            await t1.evaluation_cycle(session)

        assert "BTC/USDT" not in t1._eval_error_counts

    @pytest.mark.asyncio
    async def test_force_close_on_max_errors(self, mock_deps, session):
        """연속 3회 에러 + 포지션 보유 시 강제 청산."""
        t1 = _make_tier1(mock_deps, max_eval_errors=3)
        # Set up position
        pos = PositionState(
            symbol="BTC/USDT",
            direction=Direction.LONG,
            quantity=0.01,
            entry_price=80000.0,
            margin=100.0,
            leverage=3,
            extreme_price=80000.0,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
            strategy_name="test",
            confidence=0.8,
            tier="tier1",
        )
        mock_deps["tracker"].open_position(pos)
        t1._eval_error_counts["BTC/USDT"] = 2  # Already 2 errors

        # Make evaluator crash (not candle fetch, which is caught gracefully)
        async def failing_eval(symbol, pos, **kwargs):
            raise RuntimeError("persistent error")

        mock_deps["long_eval"].evaluate = failing_eval
        mock_deps["short_eval"].evaluate = failing_eval

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            await t1.evaluation_cycle(session)

        # Force close was called, order should have been executed
        mock_deps["safe_order"].execute_order.assert_called()
        # Position should be closed
        assert not mock_deps["tracker"].has_position("BTC/USDT")
        # Error counter should be cleared
        assert "BTC/USDT" not in t1._eval_error_counts

    @pytest.mark.asyncio
    async def test_force_close_skips_cooldown(self, mock_deps, session):
        """강제 청산 후 쿨다운이 면제됨."""
        t1 = _make_tier1(mock_deps, max_eval_errors=1)
        pos = PositionState(
            symbol="BTC/USDT",
            direction=Direction.LONG,
            quantity=0.01,
            entry_price=80000.0,
            margin=100.0,
            leverage=3,
            extreme_price=80000.0,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
            tier="tier1",
        )
        mock_deps["tracker"].open_position(pos)
        # Set existing cooldown
        t1._last_exit_time["BTC/USDT"] = time.time()
        t1._last_exit_direction["BTC/USDT"] = Direction.LONG

        # Make evaluator crash
        async def failing_eval(symbol, pos, **kwargs):
            raise RuntimeError("error")

        mock_deps["long_eval"].evaluate = failing_eval
        mock_deps["short_eval"].evaluate = failing_eval

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            await t1.evaluation_cycle(session)

        # Cooldown should be cleared (force close exemption)
        assert "BTC/USDT" not in t1._last_exit_time
        assert "BTC/USDT" not in t1._last_exit_direction

    @pytest.mark.asyncio
    async def test_force_close_no_position(self, mock_deps, session):
        """포지션 없으면 강제 청산 스킵."""
        t1 = _make_tier1(mock_deps, max_eval_errors=1)
        t1._eval_error_counts["BTC/USDT"] = 0

        async def failing_eval(symbol, pos, **kwargs):
            raise RuntimeError("error")

        mock_deps["long_eval"].evaluate = failing_eval
        mock_deps["short_eval"].evaluate = failing_eval

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            await t1.evaluation_cycle(session)

        # No force close attempted (no position)
        mock_deps["safe_order"].execute_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_close_failure_doesnt_crash_loop(self, mock_deps, session):
        """_force_close_stuck_position 실패 시 평가 루프가 계속 진행."""
        t1 = _make_tier1(mock_deps, max_eval_errors=1, coins=["BTC/USDT", "ETH/USDT"])

        # BTC has position → force close will be attempted
        pos = PositionState(
            symbol="BTC/USDT",
            direction=Direction.LONG,
            quantity=0.01,
            entry_price=80000.0,
            margin=100.0,
            leverage=3,
            extreme_price=80000.0,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
            tier="tier1",
        )
        mock_deps["tracker"].open_position(pos)

        # Both evaluators crash
        async def failing_eval(symbol, pos_arg, **kwargs):
            raise RuntimeError("error")

        mock_deps["long_eval"].evaluate = failing_eval
        mock_deps["short_eval"].evaluate = failing_eval

        # Make force close itself raise (price fetch + DB both fail)
        mock_deps["market_data"].get_current_price = AsyncMock(
            side_effect=Exception("network down")
        )

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            # Should NOT raise — force close error is caught
            stats = await t1.evaluation_cycle(session)

        # Both coins should have been evaluated (loop wasn't interrupted)
        assert stats.error_count == 2

    @pytest.mark.asyncio
    async def test_db_reset_failure_clears_error_counter(self, mock_deps, session):
        """DB 리셋 실패해도 에러 카운터와 인메모리 상태는 정리됨."""
        t1 = _make_tier1(mock_deps, max_eval_errors=1)
        pos = PositionState(
            symbol="BTC/USDT",
            direction=Direction.LONG,
            quantity=0.01,
            entry_price=80000.0,
            margin=100.0,
            leverage=3,
            extreme_price=80000.0,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
            tier="tier1",
        )
        mock_deps["tracker"].open_position(pos)
        t1._last_exit_time["BTC/USDT"] = time.time()
        t1._last_exit_direction["BTC/USDT"] = Direction.LONG

        # Price = 0 → market close skipped → DB reset path
        mock_deps["market_data"].get_current_price = AsyncMock(return_value=0.0)

        async def failing_eval(symbol, pos_arg, **kwargs):
            raise RuntimeError("error")

        mock_deps["long_eval"].evaluate = failing_eval
        mock_deps["short_eval"].evaluate = failing_eval

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            await t1.evaluation_cycle(session)

        # Even if DB has no position row (so flush is a noop),
        # in-memory state should be cleaned via finally block
        assert "BTC/USDT" not in t1._eval_error_counts
        assert "BTC/USDT" not in t1._last_exit_time
        assert "BTC/USDT" not in t1._last_exit_direction

    @pytest.mark.asyncio
    async def test_force_close_market_failed_db_reset(self, mock_deps, session):
        """거래소 매도 실패 시 DB 리셋 폴백."""
        t1 = _make_tier1(mock_deps, max_eval_errors=1)
        pos = PositionState(
            symbol="BTC/USDT",
            direction=Direction.LONG,
            quantity=0.01,
            entry_price=80000.0,
            margin=100.0,
            leverage=3,
            extreme_price=80000.0,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
            tier="tier1",
        )
        mock_deps["tracker"].open_position(pos)

        # Create DB position
        db_pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.01,
            average_buy_price=80000.0,
            total_invested=100.0,
            margin_used=33.33,
        )
        session.add(db_pos)
        await session.flush()

        # Market data fails for price → force close falls back to DB reset
        mock_deps["market_data"].get_current_price = AsyncMock(return_value=0.0)

        async def failing_eval(symbol, pos_arg, **kwargs):
            raise RuntimeError("error")

        mock_deps["long_eval"].evaluate = failing_eval
        mock_deps["short_eval"].evaluate = failing_eval

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            await t1.evaluation_cycle(session)

        # DB position should be zeroed
        result = await session.execute(
            select(Position).where(Position.symbol == "BTC/USDT")
        )
        updated_pos = result.scalar_one_or_none()
        assert updated_pos is not None
        assert updated_pos.quantity == 0
        assert updated_pos.margin_used == 0
        assert updated_pos.total_invested == 0


# ═══════════════════════════════════════════════════
# 3. Cooldown DB Persistence Tests
# ═══════════════════════════════════════════════════


class TestCooldownPersistence:
    """COIN-41: 쿨다운 DB 영속화 테스트."""

    @pytest.mark.asyncio
    async def test_persist_cooldowns(self, mock_deps, session):
        """인메모리 쿨다운을 DB에 영속화."""
        t1 = _make_tier1(mock_deps)
        now = time.time()
        t1._last_exit_time["BTC/USDT"] = now
        t1._last_exit_direction["BTC/USDT"] = Direction.LONG

        # Create DB position
        pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.0,
            average_buy_price=80000.0,
        )
        session.add(pos)
        await session.flush()

        count = await t1.persist_cooldowns(session)
        assert count == 1
        await session.flush()

        result = await session.execute(
            select(Position).where(Position.symbol == "BTC/USDT")
        )
        updated = result.scalar_one()
        assert updated.last_sell_at is not None
        assert updated.last_sell_direction == "long"

    @pytest.mark.asyncio
    async def test_persist_cooldowns_short(self, mock_deps, session):
        """숏 방향 쿨다운 영속화."""
        t1 = _make_tier1(mock_deps)
        t1._last_exit_time["ETH/USDT"] = time.time()
        t1._last_exit_direction["ETH/USDT"] = Direction.SHORT

        pos = Position(
            exchange="binance_futures",
            symbol="ETH/USDT",
            quantity=0.0,
            average_buy_price=3000.0,
        )
        session.add(pos)
        await session.flush()

        count = await t1.persist_cooldowns(session)
        assert count == 1
        await session.flush()

        result = await session.execute(
            select(Position).where(Position.symbol == "ETH/USDT")
        )
        updated = result.scalar_one()
        assert updated.last_sell_direction == "short"

    @pytest.mark.asyncio
    async def test_persist_cooldowns_no_direction(self, mock_deps, session):
        """방향 없는 쿨다운은 영속화하지 않음."""
        t1 = _make_tier1(mock_deps)
        t1._last_exit_time["BTC/USDT"] = time.time()
        # No direction set

        pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.0,
            average_buy_price=80000.0,
        )
        session.add(pos)
        await session.flush()

        count = await t1.persist_cooldowns(session)
        assert count == 0

    @pytest.mark.asyncio
    async def test_restore_cooldowns(self, mock_deps, session):
        """DB에서 쿨다운 복원."""
        t1 = _make_tier1(
            mock_deps,
            long_cooldown_seconds=43200,  # 12h
            short_cooldown_seconds=93600,  # 26h
        )

        # Create position with recent sell
        sell_time = datetime.now(timezone.utc) - timedelta(hours=2)
        pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.0,
            average_buy_price=80000.0,
            last_sell_at=sell_time,
            last_sell_direction="long",
        )
        session.add(pos)
        await session.flush()

        count = await t1.restore_cooldowns(session)
        assert count == 1
        assert "BTC/USDT" in t1._last_exit_time
        assert t1._last_exit_direction["BTC/USDT"] == Direction.LONG

    @pytest.mark.asyncio
    async def test_restore_cooldowns_expired(self, mock_deps, session):
        """만료된 쿨다운은 복원하지 않음."""
        t1 = _make_tier1(
            mock_deps,
            long_cooldown_seconds=3600,  # 1h
            short_cooldown_seconds=3600,
        )

        # Create position with old sell (2h ago > 1h cooldown)
        sell_time = datetime.now(timezone.utc) - timedelta(hours=2)
        pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.0,
            average_buy_price=80000.0,
            last_sell_at=sell_time,
            last_sell_direction="long",
        )
        session.add(pos)
        await session.flush()

        count = await t1.restore_cooldowns(session)
        assert count == 0
        assert "BTC/USDT" not in t1._last_exit_time

    @pytest.mark.asyncio
    async def test_restore_cooldowns_short_direction(self, mock_deps, session):
        """숏 방향 쿨다운 복원."""
        t1 = _make_tier1(
            mock_deps,
            long_cooldown_seconds=43200,
            short_cooldown_seconds=93600,
        )

        sell_time = datetime.now(timezone.utc) - timedelta(hours=5)
        pos = Position(
            exchange="binance_futures",
            symbol="ETH/USDT",
            quantity=0.0,
            average_buy_price=3000.0,
            last_sell_at=sell_time,
            last_sell_direction="short",
        )
        session.add(pos)
        await session.flush()

        count = await t1.restore_cooldowns(session)
        assert count == 1
        assert t1._last_exit_direction["ETH/USDT"] == Direction.SHORT

    @pytest.mark.asyncio
    async def test_restore_cooldowns_skips_unknown_direction(self, mock_deps, session):
        """last_sell_direction과 position.direction 모두 NULL이면 쿨다운 스킵."""
        from sqlalchemy import text

        t1 = _make_tier1(
            mock_deps,
            long_cooldown_seconds=43200,
            short_cooldown_seconds=93600,
        )

        sell_time = datetime.now(timezone.utc) - timedelta(hours=1)
        # Position.direction has default="long", so use raw SQL to set NULL
        pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.0,
            average_buy_price=80000.0,
            last_sell_at=sell_time,
            last_sell_direction=None,
        )
        session.add(pos)
        await session.flush()

        # Force direction to NULL via raw SQL (ORM default prevents this)
        await session.execute(
            text("UPDATE positions SET direction = NULL WHERE symbol = 'BTC/USDT'")
        )
        await session.flush()
        # Expire cached ORM state to re-read from DB
        session.expire_all()

        count = await t1.restore_cooldowns(session)
        assert count == 0
        # Should NOT be in cooldown (skipped, not defaulted to LONG)
        assert "BTC/USDT" not in t1._last_exit_time
        assert "BTC/USDT" not in t1._last_exit_direction

    @pytest.mark.asyncio
    async def test_restore_cooldowns_fallback_direction(self, mock_deps, session):
        """last_sell_direction이 없으면 position direction 사용."""
        t1 = _make_tier1(
            mock_deps,
            long_cooldown_seconds=43200,
            short_cooldown_seconds=93600,
        )

        sell_time = datetime.now(timezone.utc) - timedelta(hours=1)
        pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.01,
            average_buy_price=80000.0,
            last_sell_at=sell_time,
            last_sell_direction=None,  # Missing direction
            direction="short",
        )
        session.add(pos)
        await session.flush()

        count = await t1.restore_cooldowns(session)
        assert count == 1
        assert t1._last_exit_direction["BTC/USDT"] == Direction.SHORT

    @pytest.mark.asyncio
    async def test_restore_ignores_other_exchange(self, mock_deps, session):
        """다른 거래소의 쿨다운은 복원하지 않음."""
        t1 = _make_tier1(
            mock_deps,
            long_cooldown_seconds=43200,
            short_cooldown_seconds=93600,
        )

        sell_time = datetime.now(timezone.utc) - timedelta(hours=1)
        pos = Position(
            exchange="bithumb",  # Different exchange
            symbol="BTC/KRW",
            quantity=0.0,
            average_buy_price=100000000.0,
            last_sell_at=sell_time,
            last_sell_direction="long",
        )
        session.add(pos)
        await session.flush()

        count = await t1.restore_cooldowns(session)
        assert count == 0

    @pytest.mark.asyncio
    async def test_roundtrip_persist_restore(self, mock_deps, session):
        """영속화 후 복원 라운드트립."""
        t1 = _make_tier1(
            mock_deps,
            long_cooldown_seconds=43200,
            short_cooldown_seconds=93600,
        )

        now = time.time()
        t1._last_exit_time["BTC/USDT"] = now
        t1._last_exit_direction["BTC/USDT"] = Direction.LONG

        # Create DB position
        pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.0,
            average_buy_price=80000.0,
        )
        session.add(pos)
        await session.flush()

        # Persist
        await t1.persist_cooldowns(session)
        await session.flush()

        # Create new Tier1Manager and restore
        t2 = _make_tier1(
            mock_deps,
            long_cooldown_seconds=43200,
            short_cooldown_seconds=93600,
        )
        assert "BTC/USDT" not in t2._last_exit_time

        count = await t2.restore_cooldowns(session)
        assert count == 1
        assert "BTC/USDT" in t2._last_exit_time
        assert t2._last_exit_direction["BTC/USDT"] == Direction.LONG
        # Timestamp should be close to original
        assert abs(t2._last_exit_time["BTC/USDT"] - now) < 2.0


# ═══════════════════════════════════════════════════
# 4. Downtime SL/TP Check Tests
# ═══════════════════════════════════════════════════


class TestDowntimeStopCheck:
    """COIN-41: 다운타임 SL/TP 체크 테스트."""

    @pytest.fixture
    def engine_deps(self, mock_deps):
        """Create FuturesEngineV2 dependencies."""
        from config import AppConfig
        from engine.futures_engine_v2 import FuturesEngineV2

        app_config = AppConfig()
        exchange = AsyncMock()
        exchange.set_leverage = AsyncMock()
        exchange.fetch_balance = AsyncMock(return_value={})

        engine = FuturesEngineV2(
            config=app_config,
            exchange=exchange,
            market_data=mock_deps["market_data"],
            order_manager=MagicMock(),
            portfolio_manager=mock_deps["pm"],
        )
        return engine

    @pytest.mark.asyncio
    async def test_no_positions_no_check(self, engine_deps, session_factory):
        """포지션 없으면 바로 리턴."""
        engine = engine_deps
        with patch(
            "engine.futures_engine_v2.get_session_factory",
            return_value=session_factory,
        ):
            with patch(
                "engine.futures_engine_v2.emit_event",
                new_callable=AsyncMock,
            ):
                # Should not raise
                await engine._check_downtime_stops()

    @pytest.mark.asyncio
    async def test_downtime_stop_triggered(self, engine_deps, session_factory):
        """다운타임 중 SL 히트 포지션 청산."""
        engine = engine_deps

        # Add position that should trigger SL
        pos = PositionState(
            symbol="BTC/USDT",
            direction=Direction.LONG,
            quantity=0.01,
            entry_price=85000.0,  # Entry high
            margin=100.0,
            leverage=3,
            extreme_price=85000.0,
            stop_loss_atr=1.5,  # SL at 85000 - 1500 = 83500
            take_profit_atr=3.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
            tier="tier1",
        )
        engine._positions.open_position(pos)

        # Current price is 80000 (below SL of 83500)
        engine._market_data.get_current_price = AsyncMock(return_value=80000.0)
        engine._market_data.get_ohlcv_df = AsyncMock(return_value=_make_df(atr=1000.0))

        # Mock the tier1 _close_position
        engine._tier1._safe_order = AsyncMock(spec=SafeOrderPipeline)
        engine._tier1._safe_order.execute_order = AsyncMock(
            return_value=OrderResponse(
                success=True,
                order_id=1,
                executed_price=80000.0,
                executed_quantity=0.01,
            )
        )

        with patch(
            "engine.futures_engine_v2.get_session_factory",
            return_value=session_factory,
        ):
            with patch(
                "engine.futures_engine_v2.emit_event",
                new_callable=AsyncMock,
            ) as mock_emit:
                await engine._check_downtime_stops()

        # Position should have been closed via SL check
        assert mock_emit.call_count >= 1
        found = any(
            args[0] == "warning" and "다운타임" in args[2]
            for args, _ in (c for c in mock_emit.call_args_list)
        )
        assert found, "Expected downtime stop event not found"
        assert not engine._positions.has_position("BTC/USDT")

    @pytest.mark.asyncio
    async def test_downtime_no_trigger_normal_price(self, engine_deps, session_factory):
        """가격 정상이면 포지션 유지."""
        engine = engine_deps

        pos = PositionState(
            symbol="BTC/USDT",
            direction=Direction.LONG,
            quantity=0.01,
            entry_price=80000.0,
            margin=100.0,
            leverage=3,
            extreme_price=82000.0,
            stop_loss_atr=1.5,  # SL at 80000 - 1500 = 78500
            take_profit_atr=14.0,  # TP at 80000 + 14000 = 94000
            trailing_activation_atr=3.0,
            trailing_stop_atr=1.5,
            tier="tier1",
        )
        engine._positions.open_position(pos)

        # Price 81000 — within SL/TP bounds
        engine._market_data.get_current_price = AsyncMock(return_value=81000.0)
        engine._market_data.get_ohlcv_df = AsyncMock(
            return_value=_make_df(close=81000.0)
        )

        with patch(
            "engine.futures_engine_v2.get_session_factory",
            return_value=session_factory,
        ):
            with patch(
                "engine.futures_engine_v2.emit_event",
                new_callable=AsyncMock,
            ):
                await engine._check_downtime_stops()

        # Position should still exist
        assert engine._positions.has_position("BTC/USDT")

    @pytest.mark.asyncio
    async def test_check_position_stop_public_api(self, engine_deps, session_factory):
        """check_position_stop (public) 메서드가 _check_sl_tp에 위임."""
        engine = engine_deps

        pos = PositionState(
            symbol="BTC/USDT",
            direction=Direction.LONG,
            quantity=0.01,
            entry_price=85000.0,
            margin=100.0,
            leverage=3,
            extreme_price=85000.0,
            stop_loss_atr=1.5,  # SL at 85000 - 1500 = 83500
            take_profit_atr=3.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
            tier="tier1",
        )
        engine._positions.open_position(pos)
        pos.update_extreme(85000.0)

        # Mock the close
        engine._tier1._safe_order = AsyncMock(spec=SafeOrderPipeline)
        engine._tier1._safe_order.execute_order = AsyncMock(
            return_value=OrderResponse(
                success=True,
                order_id=1,
                executed_price=80000.0,
                executed_quantity=0.01,
            )
        )

        from db.session import get_session_factory

        sf = session_factory
        async with sf() as session:
            with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
                # Price below SL → should trigger
                result = await engine._tier1.check_position_stop(
                    session, "BTC/USDT", pos, price=80000.0, atr=1000.0
                )
            assert result is True

    @pytest.mark.asyncio
    async def test_downtime_error_handling(self, engine_deps, session_factory):
        """다운타임 체크 중 에러 시 루프 중단 없음."""
        engine = engine_deps

        pos = PositionState(
            symbol="BTC/USDT",
            direction=Direction.LONG,
            quantity=0.01,
            entry_price=80000.0,
            margin=100.0,
            leverage=3,
            extreme_price=80000.0,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
            tier="tier1",
        )
        engine._positions.open_position(pos)

        engine._market_data.get_current_price = AsyncMock(
            side_effect=Exception("network error")
        )

        with patch(
            "engine.futures_engine_v2.get_session_factory",
            return_value=session_factory,
        ):
            with patch(
                "engine.futures_engine_v2.emit_event",
                new_callable=AsyncMock,
            ):
                # Should not raise
                await engine._check_downtime_stops()


# ═══════════════════════════════════════════════════
# 5. FuturesEngineV2 Integration Tests
# ═══════════════════════════════════════════════════


class TestFuturesV2SafeguardIntegration:
    """COIN-41: FuturesEngineV2 안전장치 통합 테스트."""

    @pytest.fixture
    def engine(self, mock_deps):
        from config import AppConfig
        from engine.futures_engine_v2 import FuturesEngineV2

        app_config = AppConfig()
        exchange = AsyncMock()
        exchange.set_leverage = AsyncMock()
        exchange.fetch_balance = AsyncMock(return_value={})

        return FuturesEngineV2(
            config=app_config,
            exchange=exchange,
            market_data=mock_deps["market_data"],
            order_manager=MagicMock(),
            portfolio_manager=mock_deps["pm"],
        )

    def test_eval_error_counts_references_tier1(self, engine):
        """_eval_error_counts가 Tier1Manager 인스턴스를 참조."""
        engine._tier1._eval_error_counts["BTC/USDT"] = 2
        assert engine._eval_error_counts["BTC/USDT"] == 2

    def test_eval_error_counts_setter(self, engine):
        """_eval_error_counts 설정 시 Tier1Manager에 반영."""
        engine._eval_error_counts = {"ETH/USDT": 3}
        assert engine._tier1._eval_error_counts == {"ETH/USDT": 3}

    def test_get_status_includes_safeguards(self, engine):
        """get_status()에 안전장치 정보 포함."""
        status = engine.get_status()
        assert "daily_buy_count" in status
        assert "eval_error_counts" in status

    def test_tier1_status_includes_safeguards(self, engine):
        """tier1_status에 안전장치 정보 포함."""
        status = engine.get_tier1_status()
        assert "daily_buy_count" in status
        assert "daily_buy_limit" in status
        assert "eval_error_counts" in status

    @pytest.mark.asyncio
    async def test_initialize_restores_cooldowns(self, engine, session_factory):
        """initialize()가 쿨다운과 일일 카운터를 복원."""
        with patch(
            "engine.futures_engine_v2.get_session_factory",
            return_value=session_factory,
        ):
            await engine.initialize()
        # No error — cooldowns/counters were restored from empty DB

    @pytest.mark.asyncio
    async def test_start_calls_downtime_check(self, engine, session_factory):
        """start()가 downtime SL/TP 체크를 호출."""
        with patch(
            "engine.futures_engine_v2.emit_event",
            new_callable=AsyncMock,
        ):
            with patch.object(
                engine, "_check_downtime_stops", new_callable=AsyncMock
            ) as mock_check:
                await engine.start()
                mock_check.assert_called_once()

            await engine.stop()

    @pytest.mark.asyncio
    async def test_persist_loop_persists_cooldowns(
        self, engine, mock_deps, session_factory
    ):
        """_persist_loop가 쿨다운을 DB에 영속화."""
        mock_deps["pm"].take_snapshot = AsyncMock(return_value=None)

        engine._is_running = True
        engine._tier1._last_exit_time["BTC/USDT"] = time.time()
        engine._tier1._last_exit_direction["BTC/USDT"] = Direction.LONG

        with patch(
            "engine.futures_engine_v2.get_session_factory",
            return_value=session_factory,
        ):
            call_count = 0

            async def mock_sleep(seconds):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return  # skip initial 120s sleep
                engine._is_running = False  # stop after first persist

            with patch("asyncio.sleep", side_effect=mock_sleep):
                await engine._persist_loop()

        # persist_cooldowns was called (no error = success)


# ═══════════════════════════════════════════════════
# 6. Config Tests
# ═══════════════════════════════════════════════════


class TestConfig:
    """COIN-41: FuturesV2Config 안전장치 설정 테스트."""

    def test_default_config_values(self):
        from config import AppConfig

        config = AppConfig()
        v2 = config.futures_v2
        assert v2.tier1_daily_buy_limit == 20
        assert v2.tier1_max_daily_coin_buys == 3
        assert v2.tier1_max_eval_errors == 3

    def test_config_passed_to_tier1(self, mock_deps):
        from config import AppConfig

        config = AppConfig()
        v2 = config.futures_v2

        from engine.futures_engine_v2 import FuturesEngineV2

        engine = FuturesEngineV2(
            config=config,
            exchange=AsyncMock(),
            market_data=mock_deps["market_data"],
            order_manager=MagicMock(),
            portfolio_manager=mock_deps["pm"],
        )
        assert engine._tier1._daily_buy_limit == v2.tier1_daily_buy_limit
        assert engine._tier1._max_daily_coin_buys == v2.tier1_max_daily_coin_buys
        assert engine._tier1._max_eval_errors == v2.tier1_max_eval_errors


# ═══════════════════════════════════════════════════
# 7. Position Model Tests
# ═══════════════════════════════════════════════════


class TestPositionModel:
    """COIN-41: Position 모델 last_sell_direction 컬럼 테스트."""

    @pytest.mark.asyncio
    async def test_last_sell_direction_column(self, session):
        """Position.last_sell_direction 컬럼 존재 및 값 저장."""
        pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.01,
            average_buy_price=80000.0,
            last_sell_direction="long",
        )
        session.add(pos)
        await session.flush()

        result = await session.execute(
            select(Position).where(Position.symbol == "BTC/USDT")
        )
        saved = result.scalar_one()
        assert saved.last_sell_direction == "long"

    @pytest.mark.asyncio
    async def test_last_sell_direction_nullable(self, session):
        """last_sell_direction이 없어도 생성 가능 (기존 호환)."""
        pos = Position(
            exchange="binance_futures",
            symbol="ETH/USDT",
            quantity=0.01,
            average_buy_price=3000.0,
        )
        session.add(pos)
        await session.flush()

        result = await session.execute(
            select(Position).where(Position.symbol == "ETH/USDT")
        )
        saved = result.scalar_one()
        assert saved.last_sell_direction is None
