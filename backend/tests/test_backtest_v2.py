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
