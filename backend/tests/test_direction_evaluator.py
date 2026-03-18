"""DirectionEvaluator 프로토콜 + RegimeEvaluator 래퍼 테스트."""

import pytest
import pandas as pd
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

from engine.direction_evaluator import DirectionDecision, DirectionEvaluator
from engine.regime_evaluators import RegimeLongEvaluator, RegimeShortEvaluator
from engine.regime_detector import RegimeDetector, RegimeState
from engine.strategy_selector import StrategySelector
from engine.position_state_tracker import PositionState
from core.enums import Direction, Regime
from strategies.regime_base import StrategyDecision


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


def _long_position(symbol="BTC/USDT"):
    return PositionState(
        symbol=symbol,
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


def _short_position(symbol="BTC/USDT"):
    return PositionState(
        symbol=symbol,
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


# ──── DirectionDecision 테스트 ────────────────────


class TestDirectionDecision:
    def test_hold_decision(self):
        d = DirectionDecision(
            action="hold",
            direction=None,
            confidence=0.0,
            sizing_factor=0.0,
            stop_loss_atr=0.0,
            take_profit_atr=0.0,
            reason="no_signal",
            strategy_name="test",
        )
        assert d.is_hold
        assert not d.is_open
        assert not d.is_close

    def test_open_decision(self):
        d = DirectionDecision(
            action="open",
            direction=Direction.LONG,
            confidence=0.8,
            sizing_factor=0.7,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="long_signal",
            strategy_name="test",
        )
        assert d.is_open
        assert not d.is_hold
        assert not d.is_close

    def test_close_decision(self):
        d = DirectionDecision(
            action="close",
            direction=None,
            confidence=0.6,
            sizing_factor=0.5,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="exit_signal",
            strategy_name="test",
        )
        assert d.is_close
        assert not d.is_hold
        assert not d.is_open

    def test_frozen(self):
        d = DirectionDecision(
            action="hold",
            direction=None,
            confidence=0.0,
            sizing_factor=0.0,
            stop_loss_atr=0.0,
            take_profit_atr=0.0,
            reason="test",
            strategy_name="test",
        )
        with pytest.raises(AttributeError):
            d.action = "open"

    def test_default_indicators(self):
        d = DirectionDecision(
            action="hold",
            direction=None,
            confidence=0.0,
            sizing_factor=0.0,
            stop_loss_atr=0.0,
            take_profit_atr=0.0,
            reason="test",
            strategy_name="test",
        )
        assert d.indicators == {}


# ──── DirectionEvaluator 프로토콜 테스트 ────────────


class TestDirectionEvaluatorProtocol:
    """runtime_checkable Protocol 테스트."""

    def test_long_evaluator_is_direction_evaluator(self):
        """RegimeLongEvaluator가 DirectionEvaluator 프로토콜을 만족."""
        evaluator = RegimeLongEvaluator(
            strategy_selector=StrategySelector(),
            regime_detector=RegimeDetector(),
            market_data=AsyncMock(),
        )
        assert isinstance(evaluator, DirectionEvaluator)

    def test_short_evaluator_is_direction_evaluator(self):
        """RegimeShortEvaluator가 DirectionEvaluator 프로토콜을 만족."""
        evaluator = RegimeShortEvaluator(
            strategy_selector=StrategySelector(),
            regime_detector=RegimeDetector(),
            market_data=AsyncMock(),
        )
        assert isinstance(evaluator, DirectionEvaluator)

    def test_mock_evaluator_is_direction_evaluator(self):
        """Mock 객체도 프로토콜 만족 확인."""

        class MockEval:
            @property
            def eval_interval_sec(self):
                return 60

            async def evaluate(self, symbol, current_position, **kwargs):
                pass

        assert isinstance(MockEval(), DirectionEvaluator)

    def test_non_evaluator_fails(self):
        """프로토콜 미충족 객체는 isinstance 실패."""

        class NotEval:
            pass

        assert not isinstance(NotEval(), DirectionEvaluator)


# ──── RegimeLongEvaluator 테스트 ───────────────────


class TestRegimeLongEvaluator:
    @pytest.fixture
    def setup(self):
        regime = RegimeDetector()
        regime._current = _regime_state()

        selector = MagicMock(spec=StrategySelector)
        strategy = AsyncMock()
        selector.select.return_value = strategy
        strategy.name = "trend_follower"

        market_data = AsyncMock()
        market_data.get_ohlcv_df = AsyncMock(return_value=_make_df())

        evaluator = RegimeLongEvaluator(
            strategy_selector=selector,
            regime_detector=regime,
            market_data=market_data,
        )

        return {
            "evaluator": evaluator,
            "regime": regime,
            "selector": selector,
            "strategy": strategy,
            "market_data": market_data,
        }

    @pytest.mark.asyncio
    async def test_long_signal_passes_through(self, setup):
        """LONG 시그널 → open 결정 반환."""
        setup["strategy"].evaluate.return_value = StrategyDecision(
            direction=Direction.LONG,
            confidence=0.8,
            sizing_factor=0.7,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="long_entry",
            strategy_name="trend_follower",
        )

        decision = await setup["evaluator"].evaluate("BTC/USDT", None)
        assert decision.is_open
        assert decision.direction == Direction.LONG
        assert decision.confidence == 0.8

    @pytest.mark.asyncio
    async def test_short_signal_filtered(self, setup):
        """SHORT 시그널 → hold로 변환."""
        setup["strategy"].evaluate.return_value = StrategyDecision(
            direction=Direction.SHORT,
            confidence=0.8,
            sizing_factor=0.7,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="short_entry",
            strategy_name="trend_follower",
        )

        decision = await setup["evaluator"].evaluate("BTC/USDT", None)
        assert decision.is_hold
        assert "ignores_short" in decision.reason

    @pytest.mark.asyncio
    async def test_flat_with_long_position_closes(self, setup):
        """FLAT 시그널 + 롱 포지션 → close 결정."""
        setup["strategy"].evaluate.return_value = StrategyDecision(
            direction=Direction.FLAT,
            confidence=0.6,
            sizing_factor=0.5,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="exit_long",
            strategy_name="trend_follower",
        )

        pos = _long_position()
        decision = await setup["evaluator"].evaluate("BTC/USDT", pos)
        assert decision.is_close

    @pytest.mark.asyncio
    async def test_flat_without_position_holds(self, setup):
        """FLAT 시그널 + 포지션 없음 → hold."""
        setup["strategy"].evaluate.return_value = StrategyDecision(
            direction=Direction.FLAT,
            confidence=0.6,
            sizing_factor=0.5,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="exit",
            strategy_name="trend_follower",
        )

        decision = await setup["evaluator"].evaluate("BTC/USDT", None)
        assert decision.is_hold

    @pytest.mark.asyncio
    async def test_flat_with_short_position_holds(self, setup):
        """FLAT 시그널 + 숏 포지션 → hold (롱 이밸류에이터는 숏 청산 안 함)."""
        setup["strategy"].evaluate.return_value = StrategyDecision(
            direction=Direction.FLAT,
            confidence=0.6,
            sizing_factor=0.5,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="exit",
            strategy_name="trend_follower",
        )

        pos = _short_position()
        decision = await setup["evaluator"].evaluate("BTC/USDT", pos)
        assert decision.is_hold

    @pytest.mark.asyncio
    async def test_hold_signal_passes_through(self, setup):
        """HOLD 시그널 → hold 결정."""
        setup["strategy"].evaluate.return_value = StrategyDecision(
            direction=Direction.FLAT,
            confidence=0.0,
            sizing_factor=0.0,
            stop_loss_atr=0.0,
            take_profit_atr=0.0,
            reason="no_signal",
            strategy_name="trend_follower",
        )

        decision = await setup["evaluator"].evaluate("BTC/USDT", None)
        assert decision.is_hold

    @pytest.mark.asyncio
    async def test_no_regime_returns_hold(self, setup):
        """레짐 없으면 hold."""
        setup["regime"]._current = None
        decision = await setup["evaluator"].evaluate("BTC/USDT", None)
        assert decision.is_hold
        assert "no_regime" in decision.reason

    @pytest.mark.asyncio
    async def test_candle_error_returns_hold(self, setup):
        """캔들 에러 시 hold."""
        setup["market_data"].get_ohlcv_df = AsyncMock(return_value=None)
        decision = await setup["evaluator"].evaluate("BTC/USDT", None)
        assert decision.is_hold
        assert "candle_error" in decision.reason

    def test_eval_interval_sec(self, setup):
        assert setup["evaluator"].eval_interval_sec == 60

    def test_custom_eval_interval(self):
        evaluator = RegimeLongEvaluator(
            strategy_selector=StrategySelector(),
            regime_detector=RegimeDetector(),
            market_data=AsyncMock(),
            eval_interval=120,
        )
        assert evaluator.eval_interval_sec == 120


# ──── RegimeShortEvaluator 테스트 ──────────────────


class TestRegimeShortEvaluator:
    @pytest.fixture
    def setup(self):
        regime = RegimeDetector()
        regime._current = _regime_state()

        selector = MagicMock(spec=StrategySelector)
        strategy = AsyncMock()
        selector.select.return_value = strategy
        strategy.name = "mean_reversion"

        market_data = AsyncMock()
        market_data.get_ohlcv_df = AsyncMock(return_value=_make_df())

        evaluator = RegimeShortEvaluator(
            strategy_selector=selector,
            regime_detector=regime,
            market_data=market_data,
        )

        return {
            "evaluator": evaluator,
            "regime": regime,
            "selector": selector,
            "strategy": strategy,
            "market_data": market_data,
        }

    @pytest.mark.asyncio
    async def test_short_signal_passes_through(self, setup):
        """SHORT 시그널 → open 결정 반환."""
        setup["strategy"].evaluate.return_value = StrategyDecision(
            direction=Direction.SHORT,
            confidence=0.7,
            sizing_factor=0.6,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="short_entry",
            strategy_name="mean_reversion",
        )

        decision = await setup["evaluator"].evaluate("BTC/USDT", None)
        assert decision.is_open
        assert decision.direction == Direction.SHORT
        assert decision.confidence == 0.7

    @pytest.mark.asyncio
    async def test_long_signal_filtered(self, setup):
        """LONG 시그널 → hold로 변환."""
        setup["strategy"].evaluate.return_value = StrategyDecision(
            direction=Direction.LONG,
            confidence=0.8,
            sizing_factor=0.7,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="long_entry",
            strategy_name="mean_reversion",
        )

        decision = await setup["evaluator"].evaluate("BTC/USDT", None)
        assert decision.is_hold
        assert "ignores_long" in decision.reason

    @pytest.mark.asyncio
    async def test_flat_with_short_position_closes(self, setup):
        """FLAT 시그널 + 숏 포지션 → close 결정."""
        setup["strategy"].evaluate.return_value = StrategyDecision(
            direction=Direction.FLAT,
            confidence=0.6,
            sizing_factor=0.5,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="exit_short",
            strategy_name="mean_reversion",
        )

        pos = _short_position()
        decision = await setup["evaluator"].evaluate("BTC/USDT", pos)
        assert decision.is_close

    @pytest.mark.asyncio
    async def test_flat_without_position_holds(self, setup):
        """FLAT 시그널 + 포지션 없음 → hold."""
        setup["strategy"].evaluate.return_value = StrategyDecision(
            direction=Direction.FLAT,
            confidence=0.6,
            sizing_factor=0.5,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="exit",
            strategy_name="mean_reversion",
        )

        decision = await setup["evaluator"].evaluate("BTC/USDT", None)
        assert decision.is_hold

    @pytest.mark.asyncio
    async def test_flat_with_long_position_holds(self, setup):
        """FLAT 시그널 + 롱 포지션 → hold (숏 이밸류에이터는 롱 청산 안 함)."""
        setup["strategy"].evaluate.return_value = StrategyDecision(
            direction=Direction.FLAT,
            confidence=0.6,
            sizing_factor=0.5,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="exit",
            strategy_name="mean_reversion",
        )

        pos = _long_position()
        decision = await setup["evaluator"].evaluate("BTC/USDT", pos)
        assert decision.is_hold

    @pytest.mark.asyncio
    async def test_hold_signal_passes_through(self, setup):
        """HOLD 시그널 → hold 결정."""
        setup["strategy"].evaluate.return_value = StrategyDecision(
            direction=Direction.FLAT,
            confidence=0.0,
            sizing_factor=0.0,
            stop_loss_atr=0.0,
            take_profit_atr=0.0,
            reason="no_signal",
            strategy_name="mean_reversion",
        )

        decision = await setup["evaluator"].evaluate("BTC/USDT", None)
        assert decision.is_hold

    @pytest.mark.asyncio
    async def test_no_regime_returns_hold(self, setup):
        """레짐 없으면 hold."""
        setup["regime"]._current = None
        decision = await setup["evaluator"].evaluate("BTC/USDT", None)
        assert decision.is_hold
        assert "no_regime" in decision.reason

    @pytest.mark.asyncio
    async def test_candle_error_returns_hold(self, setup):
        """캔들 에러 시 hold."""
        setup["market_data"].get_ohlcv_df = AsyncMock(return_value=None)
        decision = await setup["evaluator"].evaluate("BTC/USDT", None)
        assert decision.is_hold
        assert "candle_error" in decision.reason

    def test_eval_interval_sec(self, setup):
        assert setup["evaluator"].eval_interval_sec == 60
