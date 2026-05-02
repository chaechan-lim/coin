"""
Momentum epoch 메커니즘 테스트.

epoch_started_at 이후 주문만 session_pnl로 카운트하여 paused 판정.
lifetime cumulative_pnl 은 표시용 (모든 주문 합산).
"""
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from engine.momentum_rotation_live_engine import MomentumRotationLiveEngine, MAX_TOTAL_LOSS_PCT


def _mk_engine(capital=200, epoch=None):
    config = MagicMock()
    if epoch:
        config.momentum_rotation_epoch_started_at = epoch
    else:
        # MagicMock 자동 속성 차단
        config.momentum_rotation_epoch_started_at = ""
    exchange = MagicMock()
    market_data = MagicMock()
    return MomentumRotationLiveEngine(config, exchange, market_data, initial_capital_usdt=capital)


def test_epoch_parses_iso_string():
    """epoch_started_at = '2026-05-02T15:00:00Z' 파싱 정상."""
    eng = _mk_engine(epoch="2026-05-02T15:00:00Z")
    assert eng._epoch_started_at is not None
    assert eng._epoch_started_at.tzinfo is not None
    assert eng._epoch_started_at.year == 2026


def test_epoch_empty_means_no_epoch():
    """epoch_started_at 비어있으면 None (하위 호환)."""
    eng = _mk_engine(epoch="")
    assert eng._epoch_started_at is None


def test_epoch_invalid_falls_back_to_none():
    """파싱 실패해도 크래시 없이 None."""
    eng = _mk_engine(epoch="not-a-date")
    assert eng._epoch_started_at is None


@pytest.mark.asyncio
async def test_check_loss_uses_session_pnl_when_epoch_set():
    """epoch 있으면 session_pnl 기반 paused 판정 (lifetime 무시)."""
    eng = _mk_engine(capital=200, epoch="2026-05-02T15:00:00Z")
    # lifetime은 -50 (한도 초과) 이지만 session은 -5 (한도 내)
    eng._cumulative_pnl = -50.0
    eng._session_pnl = -5.0
    with patch("engine.momentum_rotation_live_engine.emit_event", new_callable=AsyncMock):
        await eng._check_loss_limits()
    assert eng._paused is False  # session 기반이라 통과


@pytest.mark.asyncio
async def test_check_loss_pauses_when_session_exceeds_limit():
    """session_pnl이 한도 초과면 paused (lifetime 영향 없음)."""
    eng = _mk_engine(capital=200, epoch="2026-05-02T15:00:00Z")
    eng._cumulative_pnl = +10.0  # lifetime 양수여도
    eng._session_pnl = -25.0      # session이 -10% 초과 → paused
    with patch("engine.momentum_rotation_live_engine.emit_event", new_callable=AsyncMock):
        await eng._check_loss_limits()
    assert eng._paused is True


@pytest.mark.asyncio
async def test_check_loss_falls_back_to_cumulative_when_no_epoch():
    """epoch 없으면 cumulative_pnl 기반 (이전 동작 유지)."""
    eng = _mk_engine(capital=200, epoch="")
    eng._cumulative_pnl = -25.0
    eng._session_pnl = 0.0  # 사용 안 됨
    with patch("engine.momentum_rotation_live_engine.emit_event", new_callable=AsyncMock):
        await eng._check_loss_limits()
    assert eng._paused is True


def test_get_status_includes_session_and_epoch():
    """get_status에 session_pnl + epoch_started_at 노출."""
    eng = _mk_engine(epoch="2026-05-02T15:00:00Z")
    eng._cumulative_pnl = -22.73
    eng._session_pnl = -3.5
    s = eng.get_status()
    assert s["cumulative_pnl"] == -22.73
    assert s["session_pnl"] == -3.5
    assert s["epoch_started_at"] is not None
    assert "2026-05-02" in s["epoch_started_at"]
