"""
Tests for individual strategy signal generation (RSI, MA Crossover, Bollinger+RSI).
Uses mock candle DataFrames to verify BUY/SELL/HOLD logic.
"""
import os
os.environ.setdefault("DB_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("EXCHANGE_API_KEY", "test")
os.environ.setdefault("EXCHANGE_API_SECRET", "test")
os.environ.setdefault("TRADING_MODE", "paper")

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from core.enums import SignalType
from exchange.data_models import Ticker


def _ticker(price: float = 50_000_000) -> Ticker:
    return Ticker(
        symbol="BTC/KRW",
        last=price,
        bid=price * 0.999,
        ask=price * 1.001,
        high=price * 1.02,
        low=price * 0.98,
        volume=100.0,
        timestamp=datetime.now(timezone.utc),
    )


def _make_df(n: int = 100, close_base: float = 50_000_000, **overrides) -> pd.DataFrame:
    """Make a minimal OHLCV DataFrame with optional indicator overrides."""
    closes = np.linspace(close_base * 0.95, close_base, n)
    df = pd.DataFrame({
        "open": closes * 0.999,
        "high": closes * 1.01,
        "low": closes * 0.99,
        "close": closes,
        "volume": np.random.uniform(10, 100, n),
    })
    for col, vals in overrides.items():
        if isinstance(vals, (int, float)):
            df[col] = vals
        else:
            df[col] = vals
    return df


# ── RSI Strategy ──────────────────────────────────────────────


class TestRSIStrategy:
    @pytest.fixture
    def strategy(self):
        from strategies.rsi_strategy import RSIStrategy
        return RSIStrategy()

    @pytest.mark.asyncio
    async def test_extreme_oversold_buy(self, strategy):
        """RSI < 20 → BUY with high confidence."""
        df = _make_df(30)
        df["rsi_14"] = 15.0  # extreme oversold
        df.iloc[-2, df.columns.get_loc("rsi_14")] = 16.0  # prev
        signal = await strategy.analyze(df, _ticker())
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence >= 0.80

    @pytest.mark.asyncio
    async def test_oversold_buy(self, strategy):
        """RSI < 30 → BUY."""
        df = _make_df(30)
        df["rsi_14"] = 25.0
        df.iloc[-2, df.columns.get_loc("rsi_14")] = 24.0
        signal = await strategy.analyze(df, _ticker())
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence >= 0.50

    @pytest.mark.asyncio
    async def test_extreme_overbought_sell(self, strategy):
        """RSI > 80 → SELL with high confidence."""
        df = _make_df(30)
        df["rsi_14"] = 85.0
        df.iloc[-2, df.columns.get_loc("rsi_14")] = 84.0
        signal = await strategy.analyze(df, _ticker())
        assert signal.signal_type == SignalType.SELL
        assert signal.confidence >= 0.80

    @pytest.mark.asyncio
    async def test_overbought_sell(self, strategy):
        """RSI > 70 → SELL."""
        df = _make_df(30)
        df["rsi_14"] = 75.0
        df.iloc[-2, df.columns.get_loc("rsi_14")] = 76.0
        signal = await strategy.analyze(df, _ticker())
        assert signal.signal_type == SignalType.SELL
        assert signal.confidence >= 0.50

    @pytest.mark.asyncio
    async def test_neutral_hold(self, strategy):
        """30 < RSI < 70 → HOLD."""
        df = _make_df(30)
        df["rsi_14"] = 50.0
        df.iloc[-2, df.columns.get_loc("rsi_14")] = 49.0
        signal = await strategy.analyze(df, _ticker())
        assert signal.signal_type == SignalType.HOLD

    @pytest.mark.asyncio
    async def test_insufficient_data(self, strategy):
        """데이터 부족 시 HOLD."""
        df = _make_df(5)
        signal = await strategy.analyze(df, _ticker())
        assert signal.signal_type == SignalType.HOLD
        assert signal.confidence == 0.0

    @pytest.mark.asyncio
    async def test_rsi_rising_increases_confidence(self, strategy):
        """RSI가 반등 중이면 신뢰도 상승."""
        df = _make_df(30)
        df["rsi_14"] = 28.0
        df.iloc[-2, df.columns.get_loc("rsi_14")] = 25.0  # rising
        signal_rising = await strategy.analyze(df, _ticker())

        df2 = _make_df(30)
        df2["rsi_14"] = 28.0
        df2.iloc[-2, df2.columns.get_loc("rsi_14")] = 29.0  # falling
        signal_falling = await strategy.analyze(df2, _ticker())

        assert signal_rising.confidence > signal_falling.confidence


# ── MA Crossover Strategy ─────────────────────────────────────


class TestMACrossoverStrategy:
    @pytest.fixture
    def strategy(self):
        from strategies.ma_crossover import MACrossoverStrategy
        return MACrossoverStrategy(short_period=20, long_period=50)

    @pytest.mark.asyncio
    async def test_golden_cross_buy(self, strategy):
        """SMA20이 SMA50을 상향 돌파 → BUY."""
        df = _make_df(60)
        df["sma_20"] = 50_000_000
        df["sma_50"] = 49_900_000
        df.iloc[-2, df.columns.get_loc("sma_20")] = 49_800_000
        df.iloc[-2, df.columns.get_loc("sma_50")] = 49_900_000
        signal = await strategy.analyze(df, _ticker(50_000_000))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence >= 0.60

    @pytest.mark.asyncio
    async def test_death_cross_sell(self, strategy):
        """SMA20이 SMA50을 하향 돌파 → SELL."""
        df = _make_df(60)
        df["sma_20"] = 49_800_000
        df["sma_50"] = 49_900_000
        df.iloc[-2, df.columns.get_loc("sma_20")] = 50_000_000
        df.iloc[-2, df.columns.get_loc("sma_50")] = 49_900_000
        signal = await strategy.analyze(df, _ticker(50_000_000))
        assert signal.signal_type == SignalType.SELL
        assert signal.confidence >= 0.60

    @pytest.mark.asyncio
    async def test_uptrend_continuation_soft_buy(self, strategy):
        """SMA20 > SMA50 (크로스오버 없음) → soft BUY."""
        df = _make_df(60)
        df["sma_20"] = 51_000_000
        df["sma_50"] = 50_000_000
        df.iloc[-2, df.columns.get_loc("sma_20")] = 50_800_000
        df.iloc[-2, df.columns.get_loc("sma_50")] = 50_000_000
        signal = await strategy.analyze(df, _ticker(51_000_000))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence <= 0.55  # soft signal

    @pytest.mark.asyncio
    async def test_downtrend_continuation_soft_sell(self, strategy):
        """SMA20 < SMA50 (크로스오버 없음) → soft SELL."""
        df = _make_df(60)
        df["sma_20"] = 49_000_000
        df["sma_50"] = 50_000_000
        df.iloc[-2, df.columns.get_loc("sma_20")] = 49_200_000
        df.iloc[-2, df.columns.get_loc("sma_50")] = 50_000_000
        signal = await strategy.analyze(df, _ticker(49_000_000))
        assert signal.signal_type == SignalType.SELL
        assert signal.confidence <= 0.55

    @pytest.mark.asyncio
    async def test_insufficient_data(self, strategy):
        df = _make_df(10)
        signal = await strategy.analyze(df, _ticker())
        assert signal.signal_type == SignalType.HOLD
        assert signal.confidence == 0.0


# ── Bollinger + RSI Strategy ──────────────────────────────────


class TestBollingerRSIStrategy:
    @pytest.fixture
    def strategy(self):
        from strategies.bollinger_rsi import BollingerRSIStrategy
        return BollingerRSIStrategy()

    @pytest.mark.asyncio
    async def test_double_confirm_buy(self, strategy):
        """가격 ≤ 볼린저 하단 AND RSI < 30 → high confidence BUY."""
        df = _make_df(30)
        df["BBL_20_2.0"] = 50_100_000
        df["BBM_20_2.0"] = 51_000_000
        df["BBU_20_2.0"] = 51_900_000
        df["rsi_14"] = 25.0
        signal = await strategy.analyze(df, _ticker(50_000_000))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence >= 0.70

    @pytest.mark.asyncio
    async def test_double_confirm_sell(self, strategy):
        """가격 ≥ 볼린저 상단 AND RSI > 70 → high confidence SELL."""
        df = _make_df(30)
        df["BBL_20_2.0"] = 49_000_000
        df["BBM_20_2.0"] = 50_000_000
        df["BBU_20_2.0"] = 50_500_000
        df["rsi_14"] = 75.0
        signal = await strategy.analyze(df, _ticker(50_600_000))
        assert signal.signal_type == SignalType.SELL
        assert signal.confidence >= 0.70

    @pytest.mark.asyncio
    async def test_lower_band_only_weak_buy(self, strategy):
        """가격 ≤ 볼린저 하단, RSI 중립 → weak BUY (single confirm)."""
        df = _make_df(30)
        df["BBL_20_2.0"] = 50_100_000
        df["BBM_20_2.0"] = 51_000_000
        df["BBU_20_2.0"] = 51_900_000
        df["rsi_14"] = 50.0  # neutral
        signal = await strategy.analyze(df, _ticker(50_000_000))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence <= 0.40

    @pytest.mark.asyncio
    async def test_neutral_hold(self, strategy):
        """밴드 내 + RSI 중립 → HOLD."""
        df = _make_df(30)
        df["BBL_20_2.0"] = 49_000_000
        df["BBM_20_2.0"] = 50_000_000
        df["BBU_20_2.0"] = 51_000_000
        df["rsi_14"] = 50.0
        signal = await strategy.analyze(df, _ticker(50_000_000))
        assert signal.signal_type == SignalType.HOLD

    @pytest.mark.asyncio
    async def test_insufficient_data(self, strategy):
        df = _make_df(10)
        signal = await strategy.analyze(df, _ticker())
        assert signal.signal_type == SignalType.HOLD
        assert signal.confidence == 0.0
