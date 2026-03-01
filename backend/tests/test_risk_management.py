"""
Tests for RiskManagementAgent drawdown and concentration checks.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from config import RiskConfig
from core.models import Position, PortfolioSnapshot
from core.enums import RiskLevel
from agents.risk_management import RiskManagementAgent


def _make_agent(max_drawdown_pct=0.10, max_single_coin_pct=0.40):
    """Create a RiskManagementAgent with mock market data."""
    config = RiskConfig(
        max_single_coin_pct=max_single_coin_pct,
        max_drawdown_pct=max_drawdown_pct,
        daily_loss_limit_pct=0.03,
        max_trade_size_pct=0.20,
    )
    md = AsyncMock()
    md.get_current_price = AsyncMock(return_value=50_000_000)
    return RiskManagementAgent(config, md)


@pytest.mark.asyncio
async def test_no_positions_no_alerts(session):
    """Empty portfolio → no risk alerts."""
    agent = _make_agent()
    alerts = await agent.evaluate(session, cash_balance=500_000)
    assert alerts == []


@pytest.mark.asyncio
async def test_drawdown_warning_on_moderate_loss(session):
    """10-20% drawdown → reduce_buying (WARNING, not CRITICAL)."""
    agent = _make_agent(max_drawdown_pct=0.10, max_single_coin_pct=0.99)

    # Create a peak snapshot at 500k
    snap = PortfolioSnapshot(
        total_value_krw=500_000,
        cash_balance_krw=500_000,
        invested_value_krw=0,
        realized_pnl=0, unrealized_pnl=0,
        peak_value=500_000, drawdown_pct=0,
    )
    session.add(snap)

    # Add a position so evaluate doesn't exit early
    # Mock price=50M, qty=0.001 → position value=50k
    pos = Position(
        symbol="BTC/KRW", quantity=0.001,
        average_buy_price=50_000_000, total_invested=50_000,
        is_paper=True,
    )
    session.add(pos)
    await session.flush()

    # Total = 390k + 50k = 440k → drawdown = (500-440)/500 = 12% > 10%
    alerts = await agent.evaluate(session, cash_balance=390_000)

    drawdown_alerts = [a for a in alerts if "낙폭" in a.message]
    assert len(drawdown_alerts) >= 1

    alert = drawdown_alerts[0]
    assert alert.action == "reduce_buying"
    assert alert.level == RiskLevel.WARNING


@pytest.mark.asyncio
async def test_drawdown_critical_on_large_loss(session):
    """20%+ drawdown → stop_buying (CRITICAL)."""
    agent = _make_agent(max_drawdown_pct=0.10)

    # Peak at 500k
    snap = PortfolioSnapshot(
        total_value_krw=500_000,
        cash_balance_krw=500_000,
        invested_value_krw=0,
        realized_pnl=0, unrealized_pnl=0,
        peak_value=500_000, drawdown_pct=0,
    )
    session.add(snap)

    pos = Position(
        symbol="BTC/KRW", quantity=0.001,
        average_buy_price=50_000_000, total_invested=50_000,
        is_paper=True,
    )
    session.add(pos)
    await session.flush()

    # Mock: price dropped → current_value = 350k (30% drawdown)
    agent._market_data.get_current_price = AsyncMock(return_value=30_000_000)
    alerts = await agent.evaluate(session, cash_balance=320_000)

    drawdown_alerts = [a for a in alerts if "낙폭" in a.message]
    assert len(drawdown_alerts) >= 1
    assert drawdown_alerts[0].level == RiskLevel.CRITICAL
    assert drawdown_alerts[0].action == "stop_buying"


@pytest.mark.asyncio
async def test_no_drawdown_when_at_peak(session):
    """When current value >= peak → no drawdown alert."""
    agent = _make_agent()

    snap = PortfolioSnapshot(
        total_value_krw=500_000,
        cash_balance_krw=500_000, invested_value_krw=0,
        realized_pnl=0, unrealized_pnl=0,
        peak_value=500_000, drawdown_pct=0,
    )
    session.add(snap)

    pos = Position(
        symbol="BTC/KRW", quantity=0.001,
        average_buy_price=50_000_000, total_invested=50_000,
        is_paper=True,
    )
    session.add(pos)
    await session.flush()

    # Current value > peak → no drawdown
    alerts = await agent.evaluate(session, cash_balance=500_000)
    drawdown_alerts = [a for a in alerts if "낙폭" in a.message]
    assert len(drawdown_alerts) == 0


@pytest.mark.asyncio
async def test_drawdown_uses_peak_value_not_max_total(session):
    """출금 후 peak_value가 조정된 경우, MAX(total_value)가 아닌 latest peak_value 사용."""
    agent = _make_agent(max_drawdown_pct=0.10, max_single_coin_pct=0.99)

    # 과거 스냅샷: total_value=500k (출금 전 높은 값)
    old_snap = PortfolioSnapshot(
        total_value_krw=500_000,
        cash_balance_krw=500_000,
        invested_value_krw=0,
        realized_pnl=0, unrealized_pnl=0,
        peak_value=500_000, drawdown_pct=0,
    )
    session.add(old_snap)
    await session.flush()

    # 출금 후 최신 스냅샷: total_value=300k, peak_value=300k (비례 조정됨)
    from sqlalchemy import update as sa_update
    new_snap = PortfolioSnapshot(
        total_value_krw=300_000,
        cash_balance_krw=0,
        invested_value_krw=300_000,
        realized_pnl=0, unrealized_pnl=0,
        peak_value=300_000, drawdown_pct=0,
    )
    session.add(new_snap)
    await session.flush()

    pos = Position(
        symbol="BTC/KRW", quantity=0.001,
        average_buy_price=50_000_000, total_invested=50_000,
        is_paper=True,
    )
    session.add(pos)
    await session.flush()

    # 현재 가치 = 240k + 50k = 290k
    # peak_value=300k → drawdown = 3.3% (< 10%) → 알림 없어야 함
    # 만약 MAX(total)=500k 사용하면 → drawdown = 42% → 가짜 알림 발생
    alerts = await agent.evaluate(session, cash_balance=240_000)
    drawdown_alerts = [a for a in alerts if "낙폭" in a.message]
    assert len(drawdown_alerts) == 0


@pytest.mark.asyncio
async def test_drawdown_no_peak_no_alert(session):
    """peak_value가 없거나 0이면 drawdown 체크 skip."""
    agent = _make_agent(max_drawdown_pct=0.10, max_single_coin_pct=0.99)

    snap = PortfolioSnapshot(
        total_value_krw=500_000,
        cash_balance_krw=500_000,
        invested_value_krw=0,
        realized_pnl=0, unrealized_pnl=0,
        peak_value=0, drawdown_pct=0,
    )
    session.add(snap)

    pos = Position(
        symbol="BTC/KRW", quantity=0.001,
        average_buy_price=50_000_000, total_invested=50_000,
        is_paper=True,
    )
    session.add(pos)
    await session.flush()

    # peak=0이면 drawdown 체크 안 함
    alerts = await agent.evaluate(session, cash_balance=100_000)
    drawdown_alerts = [a for a in alerts if "낙폭" in a.message]
    assert len(drawdown_alerts) == 0


@pytest.mark.asyncio
async def test_concentration_alert(session):
    """Single coin > max_single_coin_pct → alert."""
    agent = _make_agent(max_single_coin_pct=0.40)
    # BTC at 50M × 0.01 = 500k → 500k / (500k + 100k) = 83%
    agent._market_data.get_current_price = AsyncMock(return_value=50_000_000)

    pos = Position(
        symbol="BTC/KRW", quantity=0.01,
        average_buy_price=50_000_000, total_invested=500_000,
        is_paper=True,
    )
    session.add(pos)
    await session.flush()

    alerts = await agent.evaluate(session, cash_balance=100_000)
    conc_alerts = [a for a in alerts if "비중" in a.message]
    assert len(conc_alerts) >= 1
    assert "BTC/KRW" in conc_alerts[0].affected_coins
