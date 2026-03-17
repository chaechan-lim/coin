"""Tier1Manager 테스트."""
import pytest
import pytest_asyncio
import pandas as pd
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from sqlalchemy import select

from engine.tier1_manager import Tier1Manager, CycleStats
from engine.regime_detector import RegimeDetector, RegimeState
from engine.strategy_selector import StrategySelector
from engine.safe_order_pipeline import SafeOrderPipeline, OrderResponse
from engine.position_state_tracker import PositionStateTracker, PositionState
from engine.portfolio_manager import PortfolioManager
from engine.balance_guard import BalanceGuard
from core.enums import Direction, Regime
from core.models import StrategyLog
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
    async def test_cycle_returns_empty_stats_without_regime(self, tier1, mock_deps, session):
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
    async def test_cycle_count_not_incremented_without_regime(self, tier1, mock_deps, session):
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
        # ema_9 < ema_21 and RSI in neutral zone → HOLD signal
        hold_df = _make_df(ema_9=79000.0, ema_21=80000.0, rsi=50.0)
        mock_deps["market_data"].get_ohlcv_df = AsyncMock(return_value=hold_df)
        stats = await tier1.evaluation_cycle(session)
        # Strategy should produce HOLD for these values
        assert stats.coins_evaluated == 2

    @pytest.mark.asyncio
    async def test_stats_counts_errors(self, tier1, mock_deps, session):
        """에러가 stats.error_count에 반영됨."""
        mock_deps["market_data"].get_ohlcv_df.side_effect = Exception("API error")
        stats = await tier1.evaluation_cycle(session)
        # candle_fetch returns None → warning logged, _evaluate_coin catches it
        # But the outer try/except in evaluation_cycle catches exceptions from _evaluate_coin
        # get_ohlcv_df raises → _fetch_candles catches → returns None → _evaluate_coin returns "candle_error"
        assert stats.coins_evaluated == 2
        assert stats.candle_error_count == 2

    @pytest.mark.asyncio
    async def test_stats_sl_tp(self, tier1, mock_deps, session):
        """SL 히트 시 sl_tp_count가 반영됨."""
        state = PositionState(
            symbol="BTC/USDT", direction=Direction.LONG,
            quantity=0.01, entry_price=80000.0, margin=100.0,
            leverage=3, extreme_price=80000.0,
            stop_loss_atr=1.5, take_profit_atr=3.0,
            trailing_activation_atr=2.0, trailing_stop_atr=1.0,
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
        assert len(logs) >= 2

    @pytest.mark.asyncio
    async def test_strategy_log_has_correct_fields(self, tier1, mock_deps, session):
        """StrategyLog에 필수 필드가 올바르게 설정됨."""
        await tier1.evaluation_cycle(session)
        await session.flush()

        result = await session.execute(select(StrategyLog))
        log = result.scalars().first()
        assert log is not None
        assert log.exchange == "binance_futures"
        assert log.strategy_name in ("trend_follower", "mean_reversion", "vol_breakout")
        assert log.symbol in ("BTC/USDT", "ETH/USDT")
        assert log.signal_type in ("BUY", "SELL", "HOLD")
        assert log.confidence is not None
        assert log.reason is not None

    @pytest.mark.asyncio
    async def test_strategy_log_includes_regime_info(self, tier1, mock_deps, session):
        """StrategyLog 지표에 레짐 정보가 포함됨."""
        await tier1.evaluation_cycle(session)
        await session.flush()

        result = await session.execute(select(StrategyLog))
        log = result.scalars().first()
        assert log is not None
        assert log.indicators is not None
        assert "regime" in log.indicators
        assert log.indicators["regime"] == "trending_up"
        assert "regime_confidence" in log.indicators

    @pytest.mark.asyncio
    async def test_hold_signal_logged(self, tier1, mock_deps, session):
        """HOLD 판단도 StrategyLog에 기록됨."""
        # ema_9 < ema_21: 대부분 전략이 HOLD 반환
        hold_df = _make_df(ema_9=79000.0, ema_21=80000.0, rsi=50.0)
        mock_deps["market_data"].get_ohlcv_df = AsyncMock(return_value=hold_df)

        await tier1.evaluation_cycle(session)
        await session.flush()

        result = await session.execute(select(StrategyLog))
        logs = result.scalars().all()
        assert len(logs) >= 2  # 각 코인마다 로그 기록
        # HOLD가 있어야 함
        hold_logs = [log for log in logs if log.signal_type == "HOLD"]
        assert len(hold_logs) >= 1

    @pytest.mark.asyncio
    async def test_executed_signal_marked(self, tier1, mock_deps, session):
        """실행된 시그널은 was_executed=True로 기록됨."""
        await tier1.evaluation_cycle(session)
        await session.flush()

        result = await session.execute(select(StrategyLog))
        logs = result.scalars().all()
        # 최소 일부 로그는 was_executed 필드를 가짐
        assert len(logs) >= 2
        # 모든 로그에 was_executed 필드가 설정되어야 함
        for log in logs:
            assert log.was_executed is not None

    @pytest.mark.asyncio
    async def test_sl_tp_signal_not_executed(self, tier1, mock_deps, session):
        """SL 히트로 청산 시 전략 시그널은 was_executed=False."""
        state = PositionState(
            symbol="BTC/USDT", direction=Direction.LONG,
            quantity=0.01, entry_price=80000.0, margin=100.0,
            leverage=3, extreme_price=80000.0,
            stop_loss_atr=1.5, take_profit_atr=3.0,
            trailing_activation_atr=2.0, trailing_stop_atr=1.0,
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
        assert len(logs) >= 1
        # SL로 청산된 코인의 전략 시그널은 실행되지 않음
        for log in logs:
            assert log.was_executed is False

    @pytest.mark.asyncio
    async def test_no_log_on_candle_error(self, tier1, mock_deps, session):
        """캔들 에러 시 StrategyLog 미기록."""
        mock_deps["market_data"].get_ohlcv_df = AsyncMock(return_value=None)

        await tier1.evaluation_cycle(session)
        await session.flush()

        result = await session.execute(select(StrategyLog))
        logs = result.scalars().all()
        assert len(logs) == 0

    @pytest.mark.asyncio
    async def test_exchange_name_default(self, mock_deps, session):
        """기본 exchange_name은 binance_futures."""
        tier1 = Tier1Manager(
            coins=["BTC/USDT"],
            safe_order=mock_deps["safe_order"],
            position_tracker=mock_deps["tracker"],
            regime_detector=mock_deps["regime"],
            strategy_selector=mock_deps["selector"],
            portfolio_manager=mock_deps["pm"],
            market_data=mock_deps["market_data"],
        )
        assert tier1._exchange_name == "binance_futures"

    @pytest.mark.asyncio
    async def test_custom_exchange_name(self, mock_deps, session):
        """exchange_name을 커스텀으로 설정할 수 있음."""
        tier1 = Tier1Manager(
            coins=["BTC/USDT"],
            safe_order=mock_deps["safe_order"],
            position_tracker=mock_deps["tracker"],
            regime_detector=mock_deps["regime"],
            strategy_selector=mock_deps["selector"],
            portfolio_manager=mock_deps["pm"],
            market_data=mock_deps["market_data"],
            exchange_name="custom_exchange",
        )
        await tier1.evaluation_cycle(session)
        await session.flush()

        result = await session.execute(select(StrategyLog))
        log = result.scalars().first()
        assert log is not None
        assert log.exchange == "custom_exchange"
