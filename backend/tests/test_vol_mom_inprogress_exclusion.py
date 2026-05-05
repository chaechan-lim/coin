"""
Volume Momentum 진입 평가 — 마지막 in-progress 캔들 제외 검증.

배경: ccxt fetch_ohlcv 가 현재 진행 중인 캔들을 마지막으로 반환.
xx:05 평가 시 5분치 vol을 1h 평균과 비교하면 vol_ratio < 1.0 → 진입 0건.
백테스트(완성 캔들만)와 라이브가 다른 결과 내는 원인.

Fix: _evaluate_symbol 에서 신규 진입 평가 시 df.iloc[:-1] 사용.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import numpy as np
import pandas as pd
import pytest

from engine.volume_momentum_engine import VolumeMomentumEngine


def _make_engine():
    config = MagicMock()
    exchange = MagicMock()
    market_data = MagicMock()
    eng = VolumeMomentumEngine(
        config, exchange, market_data,
        initial_capital_usdt=200, leverage=2,
        vol_mult=2.0,  # 의도적으로 baseline 사용
        coins=["BTC/USDT"],
    )
    return eng


def _df_with_inprogress_low_vol(n=30):
    """완성 캔들 vol=100 균일, 마지막 in-progress 캔들 vol=10 (5분치)."""
    idx = pd.date_range(end="2026-05-04 12:00", periods=n, freq="1h", tz="UTC")
    closes = np.linspace(100, 110, n)
    df = pd.DataFrame({
        "open": closes, "high": closes * 1.01, "low": closes * 0.99,
        "close": closes,
        "volume": [100.0] * (n - 1) + [10.0],  # 마지막 in-progress 5분치
    }, index=idx)
    return df


def _df_with_completed_vol_spike(n=30):
    """완성 캔들 vol=100, 마지막 완성 캔들 vol=300 (3x spike).
    가격은 RSI가 중간값(~50)이 되도록 oscillating + 마지막 +1% 상승.
    """
    idx = pd.date_range(end="2026-05-04 12:00", periods=n, freq="1h", tz="UTC")
    # oscillating closes (up/down 교대) → RSI ~50
    closes = []
    p = 100.0
    for i in range(n - 1):
        p = 100.5 if i % 2 == 0 else 99.5
        closes.append(p)
    closes.append(101.0)  # 마지막 +1% 상승 → momentum > 0
    closes = np.array(closes)
    vols = [100.0] * (n - 1) + [300.0]
    df = pd.DataFrame({
        "open": closes, "high": closes * 1.005, "low": closes * 0.995,
        "close": closes, "volume": vols,
    }, index=idx)
    return df


@pytest.mark.asyncio
async def test_inprogress_low_vol_does_not_block_entry_when_prior_bar_spiked():
    """마지막 in-progress 캔들의 낮은 vol 때문에 진입이 막혀선 안 됨.

    완성 캔들 ratio=300/100=3.0 (vol_mult 2.0 충족) → 진입해야 함.
    Fix 적용 전: 라이브에서 마지막 vol=10 사용 → ratio<1 → 진입 차단.
    """
    eng = _make_engine()
    # spike된 캔들 + in-progress 낮은 vol 추가 — 총 31 bars
    base = _df_with_completed_vol_spike(30)
    # 마지막에 in-progress 5분치 vol=10 캔들 추가
    last_idx = base.index[-1] + pd.Timedelta(hours=1)
    inprog = pd.DataFrame({
        "open": [base["close"].iloc[-1]],
        "high": [base["close"].iloc[-1] * 1.001],
        "low": [base["close"].iloc[-1] * 0.999],
        "close": [base["close"].iloc[-1] * 1.002],
        "volume": [10.0],  # in-progress 5분치
    }, index=[last_idx])
    df = pd.concat([base, inprog])

    eng._market_data.get_ohlcv_df = AsyncMock(return_value=df)
    open_mock = AsyncMock()
    eng._open_position = open_mock

    with patch("engine.volume_momentum_engine.emit_event", new_callable=AsyncMock):
        await eng._evaluate_symbol("BTC/USDT")

    # 신규 진입 시도가 발생해야 함 (vol_ratio = 300/100 = 3.0 ≥ 2.0)
    assert open_mock.call_count == 1


@pytest.mark.asyncio
async def test_inprogress_only_low_vol_does_block_entry_when_no_real_spike():
    """진짜 vol spike 없으면 (in-progress 제외해도) 진입 안 함."""
    eng = _make_engine()
    df = _df_with_inprogress_low_vol(31)  # 완성 vol=100 균일, 마지막 in-progress=10

    eng._market_data.get_ohlcv_df = AsyncMock(return_value=df)
    open_mock = AsyncMock()
    eng._open_position = open_mock

    with patch("engine.volume_momentum_engine.emit_event", new_callable=AsyncMock):
        await eng._evaluate_symbol("BTC/USDT")

    # 완성 캔들 ratio = 100/100 = 1.0 < 2.0 → 진입 안 함 (정상)
    open_mock.assert_not_called()


@pytest.mark.asyncio
async def test_min_bars_check_now_requires_21():
    """평가에 필요한 최소 캔들 수 21개 (in-progress 제외 후 20개 가용)."""
    eng = _make_engine()
    # 20개만 — 부족
    n = 20
    idx = pd.date_range(end="2026-05-04 12:00", periods=n, freq="1h", tz="UTC")
    df = pd.DataFrame({
        "open": [100.0]*n, "high": [101.0]*n, "low": [99.0]*n,
        "close": [100.0]*n, "volume": [100.0]*n,
    }, index=idx)
    eng._market_data.get_ohlcv_df = AsyncMock(return_value=df)
    open_mock = AsyncMock()
    eng._open_position = open_mock
    with patch("engine.volume_momentum_engine.emit_event", new_callable=AsyncMock):
        await eng._evaluate_symbol("BTC/USDT")
    open_mock.assert_not_called()  # 캔들 부족
