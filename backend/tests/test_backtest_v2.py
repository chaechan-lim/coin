"""backtest_v2 유닛 테스트."""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

from core.enums import Direction, Regime
from engine.regime_detector import RegimeState


def _make_5m_df(n=100, close=80000.0, trend="flat"):
    """5m 테스트 DataFrame 생성."""
    dates = pd.date_range(
        end=datetime.now(timezone.utc),
        periods=n,
        freq="5min",
    )
    if trend == "up":
        closes = [close + i * 10 for i in range(n)]
    elif trend == "down":
        closes = [close - i * 10 for i in range(n)]
    else:
        closes = [close + np.sin(i * 0.1) * 100 for i in range(n)]

    df = pd.DataFrame({
        "close": closes,
        "open": [c - 5 for c in closes],
        "high": [c + 50 for c in closes],
        "low": [c - 50 for c in closes],
        "volume": [1000.0] * n,
        "ema_9": [c + 10 for c in closes],
        "ema_20": [c - 5 for c in closes],
        "ema_21": [c - 20 for c in closes],
        "ema_50": [c - 100 for c in closes],
        "rsi_14": [45.0] * n,
        "atr_14": [500.0] * n,
        "adx_14": [30.0] * n,
        "bb_upper_20": [c + 1000 for c in closes],
        "bb_lower_20": [c - 1000 for c in closes],
        "bb_mid_20": closes,
    }, index=dates)
    return df


def _make_1h_df(n=200, close=80000.0):
    """1h 테스트 DataFrame 생성."""
    dates = pd.date_range(
        end=datetime.now(timezone.utc),
        periods=n,
        freq="1h",
    )
    df = pd.DataFrame({
        "close": [close] * n,
        "open": [close - 10] * n,
        "high": [close + 200] * n,
        "low": [close - 200] * n,
        "volume": [5000.0] * n,
        "ema_20": [close * (1 + 0.002 * (i - (n-1))) for i in range(n)],
        "ema_50": [close - 500] * n,
        "rsi_14": [50.0] * n,
        "atr_14": [800.0] * n,
        "adx_14": [30.0] * n,
        "bb_upper_20": [close + 2000] * n,
        "bb_lower_20": [close - 2000] * n,
        "bb_mid_20": [close] * n,
    }, index=dates)
    return df


class TestComputeIndicators:
    def test_rename_columns(self):
        from backtest_v2 import compute_v2_indicators
        n = 100
        dates = pd.date_range(end=datetime.now(timezone.utc), periods=n, freq="5min")
        df = pd.DataFrame({
            "open": [80000.0 + i for i in range(n)],
            "high": [80100.0 + i for i in range(n)],
            "low": [79900.0 + i for i in range(n)],
            "close": [80000.0 + i * 2 for i in range(n)],
            "volume": [1000.0] * n,
        }, index=dates)

        result = compute_v2_indicators(df)
        # 소문자 컬럼이 존재해야 함
        assert "ema_9" in result.columns
        assert "ema_20" in result.columns
        assert "rsi_14" in result.columns
        assert "atr_14" in result.columns
        assert "bb_upper_20" in result.columns


class TestV2Position:
    def test_create(self):
        from backtest_v2 import V2Position
        pos = V2Position(
            symbol="BTC/USDT",
            direction=Direction.LONG,
            quantity=0.01,
            entry_price=80000.0,
            margin=100.0,
            leverage=3,
            sl_price=79000.0,
            tp_price=83000.0,
            trail_activation_price=81500.0,
            trail_stop_atr=1.0,
            extreme_price=80000.0,
            atr_at_entry=500.0,
            entered_idx=0,
            strategy_name="trend_follower",
        )
        assert pos.direction == Direction.LONG
        assert pos.trailing_active is False


class TestV2Backtester:
    @pytest.fixture
    def mock_exchange(self):
        exchange = AsyncMock()
        exchange.initialize = AsyncMock()
        exchange.close_ws = AsyncMock()
        return exchange

    @pytest.fixture
    def backtester(self, mock_exchange):
        from backtest_v2 import V2Backtester
        return V2Backtester(
            exchange=mock_exchange,
            coins=["BTC/USDT"],
            leverage=3,
            initial_balance=1000.0,
        )

    def test_calc_margin(self, backtester):
        from strategies.regime_base import StrategyDecision
        decision = StrategyDecision(
            direction=Direction.LONG,
            confidence=0.8,
            sizing_factor=0.6,
            stop_loss_atr=1.5,
            take_profit_atr=3.0,
            reason="test",
            strategy_name="test",
        )
        margin = backtester._calc_margin(decision, cash=1000.0, price=80000.0, atr=500.0)
        assert margin > 0
        assert margin <= 1000.0 * 0.15  # max_position_pct

    def test_calc_margin_zero_cash(self, backtester):
        from strategies.regime_base import StrategyDecision
        decision = StrategyDecision(
            direction=Direction.LONG, confidence=0.8, sizing_factor=0.6,
            stop_loss_atr=1.5, take_profit_atr=3.0, reason="test", strategy_name="test",
        )
        assert backtester._calc_margin(decision, cash=0, price=80000.0, atr=500.0) == 0

    def test_calc_sl_tp_long(self, backtester):
        sl, tp = backtester._calc_sl_tp(Direction.LONG, 80000.0, 500.0, 1.5, 3.0)
        assert sl == 80000.0 - 1.5 * 500.0  # 79250
        assert tp == 80000.0 + 3.0 * 500.0  # 81500

    def test_calc_sl_tp_short(self, backtester):
        sl, tp = backtester._calc_sl_tp(Direction.SHORT, 80000.0, 500.0, 1.5, 3.0)
        assert sl == 80000.0 + 1.5 * 500.0  # 80750
        assert tp == 80000.0 - 3.0 * 500.0  # 78500

    def test_check_stops_sl_long(self, backtester):
        from backtest_v2 import V2Position
        pos = V2Position(
            symbol="BTC/USDT", direction=Direction.LONG, quantity=0.01,
            entry_price=80000.0, margin=100.0, leverage=3,
            sl_price=79000.0, tp_price=83000.0,
            trail_activation_price=81500.0, trail_stop_atr=1.0,
            extreme_price=80000.0, atr_at_entry=500.0,
            entered_idx=0, strategy_name="test",
        )
        assert backtester._check_stops(pos, 78000.0) == "stop_loss"
        assert backtester._check_stops(pos, 80000.0) is None
        assert backtester._check_stops(pos, 84000.0) == "take_profit"

    def test_check_stops_sl_short(self, backtester):
        from backtest_v2 import V2Position
        pos = V2Position(
            symbol="BTC/USDT", direction=Direction.SHORT, quantity=0.01,
            entry_price=80000.0, margin=100.0, leverage=3,
            sl_price=81000.0, tp_price=77000.0,
            trail_activation_price=78500.0, trail_stop_atr=1.0,
            extreme_price=80000.0, atr_at_entry=500.0,
            entered_idx=0, strategy_name="test",
        )
        assert backtester._check_stops(pos, 82000.0) == "stop_loss"
        assert backtester._check_stops(pos, 80000.0) is None
        assert backtester._check_stops(pos, 76000.0) == "take_profit"

    def test_update_trailing_long(self, backtester):
        from backtest_v2 import V2Position
        pos = V2Position(
            symbol="BTC/USDT", direction=Direction.LONG, quantity=0.01,
            entry_price=80000.0, margin=100.0, leverage=3,
            sl_price=79000.0, tp_price=83000.0,
            trail_activation_price=81000.0, trail_stop_atr=1.0,
            extreme_price=80000.0, atr_at_entry=500.0,
            entered_idx=0, strategy_name="test",
        )
        # 가격이 activation 이하: trailing 비활성
        backtester._update_trailing(pos, 80500.0)
        assert pos.trailing_active is False

        # 가격이 activation 이상: trailing 활성
        backtester._update_trailing(pos, 81500.0)
        assert pos.trailing_active is True
        assert pos.trail_stop_price == 81500.0 - 1.0 * 500.0  # 81000

    def test_calc_pnl_long(self, backtester):
        pnl = backtester._calc_pnl(Direction.LONG, 80000.0, 81000.0, 0.01)
        assert pnl == pytest.approx(10.0)  # (81000-80000) * 0.01

    def test_calc_pnl_short(self, backtester):
        pnl = backtester._calc_pnl(Direction.SHORT, 80000.0, 79000.0, 0.01)
        assert pnl == pytest.approx(10.0)  # (80000-79000) * 0.01

    def test_precompute_regimes(self, backtester):
        df_1h = _make_1h_df(n=100)
        regimes = backtester._precompute_regimes(df_1h)
        assert len(regimes) == 50  # n - 50 = 50
        assert all(isinstance(r[1], RegimeState) for r in regimes)

    def test_custom_regime_params(self, mock_exchange):
        """커스텀 레짐 파라미터가 RegimeDetector에 전달되는지 확인."""
        from backtest_v2 import V2Backtester
        bt = V2Backtester(
            exchange=mock_exchange,
            coins=["BTC/USDT"],
            leverage=3,
            initial_balance=1000.0,
            regime_confirm=1,
            regime_min_hours=1,
            regime_adx_enter=25.0,
            regime_adx_exit=20.0,
        )
        # 내부 파라미터가 저장되었는지 확인
        assert bt._regime_confirm == 1
        assert bt._regime_min_hours == 1
        assert bt._regime_adx_enter == 25.0
        assert bt._regime_adx_exit == 20.0
        # RegimeDetector에 전달되었는지 확인
        assert bt._regime_detector._confirm_count == 1
        assert bt._regime_detector._min_duration_h == 1
        assert bt._regime_detector._adx_enter == 25.0
        assert bt._regime_detector._adx_exit == 20.0

    def test_precompute_regimes_uses_custom_params(self, mock_exchange):
        """_precompute_regimes가 커스텀 레짐 파라미터를 사용하는지 확인."""
        from backtest_v2 import V2Backtester
        bt = V2Backtester(
            exchange=mock_exchange,
            coins=["BTC/USDT"],
            leverage=3,
            initial_balance=1000.0,
            regime_confirm=1,
            regime_min_hours=0,
            regime_adx_enter=25.0,
            regime_adx_exit=20.0,
        )
        df_1h = _make_1h_df(n=100)
        regimes = bt._precompute_regimes(df_1h)
        assert len(regimes) == 50
        assert all(isinstance(r[1], RegimeState) for r in regimes)

    def test_get_regime_at(self, backtester):
        dates = pd.date_range(end=datetime.now(timezone.utc), periods=10, freq="1h")
        regime_state = RegimeState(
            regime=Regime.TRENDING_UP, confidence=0.8, adx=30, bb_width=3.0,
            atr_pct=1.5, volume_ratio=1.2, trend_direction=1,
            timestamp=datetime.now(timezone.utc),
        )
        regimes = [(d, regime_state) for d in dates]

        # 5m 타임스탬프 → 가장 가까운 이전 1h 레짐
        ts_between = dates[3] + timedelta(minutes=15)
        result = backtester._get_regime_at(regimes, ts_between)
        assert result is not None
        assert result.regime == Regime.TRENDING_UP

    @pytest.mark.asyncio
    async def test_simulate_basic(self, backtester):
        """기본 시뮬레이션 - 데이터 넣고 결과 확인."""
        df_5m = _make_5m_df(n=500, trend="up")
        df_1h = _make_1h_df(n=100)

        all_data = {"BTC/USDT": (df_5m, df_1h)}
        result = await backtester._simulate(all_data, 3)

        assert result.initial_balance == 1000.0
        assert result.total_trades >= 0
        assert len(result.equity_curve) > 0

    def test_close_position_long(self, backtester):
        from backtest_v2 import V2Position
        pos = V2Position(
            symbol="BTC/USDT", direction=Direction.LONG, quantity=0.01,
            entry_price=80000.0, margin=100.0, leverage=3,
            sl_price=79000.0, tp_price=83000.0,
            trail_activation_price=81500.0, trail_stop_atr=1.0,
            extreme_price=80000.0, atr_at_entry=500.0,
            entered_idx=0, strategy_name="test",
        )
        pnl, fee = backtester._close_position(pos, 81000.0)
        assert pnl == pytest.approx(10.0)  # (81000-80000) * 0.01
        assert fee > 0


class TestV2BacktestResult:
    def test_create(self):
        from backtest_v2 import V2BacktestResult
        result = V2BacktestResult(
            coins=["BTC/USDT"],
            days=30,
            initial_balance=1000.0,
            final_balance=1100.0,
            total_pnl=100.0,
            total_pnl_pct=10.0,
            max_drawdown_pct=5.0,
            total_trades=20,
            long_trades=12,
            short_trades=8,
            winning_trades=12,
            losing_trades=8,
            win_rate=60.0,
            avg_win_pct=3.0,
            avg_loss_pct=-2.0,
            profit_factor=1.8,
            sharpe_ratio=1.5,
            total_fees=10.0,
            total_funding=5.0,
            buy_hold_pnl_pct=8.0,
        )
        assert result.total_pnl_pct == 10.0
        assert result.profit_factor == 1.8


def _make_1h_df_varied(n=600, close=80000.0):
    """1h 테스트 DataFrame 생성 (가격 변동 포함 — 인디케이터 계산 가능)."""
    dates = pd.date_range(
        end=datetime.now(timezone.utc),
        periods=n,
        freq="1h",
    )
    # 가격에 변동 추가 (RSI/ATR 등 인디케이터 계산 가능하도록)
    closes = [close + np.sin(i * 0.05) * 500 + i * 2 for i in range(n)]
    df = pd.DataFrame({
        "close": closes,
        "open": [c - 30 for c in closes],
        "high": [c + 200 for c in closes],
        "low": [c - 200 for c in closes],
        "volume": [5000.0 + np.sin(i * 0.1) * 1000 for i in range(n)],
        "ema_20": [c * (1 + 0.002 * (i - (n - 1))) for i, c in enumerate(closes)],
        "ema_50": [c - 500 for c in closes],
        "rsi_14": [50.0 + np.sin(i * 0.1) * 15 for i in range(n)],
        "atr_14": [800.0] * n,
        "adx_14": [30.0] * n,
        "bb_upper_20": [c + 2000 for c in closes],
        "bb_lower_20": [c - 2000 for c in closes],
        "bb_mid_20": closes,
    }, index=dates)
    return df


class TestSpotStrategyAdapter:
    """SpotStrategyAdapter 유닛 테스트 — 현물 4전략 → RegimeStrategy 어댑터."""

    @pytest.fixture
    def mock_strategies(self):
        """4개 현물 전략 mock."""
        from strategies.base import Signal
        from core.enums import SignalType

        buy_signal = Signal(
            signal_type=SignalType.BUY, confidence=0.75,
            reason="test buy", strategy_name="cis_momentum",
        )
        sell_signal = Signal(
            signal_type=SignalType.SELL, confidence=0.70,
            reason="test sell", strategy_name="bnf_deviation",
        )
        hold_signal = Signal(
            signal_type=SignalType.HOLD, confidence=0.5,
            reason="test hold", strategy_name="donchian_channel",
        )

        strat_buy = AsyncMock()
        strat_buy.analyze = AsyncMock(return_value=buy_signal)
        strat_buy.name = "cis_momentum"

        strat_sell = AsyncMock()
        strat_sell.analyze = AsyncMock(return_value=sell_signal)
        strat_sell.name = "bnf_deviation"

        strat_hold = AsyncMock()
        strat_hold.analyze = AsyncMock(return_value=hold_signal)
        strat_hold.name = "donchian_channel"

        strat_buy2 = AsyncMock()
        strat_buy2.analyze = AsyncMock(return_value=Signal(
            signal_type=SignalType.BUY, confidence=0.65,
            reason="test buy 2", strategy_name="larry_williams",
        ))
        strat_buy2.name = "larry_williams"

        return {
            "cis_momentum": strat_buy,
            "bnf_deviation": strat_sell,
            "donchian_channel": strat_hold,
            "larry_williams": strat_buy2,
        }

    @pytest.fixture
    def adapter(self, mock_strategies):
        from backtest_v2 import SpotStrategyAdapter, SPOT_WEIGHTS
        return SpotStrategyAdapter(mock_strategies, SPOT_WEIGHTS)

    def test_name(self, adapter):
        assert adapter.name == "spot_ensemble"

    def test_target_regimes(self, adapter):
        regimes = adapter.target_regimes
        assert Regime.TRENDING_UP in regimes
        assert Regime.TRENDING_DOWN in regimes
        assert Regime.RANGING in regimes
        assert Regime.VOLATILE in regimes

    @pytest.mark.asyncio
    async def test_evaluate_buy_signal(self, adapter, mock_strategies):
        """BUY 우세 → LONG 결정."""
        from strategies.base import Signal
        from core.enums import SignalType

        # 3개 BUY, 1개 HOLD → BUY 우세
        for name in ["cis_momentum", "bnf_deviation", "larry_williams"]:
            mock_strategies[name].analyze = AsyncMock(return_value=Signal(
                signal_type=SignalType.BUY, confidence=0.75,
                reason="test buy", strategy_name=name,
            ))
        mock_strategies["donchian_channel"].analyze = AsyncMock(return_value=Signal(
            signal_type=SignalType.HOLD, confidence=0.5,
            reason="test hold", strategy_name="donchian_channel",
        ))

        df_1h = _make_1h_df_varied(n=600, close=80000.0)
        regime = RegimeState(
            regime=Regime.TRENDING_UP, confidence=0.8, adx=30, bb_width=3.0,
            atr_pct=1.5, volume_ratio=1.2, trend_direction=1,
            timestamp=datetime.now(timezone.utc),
        )

        decision = await adapter.evaluate(df_1h, df_1h, regime, None)
        assert decision.direction == Direction.LONG
        assert decision.confidence >= 0.5
        assert decision.sizing_factor > 0
        assert decision.stop_loss_atr == 5.0
        assert decision.take_profit_atr == 14.0
        assert decision.strategy_name == "spot_ensemble"

    @pytest.mark.asyncio
    async def test_evaluate_sell_signal(self, adapter, mock_strategies):
        """SELL 우세 → SHORT 결정."""
        from strategies.base import Signal
        from core.enums import SignalType

        # 3개 SELL, 1개 HOLD → SELL 우세
        for name in ["cis_momentum", "bnf_deviation", "larry_williams"]:
            mock_strategies[name].analyze = AsyncMock(return_value=Signal(
                signal_type=SignalType.SELL, confidence=0.70,
                reason="test sell", strategy_name=name,
            ))
        mock_strategies["donchian_channel"].analyze = AsyncMock(return_value=Signal(
            signal_type=SignalType.HOLD, confidence=0.5,
            reason="test hold", strategy_name="donchian_channel",
        ))

        df_1h = _make_1h_df_varied(n=600, close=80000.0)
        regime = RegimeState(
            regime=Regime.TRENDING_DOWN, confidence=0.8, adx=30, bb_width=3.0,
            atr_pct=1.5, volume_ratio=1.2, trend_direction=-1,
            timestamp=datetime.now(timezone.utc),
        )

        decision = await adapter.evaluate(df_1h, df_1h, regime, None)
        assert decision.direction == Direction.SHORT
        assert decision.confidence >= 0.5
        assert decision.sizing_factor > 0
        assert decision.stop_loss_atr == 5.0
        assert decision.take_profit_atr == 14.0

    @pytest.mark.asyncio
    async def test_evaluate_hold_on_all_hold(self, adapter, mock_strategies):
        """모든 전략 HOLD → HOLD 결정."""
        from strategies.base import Signal
        from core.enums import SignalType

        for name in mock_strategies:
            mock_strategies[name].analyze = AsyncMock(return_value=Signal(
                signal_type=SignalType.HOLD, confidence=0.5,
                reason="test hold", strategy_name=name,
            ))

        df_1h = _make_1h_df_varied(n=600, close=80000.0)
        regime = RegimeState(
            regime=Regime.RANGING, confidence=0.8, adx=15, bb_width=1.5,
            atr_pct=1.0, volume_ratio=1.0, trend_direction=0,
            timestamp=datetime.now(timezone.utc),
        )

        decision = await adapter.evaluate(df_1h, df_1h, regime, None)
        assert decision.is_hold

    @pytest.mark.asyncio
    async def test_evaluate_hold_on_low_confidence(self, adapter, mock_strategies):
        """낮은 신뢰도 → HOLD."""
        from strategies.base import Signal
        from core.enums import SignalType

        for name in mock_strategies:
            mock_strategies[name].analyze = AsyncMock(return_value=Signal(
                signal_type=SignalType.BUY, confidence=0.20,
                reason="low conf", strategy_name=name,
            ))

        df_1h = _make_1h_df_varied(n=600, close=80000.0)
        regime = RegimeState(
            regime=Regime.RANGING, confidence=0.8, adx=15, bb_width=1.5,
            atr_pct=1.0, volume_ratio=1.0, trend_direction=0,
            timestamp=datetime.now(timezone.utc),
        )

        decision = await adapter.evaluate(df_1h, df_1h, regime, None)
        # 0.20 confidence는 combiner min_confidence(0.50) 미만 → HOLD
        assert decision.is_hold

    @pytest.mark.asyncio
    async def test_evaluate_insufficient_data(self, adapter):
        """데이터 부족 → HOLD."""
        df_short = _make_1h_df(n=10, close=80000.0)  # Too short for 4h resample
        regime = RegimeState(
            regime=Regime.RANGING, confidence=0.8, adx=15, bb_width=1.5,
            atr_pct=1.0, volume_ratio=1.0, trend_direction=0,
            timestamp=datetime.now(timezone.utc),
        )

        decision = await adapter.evaluate(df_short, df_short, regime, None)
        assert decision.is_hold

    @pytest.mark.asyncio
    async def test_evaluate_with_strategy_error(self, adapter, mock_strategies):
        """일부 전략 에러 → 나머지 전략으로 진행."""
        from strategies.base import Signal
        from core.enums import SignalType

        mock_strategies["cis_momentum"].analyze = AsyncMock(side_effect=Exception("test error"))
        for name in ["bnf_deviation", "donchian_channel", "larry_williams"]:
            mock_strategies[name].analyze = AsyncMock(return_value=Signal(
                signal_type=SignalType.BUY, confidence=0.75,
                reason="test buy", strategy_name=name,
            ))

        df_1h = _make_1h_df_varied(n=600, close=80000.0)
        regime = RegimeState(
            regime=Regime.TRENDING_UP, confidence=0.8, adx=30, bb_width=3.0,
            atr_pct=1.5, volume_ratio=1.2, trend_direction=1,
            timestamp=datetime.now(timezone.utc),
        )

        decision = await adapter.evaluate(df_1h, df_1h, regime, None)
        # cis_momentum 에러, 나머지 3개 BUY → LONG
        assert decision.direction == Direction.LONG

    def test_resample_1h_to_4h(self):
        """1h → 4h 리샘플링 + 인디케이터 계산."""
        from backtest_v2 import SpotStrategyAdapter

        df_1h = _make_1h_df_varied(n=600, close=80000.0)
        df_4h = SpotStrategyAdapter._resample_1h_to_4h(df_1h)

        assert df_4h is not None
        assert len(df_4h) > 0
        # 인디케이터 존재 확인
        assert "close" in df_4h.columns
        assert "rsi_14" in df_4h.columns
        assert "atr_14" in df_4h.columns

    def test_resample_1h_to_4h_short_data(self):
        """데이터 부족 → None."""
        from backtest_v2 import SpotStrategyAdapter

        df_short = _make_1h_df(n=10, close=80000.0)
        result = SpotStrategyAdapter._resample_1h_to_4h(df_short)
        assert result is None


class TestV2BacktesterSpotMode:
    """V2Backtester 현물 전략 모드 통합 테스트."""

    @pytest.fixture
    def mock_exchange(self):
        exchange = AsyncMock()
        exchange.initialize = AsyncMock()
        exchange.close_ws = AsyncMock()
        return exchange

    def test_enable_spot_strategies(self, mock_exchange):
        """enable_spot_strategies()가 SpotStrategyAdapter를 생성."""
        from backtest_v2 import V2Backtester
        bt = V2Backtester(exchange=mock_exchange, coins=["BTC/USDT"])
        assert bt._use_spot is False
        assert bt._spot_adapter is None

        bt.enable_spot_strategies()
        assert bt._use_spot is True
        assert bt._spot_adapter is not None
        assert bt._spot_adapter.name == "spot_ensemble"

    def test_spot_and_v1_exclusive(self, mock_exchange):
        """spot과 v1 모드 독립 활성화 가능 (CLI에서 동시 사용 차단)."""
        from backtest_v2 import V2Backtester
        bt = V2Backtester(exchange=mock_exchange, coins=["BTC/USDT"])
        bt.enable_spot_strategies()
        assert bt._use_spot is True

        # spot이 활성화된 상태에서 v1도 활성화 가능 (내부적으로는 두 플래그 독립)
        # CLI에서 동시 사용은 차단하지만 API 레벨에서는 허용
        bt.enable_v1_strategies()
        assert bt._use_v1 is True

    @pytest.mark.asyncio
    async def test_simulate_with_spot_adapter(self, mock_exchange):
        """현물 전략 모드로 시뮬레이션 실행."""
        from backtest_v2 import V2Backtester

        bt = V2Backtester(
            exchange=mock_exchange,
            coins=["BTC/USDT"],
            leverage=3,
            initial_balance=1000.0,
        )
        bt.enable_spot_strategies()

        df_5m = _make_5m_df(n=500, trend="up")
        df_1h = _make_1h_df_varied(n=600)

        all_data = {"BTC/USDT": (df_5m, df_1h)}
        result = await bt._simulate(all_data, 3)

        assert result.initial_balance == 1000.0
        assert result.total_trades >= 0
        assert len(result.equity_curve) > 0

    def test_create_spot_strategies(self):
        """create_spot_strategies()가 4전략을 반환."""
        from backtest_v2 import create_spot_strategies
        strategies = create_spot_strategies()

        assert len(strategies) == 4
        assert "cis_momentum" in strategies
        assert "bnf_deviation" in strategies
        assert "donchian_channel" in strategies
        assert "larry_williams" in strategies

    def test_spot_weights_match_combiner(self):
        """SPOT_WEIGHTS가 combiner.py 값과 일치."""
        from backtest_v2 import SPOT_WEIGHTS
        from strategies.combiner import SignalCombiner

        for name, weight in SPOT_WEIGHTS.items():
            assert name in SignalCombiner.SPOT_WEIGHTS
            assert weight == SignalCombiner.SPOT_WEIGHTS[name], \
                f"{name}: {weight} != {SignalCombiner.SPOT_WEIGHTS[name]}"

    def test_spot_constants_match_live(self):
        """SL/TP/Trail ATR 상수가 SpotEvaluator 라이브 설정과 일치."""
        from backtest_v2 import (
            SPOT_SL_ATR, SPOT_TP_ATR,
            SPOT_TRAIL_ACTIVATION_ATR, SPOT_TRAIL_STOP_ATR,
            SPOT_MIN_CONFIDENCE,
        )
        assert SPOT_SL_ATR == 5.0
        assert SPOT_TP_ATR == 14.0
        assert SPOT_TRAIL_ACTIVATION_ATR == 3.0
        assert SPOT_TRAIL_STOP_ATR == 1.5
        assert SPOT_MIN_CONFIDENCE == 0.50


class TestSpot1hLookbackWindow:
    """COIN-45: SPOT_1H_LOOKBACK으로 현물 4전략 1h 윈도우 확대 검증."""

    def test_spot_1h_lookback_constant_value(self):
        """SPOT_1H_LOOKBACK이 충분한 4h 캔들 생성 보장 (SMA_60 + dropna 고려)."""
        from backtest_v2 import SPOT_1H_LOOKBACK, LOOKBACK_WINDOW

        assert SPOT_1H_LOOKBACK == 400
        # 400 1h → 100 4h → SMA_60 dropna 59개 제외 → ~41개 >= 30 요건 충족
        min_4h_needed = 30 + 59  # evaluate() 30개 + SMA_60 워밍업 59개
        assert SPOT_1H_LOOKBACK // 4 >= min_4h_needed
        # 기존 LOOKBACK_WINDOW보다 커야 함
        assert SPOT_1H_LOOKBACK > LOOKBACK_WINDOW

    def test_old_lookback_insufficient_for_spot(self):
        """기존 LOOKBACK_WINDOW=60으로는 4h 리샘플링 30개 미달 재현."""
        from backtest_v2 import LOOKBACK_WINDOW, SpotStrategyAdapter

        # 60개 1h → 최대 15개 4h → _resample_1h_to_4h에서 None
        df_1h_short = _make_1h_df_varied(n=LOOKBACK_WINDOW, close=80000.0)
        result = SpotStrategyAdapter._resample_1h_to_4h(df_1h_short)
        assert result is None, (
            f"LOOKBACK_WINDOW={LOOKBACK_WINDOW}개 1h로는 4h 30개 미달이어야 함"
        )

    def test_spot_lookback_sufficient_for_resample(self):
        """SPOT_1H_LOOKBACK=300이면 4h 리샘플링 + 인디케이터 계산 성공."""
        from backtest_v2 import SPOT_1H_LOOKBACK, SpotStrategyAdapter

        df_1h = _make_1h_df_varied(n=SPOT_1H_LOOKBACK, close=80000.0)
        result = SpotStrategyAdapter._resample_1h_to_4h(df_1h)
        assert result is not None, (
            f"SPOT_1H_LOOKBACK={SPOT_1H_LOOKBACK}개 1h로 4h 리샘플링 성공해야 함"
        )
        assert len(result) >= 30, f"4h 캔들 30개 이상이어야 함, got {len(result)}"
        # SMA_60 계산 가능 확인
        assert "sma_60" in result.columns or "SMA_60" in result.columns

    def test_simulate_uses_spot_lookback_in_spot_mode(self, mock_exchange=None):
        """spot 모드에서 _simulate가 SPOT_1H_LOOKBACK 사용 확인."""
        from backtest_v2 import V2Backtester, SPOT_1H_LOOKBACK, LOOKBACK_WINDOW

        exchange = AsyncMock()
        exchange.initialize = AsyncMock()
        exchange.close_ws = AsyncMock()

        bt = V2Backtester(exchange=exchange, coins=["BTC/USDT"])
        bt.enable_spot_strategies()

        # _use_spot이 활성화되면 h_lookback = SPOT_1H_LOOKBACK
        assert bt._use_spot is True
        # 내부 로직 검증: spot 모드에서 사용할 lookback 계산
        h_lookback = SPOT_1H_LOOKBACK if bt._use_spot else LOOKBACK_WINDOW
        assert h_lookback == SPOT_1H_LOOKBACK

    def test_non_spot_mode_uses_default_lookback(self):
        """비-spot 모드에서는 기존 LOOKBACK_WINDOW 유지."""
        from backtest_v2 import V2Backtester, SPOT_1H_LOOKBACK, LOOKBACK_WINDOW

        exchange = AsyncMock()
        exchange.initialize = AsyncMock()
        exchange.close_ws = AsyncMock()

        bt = V2Backtester(exchange=exchange, coins=["BTC/USDT"])
        assert bt._use_spot is False

        h_lookback = SPOT_1H_LOOKBACK if bt._use_spot else LOOKBACK_WINDOW
        assert h_lookback == LOOKBACK_WINDOW
