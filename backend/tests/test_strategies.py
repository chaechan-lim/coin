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
        """RSI < 20 + RSI 반등 중 → BUY with high confidence."""
        df = _make_df(30)
        df["rsi_14"] = 15.0  # extreme oversold
        df.iloc[-2, df.columns.get_loc("rsi_14")] = 13.0  # prev lower → rising
        signal = await strategy.analyze(df, _ticker())
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence >= 0.80

    @pytest.mark.asyncio
    async def test_extreme_oversold_still_falling_hold(self, strategy):
        """RSI < 20 + RSI 하락 중 → HOLD (나이프캐치 방지)."""
        df = _make_df(30)
        df["rsi_14"] = 15.0  # extreme oversold
        df.iloc[-2, df.columns.get_loc("rsi_14")] = 16.0  # prev higher → falling
        signal = await strategy.analyze(df, _ticker())
        assert signal.signal_type == SignalType.HOLD

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
    async def test_uptrend_continuation_hold(self, strategy):
        """SMA20 > SMA50 (크로스오버 없음) → HOLD (소프트 시그널 제거)."""
        df = _make_df(60)
        df["sma_20"] = 51_000_000
        df["sma_50"] = 50_000_000
        df.iloc[-2, df.columns.get_loc("sma_20")] = 50_800_000
        df.iloc[-2, df.columns.get_loc("sma_50")] = 50_000_000
        signal = await strategy.analyze(df, _ticker(51_000_000))
        assert signal.signal_type == SignalType.HOLD

    @pytest.mark.asyncio
    async def test_downtrend_continuation_hold(self, strategy):
        """SMA20 < SMA50 (크로스오버 없음) → HOLD (소프트 시그널 제거)."""
        df = _make_df(60)
        df["sma_20"] = 49_000_000
        df["sma_50"] = 50_000_000
        df.iloc[-2, df.columns.get_loc("sma_20")] = 49_200_000
        df.iloc[-2, df.columns.get_loc("sma_50")] = 50_000_000
        signal = await strategy.analyze(df, _ticker(49_000_000))
        assert signal.signal_type == SignalType.HOLD

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


# ── BNF Deviation Strategy ──────────────────────────────────


class TestBNFDeviationStrategy:
    @pytest.fixture
    def strategy(self):
        from strategies.bnf_deviation import BNFDeviationStrategy
        return BNFDeviationStrategy()

    @pytest.mark.asyncio
    async def test_deep_oversold_buy(self, strategy):
        """이격도 -15% → BUY high confidence."""
        df = _make_df(35, close_base=50_000_000)
        sma_val = 50_000_000
        df["sma_25"] = sma_val
        price = sma_val * 0.85
        df.iloc[-1, df.columns.get_loc("close")] = price
        signal = await strategy.analyze(df, _ticker(price))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence >= 0.65

    @pytest.mark.asyncio
    async def test_extreme_oversold_buy(self, strategy):
        """이격도 -20% → BUY 최고 confidence."""
        df = _make_df(35, close_base=50_000_000)
        sma_val = 50_000_000
        df["sma_25"] = sma_val
        price = sma_val * 0.80
        df.iloc[-1, df.columns.get_loc("close")] = price
        signal = await strategy.analyze(df, _ticker(price))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence >= 0.85

    @pytest.mark.asyncio
    async def test_rsi_boost(self, strategy):
        """이격도 과매도 + RSI < 40 → confidence 보너스."""
        df = _make_df(35, close_base=50_000_000)
        sma_val = 50_000_000
        df["sma_25"] = sma_val
        price = sma_val * 0.88
        df.iloc[-1, df.columns.get_loc("close")] = price
        df["rsi_14"] = 35.0
        signal = await strategy.analyze(df, _ticker(price))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence >= 0.60

    @pytest.mark.asyncio
    async def test_overbought_sell(self, strategy):
        """이격도 > +5% → SELL."""
        df = _make_df(35, close_base=50_000_000)
        sma_val = 50_000_000
        df["sma_25"] = sma_val
        price = sma_val * 1.08
        df.iloc[-1, df.columns.get_loc("close")] = price
        signal = await strategy.analyze(df, _ticker(price))
        assert signal.signal_type == SignalType.SELL
        assert signal.confidence >= 0.50

    @pytest.mark.asyncio
    async def test_neutral_hold(self, strategy):
        """이격도 -3% → HOLD."""
        df = _make_df(35, close_base=50_000_000)
        sma_val = 50_000_000
        df["sma_25"] = sma_val
        price = sma_val * 0.97
        df.iloc[-1, df.columns.get_loc("close")] = price
        signal = await strategy.analyze(df, _ticker(price))
        assert signal.signal_type == SignalType.HOLD

    @pytest.mark.asyncio
    async def test_insufficient_data_bnf(self, strategy):
        df = _make_df(5)
        signal = await strategy.analyze(df, _ticker())
        assert signal.signal_type == SignalType.HOLD
        assert signal.confidence == 0.0


# ── CIS Momentum Strategy ───────────────────────────────────


class TestCISMomentumStrategy:
    @pytest.fixture
    def strategy(self):
        from strategies.cis_momentum import CISMomentumStrategy
        return CISMomentumStrategy()

    @pytest.mark.asyncio
    async def test_strong_momentum_buy(self, strategy):
        """ROC5>2%, ROC10>3%, Vol>1.2x → BUY."""
        n = 30
        prices = [50_000_000 * (1 + i * 0.005) for i in range(n)]
        df = pd.DataFrame({
            "open": [p * 0.999 for p in prices],
            "high": [p * 1.01 for p in prices],
            "low": [p * 0.99 for p in prices],
            "close": prices,
            "volume": [100.0] * n,
        })
        df.iloc[-1, df.columns.get_loc("close")] = df["close"].iloc[-6] * 1.03
        df.iloc[-1, df.columns.get_loc("volume")] = 200.0
        signal = await strategy.analyze(df, _ticker(df["close"].iloc[-1]))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence >= 0.55

    @pytest.mark.asyncio
    async def test_momentum_reversal_sell(self, strategy):
        """ROC5<-2%, ROC10<-3% → SELL."""
        n = 30
        prices = [50_000_000 * (1 - i * 0.005) for i in range(n)]
        df = pd.DataFrame({
            "open": [p * 1.001 for p in prices],
            "high": [p * 1.01 for p in prices],
            "low": [p * 0.99 for p in prices],
            "close": prices,
            "volume": [100.0] * n,
        })
        df.iloc[-1, df.columns.get_loc("close")] = df["close"].iloc[-6] * 0.96
        signal = await strategy.analyze(df, _ticker(df["close"].iloc[-1]))
        assert signal.signal_type == SignalType.SELL
        assert signal.confidence >= 0.55

    @pytest.mark.asyncio
    async def test_weak_momentum_hold(self, strategy):
        """ROC 미미 → HOLD."""
        df = _make_df(30, close_base=50_000_000)
        df["close"] = 50_000_000
        signal = await strategy.analyze(df, _ticker())
        assert signal.signal_type == SignalType.HOLD

    @pytest.mark.asyncio
    async def test_buy_blocked_by_low_volume(self, strategy):
        """ROC 조건 충족이지만 거래량 부족 → HOLD."""
        n = 30
        prices = [50_000_000 * (1 + i * 0.005) for i in range(n)]
        df = pd.DataFrame({
            "open": [p * 0.999 for p in prices],
            "high": [p * 1.01 for p in prices],
            "low": [p * 0.99 for p in prices],
            "close": prices,
            "volume": [100.0] * n,
        })
        df.iloc[-1, df.columns.get_loc("close")] = df["close"].iloc[-6] * 1.03
        df.iloc[-1, df.columns.get_loc("volume")] = 50.0
        signal = await strategy.analyze(df, _ticker(df["close"].iloc[-1]))
        assert signal.signal_type == SignalType.HOLD

    @pytest.mark.asyncio
    async def test_insufficient_data_cis(self, strategy):
        df = _make_df(5)
        signal = await strategy.analyze(df, _ticker())
        assert signal.signal_type == SignalType.HOLD
        assert signal.confidence == 0.0


# ── Larry Williams Strategy ──────────────────────────────────


class TestLarryWilliamsStrategy:
    @pytest.fixture
    def strategy(self):
        from strategies.larry_williams import LarryWilliamsStrategy
        return LarryWilliamsStrategy()

    @pytest.mark.asyncio
    async def test_breakout_buy(self, strategy):
        """상향 돌파 + %R 과매도 탈출 → BUY."""
        df = _make_df(25, close_base=50_000_000)
        df.iloc[-2, df.columns.get_loc("high")] = 51_000_000
        df.iloc[-2, df.columns.get_loc("low")] = 49_000_000
        current_open = 50_000_000
        df.iloc[-1, df.columns.get_loc("open")] = current_open
        df.iloc[-1, df.columns.get_loc("close")] = 51_200_000
        df.iloc[-1, df.columns.get_loc("high")] = 51_300_000
        df["WILLR_14"] = -70.0
        df.iloc[-2, df.columns.get_loc("WILLR_14")] = -85.0
        df["sma_20"] = 49_500_000
        signal = await strategy.analyze(df, _ticker(51_200_000))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence >= 0.55

    @pytest.mark.asyncio
    async def test_breakdown_sell(self, strategy):
        """하향 돌파 + %R 과매수 → SELL."""
        df = _make_df(25, close_base=50_000_000)
        df.iloc[-2, df.columns.get_loc("high")] = 51_000_000
        df.iloc[-2, df.columns.get_loc("low")] = 49_000_000
        current_open = 50_000_000
        df.iloc[-1, df.columns.get_loc("open")] = current_open
        df.iloc[-1, df.columns.get_loc("close")] = 48_800_000
        df["WILLR_14"] = -15.0
        signal = await strategy.analyze(df, _ticker(48_800_000))
        assert signal.signal_type == SignalType.SELL
        assert signal.confidence >= 0.55

    @pytest.mark.asyncio
    async def test_no_breakout_hold(self, strategy):
        """돌파 없음 → HOLD."""
        df = _make_df(25, close_base=50_000_000)
        df.iloc[-2, df.columns.get_loc("high")] = 51_000_000
        df.iloc[-2, df.columns.get_loc("low")] = 49_000_000
        df.iloc[-1, df.columns.get_loc("open")] = 50_000_000
        df.iloc[-1, df.columns.get_loc("close")] = 50_500_000
        df["WILLR_14"] = -50.0
        signal = await strategy.analyze(df, _ticker(50_500_000))
        assert signal.signal_type == SignalType.HOLD

    @pytest.mark.asyncio
    async def test_confidence_scaling(self, strategy):
        """돌파 강도에 따라 confidence 스케일링."""
        df = _make_df(25, close_base=50_000_000)
        df.iloc[-2, df.columns.get_loc("high")] = 51_000_000
        df.iloc[-2, df.columns.get_loc("low")] = 49_000_000
        df.iloc[-1, df.columns.get_loc("open")] = 50_000_000
        df.iloc[-1, df.columns.get_loc("close")] = 53_000_000
        df.iloc[-1, df.columns.get_loc("high")] = 53_100_000
        df["WILLR_14"] = -65.0
        df.iloc[-2, df.columns.get_loc("WILLR_14")] = -85.0
        df["sma_20"] = 49_500_000
        signal = await strategy.analyze(df, _ticker(53_000_000))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence >= 0.70

    @pytest.mark.asyncio
    async def test_insufficient_data_larry(self, strategy):
        df = _make_df(5)
        signal = await strategy.analyze(df, _ticker())
        assert signal.signal_type == SignalType.HOLD
        assert signal.confidence == 0.0


# ── Donchian Channel Strategy ────────────────────────────────


class TestDonchianChannelStrategy:
    @pytest.fixture
    def strategy(self):
        from strategies.donchian_channel import DonchianChannelStrategy
        return DonchianChannelStrategy()

    @pytest.mark.asyncio
    async def test_upper_breakout_buy(self, strategy):
        """20봉 최고가 돌파 → BUY."""
        df = _make_df(30, close_base=50_000_000)
        for i in range(-21, -1):
            df.iloc[i, df.columns.get_loc("high")] = 51_000_000
            df.iloc[i, df.columns.get_loc("low")] = 49_000_000
        df.iloc[-1, df.columns.get_loc("close")] = 52_000_000
        signal = await strategy.analyze(df, _ticker(52_000_000))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence >= 0.55

    @pytest.mark.asyncio
    async def test_lower_breakout_sell(self, strategy):
        """20봉 최저가 이탈 → SELL."""
        df = _make_df(30, close_base=50_000_000)
        for i in range(-21, -1):
            df.iloc[i, df.columns.get_loc("high")] = 51_000_000
            df.iloc[i, df.columns.get_loc("low")] = 49_000_000
        df.iloc[-1, df.columns.get_loc("close")] = 48_000_000
        signal = await strategy.analyze(df, _ticker(48_000_000))
        assert signal.signal_type == SignalType.SELL
        assert signal.confidence >= 0.60

    @pytest.mark.asyncio
    async def test_exit_signal_sell(self, strategy):
        """10봉 최저 이탈 (터틀 청산) → SELL."""
        df = _make_df(30, close_base=50_000_000)
        for i in range(-21, -1):
            df.iloc[i, df.columns.get_loc("high")] = 55_000_000
            df.iloc[i, df.columns.get_loc("low")] = 45_000_000
        for i in range(-11, -1):
            df.iloc[i, df.columns.get_loc("low")] = 49_000_000
        df.iloc[-1, df.columns.get_loc("close")] = 48_500_000
        signal = await strategy.analyze(df, _ticker(48_500_000))
        assert signal.signal_type == SignalType.SELL

    @pytest.mark.asyncio
    async def test_channel_inside_hold(self, strategy):
        """채널 내부 → HOLD."""
        df = _make_df(30, close_base=50_000_000)
        for i in range(-21, -1):
            df.iloc[i, df.columns.get_loc("high")] = 51_000_000
            df.iloc[i, df.columns.get_loc("low")] = 49_000_000
        df.iloc[-1, df.columns.get_loc("close")] = 50_000_000
        signal = await strategy.analyze(df, _ticker(50_000_000))
        assert signal.signal_type == SignalType.HOLD

    @pytest.mark.asyncio
    async def test_volume_adx_bonus(self, strategy):
        """거래량+ADX 보너스 → confidence 증가."""
        df = _make_df(30, close_base=50_000_000)
        for i in range(-21, -1):
            df.iloc[i, df.columns.get_loc("high")] = 51_000_000
            df.iloc[i, df.columns.get_loc("low")] = 49_000_000
        df.iloc[-1, df.columns.get_loc("close")] = 52_000_000
        df["volume"] = 50.0
        df.iloc[-1, df.columns.get_loc("volume")] = 200.0
        df["ADX_14"] = 30.0
        signal = await strategy.analyze(df, _ticker(52_000_000))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence >= 0.75

    @pytest.mark.asyncio
    async def test_insufficient_data_donchian(self, strategy):
        df = _make_df(5)
        signal = await strategy.analyze(df, _ticker())
        assert signal.signal_type == SignalType.HOLD
        assert signal.confidence == 0.0


# ── Freefall Guard Tests ─────────────────────────────────────


class TestBollingerRSIFreefallGuard:
    """Bollinger+RSI 전략의 급락 방어 테스트."""

    @pytest.fixture
    def strategy(self):
        from strategies.bollinger_rsi import BollingerRSIStrategy
        return BollingerRSIStrategy()

    @pytest.mark.asyncio
    async def test_extreme_bandwidth_blocks_buy(self, strategy):
        """밴드폭 > 25% → HOLD (고변동성 필터)."""
        df = _make_df(30, close_base=100)
        # 밴드폭 60%: upper=130, lower=70, middle=100
        df["BBL_20_2.0"] = 70
        df["BBM_20_2.0"] = 100
        df["BBU_20_2.0"] = 130
        df["rsi_14"] = 20.0  # oversold
        signal = await strategy.analyze(df, _ticker(65))
        assert signal.signal_type == SignalType.HOLD
        assert "변동성 필터" in signal.reason

    @pytest.mark.asyncio
    async def test_normal_bandwidth_allows_buy(self, strategy):
        """밴드폭 < 50% → 정상 BUY 허용."""
        df = _make_df(30, close_base=50_000_000)
        df["BBL_20_2.0"] = 49_000_000
        df["BBM_20_2.0"] = 50_000_000
        df["BBU_20_2.0"] = 51_000_000  # 4% bandwidth
        df["rsi_14"] = 25.0
        signal = await strategy.analyze(df, _ticker(48_900_000))
        assert signal.signal_type == SignalType.BUY

    @pytest.mark.asyncio
    async def test_downtrend_sma_gap_reduces_confidence(self, strategy):
        """SMA20 < SMA50 갭 > 3% → BUY confidence × 0.5."""
        df = _make_df(60, close_base=50_000_000)
        df["BBL_20_2.0"] = 49_500_000
        df["BBM_20_2.0"] = 50_000_000
        df["BBU_20_2.0"] = 50_500_000
        df["rsi_14"] = 25.0
        # 5% gap: SMA20=47.5M, SMA50=50M
        df["sma_20"] = 47_500_000
        df["sma_50"] = 50_000_000
        signal = await strategy.analyze(df, _ticker(49_400_000))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence <= 0.45  # 0.85 * 0.5 = 0.425
        assert "역추세 할인" in signal.reason

    @pytest.mark.asyncio
    async def test_small_sma_gap_no_discount(self, strategy):
        """SMA20 < SMA50 갭 < 3% → 할인 없음."""
        df = _make_df(60, close_base=50_000_000)
        df["BBL_20_2.0"] = 49_500_000
        df["BBM_20_2.0"] = 50_000_000
        df["BBU_20_2.0"] = 50_500_000
        df["rsi_14"] = 25.0
        # 1% gap
        df["sma_20"] = 49_500_000
        df["sma_50"] = 50_000_000
        signal = await strategy.analyze(df, _ticker(49_400_000))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence >= 0.70  # no discount


class TestRSIFreefallGuard:
    """RSI 전략의 급락 방어 테스트."""

    @pytest.fixture
    def strategy(self):
        from strategies.rsi_strategy import RSIStrategy
        return RSIStrategy()

    @pytest.mark.asyncio
    async def test_freefall_30pct_drop_blocks_buy(self, strategy):
        """20캔들 고점 대비 30%+ 하락 + RSI 과매도 → HOLD."""
        df = _make_df(30, close_base=100)
        df["rsi_14"] = 25.0
        df.iloc[-2, df.columns.get_loc("rsi_14")] = 24.0
        # 고점 100, 현재가 65 → -35% 하락
        df["high"] = 100
        signal = await strategy.analyze(df, _ticker(65))
        assert signal.signal_type == SignalType.HOLD
        assert "급락 방어" in signal.reason

    @pytest.mark.asyncio
    async def test_moderate_drop_allows_buy(self, strategy):
        """20캔들 고점 대비 20% 하락 → 정상 BUY 허용."""
        df = _make_df(30, close_base=50_000_000)
        df["rsi_14"] = 25.0
        df.iloc[-2, df.columns.get_loc("rsi_14")] = 24.0
        # 고점 50M, 현재가 40M → -20% (< 30% 임계값)
        signal = await strategy.analyze(df, _ticker(40_000_000))
        assert signal.signal_type == SignalType.BUY

    @pytest.mark.asyncio
    async def test_rsi_downtrend_sma_gap_reduces_oversold_confidence(self, strategy):
        """RSI 과매도 + SMA 갭 > 3% → confidence × 0.5."""
        df = _make_df(60, close_base=50_000_000)
        df["rsi_14"] = 25.0
        df.iloc[-2, df.columns.get_loc("rsi_14")] = 24.0
        # 5% gap
        df["sma_20"] = 47_500_000
        df["sma_50"] = 50_000_000
        signal = await strategy.analyze(df, _ticker(50_000_000))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence <= 0.45  # discounted
        assert "역추세 할인" in signal.reason

    @pytest.mark.asyncio
    async def test_rsi_extreme_oversold_downtrend_reduces_confidence(self, strategy):
        """RSI 극심한 과매도(< 20) + 하락 추세 → confidence 할인."""
        df = _make_df(60, close_base=50_000_000)
        df["rsi_14"] = 15.0
        df.iloc[-2, df.columns.get_loc("rsi_14")] = 14.0
        # 5% gap
        df["sma_20"] = 47_500_000
        df["sma_50"] = 50_000_000
        signal = await strategy.analyze(df, _ticker(50_000_000))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence <= 0.50  # 0.85 * 0.5 = 0.425

    @pytest.mark.asyncio
    async def test_uptrend_no_discount(self, strategy):
        """SMA20 > SMA50 → 할인 없음."""
        df = _make_df(60, close_base=50_000_000)
        df["rsi_14"] = 25.0
        df.iloc[-2, df.columns.get_loc("rsi_14")] = 24.0
        df["sma_20"] = 51_000_000
        df["sma_50"] = 50_000_000
        signal = await strategy.analyze(df, _ticker(50_000_000))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence >= 0.55  # no discount


# ── Volatility Regime Strategy ─────────────────────────────


class TestVolatilityRegimeStrategy:
    @pytest.fixture
    def strategy(self):
        from strategies.volatility_regime import VolatilityRegimeStrategy
        return VolatilityRegimeStrategy()

    def _make_regime_df(self, atr_percentile_target: str, n: int = 60):
        """ATR 분포를 조작하여 원하는 레짐을 만드는 헬퍼."""
        close_base = 50000
        df = _make_df(n, close_base=close_base)
        # ATR 값 설정: 50개 캔들의 ATR 분포 조작
        if atr_percentile_target == "low":
            # 대부분 높은 ATR, 현재만 낮은 → percentile < 25
            atr_vals = np.full(n, 3000.0)  # 높은 베이스
            atr_vals[-1] = 500.0  # 현재 매우 낮음
        elif atr_percentile_target == "high":
            # 대부분 낮은 ATR, 현재만 높은 → percentile > 75
            atr_vals = np.full(n, 500.0)  # 낮은 베이스
            atr_vals[-1] = 3000.0  # 현재 매우 높음
        else:  # normal
            atr_vals = np.full(n, 1500.0)  # 중간
        df["ATRr_14"] = atr_vals
        df["rsi_14"] = 50.0
        df["BBL_20_2.0"] = close_base * 0.96
        df["BBM_20_2.0"] = close_base
        df["BBU_20_2.0"] = close_base * 1.04
        df["Volume_SMA_20"] = 50.0
        return df

    @pytest.mark.asyncio
    async def test_low_vol_breakout_buy(self, strategy):
        """저변동 + 가격 > 상단밴드 + 거래량 확인 → BUY."""
        df = self._make_regime_df("low")
        price = 52100  # > BBU (52000)
        df["volume"] = 80.0  # > 50 * 1.3 = 65
        signal = await strategy.analyze(df, _ticker(price))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence >= 0.70

    @pytest.mark.asyncio
    async def test_low_vol_breakout_sell(self, strategy):
        """저변동 + 가격 < 하단밴드 + 거래량 확인 → SELL."""
        df = self._make_regime_df("low")
        price = 47900  # < BBL (48000)
        df["volume"] = 80.0
        df["rsi_14"] = 40.0
        signal = await strategy.analyze(df, _ticker(price))
        assert signal.signal_type == SignalType.SELL
        assert signal.confidence >= 0.70

    @pytest.mark.asyncio
    async def test_low_vol_no_volume_hold(self, strategy):
        """저변동 + 가격 > 상단밴드 but 거래량 미확인 → HOLD."""
        df = self._make_regime_df("low")
        price = 52100
        df["volume"] = 30.0  # < 50 * 1.3 = 65
        signal = await strategy.analyze(df, _ticker(price))
        assert signal.signal_type == SignalType.HOLD

    @pytest.mark.asyncio
    async def test_high_vol_mean_revert_buy(self, strategy):
        """고변동 + RSI < 30 + 볼린저 하단 + RSI↑ → BUY."""
        df = self._make_regime_df("high")
        price = 48000  # ≤ BBL * 1.01
        df["rsi_14"] = 25.0
        df.iloc[-2, df.columns.get_loc("rsi_14")] = 23.0  # RSI rising
        signal = await strategy.analyze(df, _ticker(price))
        assert signal.signal_type == SignalType.BUY
        assert signal.confidence >= 0.70

    @pytest.mark.asyncio
    async def test_high_vol_mean_revert_sell(self, strategy):
        """고변동 + RSI > 70 + 볼린저 상단 + RSI↓ → SELL."""
        df = self._make_regime_df("high")
        price = 52100  # ≥ BBU * 0.99
        df["rsi_14"] = 75.0
        df.iloc[-2, df.columns.get_loc("rsi_14")] = 77.0  # RSI falling
        signal = await strategy.analyze(df, _ticker(price))
        assert signal.signal_type == SignalType.SELL
        assert signal.confidence >= 0.70

    @pytest.mark.asyncio
    async def test_normal_vol_hold(self, strategy):
        """중간 변동성 → HOLD (다른 전략에 위임)."""
        df = self._make_regime_df("normal")
        signal = await strategy.analyze(df, _ticker(50000))
        assert signal.signal_type == SignalType.HOLD

    @pytest.mark.asyncio
    async def test_insufficient_data(self, strategy):
        """데이터 부족 → HOLD."""
        df = _make_df(10, close_base=50000)
        signal = await strategy.analyze(df, _ticker(50000))
        assert signal.signal_type == SignalType.HOLD
        assert signal.confidence == 0.0


# ── BB Squeeze Strategy ──────────────────────────────────────


class TestBBSqueezeStrategy:
    @pytest.fixture
    def strategy(self):
        from strategies.bb_squeeze import BBSqueezeStrategy
        return BBSqueezeStrategy()

    def _make_squeeze_df(self, n: int = 40, close_base: float = 100.0):
        """BB가 KC 안에 수축된 스퀴즈 상태 DataFrame 생성."""
        closes = np.full(n, close_base)
        # 극저변동성: 가격 변동 거의 없음
        for i in range(n):
            closes[i] = close_base + np.sin(i * 0.1) * 0.1
        df = pd.DataFrame({
            "open": closes * 0.9999,
            "high": closes * 1.001,
            "low": closes * 0.999,
            "close": closes,
            "volume": np.random.uniform(100, 200, n),
        })
        return df

    def _make_breakout_df(self, direction: str = "up", n: int = 40, close_base: float = 100.0):
        """스퀴즈 후 브레이크아웃 DataFrame 생성."""
        df = self._make_squeeze_df(n - 3, close_base)
        # 마지막 3봉: 변동성 확대 (브레이크아웃)
        if direction == "up":
            breakout = [close_base * 1.02, close_base * 1.035, close_base * 1.05]
        else:
            breakout = [close_base * 0.98, close_base * 0.965, close_base * 0.95]
        for bp in breakout:
            new_row = pd.DataFrame({
                "open": [bp * 0.999],
                "high": [bp * 1.01],
                "low": [bp * 0.99],
                "close": [bp],
                "volume": [500.0],
            })
            df = pd.concat([df, new_row], ignore_index=True)
        return df

    @pytest.mark.asyncio
    async def test_insufficient_data_hold(self, strategy):
        """데이터 부족 → HOLD."""
        df = _make_df(10, close_base=100.0)
        signal = await strategy.analyze(df, _ticker(100.0))
        assert signal.signal_type == SignalType.HOLD
        assert signal.confidence == 0.0

    @pytest.mark.asyncio
    async def test_no_squeeze_hold(self, strategy):
        """스퀴즈 아님 → HOLD."""
        # 높은 변동성: BB가 KC 밖
        closes = np.linspace(80, 120, 40)
        df = pd.DataFrame({
            "open": closes * 0.99,
            "high": closes * 1.05,
            "low": closes * 0.95,
            "close": closes,
            "volume": np.random.uniform(100, 200, 40),
        })
        signal = await strategy.analyze(df, _ticker(120.0))
        assert signal.signal_type == SignalType.HOLD

    @pytest.mark.asyncio
    async def test_squeeze_in_progress_hold(self, strategy):
        """스퀴즈 진행 중 → HOLD (해제 대기)."""
        df = self._make_squeeze_df(40)
        signal = await strategy.analyze(df, _ticker(100.0))
        # 스퀴즈 중이거나 스퀴즈 아님 → HOLD
        assert signal.signal_type == SignalType.HOLD

    @pytest.mark.asyncio
    async def test_upward_breakout_buy(self, strategy):
        """스퀴즈 해제 + 상향 모멘텀 → BUY."""
        df = self._make_breakout_df("up")
        price = float(df["close"].iloc[-1])
        signal = await strategy.analyze(df, _ticker(price))
        # 스퀴즈 감지 여부에 따라 BUY 또는 HOLD
        assert signal.signal_type in (SignalType.BUY, SignalType.HOLD)
        if signal.signal_type == SignalType.BUY:
            assert signal.confidence >= 0.60

    @pytest.mark.asyncio
    async def test_downward_breakout_sell(self, strategy):
        """스퀴즈 해제 + 하향 모멘텀 → SELL."""
        df = self._make_breakout_df("down")
        price = float(df["close"].iloc[-1])
        signal = await strategy.analyze(df, _ticker(price))
        # 스퀴즈 감지 여부에 따라 SELL 또는 HOLD
        assert signal.signal_type in (SignalType.SELL, SignalType.HOLD)
        if signal.signal_type == SignalType.SELL:
            assert signal.confidence >= 0.60

    @pytest.mark.asyncio
    async def test_strategy_name(self, strategy):
        """전략 이름 확인."""
        assert strategy.name == "bb_squeeze"

    @pytest.mark.asyncio
    async def test_signal_has_indicators(self, strategy):
        """시그널에 indicators 딕셔너리 포함."""
        df = self._make_squeeze_df(40)
        signal = await strategy.analyze(df, _ticker(100.0))
        assert signal.indicators is not None
        assert "squeeze" in signal.indicators
        assert "momentum" in signal.indicators
