"""VolBreakout 전략 테스트."""
import pytest
import pandas as pd
from datetime import datetime, timezone

from strategies.vol_breakout import VolBreakoutStrategy
from engine.regime_detector import RegimeState
from core.enums import Direction, Regime


def _regime() -> RegimeState:
    return RegimeState(
        regime=Regime.VOLATILE, confidence=0.7, adx=22, bb_width=8.0,
        atr_pct=4.5, volume_ratio=2.0, trend_direction=0,
        timestamp=datetime.now(timezone.utc),
    )


def _df(
    n=50,
    close=80000.0,
    ema_20=80000.0,
    atr=1000.0,
    volume=2000.0,
    vol_avg=1000.0,
    rsi=55.0,
) -> pd.DataFrame:
    data = {
        "close": [close] * n,
        "ema_20": [ema_20] * n,
        "atr_14": [atr] * n,
        "rsi_14": [rsi] * n,
        "volume": [vol_avg] * (n - 1) + [volume],  # 마지막만 높음
    }
    return pd.DataFrame(data)


@pytest.fixture
def strategy():
    return VolBreakoutStrategy()


class TestLongBreakout:
    @pytest.mark.asyncio
    async def test_kc_upper_breakout(self, strategy):
        """KC 상단 돌파 + 거래량 → 롱."""
        # KC upper = 80000 + 2*1000 = 82000, close > 82000
        df = _df(close=82500, ema_20=80000, atr=1000, volume=3000, vol_avg=1000)
        result = await strategy.evaluate(df, df, _regime(), None)
        assert result.direction == Direction.LONG
        assert result.sizing_factor > 0
        assert result.stop_loss_atr == 1.8
        assert result.take_profit_atr == 3.5

    @pytest.mark.asyncio
    async def test_no_breakout_low_volume(self, strategy):
        """가격 돌파했지만 거래량 부족 → 시그널 없음."""
        df = _df(close=82500, ema_20=80000, atr=1000, volume=1000, vol_avg=1000)
        result = await strategy.evaluate(df, df, _regime(), None)
        assert result.is_hold


class TestShortBreakout:
    @pytest.mark.asyncio
    async def test_kc_lower_breakout(self, strategy):
        """KC 하단 돌파 + 거래량 → 숏."""
        # KC lower = 80000 - 2*1000 = 78000, close < 78000
        df = _df(close=77500, ema_20=80000, atr=1000, volume=3000, vol_avg=1000)
        result = await strategy.evaluate(df, df, _regime(), None)
        assert result.direction == Direction.SHORT


class TestBreakoutFailure:
    @pytest.mark.asyncio
    async def test_long_failure(self, strategy):
        """롱 포지션 + 가격 EMA20 아래 + RSI<50 → 청산."""
        df = _df(close=79000, ema_20=80000, rsi=40)
        result = await strategy.evaluate(df, df, _regime(), Direction.LONG)
        assert result.direction == Direction.FLAT
        assert "failure" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_short_failure(self, strategy):
        """숏 포지션 + 가격 EMA20 위 + RSI>50 → 청산."""
        df = _df(close=81000, ema_20=80000, rsi=60)
        result = await strategy.evaluate(df, df, _regime(), Direction.SHORT)
        assert result.direction == Direction.FLAT


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_insufficient_data(self, strategy):
        df = _df(n=5)
        result = await strategy.evaluate(df, df, _regime(), None)
        assert result.is_hold

    @pytest.mark.asyncio
    async def test_zero_ema(self, strategy):
        df = _df(ema_20=0)
        result = await strategy.evaluate(df, df, _regime(), None)
        assert result.is_hold

    @pytest.mark.asyncio
    async def test_zero_atr(self, strategy):
        df = _df(atr=0)
        result = await strategy.evaluate(df, df, _regime(), None)
        assert result.is_hold

    @pytest.mark.asyncio
    async def test_max_sizing_cap(self, strategy):
        """사이징 상한 0.8."""
        df = _df(close=82500, ema_20=80000, atr=1000, volume=10000, vol_avg=1000)
        result = await strategy.evaluate(df, df, _regime(), None)
        assert result.sizing_factor <= 0.8
