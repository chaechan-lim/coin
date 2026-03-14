"""TrendFollower 전략 테스트."""
import pytest
import pandas as pd
from datetime import datetime, timezone

from strategies.trend_follower import TrendFollowerStrategy
from strategies.regime_base import StrategyDecision
from engine.regime_detector import RegimeState
from core.enums import Direction, Regime


def _regime(regime: Regime = Regime.TRENDING_UP) -> RegimeState:
    return RegimeState(
        regime=regime, confidence=0.8, adx=30, bb_width=3.0,
        atr_pct=1.5, volume_ratio=1.2, trend_direction=1,
        timestamp=datetime.now(timezone.utc),
    )


def _df_5m(
    n=50,
    ema_9=81000.0,
    ema_21=80000.0,
    rsi=40.0,
    atr=1000.0,
    close=80500.0,
) -> pd.DataFrame:
    return pd.DataFrame({
        "close": [close] * n,
        "ema_9": [ema_9] * n,
        "ema_21": [ema_21] * n,
        "rsi_14": [rsi] * n,
        "atr_14": [atr] * n,
        "volume": [1000.0] * n,
    })


@pytest.fixture
def strategy():
    return TrendFollowerStrategy()


class TestProperties:
    def test_name(self, strategy):
        assert strategy.name == "trend_follower"

    def test_target_regimes(self, strategy):
        assert Regime.TRENDING_UP in strategy.target_regimes
        assert Regime.TRENDING_DOWN in strategy.target_regimes


class TestUptrend:
    @pytest.mark.asyncio
    async def test_pullback_buy(self, strategy):
        """상승 추세 + EMA9>EMA21 + RSI 풀백 → 롱."""
        df = _df_5m(ema_9=81000, ema_21=80000, rsi=40)
        result = await strategy.evaluate(df, df, _regime(Regime.TRENDING_UP), None)
        assert result.direction == Direction.LONG
        assert result.confidence > 0.3
        assert result.sizing_factor > 0

    @pytest.mark.asyncio
    async def test_no_signal_rsi_too_high(self, strategy):
        """RSI 70 → 풀백 아님, 시그널 없음."""
        df = _df_5m(ema_9=81000, ema_21=80000, rsi=70)
        result = await strategy.evaluate(df, df, _regime(Regime.TRENDING_UP), None)
        assert result.is_hold

    @pytest.mark.asyncio
    async def test_no_signal_rsi_too_low(self, strategy):
        """RSI 20 → 풀백 아님."""
        df = _df_5m(ema_9=81000, ema_21=80000, rsi=20)
        result = await strategy.evaluate(df, df, _regime(Regime.TRENDING_UP), None)
        assert result.is_hold

    @pytest.mark.asyncio
    async def test_sar_cross_down(self, strategy):
        """상승 추세에서 EMA 데드크로스 + 롱 보유 → 숏 전환."""
        df = _df_5m(ema_9=79000, ema_21=80000, rsi=50)
        result = await strategy.evaluate(
            df, df, _regime(Regime.TRENDING_UP), Direction.LONG
        )
        assert result.direction == Direction.SHORT
        assert result.sizing_factor > 0

    @pytest.mark.asyncio
    async def test_no_sar_without_position(self, strategy):
        """포지션 없으면 SAR 안 함."""
        df = _df_5m(ema_9=79000, ema_21=80000, rsi=50)
        result = await strategy.evaluate(
            df, df, _regime(Regime.TRENDING_UP), None
        )
        assert result.is_hold


class TestDowntrend:
    @pytest.mark.asyncio
    async def test_rally_sell(self, strategy):
        """하락 추세 + EMA9<EMA21 + RSI 랠리 → 숏."""
        df = _df_5m(ema_9=79000, ema_21=80000, rsi=60)
        result = await strategy.evaluate(df, df, _regime(Regime.TRENDING_DOWN), None)
        assert result.direction == Direction.SHORT
        assert result.sizing_factor > 0

    @pytest.mark.asyncio
    async def test_sar_cross_up(self, strategy):
        """하락 추세에서 EMA 골든크로스 + 숏 보유 → 롱 전환."""
        df = _df_5m(ema_9=81000, ema_21=80000, rsi=50)
        result = await strategy.evaluate(
            df, df, _regime(Regime.TRENDING_DOWN), Direction.SHORT
        )
        assert result.direction == Direction.LONG


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_insufficient_data(self, strategy):
        df = _df_5m(n=5)
        result = await strategy.evaluate(df, df, _regime(), None)
        assert result.is_hold
        assert "insufficient" in result.reason

    @pytest.mark.asyncio
    async def test_regime_mismatch(self, strategy):
        """RANGING 레짐이면 hold."""
        df = _df_5m()
        result = await strategy.evaluate(df, df, _regime(Regime.RANGING), None)
        assert result.is_hold
        assert "mismatch" in result.reason

    @pytest.mark.asyncio
    async def test_zero_close(self, strategy):
        df = _df_5m(close=0.0)
        result = await strategy.evaluate(df, df, _regime(), None)
        assert result.is_hold

    @pytest.mark.asyncio
    async def test_missing_columns(self, strategy):
        df = pd.DataFrame({"close": [80000.0] * 50})
        result = await strategy.evaluate(df, df, _regime(), None)
        assert result.is_hold


class TestSizing:
    @pytest.mark.asyncio
    async def test_low_volatility_larger_size(self, strategy):
        """저변동 → 큰 사이징."""
        df = _df_5m(ema_9=81000, ema_21=80000, rsi=40, atr=500, close=80000)
        result = await strategy.evaluate(df, df, _regime(), None)
        assert result.sizing_factor > 0.5

    @pytest.mark.asyncio
    async def test_high_volatility_smaller_size(self, strategy):
        """고변동 → 작은 사이징."""
        df = _df_5m(ema_9=81000, ema_21=80000, rsi=40, atr=3000, close=80000)
        result = await strategy.evaluate(df, df, _regime(), None)
        assert result.sizing_factor < 0.7
