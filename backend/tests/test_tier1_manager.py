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


def _long_open_decision(confidence=0.8, sizing_factor=0.7):
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
    )


def _short_open_decision(confidence=0.7, sizing_factor=0.6):
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

        async def raising_eval(symbol, pos):
            if symbol == "BTC/USDT":
                raise RuntimeError("test error")
            return await original_eval(symbol, pos)

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
            lg
            for lg in logs
            if lg.signal_type == "SELL" and lg.was_executed
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
        total_calls = mock_deps["market_data"].get_ohlcv_df.call_count - initial_call_count
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
        total_calls = mock_deps["market_data"].get_ohlcv_df.call_count - initial_call_count
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
        result = tier1._resolve_entry(_hold_decision(), _hold_decision())
        assert result is None

    def test_long_only(self, tier1):
        result = tier1._resolve_entry(_long_open_decision(), _hold_decision())
        assert result is not None
        assert result.direction == Direction.LONG

    def test_short_only(self, tier1):
        result = tier1._resolve_entry(_hold_decision(), _short_open_decision())
        assert result is not None
        assert result.direction == Direction.SHORT

    def test_both_open_long_wins(self, tier1):
        long_d = _long_open_decision(confidence=0.9)
        short_d = _short_open_decision(confidence=0.7)
        result = tier1._resolve_entry(long_d, short_d)
        assert result.direction == Direction.LONG

    def test_both_open_short_wins(self, tier1):
        long_d = _long_open_decision(confidence=0.5)
        short_d = _short_open_decision(confidence=0.8)
        result = tier1._resolve_entry(long_d, short_d)
        assert result.direction == Direction.SHORT

    def test_equal_confidence_long_wins(self, tier1):
        long_d = _long_open_decision(confidence=0.7)
        short_d = _short_open_decision(confidence=0.7)
        result = tier1._resolve_entry(long_d, short_d)
        assert result.direction == Direction.LONG
