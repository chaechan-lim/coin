"""
Momentum Rotation 시장 체제 필터 테스트.

A 옵션: BTC 30일 모멘텀 음수면 롱 차단 (백테스트 360d -24% → +72%).
"""
from unittest.mock import AsyncMock, MagicMock
import pytest
import pandas as pd
import numpy as np

from engine.momentum_rotation_live_engine import (
    MomentumRotationLiveEngine, REGIME_LOOKBACK_DAYS,
)


def _make_btc_df(start_price: float, end_price: float, days: int = 35):
    """BTC 일봉 — 시작/끝 가격으로 모멘텀 계산 가능한 DataFrame."""
    idx = pd.date_range(end="2026-04-29", periods=days, freq="1D", tz="UTC")
    # 단순 선형: start → end
    closes = np.linspace(start_price, end_price, days)
    return pd.DataFrame({
        "open": closes, "high": closes * 1.01, "low": closes * 0.99,
        "close": closes, "volume": [1000.0] * days,
    }, index=idx)


def _make_engine():
    config = MagicMock()
    exchange = MagicMock()
    market_data = MagicMock()
    return MomentumRotationLiveEngine(config, exchange, market_data, initial_capital_usdt=200)


@pytest.mark.asyncio
async def test_regime_momentum_positive():
    """BTC 30d +10% → 양수 반환."""
    engine = _make_engine()
    df = _make_btc_df(start_price=100.0, end_price=110.0, days=REGIME_LOOKBACK_DAYS + 5)
    engine._market_data.get_ohlcv_df = AsyncMock(return_value=df)

    mom = await engine._fetch_btc_regime_momentum()
    assert mom is not None
    assert mom > 0


@pytest.mark.asyncio
async def test_regime_momentum_negative():
    """BTC 30d -10% → 음수 반환."""
    engine = _make_engine()
    df = _make_btc_df(start_price=110.0, end_price=100.0, days=REGIME_LOOKBACK_DAYS + 5)
    engine._market_data.get_ohlcv_df = AsyncMock(return_value=df)

    mom = await engine._fetch_btc_regime_momentum()
    assert mom is not None
    assert mom < 0


@pytest.mark.asyncio
async def test_regime_momentum_insufficient_data():
    """데이터 부족 → None."""
    engine = _make_engine()
    df = _make_btc_df(100.0, 100.0, days=10)  # 30일 미만
    engine._market_data.get_ohlcv_df = AsyncMock(return_value=df)

    mom = await engine._fetch_btc_regime_momentum()
    assert mom is None


@pytest.mark.asyncio
async def test_regime_momentum_none_df():
    """get_ohlcv_df 실패 → None."""
    engine = _make_engine()
    engine._market_data.get_ohlcv_df = AsyncMock(return_value=None)

    mom = await engine._fetch_btc_regime_momentum()
    assert mom is None


@pytest.mark.asyncio
async def test_regime_momentum_zero_past_price():
    """과거 가격 0 가드."""
    engine = _make_engine()
    df = _make_btc_df(0.0, 100.0, days=REGIME_LOOKBACK_DAYS + 5)
    df.iloc[0, df.columns.get_loc("close")] = 0.0
    # in-progress 제외 fix 후: past = iloc[-(LOOKBACK + 2)]
    df.iloc[-(REGIME_LOOKBACK_DAYS + 2), df.columns.get_loc("close")] = 0.0
    engine._market_data.get_ohlcv_df = AsyncMock(return_value=df)

    mom = await engine._fetch_btc_regime_momentum()
    assert mom is None


@pytest.mark.asyncio
async def test_regime_momentum_exception():
    """fetch 예외 → None (안전 fallback)."""
    engine = _make_engine()
    engine._market_data.get_ohlcv_df = AsyncMock(side_effect=Exception("network"))

    mom = await engine._fetch_btc_regime_momentum()
    assert mom is None
