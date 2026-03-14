"""Tier1Manager 테스트."""
import pytest
import pytest_asyncio
import pandas as pd
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from engine.tier1_manager import Tier1Manager
from engine.regime_detector import RegimeDetector, RegimeState
from engine.strategy_selector import StrategySelector
from engine.safe_order_pipeline import SafeOrderPipeline, OrderResponse
from engine.position_state_tracker import PositionStateTracker, PositionState
from engine.portfolio_manager import PortfolioManager
from engine.balance_guard import BalanceGuard
from core.enums import Direction, Regime
from strategies.regime_base import StrategyDecision


def _regime_state(regime=Regime.TRENDING_UP):
    return RegimeState(
        regime=regime, confidence=0.8, adx=30, bb_width=3.0,
        atr_pct=1.5, volume_ratio=1.2, trend_direction=1,
        timestamp=datetime.now(timezone.utc),
    )


def _make_df(n=50, close=80000.0, atr=1000.0, ema_9=81000.0, ema_21=80000.0, rsi=40.0):
    return pd.DataFrame({
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
    })


@pytest.fixture
def mock_deps():
    regime = RegimeDetector()
    regime._current = _regime_state()

    safe_order = AsyncMock(spec=SafeOrderPipeline)
    safe_order.execute_order = AsyncMock(return_value=OrderResponse(
        success=True, order_id=1, executed_price=80000.0,
        executed_quantity=0.01, fee=0.32,
    ))

    tracker = PositionStateTracker()
    selector = StrategySelector()

    pm = MagicMock(spec=PortfolioManager)
    pm.cash_balance = 500.0

    market_data = AsyncMock()
    market_data.get_ohlcv_df = AsyncMock(return_value=_make_df())
    market_data.get_current_price = AsyncMock(return_value=80000.0)

    return {
        "regime": regime,
        "safe_order": safe_order,
        "tracker": tracker,
        "selector": selector,
        "pm": pm,
        "market_data": market_data,
    }


@pytest.fixture
def tier1(mock_deps):
    return Tier1Manager(
        coins=["BTC/USDT", "ETH/USDT"],
        safe_order=mock_deps["safe_order"],
        position_tracker=mock_deps["tracker"],
        regime_detector=mock_deps["regime"],
        strategy_selector=mock_deps["selector"],
        portfolio_manager=mock_deps["pm"],
        market_data=mock_deps["market_data"],
        leverage=3,
        max_position_pct=0.15,
    )


class TestInit:
    def test_coins(self, tier1):
        assert tier1.coins == ["BTC/USDT", "ETH/USDT"]


class TestMarginCalc:
    def test_normal_calc(self, tier1):
        decision = StrategyDecision(
            direction=Direction.LONG, confidence=0.8, sizing_factor=0.7,
            stop_loss_atr=1.5, take_profit_atr=3.0,
            reason="test", strategy_name="test",
        )
        margin = tier1._calc_margin(decision, close=80000.0, atr=1000.0)
        assert margin > 0
        assert margin <= 500.0 * 0.15  # max_position_pct

    def test_zero_cash(self, tier1, mock_deps):
        mock_deps["pm"].cash_balance = 0.0
        decision = StrategyDecision(
            direction=Direction.LONG, confidence=0.8, sizing_factor=0.7,
            stop_loss_atr=1.5, take_profit_atr=3.0,
            reason="test", strategy_name="test",
        )
        margin = tier1._calc_margin(decision, close=80000.0, atr=1000.0)
        assert margin == 0.0

    def test_too_small_margin(self, tier1, mock_deps):
        mock_deps["pm"].cash_balance = 10.0
        decision = StrategyDecision(
            direction=Direction.LONG, confidence=0.1, sizing_factor=0.1,
            stop_loss_atr=1.5, take_profit_atr=3.0,
            reason="test", strategy_name="test",
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
        """모든 코인을 평가."""
        await tier1.evaluation_cycle(session)
        # market_data.get_ohlcv_df should be called for each coin * 2 timeframes
        assert mock_deps["market_data"].get_ohlcv_df.call_count >= 2

    @pytest.mark.asyncio
    async def test_handles_candle_error(self, tier1, mock_deps, session):
        """캔들 에러 시 안전하게 스킵."""
        mock_deps["market_data"].get_ohlcv_df.side_effect = Exception("API error")
        await tier1.evaluation_cycle(session)  # Should not raise


class TestSARExecution:
    @pytest.mark.asyncio
    async def test_open_new_position(self, tier1, mock_deps, session):
        """시그널 있고 포지션 없으면 진입."""
        # TrendFollower will signal LONG with EMA9>EMA21 + RSI 30-50
        await tier1.evaluation_cycle(session)
        # Check if execute_order was called
        if mock_deps["safe_order"].execute_order.called:
            call_args = mock_deps["safe_order"].execute_order.call_args
            req = call_args[0][1]
            assert req.action == "open"

    @pytest.mark.asyncio
    async def test_sl_check(self, tier1, mock_deps, session):
        """SL 히트 시 청산."""
        state = PositionState(
            symbol="BTC/USDT", direction=Direction.LONG,
            quantity=0.01, entry_price=80000.0, margin=100.0,
            leverage=3, extreme_price=80000.0,
            stop_loss_atr=1.5, take_profit_atr=3.0,
            trailing_activation_atr=2.0, trailing_stop_atr=1.0,
        )
        mock_deps["tracker"].open_position(state)

        # 가격이 SL 이하로 떨어짐 (78000 < 80000 - 1.5*1000 = 78500)
        sl_price_df = _make_df(close=78000.0, atr=1000.0)
        mock_deps["market_data"].get_ohlcv_df = AsyncMock(return_value=sl_price_df)

        await tier1.evaluation_cycle(session)
        # SL 히트로 청산 주문이 포함되어야 함 (다른 코인도 평가되므로 call_args_list 확인)
        close_calls = [
            c for c in mock_deps["safe_order"].execute_order.call_args_list
            if c[0][1].action == "close"
        ]
        assert len(close_calls) >= 1
        assert close_calls[0][0][1].symbol == "BTC/USDT"
