"""MeanReversion 전략 테스트."""
import pytest
import pandas as pd
from datetime import datetime, timezone

from strategies.mean_reversion import MeanReversionStrategy
from engine.regime_detector import RegimeState
from core.enums import Direction, Regime


def _regime() -> RegimeState:
    return RegimeState(
        regime=Regime.RANGING, confidence=0.7, adx=18, bb_width=3.0,
        atr_pct=1.5, volume_ratio=1.0, trend_direction=0,
        timestamp=datetime.now(timezone.utc),
    )


def _df(
    n=50,
    close=80000.0,
    bb_upper=82000.0,
    bb_lower=78000.0,
    bb_mid=80000.0,
    rsi=50.0,
    atr=1000.0,
) -> pd.DataFrame:
    return pd.DataFrame({
        "close": [close] * n,
        "bb_upper_20": [bb_upper] * n,
        "bb_lower_20": [bb_lower] * n,
        "bb_mid_20": [bb_mid] * n,
        "rsi_14": [rsi] * n,
        "atr_14": [atr] * n,
        "volume": [1000.0] * n,
    })


def _df_1h_rising(n=50, rsi_start=25.0, rsi_end=30.0) -> pd.DataFrame:
    """1h RSI가 상승하는 DataFrame."""
    rsi_values = [rsi_start] * (n - 1) + [rsi_end]
    return pd.DataFrame({
        "close": [80000.0] * n,
        "rsi_14": rsi_values,
    })


def _df_1h_falling(n=50, rsi_start=75.0, rsi_end=70.0) -> pd.DataFrame:
    """1h RSI가 하락하는 DataFrame."""
    rsi_values = [rsi_start] * (n - 1) + [rsi_end]
    return pd.DataFrame({
        "close": [80000.0] * n,
        "rsi_14": rsi_values,
    })


@pytest.fixture
def strategy():
    return MeanReversionStrategy()


class TestLongEntry:
    @pytest.mark.asyncio
    async def test_bb_lower_touch_oversold(self, strategy):
        """BB 하단 + RSI 과매도 + 1h RSI 반등 → 롱."""
        df = _df(close=78100, bb_upper=82000, bb_lower=78000, bb_mid=80000, rsi=28)
        df_1h = _df_1h_rising(rsi_start=25, rsi_end=30)
        result = await strategy.evaluate(df, df_1h, _regime(), None)
        assert result.direction == Direction.LONG
        assert result.confidence > 0.3

    @pytest.mark.asyncio
    async def test_no_signal_rsi_not_low(self, strategy):
        """BB 하단이지만 RSI 높으면 시그널 없음."""
        df = _df(close=78100, rsi=55)
        df_1h = _df_1h_rising()
        result = await strategy.evaluate(df, df_1h, _regime(), None)
        assert result.is_hold

    @pytest.mark.asyncio
    async def test_no_signal_1h_not_rising(self, strategy):
        """BB 하단 + RSI 과매도지만 1h RSI 미반등 → 시그널 없음."""
        df = _df(close=78100, bb_upper=82000, bb_lower=78000, bb_mid=80000, rsi=28)
        result = await strategy.evaluate(df, df, _regime(), None)
        assert result.is_hold


class TestShortEntry:
    @pytest.mark.asyncio
    async def test_bb_upper_touch_overbought(self, strategy):
        """BB 상단 + RSI 과매수 + 1h RSI 하락 → 숏."""
        df = _df(close=81900, bb_upper=82000, bb_lower=78000, bb_mid=80000, rsi=72)
        df_1h = _df_1h_falling(rsi_start=75, rsi_end=70)
        result = await strategy.evaluate(df, df_1h, _regime(), None)
        assert result.direction == Direction.SHORT

    @pytest.mark.asyncio
    async def test_no_signal_rsi_not_high(self, strategy):
        """BB 상단이지만 RSI 낮으면 시그널 없음."""
        df = _df(close=81900, rsi=50)
        df_1h = _df_1h_falling()
        result = await strategy.evaluate(df, df_1h, _regime(), None)
        assert result.is_hold


class TestExit:
    @pytest.mark.asyncio
    async def test_long_exit_at_mid(self, strategy):
        """롱 포지션 + BB 중앙 도달 → 청산."""
        df = _df(close=80500, bb_upper=82000, bb_lower=78000, bb_mid=80000)
        result = await strategy.evaluate(df, df, _regime(), Direction.LONG)
        assert result.direction == Direction.FLAT

    @pytest.mark.asyncio
    async def test_short_exit_at_mid(self, strategy):
        """숏 포지션 + BB 중앙 이하 → 청산."""
        df = _df(close=79500, bb_upper=82000, bb_lower=78000, bb_mid=80000)
        result = await strategy.evaluate(df, df, _regime(), Direction.SHORT)
        assert result.direction == Direction.FLAT


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_insufficient_data(self, strategy):
        df = _df(n=5)
        result = await strategy.evaluate(df, df, _regime(), None)
        assert result.is_hold

    @pytest.mark.asyncio
    async def test_zero_bb_range(self, strategy):
        """BB range가 0이면 hold."""
        df = _df(bb_upper=80000, bb_lower=80000, bb_mid=80000)
        result = await strategy.evaluate(df, df, _regime(), None)
        assert result.is_hold

    @pytest.mark.asyncio
    async def test_tight_sl_tp(self, strategy):
        """횡보장 SL/TP는 타이트."""
        df = _df(close=78100, bb_upper=82000, bb_lower=78000, bb_mid=80000, rsi=28)
        df_1h = _df_1h_rising(rsi_start=25, rsi_end=30)
        result = await strategy.evaluate(df, df_1h, _regime(), None)
        assert result.stop_loss_atr == 1.5
        assert result.take_profit_atr == 2.0
