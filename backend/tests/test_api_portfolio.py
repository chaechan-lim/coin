"""
Tests for portfolio API schema and snapshot logic.
"""
from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import PortfolioSnapshot
from core.schemas import PortfolioSummaryResponse, PortfolioHistoryPoint


def test_portfolio_summary_response_schema():
    """PortfolioSummaryResponse has all required fields."""
    data = PortfolioSummaryResponse(
        total_value_krw=500000,
        cash_balance_krw=450000,
        invested_value_krw=50000,
        initial_balance_krw=500000,
        realized_pnl=0,
        unrealized_pnl=1000,
        total_pnl=1000,
        total_pnl_pct=0.2,
        total_fees=150,
        trade_count=1,
        peak_value=501000,
        drawdown_pct=0.0,
        positions=[],
    )
    assert data.initial_balance_krw == 500000
    assert data.total_pnl_pct == 0.2


def test_portfolio_summary_with_positions():
    """Positions nested correctly in summary response."""
    from core.schemas import PositionResponse
    pos = PositionResponse(
        symbol="BTC/KRW",
        quantity=0.001,
        average_buy_price=50000000,
        current_price=52000000,
        current_value=52000,
        unrealized_pnl=2000,
        unrealized_pnl_pct=4.0,
    )
    summary = PortfolioSummaryResponse(
        total_value_krw=502000,
        cash_balance_krw=450000,
        invested_value_krw=52000,
        initial_balance_krw=500000,
        realized_pnl=0,
        unrealized_pnl=2000,
        total_pnl=2000,
        total_pnl_pct=0.4,
        total_fees=150,
        trade_count=1,
        peak_value=502000,
        drawdown_pct=0.0,
        positions=[pos],
    )
    assert len(summary.positions) == 1
    assert summary.positions[0].symbol == "BTC/KRW"


@pytest.mark.asyncio
async def test_portfolio_snapshot_creation(session):
    """Snapshots are stored and retrievable."""
    snap = PortfolioSnapshot(
        total_value_krw=500000,
        cash_balance_krw=450000,
        invested_value_krw=50000,
        realized_pnl=0,
        unrealized_pnl=1000,
        peak_value=501000,
        drawdown_pct=0.0,
    )
    session.add(snap)
    await session.flush()

    from sqlalchemy import select
    result = await session.execute(select(PortfolioSnapshot))
    snaps = result.scalars().all()
    assert len(snaps) == 1
    assert snaps[0].total_value_krw == 500000


def test_portfolio_history_point_schema():
    """PortfolioHistoryPoint accepts datetime correctly."""
    pt = PortfolioHistoryPoint(
        timestamp=datetime.now(timezone.utc),
        total_value=500000,
        cash_balance=450000,
        unrealized_pnl=1000,
        drawdown_pct=0.5,
    )
    assert pt.total_value == 500000
