"""Tier1Manager 테스트 — 듀얼 이밸류에이터 아키텍처."""

import pytest
import time
import pandas as pd
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from sqlalchemy import select

from engine.tier1_manager import Tier1Manager, CycleStats
from engine.direction_evaluator import DirectionDecision
from engine.regime_detector import RegimeDetector, RegimeState
from engine.safe_order_pipeline import SafeOrderPipeline, OrderResponse
from engine.position_state_tracker import PositionStateTracker, PositionState
from engine.portfolio_manager import PortfolioManager
from core.constants import MIN_NOTIONAL
from core.enums import Direction, Regime
from core.models import StrategyLog


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


def _make_df(n=50, close=80000.0, atr=1000.0, ema_9=81000.0, ema_21=80000.0, rsi=40.0):
    return pd.DataFrame(
        {
            "close": [close] * n,
            "ema_9": [ema_9] * n,
            "ema_21": [ema_21] * n,
            "rsi_14": [rsi] * n,
            "atr_14": [atr] * n,
            "ema_20": [80000.0] * n,
            "ema_50": [79000.0] * n,
            "bb_upper_20": [82000.0] * n,
            "bb_lower_20": [78000.0] * n,
            "bb_mid_20": [80000.0] * n,
            "volume": [1000.0] * n,
        }
    )


def _hold_decision(strategy_name="test_strategy"):
    """HOLD 결정 생성."""
    return DirectionDecision(
        action="hold",
        direction=None,
        confidence=0.0,
        sizing_factor=0.0,
        stop_loss_atr=0.0,
        take_profit_atr=0.0,
        reason="no_signal",
        strategy_name=strategy_name,
    )


def _long_open_decision(confidence=0.8, sizing_factor=0.7, indicators=None):
    """LONG 진입 결정 생성."""
    return DirectionDecision(
        action="open",
        direction=Direction.LONG,
        confidence=confidence,
        sizing_factor=sizing_factor,
        stop_loss_atr=1.5,
        take_profit_atr=3.0,
        reason="long_signal",
        strategy_name="trend_follower",
        indicators=indicators or {},
    )


def _short_open_decision(confidence=0.7, sizing_factor=0.6, indicators=None):
    """SHORT 진입 결정 생성."""
    return DirectionDecision(
        action="open",
        direction=Direction.SHORT,
        confidence=confidence,
        sizing_factor=sizing_factor,
        stop_loss_atr=1.5,
        take_profit_atr=3.0,
        reason="short_signal",
        strategy_name="mean_reversion",
        indicators=indicators or {},
    )


def _close_decision(strategy_name="trend_follower"):
    """청산 결정 생성."""
    return DirectionDecision(
        action="close",
        direction=None,
        confidence=0.6,
        sizing_factor=0.5,
        stop_loss_atr=1.5,
        take_profit_atr=3.0,
        reason="exit_signal",
        strategy_name=strategy_name,
    )


class MockLongEvaluator:
    """테스트용 롱 이밸류에이터."""

    def __init__(self, default_decision=None):
        self._default = default_decision or _hold_decision("long_eval")
        self._decisions: dict[str, DirectionDecision] = {}
        self.call_count = 0
        self.call_args: list[tuple] = []

    @property
    def eval_interval_sec(self) -> int:
        return 60

    async def evaluate(self, symbol, current_position, **kwargs):
        self.call_count += 1
        self.call_args.append((symbol, current_position))
        self.last_kwargs = kwargs
        return self._decisions.get(symbol, self._default)

    def set_decision(self, symbol, decision):
        self._decisions[symbol] = decision


class MockShortEvaluator:
    """테스트용 숏 이밸류에이터."""

    def __init__(self, default_decision=None):
        self._default = default_decision or _hold_decision("short_eval")
        self._decisions: dict[str, DirectionDecision] = {}
        self.call_count = 0
        self.call_args: list[tuple] = []

    @property
    def eval_interval_sec(self) -> int:
        return 60

    async def evaluate(self, symbol, current_position, **kwargs):
        self.call_count += 1
        self.call_args.append((symbol, current_position))
        self.last_kwargs = kwargs
        return self._decisions.get(symbol, self._default)

    def set_decision(self, symbol, decision):
        self._decisions[symbol] = decision


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

    long_eval = MockLongEvaluator()
    short_eval = MockShortEvaluator()

    return {
        "regime": regime,
        "safe_order": safe_order,
        "tracker": tracker,
        "pm": pm,
        "market_data": market_data,
        "long_eval": long_eval,
        "short_eval": short_eval,
    }


@pytest.fixture
def tier1(mock_deps):
    return Tier1Manager(
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
    )


class TestInit:
    def test_coins(self, tier1):
        assert tier1.coins == ["BTC/USDT", "ETH/USDT"]

    def test_dual_evaluators_stored(self, tier1, mock_deps):
        """롱/숏 이밸류에이터가 올바르게 저장됨."""
        assert tier1._long_evaluator is mock_deps["long_eval"]
        assert tier1._short_evaluator is mock_deps["short_eval"]


class TestMarginCalc:
    def test_normal_calc(self, tier1):
        decision = DirectionDecision(
            action="open",
            direction=Direction.LONG,
            confidence=0.8,
            sizing_factor=0.7,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="test",
            strategy_name="test",
        )
        margin = tier1._calc_margin(decision, close=80000.0, atr=1000.0)
        assert margin > 0
        assert margin <= 500.0 * 0.15  # max_position_pct

    def test_zero_cash(self, tier1, mock_deps):
        mock_deps["pm"].cash_balance = 0.0
        decision = DirectionDecision(
            action="open",
            direction=Direction.LONG,
            confidence=0.8,
            sizing_factor=0.7,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="test",
            strategy_name="test",
        )
        margin = tier1._calc_margin(decision, close=80000.0, atr=1000.0)
        assert margin == 0.0

    def test_too_small_margin(self, tier1, mock_deps):
        mock_deps["pm"].cash_balance = 10.0
        decision = DirectionDecision(
            action="open",
            direction=Direction.LONG,
            confidence=0.1,
            sizing_factor=0.1,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="test",
            strategy_name="test",
        )
        margin = tier1._calc_margin(decision, close=80000.0, atr=1000.0)
        assert margin == 0.0  # < 5 USDT

    def test_min_notional_floor_btc_small_balance(self, tier1, mock_deps):
        """COIN-31: BTC $259 잔고에서 ATR sizing이 작아도 MIN_NOTIONAL 보장."""
        mock_deps["pm"].cash_balance = 259.0
        decision = DirectionDecision(
            action="open",
            direction=Direction.SHORT,
            confidence=0.6,
            sizing_factor=0.5,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="short_signal",
            strategy_name="mean_reversion",
        )
        margin = tier1._calc_margin(decision, close=84000.0, atr=1200.0)
        assert margin > 0
        assert margin * tier1._leverage >= MIN_NOTIONAL

    def test_min_notional_floor_cash_sufficient(self, tier1, mock_deps):
        """잔고가 min_margin 이상이고 max_margin >= min_margin이면 MIN_NOTIONAL 보장."""
        # cash=250 → max_margin=250*0.15=37.5 >= min_margin=105/3=35
        mock_deps["pm"].cash_balance = 250.0
        decision = DirectionDecision(
            action="open",
            direction=Direction.LONG,
            confidence=0.5,
            sizing_factor=0.3,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="test",
            strategy_name="test",
        )
        margin = tier1._calc_margin(decision, close=84000.0, atr=2000.0)
        assert margin >= MIN_NOTIONAL / tier1._leverage

    def test_min_notional_returns_zero_when_max_margin_below_min_margin(
        self, tier1, mock_deps
    ):
        """max_margin < min_margin이면 리스크 한도 보호를 위해 0 반환."""
        # cash=100 → max_margin=100*0.15=15.0 < min_margin=105/3=35.0
        # 계좌가 이 코인을 안전하게 거래하기엔 너무 작음
        mock_deps["pm"].cash_balance = 100.0
        decision = DirectionDecision(
            action="open",
            direction=Direction.LONG,
            confidence=0.5,
            sizing_factor=0.3,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="test",
            strategy_name="test",
        )
        margin = tier1._calc_margin(decision, close=84000.0, atr=1200.0)
        assert margin == 0.0

    def test_min_notional_no_bump_when_already_sufficient(self, tier1, mock_deps):
        """ATR sizing이 이미 충분하면 bumping 안 함."""
        mock_deps["pm"].cash_balance = 500.0
        decision = DirectionDecision(
            action="open",
            direction=Direction.LONG,
            confidence=0.8,
            sizing_factor=0.7,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="test",
            strategy_name="test",
        )
        margin = tier1._calc_margin(decision, close=80000.0, atr=1000.0)
        assert margin > 0
        assert margin * tier1._leverage >= MIN_NOTIONAL

    def test_min_notional_cash_insufficient(self, tier1, mock_deps):
        """잔고가 min_margin 미만이면 0 반환."""
        mock_deps["pm"].cash_balance = 30.0  # < 105/3 = 35
        decision = DirectionDecision(
            action="open",
            direction=Direction.SHORT,
            confidence=0.6,
            sizing_factor=0.5,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="test",
            strategy_name="test",
        )
        margin = tier1._calc_margin(decision, close=84000.0, atr=1200.0)
        assert margin == 0.0

    def test_min_notional_other_coins_unaffected(self, tier1, mock_deps):
        """ETH 등 가격 낮은 코인은 기존 동작 유지."""
        mock_deps["pm"].cash_balance = 259.0
        decision = DirectionDecision(
            action="open",
            direction=Direction.SHORT,
            confidence=0.7,
            sizing_factor=0.6,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="test",
            strategy_name="test",
        )
        margin = tier1._calc_margin(decision, close=2000.0, atr=50.0)
        assert margin > 0
        assert margin * tier1._leverage >= MIN_NOTIONAL


class TestEvaluationCycle:
    @pytest.mark.asyncio
    async def test_skips_without_regime(self, tier1, mock_deps, session):
        """레짐 없으면 스킵."""
        mock_deps["regime"]._current = None
        await tier1.evaluation_cycle(session)
        mock_deps["safe_order"].execute_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_evaluates_all_coins(self, tier1, mock_deps, session):
        """모든 코인을 평가 — 양쪽 이밸류에이터 호출됨."""
        await tier1.evaluation_cycle(session)
        # No position → both evaluators called for each coin
        assert mock_deps["long_eval"].call_count >= 2
        assert mock_deps["short_eval"].call_count >= 2

    @pytest.mark.asyncio
    async def test_handles_candle_error(self, tier1, mock_deps, session):
        """캔들 에러 시 안전하게 스킵."""
        mock_deps["market_data"].get_ohlcv_df.side_effect = Exception("API error")
        # Even with candle error, evaluators may or may not be called
        # but cycle should not raise
        await tier1.evaluation_cycle(session)


class TestDualEvaluatorExecution:
    """듀얼 이밸류에이터 진입/청산 시나리오 테스트."""

    @pytest.mark.asyncio
    async def test_long_open_on_long_signal(self, tier1, mock_deps, session):
        """롱 이밸류에이터가 open 반환 → 롱 포지션 진입."""
        mock_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision())
        await tier1.evaluation_cycle(session)

        # execute_order가 호출되어야 함
        open_calls = [
            c
            for c in mock_deps["safe_order"].execute_order.call_args_list
            if c[0][1].action == "open" and c[0][1].symbol == "BTC/USDT"
        ]
        assert len(open_calls) >= 1
        assert open_calls[0][0][1].direction == Direction.LONG

    @pytest.mark.asyncio
    async def test_short_open_on_short_signal(self, tier1, mock_deps, session):
        """숏 이밸류에이터가 open 반환 → 숏 포지션 진입."""
        mock_deps["short_eval"].set_decision("BTC/USDT", _short_open_decision())
        await tier1.evaluation_cycle(session)

        open_calls = [
            c
            for c in mock_deps["safe_order"].execute_order.call_args_list
            if c[0][1].action == "open" and c[0][1].symbol == "BTC/USDT"
        ]
        assert len(open_calls) >= 1
        assert open_calls[0][0][1].direction == Direction.SHORT

    @pytest.mark.asyncio
    async def test_both_hold_no_action(self, tier1, mock_deps, session):
        """양쪽 다 hold → 아무 행동 없음."""
        # Default is hold for both evaluators
        await tier1.evaluation_cycle(session)
        mock_deps["safe_order"].execute_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_conflict_resolution_higher_confidence_wins(
        self, tier1, mock_deps, session
    ):
        """양쪽 동시 open → confidence 높은 쪽 선택."""
        mock_deps["long_eval"].set_decision(
            "BTC/USDT", _long_open_decision(confidence=0.9)
        )
        mock_deps["short_eval"].set_decision(
            "BTC/USDT", _short_open_decision(confidence=0.7)
        )

        await tier1.evaluation_cycle(session)

        open_calls = [
            c
            for c in mock_deps["safe_order"].execute_order.call_args_list
            if c[0][1].action == "open" and c[0][1].symbol == "BTC/USDT"
        ]
        assert len(open_calls) >= 1
        # 롱의 confidence가 더 높으므로 롱 선택
        assert open_calls[0][0][1].direction == Direction.LONG

    @pytest.mark.asyncio
    async def test_conflict_resolution_short_higher_confidence(
        self, tier1, mock_deps, session
    ):
        """양쪽 동시 open → 숏의 confidence가 더 높으면 숏 선택."""
        mock_deps["long_eval"].set_decision(
            "BTC/USDT", _long_open_decision(confidence=0.5)
        )
        mock_deps["short_eval"].set_decision(
            "BTC/USDT", _short_open_decision(confidence=0.8)
        )

        await tier1.evaluation_cycle(session)

        open_calls = [
            c
            for c in mock_deps["safe_order"].execute_order.call_args_list
            if c[0][1].action == "open" and c[0][1].symbol == "BTC/USDT"
        ]
        assert len(open_calls) >= 1
        assert open_calls[0][0][1].direction == Direction.SHORT

    @pytest.mark.asyncio
    async def test_conflict_equal_confidence_prefers_long(
        self, tier1, mock_deps, session
    ):
        """양쪽 동일 confidence → 롱 우선 (>=)."""
        mock_deps["long_eval"].set_decision(
            "BTC/USDT", _long_open_decision(confidence=0.7)
        )
        mock_deps["short_eval"].set_decision(
            "BTC/USDT", _short_open_decision(confidence=0.7)
        )

        await tier1.evaluation_cycle(session)

        open_calls = [
            c
            for c in mock_deps["safe_order"].execute_order.call_args_list
            if c[0][1].action == "open" and c[0][1].symbol == "BTC/USDT"
        ]
        assert len(open_calls) >= 1
        assert open_calls[0][0][1].direction == Direction.LONG


class TestPositionHeld:
    """포지션 보유 중 시나리오."""

    @pytest.mark.asyncio
    async def test_long_held_close_signal(self, tier1, mock_deps, session):
        """롱 포지션 보유 중 — 롱 이밸류에이터가 close 반환 → 청산."""
        state = PositionState(
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
        )
        mock_deps["tracker"].open_position(state)
        mock_deps["long_eval"].set_decision("BTC/USDT", _close_decision())

        await tier1.evaluation_cycle(session)

        close_calls = [
            c
            for c in mock_deps["safe_order"].execute_order.call_args_list
            if c[0][1].action == "close" and c[0][1].symbol == "BTC/USDT"
        ]
        assert len(close_calls) >= 1

    @pytest.mark.asyncio
    async def test_short_held_close_signal(self, tier1, mock_deps, session):
        """숏 포지션 보유 중 — 숏 이밸류에이터가 close 반환 → 청산."""
        state = PositionState(
            symbol="BTC/USDT",
            direction=Direction.SHORT,
            quantity=0.01,
            entry_price=80000.0,
            margin=100.0,
            leverage=3,
            extreme_price=80000.0,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
        )
        mock_deps["tracker"].open_position(state)
        mock_deps["short_eval"].set_decision(
            "BTC/USDT", _close_decision("mean_reversion")
        )

        await tier1.evaluation_cycle(session)

        close_calls = [
            c
            for c in mock_deps["safe_order"].execute_order.call_args_list
            if c[0][1].action == "close" and c[0][1].symbol == "BTC/USDT"
        ]
        assert len(close_calls) >= 1

    @pytest.mark.asyncio
    async def test_long_held_hold_signal_no_action(self, tier1, mock_deps, session):
        """롱 포지션 보유 중 — 롱 이밸류에이터가 hold → 유지."""
        state = PositionState(
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
        )
        mock_deps["tracker"].open_position(state)
        # Default is hold for both evaluators

        await tier1.evaluation_cycle(session)

        # No close/open calls (only possible for other coins)
        btc_calls = [
            c
            for c in mock_deps["safe_order"].execute_order.call_args_list
            if c[0][1].symbol == "BTC/USDT"
        ]
        assert len(btc_calls) == 0


class TestSLTPCheck:
    """SL/TP/trailing 체크 테스트."""

    @pytest.mark.asyncio
    async def test_sl_check(self, tier1, mock_deps, session):
        """SL 히트 시 청산."""
        state = PositionState(
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
        )
        mock_deps["tracker"].open_position(state)

        # 가격이 SL 이하로 떨어짐 (78000 < 80000 - 1.5*1000 = 78500)
        sl_price_df = _make_df(close=78000.0, atr=1000.0)
        mock_deps["market_data"].get_ohlcv_df = AsyncMock(return_value=sl_price_df)

        await tier1.evaluation_cycle(session)
        close_calls = [
            c
            for c in mock_deps["safe_order"].execute_order.call_args_list
            if c[0][1].action == "close"
        ]
        assert len(close_calls) >= 1
        assert close_calls[0][0][1].symbol == "BTC/USDT"

    @pytest.mark.asyncio
    async def test_sl_tp_happens_before_evaluator(self, tier1, mock_deps, session):
        """SL 히트 시 이밸류에이터는 호출되지 않음."""
        state = PositionState(
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
        )
        mock_deps["tracker"].open_position(state)

        sl_price_df = _make_df(close=78000.0, atr=1000.0)
        mock_deps["market_data"].get_ohlcv_df = AsyncMock(return_value=sl_price_df)

        await tier1.evaluation_cycle(session)

        # BTC/USDT에 대해 long_eval이 호출되지 않아야 함 (SL이 먼저 발동)
        btc_calls = [
            args for args in mock_deps["long_eval"].call_args if args[0] == "BTC/USDT"
        ]
        assert len(btc_calls) == 0


class TestCooldown:
    """쿨다운 테스트."""

    @pytest.mark.asyncio
    async def test_cooldown_blocks_entry(self, tier1, mock_deps, session):
        """쿨다운 중 진입 차단."""
        tier1._last_exit_time["BTC/USDT"] = time.time()  # 방금 청산
        mock_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision())

        stats = await tier1.evaluation_cycle(session)

        btc_open = [
            c
            for c in mock_deps["safe_order"].execute_order.call_args_list
            if c[0][1].action == "open" and c[0][1].symbol == "BTC/USDT"
        ]
        assert len(btc_open) == 0
        assert stats.cooldown_count >= 1

    @pytest.mark.asyncio
    async def test_cooldown_expired_allows_entry(self, tier1, mock_deps, session):
        """쿨다운 만료 후 진입 허용."""
        tier1._last_exit_time["BTC/USDT"] = time.time() - 100000  # 오래전 청산
        mock_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision())

        await tier1.evaluation_cycle(session)

        btc_open = [
            c
            for c in mock_deps["safe_order"].execute_order.call_args_list
            if c[0][1].action == "open" and c[0][1].symbol == "BTC/USDT"
        ]
        assert len(btc_open) >= 1


class TestLowConfidence:
    """최소 신뢰도 필터 테스트."""

    @pytest.mark.asyncio
    async def test_low_confidence_blocks_entry(self, tier1, mock_deps, session):
        """min_confidence 미만이면 진입 차단."""
        mock_deps["long_eval"].set_decision(
            "BTC/USDT",
            _long_open_decision(confidence=0.1),
        )

        stats = await tier1.evaluation_cycle(session)

        btc_open = [
            c
            for c in mock_deps["safe_order"].execute_order.call_args_list
            if c[0][1].action == "open" and c[0][1].symbol == "BTC/USDT"
        ]
        assert len(btc_open) == 0
        assert stats.low_confidence_count >= 1


class TestMarginInsufficient:
    """마진 부족 시 was_executed=False 테스트."""

    @pytest.mark.asyncio
    async def test_margin_insufficient_not_executed(self, tier1, mock_deps, session):
        """마진 부족(cash=0) 시 was_executed=False, outcome=margin_insufficient."""
        mock_deps["pm"].cash_balance = 0.0
        mock_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision())

        stats = await tier1.evaluation_cycle(session)

        # 주문 미실행
        btc_open = [
            c
            for c in mock_deps["safe_order"].execute_order.call_args_list
            if c[0][1].action == "open" and c[0][1].symbol == "BTC/USDT"
        ]
        assert len(btc_open) == 0

        # stats에 margin_insufficient 반영 (low_confidence_count에 합산)
        assert stats.low_confidence_count >= 1
        assert stats.decisions.get("BTC/USDT") == "margin_insufficient"

    @pytest.mark.asyncio
    async def test_margin_insufficient_logged_not_executed(
        self,
        tier1,
        mock_deps,
        session,
    ):
        """마진 부족 시 StrategyLog에 was_executed=False로 기록됨."""
        mock_deps["pm"].cash_balance = 0.0
        mock_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision())

        await tier1.evaluation_cycle(session)
        await session.flush()

        result = await session.execute(
            select(StrategyLog).where(StrategyLog.symbol == "BTC/USDT")
        )
        logs = result.scalars().all()
        assert len(logs) >= 1
        # 마진 부족이므로 실행되지 않아야 함
        buy_logs = [lg for lg in logs if lg.signal_type == "BUY"]
        assert len(buy_logs) >= 1
        assert buy_logs[0].was_executed is False, (
            "Margin insufficient should log was_executed=False"
        )

    @pytest.mark.asyncio
    async def test_order_failure_not_executed(self, tier1, mock_deps, session):
        """주문 실패 시 was_executed=False."""
        mock_deps["safe_order"].execute_order = AsyncMock(
            return_value=OrderResponse(
                success=False,
                order_id=None,
                executed_price=0.0,
                executed_quantity=0.0,
                fee=0.0,
            )
        )
        mock_deps["long_eval"].set_decision(
            "BTC/USDT",
            DirectionDecision(
                action="open",
                direction=Direction.LONG,
                confidence=0.8,
                sizing_factor=0.7,
                stop_loss_atr=1.5,
                take_profit_atr=3.0,
                reason="long_signal",
                strategy_name="trend_follower",
                indicators={"close": 80000.0, "atr": 1000.0},
            ),
        )

        stats = await tier1.evaluation_cycle(session)
        assert stats.decisions.get("BTC/USDT") == "margin_insufficient"

        await session.flush()
        result = await session.execute(
            select(StrategyLog).where(StrategyLog.symbol == "BTC/USDT")
        )
        logs = result.scalars().all()
        buy_logs = [lg for lg in logs if lg.signal_type == "BUY"]
        assert len(buy_logs) >= 1
        assert buy_logs[0].was_executed is False


class TestCycleStats:
    """CycleStats 데이터클래스 테스트."""

    def test_default_values(self):
        stats = CycleStats()
        assert stats.coins_evaluated == 0
        assert stats.hold_count == 0
        assert stats.low_confidence_count == 0
        assert stats.cooldown_count == 0
        assert stats.sl_tp_count == 0
        assert stats.executed_count == 0
        assert stats.error_count == 0
        assert stats.candle_error_count == 0
        assert stats.decisions == {}

    def test_decisions_dict_independent(self):
        """각 인스턴스가 독립적인 decisions dict를 가짐."""
        s1 = CycleStats()
        s2 = CycleStats()
        s1.decisions["BTC"] = "hold"
        assert s2.decisions == {}


class TestCycleObservability:
    """COIN-17: 평가 사이클 관측성 테스트."""

    @pytest.mark.asyncio
    async def test_cycle_returns_stats(self, tier1, mock_deps, session):
        """evaluation_cycle은 CycleStats를 반환한다."""
        stats = await tier1.evaluation_cycle(session)
        assert isinstance(stats, CycleStats)
        assert stats.coins_evaluated == 2  # BTC, ETH

    @pytest.mark.asyncio
    async def test_cycle_returns_empty_stats_without_regime(
        self, tier1, mock_deps, session
    ):
        """레짐 없으면 빈 stats 반환."""
        mock_deps["regime"]._current = None
        stats = await tier1.evaluation_cycle(session)
        assert stats.coins_evaluated == 0

    @pytest.mark.asyncio
    async def test_cycle_count_increments(self, tier1, mock_deps, session):
        """사이클 카운터가 올바르게 증가."""
        assert tier1._cycle_count == 0
        await tier1.evaluation_cycle(session)
        assert tier1._cycle_count == 1
        await tier1.evaluation_cycle(session)
        assert tier1._cycle_count == 2

    @pytest.mark.asyncio
    async def test_cycle_count_not_incremented_without_regime(
        self, tier1, mock_deps, session
    ):
        """레짐 없으면 사이클 카운터 증가하지 않음."""
        mock_deps["regime"]._current = None
        await tier1.evaluation_cycle(session)
        assert tier1._cycle_count == 0

    @pytest.mark.asyncio
    async def test_last_cycle_at_set(self, tier1, mock_deps, session):
        """사이클 실행 후 last_cycle_at이 설정됨."""
        assert tier1._last_cycle_at is None
        await tier1.evaluation_cycle(session)
        assert tier1._last_cycle_at is not None
        assert isinstance(tier1._last_cycle_at, datetime)

    @pytest.mark.asyncio
    async def test_last_decisions_tracked(self, tier1, mock_deps, session):
        """각 코인별 마지막 결정이 추적됨."""
        await tier1.evaluation_cycle(session)
        assert "BTC/USDT" in tier1._last_decisions
        assert "ETH/USDT" in tier1._last_decisions

    @pytest.mark.asyncio
    async def test_stats_counts_hold(self, tier1, mock_deps, session):
        """HOLD 결정이 stats에 반영됨."""
        # Default evaluators return hold
        stats = await tier1.evaluation_cycle(session)
        assert stats.coins_evaluated == 2
        assert stats.hold_count == 2  # Both coins hold

    @pytest.mark.asyncio
    async def test_stats_counts_errors(self, tier1, mock_deps, session):
        """에러가 stats에 반영됨."""
        # Make the long_eval raise for BTC
        original_eval = mock_deps["long_eval"].evaluate

        async def raising_eval(symbol, pos, **kwargs):
            if symbol == "BTC/USDT":
                raise RuntimeError("test error")
            return await original_eval(symbol, pos, **kwargs)

        mock_deps["long_eval"].evaluate = raising_eval

        stats = await tier1.evaluation_cycle(session)
        assert stats.error_count >= 1

    @pytest.mark.asyncio
    async def test_stats_sl_tp(self, tier1, mock_deps, session):
        """SL 히트 시 sl_tp_count가 반영됨."""
        state = PositionState(
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
        )
        mock_deps["tracker"].open_position(state)
        sl_price_df = _make_df(close=78000.0, atr=1000.0)
        mock_deps["market_data"].get_ohlcv_df = AsyncMock(return_value=sl_price_df)

        stats = await tier1.evaluation_cycle(session)
        assert stats.sl_tp_count >= 1

    @pytest.mark.asyncio
    async def test_info_log_emitted(self, tier1, mock_deps, session):
        """tier1_cycle_complete info 로그가 발생."""
        with patch("engine.tier1_manager.logger") as mock_logger:
            await tier1.evaluation_cycle(session)
            mock_logger.info.assert_called_once()
            call_kwargs = mock_logger.info.call_args
            assert call_kwargs[0][0] == "tier1_cycle_complete"
            assert "coins_evaluated" in call_kwargs[1]
            assert "regime" in call_kwargs[1]
            assert "elapsed_ms" in call_kwargs[1]
            assert "cycle" in call_kwargs[1]
            assert call_kwargs[1]["cycle"] == 1

    @pytest.mark.asyncio
    async def test_no_info_log_without_regime(self, tier1, mock_deps, session):
        """레짐 없을 때 info 로그 미발생 (debug만)."""
        mock_deps["regime"]._current = None
        with patch("engine.tier1_manager.logger") as mock_logger:
            await tier1.evaluation_cycle(session)
            mock_logger.info.assert_not_called()
            mock_logger.debug.assert_called_once_with("tier1_skip_no_regime")


class TestGetStatus:
    """COIN-17: Tier1Manager.get_status() 테스트."""

    def test_initial_status(self, tier1):
        """초기 상태 반환."""
        status = tier1.get_status()
        assert status["cycle_count"] == 0
        assert status["last_cycle_at"] is None
        assert status["last_action_at"] is None
        assert status["coins"] == ["BTC/USDT", "ETH/USDT"]
        assert status["active_positions"] == 0
        assert status["last_decisions"] == {}
        assert status["regime"] is not None  # fixture has a regime set

    def test_status_regime_none(self, tier1, mock_deps):
        """레짐이 없을 때 None 반환."""
        mock_deps["regime"]._current = None
        status = tier1.get_status()
        assert status["regime"] is None

    @pytest.mark.asyncio
    async def test_status_after_cycle(self, tier1, mock_deps, session):
        """사이클 실행 후 상태 업데이트 확인."""
        await tier1.evaluation_cycle(session)
        status = tier1.get_status()
        assert status["cycle_count"] == 1
        assert status["last_cycle_at"] is not None
        assert len(status["last_decisions"]) == 2
        assert "BTC/USDT" in status["last_decisions"]
        assert "ETH/USDT" in status["last_decisions"]

    @pytest.mark.asyncio
    async def test_status_decisions_are_copied(self, tier1, mock_deps, session):
        """get_status()가 last_decisions의 복사본을 반환."""
        await tier1.evaluation_cycle(session)
        status = tier1.get_status()
        status["last_decisions"]["NEW_COIN"] = "test"
        assert "NEW_COIN" not in tier1._last_decisions


class TestDirectionToSignalType:
    """Direction → signal_type 변환 테스트."""

    def test_long_to_buy(self):
        assert Tier1Manager._direction_to_signal_type(Direction.LONG) == "BUY"

    def test_short_to_sell(self):
        assert Tier1Manager._direction_to_signal_type(Direction.SHORT) == "SELL"

    def test_flat_to_hold(self):
        assert Tier1Manager._direction_to_signal_type(Direction.FLAT) == "HOLD"

    def test_none_to_hold(self):
        assert Tier1Manager._direction_to_signal_type(None) == "HOLD"


class TestStrategySignalLogging:
    """COIN-21: V2 전략 시그널 로그 기록 테스트."""

    @pytest.mark.asyncio
    async def test_strategy_log_created_on_eval(self, tier1, mock_deps, session):
        """evaluation_cycle 실행 시 StrategyLog가 DB에 기록됨."""
        await tier1.evaluation_cycle(session)
        await session.flush()

        result = await session.execute(select(StrategyLog))
        logs = result.scalars().all()
        # 2개 코인(BTC, ETH)에 대해 각각 로그 기록
        # Both evaluators return hold → 양쪽 다 로깅 (coin당 2개)
        assert len(logs) >= 2

    @pytest.mark.asyncio
    async def test_strategy_log_has_correct_fields(self, tier1, mock_deps, session):
        """StrategyLog에 필수 필드가 올바르게 설정됨."""
        mock_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision())
        await tier1.evaluation_cycle(session)
        await session.flush()

        result = await session.execute(select(StrategyLog))
        logs = result.scalars().all()
        assert len(logs) >= 1
        # 최소 하나의 BUY 로그가 있어야 함
        buy_logs = [log for log in logs if log.signal_type == "BUY"]
        assert len(buy_logs) >= 1
        buy_log = buy_logs[0]
        assert buy_log.exchange == "binance_futures"
        assert buy_log.symbol == "BTC/USDT"
        assert buy_log.confidence is not None
        assert buy_log.reason is not None

    @pytest.mark.asyncio
    async def test_strategy_log_includes_regime_info(self, tier1, mock_deps, session):
        """StrategyLog 지표에 레짐 정보가 포함됨."""
        mock_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision())
        await tier1.evaluation_cycle(session)
        await session.flush()

        result = await session.execute(select(StrategyLog))
        logs = result.scalars().all()
        assert len(logs) >= 1
        # 적어도 하나의 로그에 레짐 정보가 있어야 함
        regime_logs = [lg for lg in logs if lg.indicators and "regime" in lg.indicators]
        assert len(regime_logs) >= 1
        assert regime_logs[0].indicators["regime"] == "trending_up"
        assert "regime_confidence" in regime_logs[0].indicators

    @pytest.mark.asyncio
    async def test_hold_signal_logged(self, tier1, mock_deps, session):
        """HOLD 판단도 StrategyLog에 기록됨."""
        # Default evaluators return hold
        await tier1.evaluation_cycle(session)
        await session.flush()

        result = await session.execute(select(StrategyLog))
        logs = result.scalars().all()
        assert len(logs) >= 2  # 각 코인마다 로그 기록
        hold_logs = [log for log in logs if log.signal_type == "HOLD"]
        assert len(hold_logs) >= 1

    @pytest.mark.asyncio
    async def test_executed_signal_marked(self, tier1, mock_deps, session):
        """실행된 시그널은 was_executed=True로 기록됨."""
        mock_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision())
        await tier1.evaluation_cycle(session)
        await session.flush()

        result = await session.execute(select(StrategyLog))
        logs = result.scalars().all()
        assert len(logs) >= 1
        # 모든 로그에 was_executed 필드가 설정되어야 함
        for log in logs:
            assert log.was_executed is not None

        # BTC/USDT BUY 로그는 was_executed=True
        buy_executed = [
            lg
            for lg in logs
            if lg.symbol == "BTC/USDT" and lg.signal_type == "BUY" and lg.was_executed
        ]
        assert len(buy_executed) >= 1

    @pytest.mark.asyncio
    async def test_sl_tp_signal_not_executed(self, tier1, mock_deps, session):
        """SL 히트로 청산 시 전략 시그널은 was_executed=False."""
        state = PositionState(
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
        )
        mock_deps["tracker"].open_position(state)
        sl_price_df = _make_df(close=78000.0, atr=1000.0)
        mock_deps["market_data"].get_ohlcv_df = AsyncMock(return_value=sl_price_df)

        await tier1.evaluation_cycle(session)
        await session.flush()

        result = await session.execute(
            select(StrategyLog).where(StrategyLog.symbol == "BTC/USDT")
        )
        logs = result.scalars().all()
        # SL 히트로 인한 청산은 전략 로그가 없음 (SL은 이밸류에이터가 아닌 SL/TP 체크로 발동)
        # SL 히트 시 이밸류에이터 미호출 → 로그 0건
        assert len(logs) == 0, (
            f"Expected no strategy logs for SL/TP hit, got {len(logs)}"
        )

    @pytest.mark.asyncio
    async def test_exchange_name_default(self, mock_deps, session):
        """기본 exchange_name은 binance_futures."""
        tier1 = Tier1Manager(
            coins=["BTC/USDT"],
            safe_order=mock_deps["safe_order"],
            position_tracker=mock_deps["tracker"],
            regime_detector=mock_deps["regime"],
            portfolio_manager=mock_deps["pm"],
            market_data=mock_deps["market_data"],
            long_evaluator=mock_deps["long_eval"],
            short_evaluator=mock_deps["short_eval"],
        )
        assert tier1._exchange_name == "binance_futures"

    @pytest.mark.asyncio
    async def test_executed_close_logged_as_executed(self, tier1, mock_deps, session):
        """실행된 청산 시그널은 was_executed=True로 기록됨."""
        state = PositionState(
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
        )
        mock_deps["tracker"].open_position(state)
        mock_deps["long_eval"].set_decision("BTC/USDT", _close_decision())

        await tier1.evaluation_cycle(session)
        await session.flush()

        result = await session.execute(
            select(StrategyLog).where(StrategyLog.symbol == "BTC/USDT")
        )
        logs = result.scalars().all()
        assert len(logs) >= 1
        # 청산 시그널은 was_executed=True로 기록되어야 함
        sell_executed = [
            lg for lg in logs if lg.signal_type == "SELL" and lg.was_executed
        ]
        assert len(sell_executed) >= 1, (
            "Close signal should be logged with was_executed=True"
        )

    @pytest.mark.asyncio
    async def test_position_held_hold_logged(self, tier1, mock_deps, session):
        """포지션 보유 중 hold 결정도 StrategyLog에 기록됨."""
        state = PositionState(
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
        )
        mock_deps["tracker"].open_position(state)
        # long_eval returns hold (default) → keep holding

        await tier1.evaluation_cycle(session)
        await session.flush()

        result = await session.execute(
            select(StrategyLog).where(StrategyLog.symbol == "BTC/USDT")
        )
        logs = result.scalars().all()
        # 포지션 유지 중 hold 판단도 로깅되어야 함
        hold_logs = [lg for lg in logs if lg.signal_type == "HOLD"]
        assert len(hold_logs) >= 1, (
            "Hold decision for held position should also be logged"
        )

    @pytest.mark.asyncio
    async def test_custom_exchange_name(self, mock_deps, session):
        """exchange_name을 커스텀으로 설정할 수 있음."""
        tier1 = Tier1Manager(
            coins=["BTC/USDT"],
            safe_order=mock_deps["safe_order"],
            position_tracker=mock_deps["tracker"],
            regime_detector=mock_deps["regime"],
            portfolio_manager=mock_deps["pm"],
            market_data=mock_deps["market_data"],
            long_evaluator=mock_deps["long_eval"],
            short_evaluator=mock_deps["short_eval"],
            exchange_name="custom_exchange",
        )
        mock_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision())
        await tier1.evaluation_cycle(session)
        await session.flush()

        result = await session.execute(select(StrategyLog))
        log = result.scalars().first()
        assert log is not None
        assert log.exchange == "custom_exchange"


class TestCachedIndicators:
    """캐시된 indicators를 통한 _open_position_from_decision 경로 테스트."""

    @pytest.mark.asyncio
    async def test_open_uses_cached_close_atr(self, tier1, mock_deps, session):
        """indicators에 close/atr가 있으면 캔들 재조회 없이 사용."""
        decision_with_cache = DirectionDecision(
            action="open",
            direction=Direction.LONG,
            confidence=0.8,
            sizing_factor=0.7,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="long_signal",
            strategy_name="trend_follower",
            indicators={"close": 80000.0, "atr": 1000.0},
        )
        mock_deps["long_eval"].set_decision("BTC/USDT", decision_with_cache)

        # 캔들 조회 호출 횟수 기록 (evaluation_cycle에서 사전 조회 2회)
        initial_call_count = mock_deps["market_data"].get_ohlcv_df.call_count
        await tier1.evaluation_cycle(session)

        # 사전 조회 (5m + 1h) * 2코인 = 4회. indicators 캐시 덕분에
        # _open_position_from_decision에서 추가 조회 없음
        total_calls = (
            mock_deps["market_data"].get_ohlcv_df.call_count - initial_call_count
        )
        assert total_calls == 4, (
            f"Expected 4 candle fetches (2 per coin), got {total_calls}. "
            "indicators cache should prevent extra fetch in _open_position_from_decision"
        )

        # 주문이 실행되었는지 확인
        open_calls = [
            c
            for c in mock_deps["safe_order"].execute_order.call_args_list
            if c[0][1].action == "open" and c[0][1].symbol == "BTC/USDT"
        ]
        assert len(open_calls) >= 1

    @pytest.mark.asyncio
    async def test_open_fallback_without_cached_indicators(
        self, tier1, mock_deps, session
    ):
        """indicators에 close/atr가 없으면 캔들 재조회 (fallback)."""
        decision_no_cache = DirectionDecision(
            action="open",
            direction=Direction.LONG,
            confidence=0.8,
            sizing_factor=0.7,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="long_signal",
            strategy_name="trend_follower",
            indicators={},  # close/atr 없음
        )
        mock_deps["long_eval"].set_decision("BTC/USDT", decision_no_cache)

        initial_call_count = mock_deps["market_data"].get_ohlcv_df.call_count
        await tier1.evaluation_cycle(session)

        # 사전 조회 4회 + BTC fallback 1회 = 5회
        total_calls = (
            mock_deps["market_data"].get_ohlcv_df.call_count - initial_call_count
        )
        assert total_calls == 5, (
            f"Expected 5 candle fetches (4 pre-fetch + 1 fallback), got {total_calls}"
        )


class TestPreFetchedCandles:
    """사전 조회된 캔들이 이밸류에이터에 전달되는지 테스트."""

    @pytest.mark.asyncio
    async def test_evaluators_receive_prefetched_candles(
        self, tier1, mock_deps, session
    ):
        """이밸류에이터가 df_5m, df_1h kwargs를 받는지 확인."""
        await tier1.evaluation_cycle(session)

        # 이밸류에이터의 마지막 호출에 df_5m, df_1h가 전달되어야 함
        assert hasattr(mock_deps["long_eval"], "last_kwargs")
        assert "df_5m" in mock_deps["long_eval"].last_kwargs
        assert "df_1h" in mock_deps["long_eval"].last_kwargs
        assert mock_deps["long_eval"].last_kwargs["df_5m"] is not None

    @pytest.mark.asyncio
    async def test_single_candle_fetch_per_coin(self, tier1, mock_deps, session):
        """코인당 캔들 조회가 1회씩만 발생 (5m + 1h)."""
        mock_deps["market_data"].get_ohlcv_df.reset_mock()
        await tier1.evaluation_cycle(session)

        # 2코인 × (5m + 1h) = 4회
        assert mock_deps["market_data"].get_ohlcv_df.call_count == 4


class TestResolveEntry:
    """_resolve_entry 충돌 해소 로직 단위 테스트."""

    def test_both_hold(self, tier1):
        winner, loser = tier1._resolve_entry(_hold_decision(), _hold_decision())
        assert winner is None
        assert loser is None

    def test_long_only(self, tier1):
        winner, loser = tier1._resolve_entry(_long_open_decision(), _hold_decision())
        assert winner is not None
        assert winner.direction == Direction.LONG
        assert loser is None  # 충돌 아님

    def test_short_only(self, tier1):
        winner, loser = tier1._resolve_entry(_hold_decision(), _short_open_decision())
        assert winner is not None
        assert winner.direction == Direction.SHORT
        assert loser is None  # 충돌 아님

    def test_both_open_long_wins(self, tier1):
        long_d = _long_open_decision(confidence=0.9)
        short_d = _short_open_decision(confidence=0.7)
        winner, loser = tier1._resolve_entry(long_d, short_d)
        assert winner.direction == Direction.LONG
        assert loser is not None
        assert loser.direction == Direction.SHORT

    def test_both_open_short_wins(self, tier1):
        long_d = _long_open_decision(confidence=0.5)
        short_d = _short_open_decision(confidence=0.8)
        winner, loser = tier1._resolve_entry(long_d, short_d)
        assert winner.direction == Direction.SHORT
        assert loser is not None
        assert loser.direction == Direction.LONG

    def test_equal_confidence_long_wins(self, tier1):
        long_d = _long_open_decision(confidence=0.7)
        short_d = _short_open_decision(confidence=0.7)
        winner, loser = tier1._resolve_entry(long_d, short_d)
        assert winner.direction == Direction.LONG
        assert loser is not None
        assert loser.direction == Direction.SHORT


class TestConflictObservability:
    """충돌 해소 시 탈락한 결정의 관측성 테스트."""

    @pytest.mark.asyncio
    async def test_loser_decision_logged_to_db(self, tier1, mock_deps, session):
        """충돌 시 탈락한 결정도 StrategyLog에 기록됨."""
        mock_deps["long_eval"].set_decision(
            "BTC/USDT", _long_open_decision(confidence=0.9)
        )
        mock_deps["short_eval"].set_decision(
            "BTC/USDT", _short_open_decision(confidence=0.7)
        )

        await tier1.evaluation_cycle(session)
        await session.flush()

        result = await session.execute(
            select(StrategyLog).where(StrategyLog.symbol == "BTC/USDT")
        )
        logs = result.scalars().all()

        # 실행된 BUY (long winner) + 탈락한 SELL (short loser) = 최소 2건
        assert len(logs) >= 2, (
            f"Expected at least 2 logs (winner + loser), got {len(logs)}"
        )
        buy_logs = [lg for lg in logs if lg.signal_type == "BUY"]
        sell_logs = [lg for lg in logs if lg.signal_type == "SELL"]
        assert len(buy_logs) >= 1, "Winner (LONG/BUY) should be logged"
        assert len(sell_logs) >= 1, "Loser (SHORT/SELL) should be logged"

        # 탈락한 결정은 was_executed=False
        loser_log = sell_logs[0]
        assert loser_log.was_executed is False

    @pytest.mark.asyncio
    async def test_conflict_info_log_emitted(self, tier1, mock_deps, session):
        """충돌 해소 시 tier1_conflict_resolved 로그가 발생."""
        mock_deps["long_eval"].set_decision(
            "BTC/USDT", _long_open_decision(confidence=0.9)
        )
        mock_deps["short_eval"].set_decision(
            "BTC/USDT", _short_open_decision(confidence=0.7)
        )

        with patch("engine.tier1_manager.logger") as mock_logger:
            await tier1.evaluation_cycle(session)

            conflict_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c[0][0] == "tier1_conflict_resolved"
            ]
            assert len(conflict_calls) >= 1, (
                "tier1_conflict_resolved log should be emitted on conflict"
            )
            kwargs = conflict_calls[0][1]
            assert kwargs["symbol"] == "BTC/USDT"
            assert kwargs["winner_direction"] == Direction.LONG.value
            assert kwargs["winner_confidence"] == 0.9
            assert kwargs["loser_direction"] == Direction.SHORT.value
            assert kwargs["loser_confidence"] == 0.7

    @pytest.mark.asyncio
    async def test_no_conflict_log_when_single_open(self, tier1, mock_deps, session):
        """충돌이 아닌 경우 tier1_conflict_resolved 로그 미발생."""
        mock_deps["long_eval"].set_decision(
            "BTC/USDT", _long_open_decision(confidence=0.8)
        )
        # short_eval returns hold (default)

        with patch("engine.tier1_manager.logger") as mock_logger:
            await tier1.evaluation_cycle(session)

            conflict_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c[0][0] == "tier1_conflict_resolved"
            ]
            assert len(conflict_calls) == 0, (
                "No conflict log when only one evaluator returns open"
            )


class TestSLTPCooldown:
    """SL/TP 청산 후 쿨다운 설정 테스트."""

    @pytest.mark.asyncio
    async def test_sl_sets_cooldown(self, tier1, mock_deps, session):
        """SL 히트로 청산 후 쿨다운이 설정됨."""
        state = PositionState(
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
        )
        mock_deps["tracker"].open_position(state)

        # SL 가격으로 설정
        sl_price_df = _make_df(close=78000.0, atr=1000.0)
        mock_deps["market_data"].get_ohlcv_df = AsyncMock(return_value=sl_price_df)

        assert "BTC/USDT" not in tier1._last_exit_time
        await tier1.evaluation_cycle(session)

        # SL 히트 후 쿨다운 타이머가 설정되어야 함
        assert "BTC/USDT" in tier1._last_exit_time
        assert tier1._last_exit_time["BTC/USDT"] > 0

    @pytest.mark.asyncio
    async def test_sl_blocks_immediate_reentry(self, tier1, mock_deps, session):
        """SL 청산 후 쿨다운 동안 재진입이 차단됨."""
        state = PositionState(
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
        )
        mock_deps["tracker"].open_position(state)

        # 1차: SL 가격으로 청산
        sl_price_df = _make_df(close=78000.0, atr=1000.0)
        mock_deps["market_data"].get_ohlcv_df = AsyncMock(return_value=sl_price_df)
        await tier1.evaluation_cycle(session)

        # 2차: 정상 가격 복원, 롱 시그널 발생 → 쿨다운으로 차단
        mock_deps["market_data"].get_ohlcv_df = AsyncMock(return_value=_make_df())
        mock_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision())

        stats = await tier1.evaluation_cycle(session)

        btc_open = [
            c
            for c in mock_deps["safe_order"].execute_order.call_args_list
            if c[0][1].action == "open" and c[0][1].symbol == "BTC/USDT"
        ]
        assert len(btc_open) == 0, (
            "Should not re-enter BTC/USDT during cooldown after SL hit"
        )
        assert stats.cooldown_count >= 1

    @pytest.mark.asyncio
    async def test_tp_sets_cooldown(self, tier1, mock_deps, session):
        """TP 히트로 청산 후 쿨다운이 설정됨."""
        state = PositionState(
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
        )
        mock_deps["tracker"].open_position(state)

        # TP 가격 설정 (entry + atr * tp_atr = 80000 + 1000*3 = 83000 이상)
        tp_price_df = _make_df(close=84000.0, atr=1000.0)
        mock_deps["market_data"].get_ohlcv_df = AsyncMock(return_value=tp_price_df)

        assert "BTC/USDT" not in tier1._last_exit_time
        await tier1.evaluation_cycle(session)

        # TP 히트 후 쿨다운 타이머가 설정되어야 함
        assert "BTC/USDT" in tier1._last_exit_time


class TestSameInstanceDedup:
    """같은 이밸류에이터 인스턴스 사용 시 중복 호출 방지 (COIN-28)."""

    @pytest.fixture
    def shared_eval(self):
        """롱/숏 모두 담당하는 단일 이밸류에이터."""
        eval_ = MockLongEvaluator()
        return eval_

    @pytest.fixture
    def tier1_shared(self, mock_deps, shared_eval):
        """같은 인스턴스를 long/short 양쪽에 할당한 Tier1Manager."""
        return Tier1Manager(
            coins=["BTC/USDT"],
            safe_order=mock_deps["safe_order"],
            position_tracker=mock_deps["tracker"],
            regime_detector=mock_deps["regime"],
            portfolio_manager=mock_deps["pm"],
            market_data=mock_deps["market_data"],
            long_evaluator=shared_eval,
            short_evaluator=shared_eval,
            leverage=3,
            max_position_pct=0.15,
        )

    @pytest.mark.asyncio
    async def test_no_position_calls_evaluate_once(
        self,
        tier1_shared,
        shared_eval,
        mock_deps,
        session,
    ):
        """포지션 없을 때 같은 인스턴스면 evaluate()를 1번만 호출."""
        shared_eval.set_decision("BTC/USDT", _hold_decision("spot_eval"))

        await tier1_shared.evaluation_cycle(session)

        # 같은 인스턴스이므로 1번만 호출되어야 함 (2번이 아님)
        assert shared_eval.call_count == 1

    @pytest.mark.asyncio
    async def test_no_position_different_instances_calls_twice(
        self,
        tier1,
        mock_deps,
        session,
    ):
        """다른 인스턴스면 evaluate()를 2번 호출 (기존 동작 유지)."""
        mock_deps["long_eval"].set_decision("BTC/USDT", _hold_decision("long_eval"))
        mock_deps["short_eval"].set_decision("BTC/USDT", _hold_decision("short_eval"))

        await tier1.evaluation_cycle(session)

        # BTC/USDT + ETH/USDT = 2코인, 각각 long+short = 4번
        assert mock_deps["long_eval"].call_count == 2
        assert mock_deps["short_eval"].call_count == 2

    @pytest.mark.asyncio
    async def test_shared_instance_open_long(
        self,
        tier1_shared,
        shared_eval,
        mock_deps,
        session,
    ):
        """같은 인스턴스: BUY 시그널 → LONG 진입 정상 동작."""
        shared_eval.set_decision("BTC/USDT", _long_open_decision(confidence=0.8))

        stats = await tier1_shared.evaluation_cycle(session)

        assert shared_eval.call_count == 1
        assert stats.executed_count == 1

    @pytest.mark.asyncio
    async def test_shared_instance_open_short(
        self,
        tier1_shared,
        shared_eval,
        mock_deps,
        session,
    ):
        """같은 인스턴스: SHORT open 시그널 → SHORT 진입 정상 동작."""
        shared_eval.set_decision("BTC/USDT", _short_open_decision(confidence=0.8))

        stats = await tier1_shared.evaluation_cycle(session)

        assert shared_eval.call_count == 1
        assert stats.executed_count == 1

    @pytest.mark.asyncio
    async def test_shared_instance_long_hold_skips_sar(
        self,
        tier1_shared,
        shared_eval,
        mock_deps,
        session,
    ):
        """같은 인스턴스 + LONG 보유 + hold → SAR evaluate() 호출 안 함 (COIN-29)."""
        # LONG 포지션 주입
        state = PositionState(
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
        )
        mock_deps["tracker"].open_position(state)

        # evaluator가 hold 반환 → SAR 분기로 진입하지만, 같은 인스턴스이므로 skip
        shared_eval.set_decision("BTC/USDT", _hold_decision("spot_eval"))

        stats = await tier1_shared.evaluation_cycle(session)

        # 같은 인스턴스: LONG 평가 1회만 (SAR용 추가 호출 없음)
        assert shared_eval.call_count == 1
        assert stats.hold_count == 1

    @pytest.mark.asyncio
    async def test_shared_instance_short_hold_skips_sar(
        self,
        tier1_shared,
        shared_eval,
        mock_deps,
        session,
    ):
        """같은 인스턴스 + SHORT 보유 + hold → SAR evaluate() 호출 안 함 (COIN-29)."""
        # SHORT 포지션 주입
        state = PositionState(
            symbol="BTC/USDT",
            direction=Direction.SHORT,
            quantity=0.01,
            entry_price=80000.0,
            margin=100.0,
            leverage=3,
            extreme_price=80000.0,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
        )
        mock_deps["tracker"].open_position(state)

        shared_eval.set_decision("BTC/USDT", _hold_decision("spot_eval"))

        stats = await tier1_shared.evaluation_cycle(session)

        # 같은 인스턴스: SHORT 평가 1회만 (SAR용 추가 호출 없음)
        assert shared_eval.call_count == 1
        assert stats.hold_count == 1

    @pytest.mark.asyncio
    async def test_shared_instance_long_close_still_works(
        self,
        tier1_shared,
        shared_eval,
        mock_deps,
        session,
    ):
        """같은 인스턴스 + LONG 보유 + close 시그널 → 정상 청산."""
        state = PositionState(
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
        )
        mock_deps["tracker"].open_position(state)

        shared_eval.set_decision("BTC/USDT", _close_decision("spot_eval"))

        stats = await tier1_shared.evaluation_cycle(session)

        assert shared_eval.call_count == 1
        assert stats.executed_count == 1

    @pytest.mark.asyncio
    async def test_shared_instance_short_close_still_works(
        self,
        tier1_shared,
        shared_eval,
        mock_deps,
        session,
    ):
        """같은 인스턴스 + SHORT 보유 + close 시그널 → 정상 청산."""
        state = PositionState(
            symbol="BTC/USDT",
            direction=Direction.SHORT,
            quantity=0.01,
            entry_price=80000.0,
            margin=100.0,
            leverage=3,
            extreme_price=80000.0,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
        )
        mock_deps["tracker"].open_position(state)

        shared_eval.set_decision("BTC/USDT", _close_decision("spot_eval"))

        stats = await tier1_shared.evaluation_cycle(session)

        assert shared_eval.call_count == 1
        assert stats.executed_count == 1


class TestDifferentInstanceSARPreserved:
    """다른 인스턴스일 때 SAR 로직이 여전히 동작하는지 확인 (COIN-29 회귀 방지)."""

    @pytest.mark.asyncio
    async def test_different_instance_long_hold_calls_sar(
        self,
        tier1,
        mock_deps,
        session,
    ):
        """다른 인스턴스 + LONG 보유 + long=hold + short=open → SAR 호출."""
        state = PositionState(
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
        )
        mock_deps["tracker"].open_position(state)

        mock_deps["long_eval"].set_decision("BTC/USDT", _hold_decision("long_eval"))
        mock_deps["short_eval"].set_decision(
            "BTC/USDT",
            _short_open_decision(confidence=0.8),
        )

        stats = await tier1.evaluation_cycle(session)

        # long 1회 + short SAR 1회 = 2회 호출
        assert mock_deps["long_eval"].call_count >= 1
        assert mock_deps["short_eval"].call_count >= 1
        # SAR 실행됨
        assert stats.executed_count >= 1

    @pytest.mark.asyncio
    async def test_different_instance_short_hold_calls_sar(
        self,
        tier1,
        mock_deps,
        session,
    ):
        """다른 인스턴스 + SHORT 보유 + short=hold + long=open → SAR 호출."""
        state = PositionState(
            symbol="BTC/USDT",
            direction=Direction.SHORT,
            quantity=0.01,
            entry_price=80000.0,
            margin=100.0,
            leverage=3,
            extreme_price=80000.0,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
        )
        mock_deps["tracker"].open_position(state)

        mock_deps["short_eval"].set_decision("BTC/USDT", _hold_decision("short_eval"))
        mock_deps["long_eval"].set_decision(
            "BTC/USDT",
            _long_open_decision(confidence=0.8),
        )

        stats = await tier1.evaluation_cycle(session)

        # short 1회 + long SAR 1회 = 2회 호출
        assert mock_deps["short_eval"].call_count >= 1
        assert mock_deps["long_eval"].call_count >= 1
        # SAR 실행됨
        assert stats.executed_count >= 1


class TestCloseCallback:
    """COIN-38: on_close_callback 파라미터 테스트."""

    def test_default_callback_is_none(self, tier1):
        """기본값은 None."""
        assert tier1._on_close_callback is None

    def test_callback_set_via_constructor(self, mock_deps):
        """생성자에서 on_close_callback 설정."""
        callback = AsyncMock()
        t1 = Tier1Manager(
            coins=["BTC/USDT"],
            safe_order=mock_deps["safe_order"],
            position_tracker=mock_deps["tracker"],
            regime_detector=mock_deps["regime"],
            portfolio_manager=mock_deps["pm"],
            market_data=mock_deps["market_data"],
            long_evaluator=mock_deps["long_eval"],
            short_evaluator=mock_deps["short_eval"],
            on_close_callback=callback,
        )
        assert t1._on_close_callback is callback

    @pytest.mark.asyncio
    async def test_callback_invoked_on_close(self, mock_deps, session):
        """포지션 청산 시 콜백이 호출됨."""
        from engine.position_state_tracker import PositionState

        callback = AsyncMock()
        t1 = Tier1Manager(
            coins=["BTC/USDT"],
            safe_order=mock_deps["safe_order"],
            position_tracker=mock_deps["tracker"],
            regime_detector=mock_deps["regime"],
            portfolio_manager=mock_deps["pm"],
            market_data=mock_deps["market_data"],
            long_evaluator=mock_deps["long_eval"],
            short_evaluator=mock_deps["short_eval"],
            on_close_callback=callback,
        )

        # 포지션 설정
        mock_deps["tracker"].open_position(
            PositionState(
                symbol="BTC/USDT",
                direction=Direction.LONG,
                entry_price=80000.0,
                quantity=0.01,
                margin=26.67,
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
        )

        # safe_order.execute_order가 success 반환하도록 설정
        mock_deps["safe_order"].execute_order = AsyncMock(
            return_value=OrderResponse(
                success=True,
                order_id=1,
                executed_price=80000.0,
                executed_quantity=0.01,
                fee=0.32,
            )
        )

        # _close_position 호출
        result = await t1._close_position(
            session, "BTC/USDT", Direction.LONG, "test_close"
        )
        assert result is True
        callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_callback_when_none(self, mock_deps, session):
        """콜백 미설정 시 에러 없이 청산 진행."""
        from engine.position_state_tracker import PositionState

        t1 = Tier1Manager(
            coins=["BTC/USDT"],
            safe_order=mock_deps["safe_order"],
            position_tracker=mock_deps["tracker"],
            regime_detector=mock_deps["regime"],
            portfolio_manager=mock_deps["pm"],
            market_data=mock_deps["market_data"],
            long_evaluator=mock_deps["long_eval"],
            short_evaluator=mock_deps["short_eval"],
            on_close_callback=None,
        )

        mock_deps["tracker"].open_position(
            PositionState(
                symbol="ETH/USDT",
                direction=Direction.SHORT,
                entry_price=3000.0,
                quantity=0.1,
                margin=100.0,
                leverage=3,
                extreme_price=3000.0,
                stop_loss_atr=1.5,
                take_profit_atr=3.0,
                trailing_activation_atr=2.0,
                trailing_stop_atr=1.0,
                strategy_name="test",
                confidence=0.7,
                tier="tier1",
            )
        )

        result = await t1._close_position(
            session, "ETH/USDT", Direction.SHORT, "test_close"
        )
        assert result is True  # no error even without callback


# ── ML Signal Filter Tests (COIN-40) ──


class MockMLFilter:
    """테스트용 ML 필터."""

    def __init__(self, should_trade=True, win_probability=0.6):
        self._should_trade = should_trade
        self._win_probability = win_probability
        self.predict_count = 0
        self.last_features = None

    @staticmethod
    def extract_features(signals, row, price, market_state, combined_confidence):
        """MLSignalFilter.extract_features 위임."""
        from strategies.ml_filter import MLSignalFilter

        return MLSignalFilter.extract_features(
            signals=signals,
            row=row,
            price=price,
            market_state=market_state,
            combined_confidence=combined_confidence,
        )

    def predict(self, features):
        from strategies.ml_filter import MLPrediction

        self.predict_count += 1
        self.last_features = features
        return MLPrediction(
            should_trade=self._should_trade,
            win_probability=self._win_probability,
        )


def _make_candle_row():
    """ML 필터 feature 추출용 캔들 row 생성."""
    return pd.Series(
        {
            "close": 80000.0,
            "RSI_14": 45.0,
            "ATRr_14": 1200.0,
            "SMA_20": 79000.0,
            "SMA_50": 78000.0,
            "volume": 1000.0,
            "Volume_SMA_20": 800.0,
            "BBU_20_2.0": 82000.0,
            "BBL_20_2.0": 78000.0,
            "BBM_20_2.0": 80000.0,
        }
    )


def _long_open_with_ml_indicators(confidence=0.8, sizing_factor=0.7):
    """ML 필터 데이터가 포함된 LONG 진입 결정."""
    return DirectionDecision(
        action="open",
        direction=Direction.LONG,
        confidence=confidence,
        sizing_factor=sizing_factor,
        stop_loss_atr=1.5,
        take_profit_atr=3.0,
        reason="long_signal",
        strategy_name="trend_follower",
        indicators={
            "close": 80000.0,
            "atr": 1000.0,
            "_signals": [],
            "_candle_row": _make_candle_row(),
            "_combined_confidence": confidence,
        },
    )


def _short_open_with_ml_indicators(confidence=0.7, sizing_factor=0.6):
    """ML 필터 데이터가 포함된 SHORT 진입 결정."""
    return DirectionDecision(
        action="open",
        direction=Direction.SHORT,
        confidence=confidence,
        sizing_factor=sizing_factor,
        stop_loss_atr=1.5,
        take_profit_atr=3.0,
        reason="short_signal",
        strategy_name="mean_reversion",
        indicators={
            "close": 80000.0,
            "atr": 1000.0,
            "_signals": [],
            "_candle_row": _make_candle_row(),
            "_combined_confidence": confidence,
        },
    )


@pytest.fixture
def ml_filter_deps(mock_deps):
    """ML 필터가 포함된 의존성."""
    ml_filter = MockMLFilter(should_trade=True, win_probability=0.6)
    return {**mock_deps, "ml_filter": ml_filter}


@pytest.fixture
def tier1_with_ml(ml_filter_deps):
    """ML 필터가 활성화된 Tier1Manager."""
    return Tier1Manager(
        coins=["BTC/USDT", "ETH/USDT"],
        safe_order=ml_filter_deps["safe_order"],
        position_tracker=ml_filter_deps["tracker"],
        regime_detector=ml_filter_deps["regime"],
        portfolio_manager=ml_filter_deps["pm"],
        market_data=ml_filter_deps["market_data"],
        long_evaluator=ml_filter_deps["long_eval"],
        short_evaluator=ml_filter_deps["short_eval"],
        leverage=3,
        max_position_pct=0.15,
        ml_filter=ml_filter_deps["ml_filter"],
    )


class TestMLFilterInit:
    """ML 필터 초기화 테스트."""

    def test_ml_filter_none_by_default(self, tier1):
        """기본값은 ML 필터 없음."""
        assert tier1._ml_filter is None

    def test_ml_filter_stored(self, tier1_with_ml, ml_filter_deps):
        """ML 필터가 올바르게 저장됨."""
        assert tier1_with_ml._ml_filter is ml_filter_deps["ml_filter"]

    def test_status_ml_filter_inactive(self, tier1):
        """ML 필터 없으면 status에 inactive."""
        status = tier1.get_status()
        assert status["ml_filter_active"] is False

    def test_status_ml_filter_active(self, tier1_with_ml):
        """ML 필터 있으면 status에 active."""
        status = tier1_with_ml.get_status()
        assert status["ml_filter_active"] is True


class TestMLFilterGate:
    """ML 필터 진입 차단 테스트."""

    def test_check_ml_filter_no_filter_returns_true(self, tier1):
        """ML 필터 없으면 항상 통과."""
        decision = _long_open_with_ml_indicators()
        regime = _regime_state()
        result = tier1._check_ml_filter("BTC/USDT", decision, regime)
        assert result is True

    def test_check_ml_filter_pass(self, tier1_with_ml, ml_filter_deps):
        """ML 필터 통과 시 True 반환."""
        ml_filter_deps["ml_filter"]._should_trade = True
        ml_filter_deps["ml_filter"]._win_probability = 0.65

        decision = _long_open_with_ml_indicators()
        regime = _regime_state()
        result = tier1_with_ml._check_ml_filter("BTC/USDT", decision, regime)
        assert result is True
        assert ml_filter_deps["ml_filter"].predict_count == 1

    def test_check_ml_filter_block(self, tier1_with_ml, ml_filter_deps):
        """ML 필터 차단 시 False 반환."""
        ml_filter_deps["ml_filter"]._should_trade = False
        ml_filter_deps["ml_filter"]._win_probability = 0.40

        decision = _long_open_with_ml_indicators()
        regime = _regime_state()
        result = tier1_with_ml._check_ml_filter("BTC/USDT", decision, regime)
        assert result is False

    def test_check_ml_filter_no_candle_row_passes(self, tier1_with_ml):
        """캔들 데이터 없으면 필터 통과 (graceful degradation)."""
        decision = DirectionDecision(
            action="open",
            direction=Direction.LONG,
            confidence=0.8,
            sizing_factor=0.7,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="test",
            strategy_name="test",
            indicators={"close": 80000.0, "atr": 1000.0},
        )
        regime = _regime_state()
        result = tier1_with_ml._check_ml_filter("BTC/USDT", decision, regime)
        assert result is True  # 캔들 없으면 통과

    def test_check_ml_filter_error_passes(self, tier1_with_ml, ml_filter_deps):
        """ML 필터 에러 시 필터 통과 (graceful degradation)."""
        ml_filter_deps["ml_filter"].predict = MagicMock(
            side_effect=RuntimeError("model error")
        )
        decision = _long_open_with_ml_indicators()
        regime = _regime_state()
        result = tier1_with_ml._check_ml_filter("BTC/USDT", decision, regime)
        assert result is True  # 에러 시 통과


class TestMLFilterIntegration:
    """ML 필터 통합 테스트 — evaluation_cycle에서 차단 동작 확인."""

    @pytest.mark.asyncio
    async def test_ml_filter_blocks_entry(self, tier1_with_ml, ml_filter_deps, session):
        """ML 필터가 신규 진입을 차단하면 'ml_filtered' 반환."""
        ml_filter_deps["ml_filter"]._should_trade = False
        ml_filter_deps["ml_filter"]._win_probability = 0.40

        ml_filter_deps["long_eval"].set_decision(
            "BTC/USDT", _long_open_with_ml_indicators()
        )

        regime = _regime_state()
        result = await tier1_with_ml._evaluate_coin(session, "BTC/USDT", regime)
        assert result == "ml_filtered"
        # 주문이 실행되지 않아야 함
        ml_filter_deps["safe_order"].execute_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_ml_filter_allows_entry(self, tier1_with_ml, ml_filter_deps, session):
        """ML 필터 통과 시 정상 진입."""
        ml_filter_deps["ml_filter"]._should_trade = True
        ml_filter_deps["ml_filter"]._win_probability = 0.65

        ml_filter_deps["long_eval"].set_decision(
            "BTC/USDT", _long_open_with_ml_indicators()
        )

        regime = _regime_state()
        result = await tier1_with_ml._evaluate_coin(session, "BTC/USDT", regime)
        assert result == "opened"
        ml_filter_deps["safe_order"].execute_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_ml_filter_does_not_block_close(
        self, tier1_with_ml, ml_filter_deps, session
    ):
        """ML 필터는 청산 시그널을 차단하지 않아야 함."""
        ml_filter_deps["ml_filter"]._should_trade = False  # 차단 설정

        # 포지션 생성
        ml_filter_deps["tracker"].open_position(
            PositionState(
                symbol="BTC/USDT",
                direction=Direction.LONG,
                entry_price=80000.0,
                quantity=0.01,
                margin=50.0,
                leverage=3,
                extreme_price=80000.0,
                stop_loss_atr=1.5,
                take_profit_atr=3.0,
                trailing_activation_atr=2.0,
                trailing_stop_atr=1.0,
                strategy_name="test",
                confidence=0.7,
                tier="tier1",
            )
        )

        # 롱 이밸류에이터가 청산 시그널 반환
        ml_filter_deps["long_eval"].set_decision("BTC/USDT", _close_decision())

        regime = _regime_state()
        result = await tier1_with_ml._evaluate_coin(session, "BTC/USDT", regime)
        assert result == "flat_close"  # 차단되지 않고 청산 실행

    @pytest.mark.asyncio
    async def test_ml_filtered_count_in_cycle_stats(
        self, tier1_with_ml, ml_filter_deps, session
    ):
        """evaluation_cycle에서 ml_filtered_count가 집계됨."""
        ml_filter_deps["ml_filter"]._should_trade = False

        ml_filter_deps["long_eval"].set_decision(
            "BTC/USDT", _long_open_with_ml_indicators()
        )
        ml_filter_deps["long_eval"].set_decision(
            "ETH/USDT", _long_open_with_ml_indicators()
        )

        stats = await tier1_with_ml.evaluation_cycle(session)
        assert stats.ml_filtered_count == 2
        assert stats.executed_count == 0

    @pytest.mark.asyncio
    async def test_no_ml_filter_entries_proceed(self, tier1, mock_deps, session):
        """ML 필터 없으면 진입이 정상 실행됨 (후방 호환)."""
        mock_deps["long_eval"].set_decision(
            "BTC/USDT",
            DirectionDecision(
                action="open",
                direction=Direction.LONG,
                confidence=0.8,
                sizing_factor=0.7,
                stop_loss_atr=1.5,
                take_profit_atr=3.0,
                reason="long_signal",
                strategy_name="trend_follower",
                indicators={"close": 80000.0, "atr": 1000.0},
            ),
        )

        regime = _regime_state()
        result = await tier1._evaluate_coin(session, "BTC/USDT", regime)
        assert result == "opened"


class TestCycleStatsMLFiltered:
    """CycleStats ml_filtered_count 필드 테스트."""

    def test_default_zero(self):
        stats = CycleStats()
        assert stats.ml_filtered_count == 0

    def test_increment(self):
        stats = CycleStats()
        stats.ml_filtered_count += 1
        assert stats.ml_filtered_count == 1


class TestMLFilterSAR:
    """SAR + ML 필터 상호작용 테스트."""

    @pytest.mark.asyncio
    async def test_sar_blocked_by_ml_filter(self, ml_filter_deps, session):
        """LONG 보유 → 숏 SAR 후보 → ML 필터 차단 → hold 반환."""
        ml_filter_deps["ml_filter"]._should_trade = False
        ml_filter_deps["ml_filter"]._win_probability = 0.30

        # SAR 경로 진입을 위해 long/short evaluator가 다른 인스턴스여야 함
        long_eval = MockLongEvaluator()
        short_eval = MockShortEvaluator()

        t1 = Tier1Manager(
            coins=["BTC/USDT"],
            safe_order=ml_filter_deps["safe_order"],
            position_tracker=ml_filter_deps["tracker"],
            regime_detector=ml_filter_deps["regime"],
            portfolio_manager=ml_filter_deps["pm"],
            market_data=ml_filter_deps["market_data"],
            long_evaluator=long_eval,
            short_evaluator=short_eval,
            leverage=3,
            max_position_pct=0.15,
            ml_filter=ml_filter_deps["ml_filter"],
        )

        # LONG 포지션 보유
        ml_filter_deps["tracker"].open_position(
            PositionState(
                symbol="BTC/USDT",
                direction=Direction.LONG,
                entry_price=80000.0,
                quantity=0.01,
                margin=50.0,
                leverage=3,
                extreme_price=80000.0,
                stop_loss_atr=1.5,
                take_profit_atr=3.0,
                trailing_activation_atr=2.0,
                trailing_stop_atr=1.0,
                strategy_name="test",
                confidence=0.7,
                tier="tier1",
            )
        )

        # long_eval hold → SAR 분기 진입
        long_eval.set_decision("BTC/USDT", _hold_decision("long_eval"))
        # short_eval open → SAR 후보
        short_eval.set_decision("BTC/USDT", _short_open_with_ml_indicators())

        regime = _regime_state()
        result = await t1._evaluate_coin(session, "BTC/USDT", regime)

        # ML 필터가 차단 → SAR 실행 안됨 → hold
        assert result == "hold"
        ml_filter_deps["safe_order"].execute_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_sar_allowed_by_ml_filter(self, ml_filter_deps, session):
        """LONG 보유 → 숏 SAR 후보 → ML 필터 통과 → SAR 실행."""
        ml_filter_deps["ml_filter"]._should_trade = True
        ml_filter_deps["ml_filter"]._win_probability = 0.65

        long_eval = MockLongEvaluator()
        short_eval = MockShortEvaluator()

        t1 = Tier1Manager(
            coins=["BTC/USDT"],
            safe_order=ml_filter_deps["safe_order"],
            position_tracker=ml_filter_deps["tracker"],
            regime_detector=ml_filter_deps["regime"],
            portfolio_manager=ml_filter_deps["pm"],
            market_data=ml_filter_deps["market_data"],
            long_evaluator=long_eval,
            short_evaluator=short_eval,
            leverage=3,
            max_position_pct=0.15,
            ml_filter=ml_filter_deps["ml_filter"],
        )

        ml_filter_deps["tracker"].open_position(
            PositionState(
                symbol="BTC/USDT",
                direction=Direction.LONG,
                entry_price=80000.0,
                quantity=0.01,
                margin=50.0,
                leverage=3,
                extreme_price=80000.0,
                stop_loss_atr=1.5,
                take_profit_atr=3.0,
                trailing_activation_atr=2.0,
                trailing_stop_atr=1.0,
                strategy_name="test",
                confidence=0.7,
                tier="tier1",
            )
        )

        long_eval.set_decision("BTC/USDT", _hold_decision("long_eval"))
        short_eval.set_decision("BTC/USDT", _short_open_with_ml_indicators())

        regime = _regime_state()
        result = await t1._evaluate_coin(session, "BTC/USDT", regime)

        # ML 필터 통과 → SAR 실행
        assert result == "sar"


class TestMLFilterSerialization:
    """ML 필터 데이터가 포함된 indicators의 DB 직렬화 테스트."""

    @pytest.mark.asyncio
    async def test_signal_objects_not_stored_in_strategy_log(
        self, tier1_with_ml, ml_filter_deps, session
    ):
        """실제 Signal 객체가 indicators에 포함돼도 DB 직렬화 시 _-prefix 키가 제거됨."""
        from strategies.base import Signal
        from core.enums import SignalType

        real_signals = [
            Signal(
                signal_type=SignalType.BUY,
                confidence=0.75,
                strategy_name="cis_momentum",
                reason="test signal",
            ),
            Signal(
                signal_type=SignalType.HOLD,
                confidence=0.30,
                strategy_name="bnf_deviation",
                reason="no signal",
            ),
        ]

        decision_with_signals = DirectionDecision(
            action="open",
            direction=Direction.LONG,
            confidence=0.8,
            sizing_factor=0.7,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="long_signal",
            strategy_name="cis_momentum",
            indicators={
                "close": 80000.0,
                "atr": 1000.0,
                "_signals": real_signals,
                "_candle_row": _make_candle_row(),
                "_combined_confidence": 0.8,
            },
        )

        ml_filter_deps["ml_filter"]._should_trade = True
        ml_filter_deps["long_eval"].set_decision(
            "BTC/USDT", decision_with_signals
        )

        regime = _regime_state()
        result = await tier1_with_ml._evaluate_coin(
            session, "BTC/USDT", regime
        )
        assert result == "opened"

        # flush → DB 쓰기 (JSON 직렬화 발생)
        await session.flush()

        # StrategyLog 조회하여 _-prefix 키가 제거됐는지 확인
        from sqlalchemy import select
        from core.models import StrategyLog

        logs = (
            await session.execute(
                select(StrategyLog).where(StrategyLog.symbol == "BTC/USDT")
            )
        ).scalars().all()
        assert len(logs) >= 1

        for log in logs:
            if log.indicators:
                for key in log.indicators:
                    assert not key.startswith("_"), (
                        f"Internal key '{key}' leaked to DB"
                    )


# ══════════════════════════════════════════════════════════════════
# COIN-42: 리스크 관리 테스트
# ══════════════════════════════════════════════════════════════════


def _regime_state_with_trend(regime=Regime.TRENDING_UP, trend_direction=1):
    """트렌드 방향을 지정할 수 있는 RegimeState 생성."""
    return RegimeState(
        regime=regime,
        confidence=0.8,
        adx=30,
        bb_width=3.0,
        atr_pct=1.5,
        volume_ratio=1.2,
        trend_direction=trend_direction,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.fixture
def risk_deps():
    """리스크 관리 테스트용 의존성 (all risk flags enabled)."""
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
    pm.cash_balance = 10000.0  # large enough to avoid cap issues

    market_data = AsyncMock()
    market_data.get_ohlcv_df = AsyncMock(return_value=_make_df())
    market_data.get_current_price = AsyncMock(return_value=80000.0)

    long_eval = MockLongEvaluator()
    short_eval = MockShortEvaluator()

    return {
        "regime": regime,
        "safe_order": safe_order,
        "tracker": tracker,
        "pm": pm,
        "market_data": market_data,
        "long_eval": long_eval,
        "short_eval": short_eval,
    }


@pytest.fixture
def tier1_risk(risk_deps):
    """Tier1Manager with all COIN-42 risk flags enabled."""
    return Tier1Manager(
        coins=["BTC/USDT", "ETH/USDT"],
        safe_order=risk_deps["safe_order"],
        position_tracker=risk_deps["tracker"],
        regime_detector=risk_deps["regime"],
        portfolio_manager=risk_deps["pm"],
        market_data=risk_deps["market_data"],
        long_evaluator=risk_deps["long_eval"],
        short_evaluator=risk_deps["short_eval"],
        leverage=5,
        max_position_pct=0.15,
        min_confidence=0.4,
        cooldown_seconds=0,
        asymmetric_mode=True,
        dynamic_sl=True,
        atr_leverage_scaling=True,
    )


# ── TestAsymmetricMode ──────────────────────────────────────────


class TestAsymmetricMode:
    """비대칭 모드: TRENDING_DOWN/VOLATILE(bearish)에서 롱 차단."""

    def test_is_bearish_trending_down(self):
        rs = _regime_state_with_trend(Regime.TRENDING_DOWN, trend_direction=-1)
        assert Tier1Manager._is_bearish_regime(rs) is True

    def test_is_bearish_trending_down_positive_trend(self):
        """TRENDING_DOWN은 trend_direction과 무관하게 항상 bearish."""
        rs = _regime_state_with_trend(Regime.TRENDING_DOWN, trend_direction=1)
        assert Tier1Manager._is_bearish_regime(rs) is True

    def test_is_bearish_volatile_negative_trend(self):
        rs = _regime_state_with_trend(Regime.VOLATILE, trend_direction=-1)
        assert Tier1Manager._is_bearish_regime(rs) is True

    def test_not_bearish_volatile_positive_trend(self):
        rs = _regime_state_with_trend(Regime.VOLATILE, trend_direction=1)
        assert Tier1Manager._is_bearish_regime(rs) is False

    def test_not_bearish_trending_up(self):
        rs = _regime_state_with_trend(Regime.TRENDING_UP, trend_direction=1)
        assert Tier1Manager._is_bearish_regime(rs) is False

    def test_not_bearish_ranging(self):
        rs = _regime_state_with_trend(Regime.RANGING, trend_direction=0)
        assert Tier1Manager._is_bearish_regime(rs) is False

    @pytest.mark.asyncio
    async def test_long_blocked_in_downtrend(self, risk_deps, tier1_risk, session):
        """TRENDING_DOWN에서 롱 진입 → hold로 차단."""
        risk_deps["regime"]._current = _regime_state(Regime.TRENDING_DOWN)

        risk_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision(
            indicators={"close": 80000.0, "atr": 1000.0},
        ))

        result = await tier1_risk._evaluate_coin(
            session, "BTC/USDT", risk_deps["regime"]._current
        )
        assert result == "hold"

    @pytest.mark.asyncio
    async def test_short_allowed_in_downtrend(self, risk_deps, tier1_risk, session):
        """TRENDING_DOWN에서 숏 진입 → 허용."""
        risk_deps["regime"]._current = _regime_state(Regime.TRENDING_DOWN)

        risk_deps["short_eval"].set_decision("BTC/USDT", _short_open_decision(
            indicators={"close": 80000.0, "atr": 1000.0},
        ))

        result = await tier1_risk._evaluate_coin(
            session, "BTC/USDT", risk_deps["regime"]._current
        )
        assert result == "opened"

    @pytest.mark.asyncio
    async def test_long_allowed_in_uptrend(self, risk_deps, tier1_risk, session):
        """TRENDING_UP에서 롱 진입 → 허용."""
        risk_deps["regime"]._current = _regime_state(Regime.TRENDING_UP)

        risk_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision(
            indicators={"close": 80000.0, "atr": 1000.0},
        ))

        result = await tier1_risk._evaluate_coin(
            session, "BTC/USDT", risk_deps["regime"]._current
        )
        assert result == "opened"

    @pytest.mark.asyncio
    async def test_disabled_allows_long_in_downtrend(self, risk_deps, session):
        """asymmetric_mode=False면 TRENDING_DOWN에서도 롱 허용."""
        risk_deps["regime"]._current = _regime_state(Regime.TRENDING_DOWN)

        tier1_no_asym = Tier1Manager(
            coins=["BTC/USDT"],
            safe_order=risk_deps["safe_order"],
            position_tracker=risk_deps["tracker"],
            regime_detector=risk_deps["regime"],
            portfolio_manager=risk_deps["pm"],
            market_data=risk_deps["market_data"],
            long_evaluator=risk_deps["long_eval"],
            short_evaluator=risk_deps["short_eval"],
            leverage=5,
            max_position_pct=0.15,
            min_confidence=0.4,
            cooldown_seconds=0,
            asymmetric_mode=False,
        )

        risk_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision(
            indicators={"close": 80000.0, "atr": 1000.0},
        ))

        result = await tier1_no_asym._evaluate_coin(
            session, "BTC/USDT", risk_deps["regime"]._current
        )
        assert result == "opened"


# ── TestDynamicSL ───────────────────────────────────────────────


class TestDynamicSL:
    """동적 SL: 레짐별 ATR mult 스케일링."""

    def test_trending_up_no_change(self, risk_deps, tier1_risk):
        """TRENDING_UP: multiplier=1.0 → 그대로."""
        risk_deps["regime"]._current = _regime_state(Regime.TRENDING_UP)
        result = tier1_risk._apply_dynamic_sl(2.0)
        assert result == 2.0  # 2.0 * 1.0 = 2.0

    def test_trending_down_tighter(self, risk_deps, tier1_risk):
        """TRENDING_DOWN: multiplier=0.6 → 타이트."""
        risk_deps["regime"]._current = _regime_state(Regime.TRENDING_DOWN)
        result = tier1_risk._apply_dynamic_sl(2.0)
        assert result == 1.2  # 2.0 * 0.6 = 1.2

    def test_ranging_moderate(self, risk_deps, tier1_risk):
        """RANGING: multiplier=0.8."""
        risk_deps["regime"]._current = _regime_state(Regime.RANGING)
        result = tier1_risk._apply_dynamic_sl(2.0)
        assert result == 1.6  # 2.0 * 0.8 = 1.6

    def test_volatile_tighter(self, risk_deps, tier1_risk):
        """VOLATILE: multiplier=0.7."""
        risk_deps["regime"]._current = _regime_state(Regime.VOLATILE)
        result = tier1_risk._apply_dynamic_sl(2.0)
        assert result == 1.4  # 2.0 * 0.7 = 1.4

    def test_floor_clamp(self, risk_deps, tier1_risk):
        """SL이 floor 미만이면 floor로 클램프."""
        risk_deps["regime"]._current = _regime_state(Regime.TRENDING_DOWN)
        # 0.5 * 0.6 = 0.3, but floor for TRENDING_DOWN = 0.8
        result = tier1_risk._apply_dynamic_sl(0.5)
        assert result == 0.8

    def test_cap_clamp(self, risk_deps, tier1_risk):
        """SL이 cap 초과이면 cap으로 클램프."""
        risk_deps["regime"]._current = _regime_state(Regime.TRENDING_DOWN)
        # 10.0 * 0.6 = 6.0, but cap for TRENDING_DOWN = 4.0
        result = tier1_risk._apply_dynamic_sl(10.0)
        assert result == 4.0

    def test_no_regime_returns_base(self, risk_deps, tier1_risk):
        """레짐 없으면 base_sl_atr 그대로 반환."""
        risk_deps["regime"]._current = None
        result = tier1_risk._apply_dynamic_sl(2.0)
        assert result == 2.0

    @pytest.mark.asyncio
    async def test_sl_check_uses_dynamic_and_restores(self, risk_deps, tier1_risk, session):
        """_check_sl_tp에서 동적 SL 적용 후 원복 확인."""
        risk_deps["regime"]._current = _regime_state(Regime.TRENDING_DOWN)

        state = PositionState(
            symbol="BTC/USDT",
            direction=Direction.LONG,
            quantity=0.01,
            entry_price=80000.0,
            margin=100.0,
            leverage=5,
            extreme_price=80000.0,
            stop_loss_atr=2.0,
            take_profit_atr=4.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
            tier="tier1",
        )

        original_sl = state.stop_loss_atr
        # Price that doesn't trigger SL
        await tier1_risk._check_sl_tp(session, "BTC/USDT", state, 79500.0, 1000.0)
        # SL should be restored to original
        assert state.stop_loss_atr == original_sl

    @pytest.mark.asyncio
    async def test_dynamic_sl_triggers_tighter_sl(self, risk_deps, tier1_risk, session):
        """TRENDING_DOWN에서 동적 SL이 더 타이트하게 적용되어 SL 히트."""
        risk_deps["regime"]._current = _regime_state(Regime.TRENDING_DOWN)

        # entry=80000, atr=1000
        # Original SL: 80000 - (1000 * 2.0) = 78000
        # Dynamic SL (TRENDING_DOWN, mult=0.6): 2.0 * 0.6 = 1.2
        #   → SL: 80000 - (1000 * 1.2) = 78800
        # Price 78700 < 78800 → SL hit with dynamic, but NOT without
        state = PositionState(
            symbol="BTC/USDT",
            direction=Direction.LONG,
            quantity=0.01,
            entry_price=80000.0,
            margin=100.0,
            leverage=5,
            extreme_price=80000.0,
            stop_loss_atr=2.0,
            take_profit_atr=4.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
            tier="tier1",
        )
        risk_deps["tracker"].open_position(state)

        result = await tier1_risk._check_sl_tp(
            session, "BTC/USDT", state, 78700.0, 1000.0
        )
        assert result is True  # dynamic SL triggered


# ── TestATRLeverageScaling ──────────────────────────────────────


class TestATRLeverageScaling:
    """ATR% 기반 레버리지 스케일링."""

    def test_low_atr_full_leverage(self, tier1_risk):
        """낮은 ATR(1%) → 최대 레버리지(5x)."""
        lev = tier1_risk._calc_atr_leverage(atr=800.0, close=80000.0)
        # ATR% = 1% → tier (0.0, 5) → min(5, 5) = 5
        assert lev == 5

    def test_high_atr_reduced_leverage(self, tier1_risk):
        """높은 ATR(8%) → 레버리지 3x로 축소."""
        lev = tier1_risk._calc_atr_leverage(atr=6400.0, close=80000.0)
        # ATR% = 8% → tier (7.0, 3) → min(5, 3) = 3
        assert lev == 3

    def test_very_high_atr_minimal_leverage(self, tier1_risk):
        """매우 높은 ATR(25%) → 레버리지 1x."""
        lev = tier1_risk._calc_atr_leverage(atr=20000.0, close=80000.0)
        # ATR% = 25% → tier (20.0, 1) → min(5, 1) = 1
        assert lev == 1

    def test_medium_atr_4x(self, tier1_risk):
        """중간 ATR(6%) → 레버리지 4x."""
        lev = tier1_risk._calc_atr_leverage(atr=4800.0, close=80000.0)
        # ATR% = 6% → tier (5.0, 4) → min(5, 4) = 4
        assert lev == 4

    def test_zero_close_returns_1(self, tier1_risk):
        """close=0 → 안전하게 1x."""
        lev = tier1_risk._calc_atr_leverage(atr=1000.0, close=0.0)
        assert lev == 1

    def test_config_leverage_cap(self, risk_deps):
        """설정 레버리지(2x)가 ATR 레버리지(5x)보다 작으면 설정값 우선."""
        tier1_low_lev = Tier1Manager(
            coins=["BTC/USDT"],
            safe_order=risk_deps["safe_order"],
            position_tracker=risk_deps["tracker"],
            regime_detector=risk_deps["regime"],
            portfolio_manager=risk_deps["pm"],
            market_data=risk_deps["market_data"],
            long_evaluator=risk_deps["long_eval"],
            short_evaluator=risk_deps["short_eval"],
            leverage=2,
            atr_leverage_scaling=True,
        )
        # ATR% = 1% → tier allows 5, but config is 2 → min(2, 5) = 2
        lev = tier1_low_lev._calc_atr_leverage(atr=800.0, close=80000.0)
        assert lev == 2


# ── TestRegimeSizing ────────────────────────────────────────────


class TestRegimeSizing:
    """레짐별 포지션 사이징 팩터."""

    def test_trending_up_full_size(self, risk_deps, tier1_risk):
        risk_deps["regime"]._current = _regime_state(Regime.TRENDING_UP)
        assert tier1_risk._get_regime_sizing_factor() == 1.0

    def test_trending_down_half_size(self, risk_deps, tier1_risk):
        risk_deps["regime"]._current = _regime_state(Regime.TRENDING_DOWN)
        assert tier1_risk._get_regime_sizing_factor() == 0.5

    def test_ranging_80_pct(self, risk_deps, tier1_risk):
        risk_deps["regime"]._current = _regime_state(Regime.RANGING)
        assert tier1_risk._get_regime_sizing_factor() == 0.8

    def test_volatile_60_pct(self, risk_deps, tier1_risk):
        risk_deps["regime"]._current = _regime_state(Regime.VOLATILE)
        assert tier1_risk._get_regime_sizing_factor() == 0.6

    def test_no_regime_defaults_to_1(self, risk_deps, tier1_risk):
        risk_deps["regime"]._current = None
        assert tier1_risk._get_regime_sizing_factor() == 1.0

    def test_margin_smaller_in_downtrend(self, risk_deps, tier1_risk):
        """TRENDING_DOWN 마진 < TRENDING_UP 마진."""
        decision = _long_open_decision(confidence=0.8, sizing_factor=0.7)

        # Large ATR to keep raw_margin below max_position_pct cap
        risk_deps["regime"]._current = _regime_state(Regime.TRENDING_UP)
        margin_up = tier1_risk._calc_margin(decision, close=80000.0, atr=5000.0)

        risk_deps["regime"]._current = _regime_state(Regime.TRENDING_DOWN)
        margin_down = tier1_risk._calc_margin(decision, close=80000.0, atr=5000.0)

        assert margin_down < margin_up
        assert abs(margin_down / margin_up - 0.5) < 0.01  # 50% ratio


# ── TestMinSellActiveWeight ─────────────────────────────────────


class TestMinSellActiveWeight:
    """MIN_SELL_ACTIVE_WEIGHT: 숏 진입 시 최소 2전략 합의 필요."""

    def test_combiner_blocks_single_strategy_short(self):
        """단일 전략 SELL → MIN_SELL_ACTIVE_WEIGHT 미충족 → HOLD."""
        from strategies.combiner import SignalCombiner
        from strategies.base import Signal
        from core.enums import SignalType

        # 가장 무거운 단일 전략(cis_momentum=0.42)보다 높게 설정
        combiner = SignalCombiner(
            strategy_weights=SignalCombiner.SPOT_WEIGHTS.copy(),
            min_confidence=0.50,
            min_sell_active_weight=0.45,
        )

        # 단일 전략 cis_momentum(0.42)만 SELL → 0.42 < 0.45 → 차단
        signals = [
            Signal(signal_type=SignalType.SELL, confidence=0.9, strategy_name="cis_momentum", reason="test"),
            Signal(signal_type=SignalType.HOLD, confidence=0.0, strategy_name="bnf_deviation", reason="test"),
            Signal(signal_type=SignalType.HOLD, confidence=0.0, strategy_name="donchian_channel", reason="test"),
            Signal(signal_type=SignalType.HOLD, confidence=0.0, strategy_name="larry_williams", reason="test"),
        ]
        result = combiner.combine(signals)
        assert result.action == SignalType.HOLD  # blocked by MIN_SELL_ACTIVE_WEIGHT

    def test_combiner_allows_two_strategy_short(self):
        """2전략 SELL → active_weight >= 0.45 → 통과."""
        from strategies.combiner import SignalCombiner
        from strategies.base import Signal
        from core.enums import SignalType

        combiner = SignalCombiner(
            strategy_weights=SignalCombiner.SPOT_WEIGHTS.copy(),
            min_confidence=0.50,
            min_sell_active_weight=0.45,
        )

        # 2전략 SELL: cis_momentum(0.42) + bnf_deviation(0.25) = 0.67 >= 0.45
        signals = [
            Signal(signal_type=SignalType.SELL, confidence=0.8, strategy_name="cis_momentum", reason="test"),
            Signal(signal_type=SignalType.SELL, confidence=0.7, strategy_name="bnf_deviation", reason="test"),
            Signal(signal_type=SignalType.HOLD, confidence=0.0, strategy_name="donchian_channel", reason="test"),
            Signal(signal_type=SignalType.HOLD, confidence=0.0, strategy_name="larry_williams", reason="test"),
        ]
        result = combiner.combine(signals)
        assert result.action == SignalType.SELL

    def test_combiner_default_no_min_sell_weight(self):
        """min_sell_active_weight=0 (기본값) → 단일 전략 SELL도 허용."""
        from strategies.combiner import SignalCombiner
        from strategies.base import Signal
        from core.enums import SignalType

        combiner = SignalCombiner(
            strategy_weights=SignalCombiner.SPOT_WEIGHTS.copy(),
            min_confidence=0.50,
            min_sell_active_weight=0.0,
        )

        signals = [
            Signal(signal_type=SignalType.SELL, confidence=0.9, strategy_name="cis_momentum", reason="test"),
            Signal(signal_type=SignalType.HOLD, confidence=0.0, strategy_name="bnf_deviation", reason="test"),
            Signal(signal_type=SignalType.HOLD, confidence=0.0, strategy_name="donchian_channel", reason="test"),
            Signal(signal_type=SignalType.HOLD, confidence=0.0, strategy_name="larry_williams", reason="test"),
        ]
        result = combiner.combine(signals)
        # With default, single strategy SELL should be allowed
        # (if it passes MIN_ACTIVE_WEIGHT=0.12, which 0.22 does)
        assert result.action == SignalType.SELL


# ── TestRiskFlagsInit ───────────────────────────────────────────


class TestRiskFlagsInit:
    """리스크 관리 플래그 초기화."""

    def test_default_flags_off(self, risk_deps):
        """기본값: 모든 리스크 플래그 비활성."""
        tier1 = Tier1Manager(
            coins=["BTC/USDT"],
            safe_order=risk_deps["safe_order"],
            position_tracker=risk_deps["tracker"],
            regime_detector=risk_deps["regime"],
            portfolio_manager=risk_deps["pm"],
            market_data=risk_deps["market_data"],
            long_evaluator=risk_deps["long_eval"],
            short_evaluator=risk_deps["short_eval"],
        )
        assert tier1._asymmetric_mode is False
        assert tier1._dynamic_sl is False
        assert tier1._atr_leverage_scaling is False

    def test_explicit_flags_on(self, tier1_risk):
        """명시적 활성화: 모든 리스크 플래그 활성."""
        assert tier1_risk._asymmetric_mode is True
        assert tier1_risk._dynamic_sl is True
        assert tier1_risk._atr_leverage_scaling is True


# ── TestRiskIntegration ─────────────────────────────────────────


class TestRiskIntegration:
    """리스크 관리 통합 테스트."""

    @pytest.mark.asyncio
    async def test_downtrend_short_smaller_position(self, risk_deps, tier1_risk, session):
        """TRENDING_DOWN에서 숏 포지션 사이징 50% 축소 확인."""
        # TRENDING_UP에서 숏 열기
        risk_deps["regime"]._current = _regime_state(Regime.TRENDING_UP)
        risk_deps["short_eval"].set_decision("BTC/USDT", _short_open_decision(
            indicators={"close": 80000.0, "atr": 5000.0},
        ))
        await tier1_risk._evaluate_coin(
            session, "BTC/USDT", risk_deps["regime"]._current
        )
        order_up = risk_deps["safe_order"].execute_order.call_args
        margin_up = order_up[0][1].margin  # OrderRequest.margin

        # 리셋
        risk_deps["safe_order"].execute_order.reset_mock()
        risk_deps["tracker"].close_position("BTC/USDT")

        # TRENDING_DOWN에서 숏 열기
        risk_deps["regime"]._current = _regime_state(Regime.TRENDING_DOWN)
        risk_deps["short_eval"].set_decision("BTC/USDT", _short_open_decision(
            indicators={"close": 80000.0, "atr": 5000.0},
        ))
        await tier1_risk._evaluate_coin(
            session, "BTC/USDT", risk_deps["regime"]._current
        )
        order_down = risk_deps["safe_order"].execute_order.call_args
        margin_down = order_down[0][1].margin

        assert margin_down < margin_up

    @pytest.mark.asyncio
    async def test_high_atr_reduces_leverage_in_order(self, risk_deps, session):
        """높은 ATR% → OrderRequest의 leverage가 축소되는지 확인."""
        risk_deps["regime"]._current = _regime_state(Regime.TRENDING_UP)

        tier1 = Tier1Manager(
            coins=["BTC/USDT"],
            safe_order=risk_deps["safe_order"],
            position_tracker=risk_deps["tracker"],
            regime_detector=risk_deps["regime"],
            portfolio_manager=risk_deps["pm"],
            market_data=risk_deps["market_data"],
            long_evaluator=risk_deps["long_eval"],
            short_evaluator=risk_deps["short_eval"],
            leverage=5,
            max_position_pct=0.15,
            min_confidence=0.4,
            cooldown_seconds=0,
            atr_leverage_scaling=True,
        )

        # ATR% = 8000/80000 = 10% → tier (10.0, 2) triggers → but actually > 10%
        # Let's use ATR = 8800, close = 80000 → ATR% = 11% → tier (10.0, 2) → lev=2
        risk_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision(
            indicators={"close": 80000.0, "atr": 8800.0},
        ))

        await tier1._evaluate_coin(
            session, "BTC/USDT", risk_deps["regime"]._current
        )

        order = risk_deps["safe_order"].execute_order.call_args
        req = order[0][1]  # OrderRequest
        assert req.leverage == 2  # scaled down from 5


class TestUSOpenNoEntry:
    """US 마켓 오픈 시간(KST 22-23) 진입 차단 테스트."""

    @pytest.mark.asyncio
    async def test_entry_blocked_during_us_open_kst22(self, tier1, mock_deps, session):
        """KST 22시(UTC 13시)에 신규 진입 차단."""
        mock_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision())

        # UTC 13:30 → KST 22:30
        fake_utc = datetime(2026, 4, 1, 13, 30, tzinfo=timezone.utc)
        with patch("engine.tier1_manager.datetime") as mock_dt:
            mock_dt.now.return_value = fake_utc
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())

        assert result == "us_open_blocked"
        mock_deps["safe_order"].execute_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_entry_blocked_during_us_open_kst23(self, tier1, mock_deps, session):
        """KST 23시(UTC 14시)에 신규 진입 차단."""
        mock_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision())

        # UTC 14:00 → KST 23:00
        fake_utc = datetime(2026, 4, 1, 14, 0, tzinfo=timezone.utc)
        with patch("engine.tier1_manager.datetime") as mock_dt:
            mock_dt.now.return_value = fake_utc
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())

        assert result == "us_open_blocked"
        mock_deps["safe_order"].execute_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_entry_allowed_outside_us_open(self, tier1, mock_deps, session):
        """KST 21시(UTC 12시)에는 정상 진입."""
        mock_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision())

        # UTC 12:00 → KST 21:00
        fake_utc = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
        with patch("engine.tier1_manager.datetime") as mock_dt:
            mock_dt.now.return_value = fake_utc
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())

        assert result == "opened"
        mock_deps["safe_order"].execute_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_sl_tp_not_blocked_during_us_open(self, tier1, mock_deps, session):
        """US 오픈 시간에도 SL/TP 청산은 정상 동작."""
        # 포지션 생성
        mock_deps["tracker"].open_position(
            PositionState(
                symbol="BTC/USDT",
                direction=Direction.LONG,
                entry_price=80000.0,
                quantity=0.01,
                margin=50.0,
                leverage=3,
                extreme_price=80000.0,
                stop_loss_atr=1.5,
                take_profit_atr=3.0,
                trailing_activation_atr=2.0,
                trailing_stop_atr=1.0,
                strategy_name="test",
                confidence=0.7,
                tier="tier1",
            )
        )

        # SL 히트 가격 설정 (78500 < 80000 - 1.5*1000 = 78500)
        mock_deps["market_data"].get_ohlcv_df = AsyncMock(return_value=_make_df(close=78000.0))

        # UTC 13:30 → KST 22:30 (US 오픈 시간)
        fake_utc = datetime(2026, 4, 1, 13, 30, tzinfo=timezone.utc)
        with patch("engine.tier1_manager.datetime") as mock_dt:
            mock_dt.now.return_value = fake_utc
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())

        # SL/TP는 US 오픈 필터보다 먼저 체크되므로 청산됨
        assert result == "sl_tp"
