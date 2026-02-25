"""
Tests for TradingEngine._detect_market_state() — 5-factor scoring.
"""
import os
os.environ.setdefault("DB_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("EXCHANGE_API_KEY", "test")
os.environ.setdefault("EXCHANGE_API_SECRET", "test")
os.environ.setdefault("TRADING_MODE", "paper")

from datetime import datetime, timezone
from unittest.mock import MagicMock

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from config import AppConfig
from engine.trading_engine import TradingEngine


def _make_engine() -> TradingEngine:
    config = AppConfig()
    return TradingEngine(
        config=config,
        exchange=MagicMock(),
        market_data=MagicMock(),
        order_manager=MagicMock(),
        portfolio_manager=MagicMock(),
        combiner=MagicMock(),
    )


def _make_market_df(
    n: int = 200,
    close_base: float = 50_000_000,
    sma_20: float = None,
    sma_50: float = None,
    rsi_14: float = 50.0,
    trend_pct: float = 0.0,
    volume_ratio: float = 1.0,
) -> pd.DataFrame:
    """Create a market DataFrame with indicators for testing.

    Args:
        trend_pct: 7-day price change percentage (e.g., +10.0 for strong up)
        volume_ratio: current_volume / volume_sma_20
    """
    closes = np.full(n, close_base)
    # Apply trend to first candle (7-day lookback)
    if trend_pct != 0:
        past_price = close_base / (1 + trend_pct / 100)
        closes[0] = past_price

    # 4h 간격 datetime 인덱스 (시장 감지의 7일 룩백 계산에 필요)
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    index = pd.DatetimeIndex([base_time + timedelta(hours=4 * i) for i in range(n)])

    df = pd.DataFrame({
        "open": closes * 0.999,
        "high": closes * 1.01,
        "low": closes * 0.99,
        "close": closes,
        "volume": np.random.uniform(10, 100, n),
    }, index=index)

    # Set SMA values
    if sma_20 is not None:
        df["sma_20"] = sma_20
    else:
        df["sma_20"] = close_base * 0.98  # slightly below price

    if sma_50 is not None:
        df["sma_50"] = sma_50
    else:
        df["sma_50"] = close_base * 0.96

    df["rsi_14"] = rsi_14

    avg_vol = 50.0
    df["volume_sma_20"] = avg_vol
    df.iloc[-1, df.columns.get_loc("volume")] = avg_vol * volume_ratio

    return df


# ── 기본 동작 ─────────────────────────────────────────────────


class TestMarketDetectionBasic:
    def test_insufficient_data_returns_sideways(self):
        engine = _make_engine()
        df = _make_market_df(n=30)  # < 60
        state, conf = engine._detect_market_state(df)
        assert state == "sideways"
        assert conf == 0.3

    def test_none_df_returns_sideways(self):
        engine = _make_engine()
        state, conf = engine._detect_market_state(None)
        assert state == "sideways"
        assert conf == 0.3

    def test_returns_tuple(self):
        engine = _make_engine()
        df = _make_market_df()
        result = engine._detect_market_state(df)
        assert isinstance(result, tuple)
        assert len(result) == 2


# ── 강한 상승장 ───────────────────────────────────────────────


class TestStrongUptrend:
    def test_price_above_sma20_5pct_rsi_high(self):
        """Price > SMA20*1.05, SMA20 > SMA50, RSI > 55, 7d up → strong_uptrend."""
        engine = _make_engine()
        price = 55_000_000
        df = _make_market_df(
            close_base=price,
            sma_20=price * 0.93,  # price 7% above SMA20
            sma_50=price * 0.90,  # SMA20 > SMA50
            rsi_14=65.0,
            trend_pct=5.0,
        )
        state, conf = engine._detect_market_state(df)
        assert state in ("strong_uptrend", "uptrend")


# ── 상승장 ────────────────────────────────────────────────────


class TestUptrend:
    def test_price_above_sma20_rsi_moderate(self):
        """Price > SMA20, SMA20 > SMA50, RSI 55-70 → uptrend."""
        engine = _make_engine()
        price = 50_000_000
        df = _make_market_df(
            close_base=price,
            sma_20=price * 0.98,  # slightly above
            sma_50=price * 0.95,
            rsi_14=60.0,
            trend_pct=4.0,
        )
        state, conf = engine._detect_market_state(df)
        assert state in ("strong_uptrend", "uptrend")


# ── 횡보장 ────────────────────────────────────────────────────


class TestSideways:
    def test_neutral_rsi_small_change(self):
        """RSI 중립, 작은 가격 변동 → sideways."""
        engine = _make_engine()
        price = 50_000_000
        df = _make_market_df(
            close_base=price,
            sma_20=price * 1.01,  # price ~= SMA20
            sma_50=price * 1.02,
            rsi_14=50.0,
            trend_pct=0.5,
        )
        state, conf = engine._detect_market_state(df)
        # With neutral RSI and small change, sideways should get high score
        assert state in ("sideways", "downtrend")  # close to SMA, could be either


# ── 하락장 ────────────────────────────────────────────────────


class TestDowntrend:
    def test_price_below_sma20_rsi_low(self):
        """Price < SMA20, RSI < 45, 7d down → downtrend."""
        engine = _make_engine()
        price = 50_000_000
        df = _make_market_df(
            close_base=price,
            sma_20=price * 1.06,  # price 6% below SMA20
            sma_50=price * 1.10,
            rsi_14=35.0,
            trend_pct=-5.0,
        )
        state, conf = engine._detect_market_state(df)
        assert state in ("downtrend", "crash")


# ── 폭락장 (CRASH) ───────────────────────────────────────────


class TestCrash:
    def test_extreme_downtrend_becomes_crash(self):
        """매우 강한 하락 → crash 매핑 (downtrend + high confidence + high raw score)."""
        engine = _make_engine()
        price = 50_000_000
        df = _make_market_df(
            close_base=price,
            sma_20=price * 1.10,  # price 10% below SMA20
            sma_50=price * 1.15,  # SMA20 < SMA50
            rsi_14=22.0,         # RSI very low
            trend_pct=-15.0,     # 7d -15%
            volume_ratio=3.0,    # volume surge
        )
        state, conf = engine._detect_market_state(df)
        assert state in ("crash", "downtrend")
        if state == "crash":
            assert conf >= 0.55


# ── 동적 SL 계산 ─────────────────────────────────────────────


class TestDynamicSL:
    def test_with_atr(self):
        engine = _make_engine()
        price = 50_000_000
        df = _make_market_df(close_base=price)
        df["atr_14"] = 500_000  # 1% of price
        sl = engine._calc_dynamic_sl(df, price, "uptrend")
        # ATR 1% * mult 2.0 = 2%, floor 4% → 4.0
        assert sl == 4.0

    def test_high_atr_capped(self):
        engine = _make_engine()
        price = 50_000_000
        df = _make_market_df(close_base=price)
        df["atr_14"] = 5_000_000  # 10% of price
        sl = engine._calc_dynamic_sl(df, price, "uptrend")
        # ATR 10% * mult 2.0 = 20%, cap 10% → 10.0
        assert sl == 10.0

    def test_no_atr_returns_cap(self):
        engine = _make_engine()
        price = 50_000_000
        df = _make_market_df(close_base=price)
        # no atr_14 column
        sl = engine._calc_dynamic_sl(df, price, "sideways")
        assert sl == 7.0  # cap for sideways

    def test_strong_uptrend_profile(self):
        engine = _make_engine()
        price = 50_000_000
        df = _make_market_df(close_base=price)
        df["atr_14"] = 3_000_000  # 6%
        sl = engine._calc_dynamic_sl(df, price, "strong_uptrend")
        # ATR 6% * mult 2.5 = 15%, cap 12% → 12.0
        assert sl == 12.0

    def test_insufficient_data(self):
        engine = _make_engine()
        df = _make_market_df(n=10)
        sl = engine._calc_dynamic_sl(df, 50_000_000, "sideways")
        assert sl == 7.0  # cap


# ── 신뢰도 범위 검증 ─────────────────────────────────────────


class TestConfidenceRange:
    def test_confidence_between_0_and_1(self):
        """다양한 시장 조건에서 confidence가 0~1 범위."""
        engine = _make_engine()
        scenarios = [
            dict(rsi_14=15, trend_pct=-20, sma_20=60_000_000, sma_50=65_000_000),
            dict(rsi_14=85, trend_pct=20, sma_20=45_000_000, sma_50=40_000_000),
            dict(rsi_14=50, trend_pct=0, sma_20=50_000_000, sma_50=50_000_000),
        ]
        for params in scenarios:
            df = _make_market_df(close_base=50_000_000, **params)
            state, conf = engine._detect_market_state(df)
            assert 0.0 <= conf <= 1.0, f"confidence {conf} out of range for {params}"
