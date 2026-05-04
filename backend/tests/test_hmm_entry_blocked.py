"""
HMM 신규 진입 차단 — entry_blocked 리스트로 특정 심볼만 차단.
기존 포지션의 SL/TP/regime exit 은 정상 동작해야 함.
"""
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from engine.hmm_regime_live_engine import HMMRegimeLiveEngine


def _make_engine(symbols=None, entry_blocked=None):
    config = MagicMock()
    exchange = MagicMock()
    market_data = MagicMock()
    return HMMRegimeLiveEngine(
        config, exchange, market_data,
        initial_capital_usdt=300,
        leverage=2,
        symbols=symbols or ["BTC/USDT", "ETH/USDT"],
        entry_blocked=entry_blocked or [],
    )


def test_entry_blocked_default_empty():
    eng = _make_engine()
    assert eng._entry_blocked == set()


def test_entry_blocked_initialized_from_list():
    eng = _make_engine(entry_blocked=["ETH/USDT"])
    assert "ETH/USDT" in eng._entry_blocked
    assert "BTC/USDT" not in eng._entry_blocked


def test_entry_blocked_set_semantics_dedupe():
    eng = _make_engine(entry_blocked=["ETH/USDT", "ETH/USDT"])
    assert eng._entry_blocked == {"ETH/USDT"}


def test_entry_blocked_does_not_affect_symbols_list():
    """blocked 심볼도 _symbols 에 남아있어야 (기존 포지션 관리 위해)."""
    eng = _make_engine(symbols=["BTC/USDT", "ETH/USDT"], entry_blocked=["ETH/USDT"])
    assert "ETH/USDT" in eng._symbols
    assert "BTC/USDT" in eng._symbols


@pytest.mark.asyncio
async def test_blocked_symbol_skips_open_position(monkeypatch):
    """blocked 심볼은 _open_position 호출 안됨, close 는 호출됨."""
    eng = _make_engine(symbols=["ETH/USDT"], entry_blocked=["ETH/USDT"])

    # Mocks
    open_mock = AsyncMock()
    close_mock = AsyncMock()
    eng._open_position = open_mock
    eng._close_position = close_mock
    eng._check_loss_limits = AsyncMock()

    # Patch _evaluate internals — desired=long(1), current=flat(0), no existing position
    # → close not called, but _open_position would be called if not blocked
    import pandas as pd
    from datetime import datetime, timezone
    df = pd.DataFrame({
        "open": [3000.0]*30, "high": [3030]*30, "low": [2970]*30,
        "close": [3000.0]*30, "volume": [1.0]*30,
    }, index=pd.date_range(end=datetime.now(timezone.utc), periods=30, freq="4h", tz="UTC"))
    eng._market_data.get_ohlcv_df = AsyncMock(return_value=df)

    # Force model state: bullish desired, currently flat
    from engine.hmm_regime_live_engine import HMMModelState
    ms = eng._models["ETH/USDT"]
    ms.last_refit_at = datetime.now(timezone.utc)
    ms.bullish_state = 0
    ms.bearish_state = 1
    ms.neutral_state = 2
    # Patch predict to return bullish state with high prob
    import numpy as np
    fake_model = MagicMock()
    fake_model.predict_proba = MagicMock(return_value=np.array([[0.9, 0.05, 0.05]]))
    fake_model.predict = MagicMock(return_value=np.array([0]))
    ms.model = fake_model

    with patch("engine.hmm_regime_live_engine.emit_event", new_callable=AsyncMock):
        await eng._evaluate("ETH/USDT")

    # blocked 이라 open 호출 안 됨
    open_mock.assert_not_called()
