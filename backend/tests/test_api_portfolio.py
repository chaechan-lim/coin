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


@pytest.mark.asyncio
async def test_merge_surge_positions_into_futures(session):
    """선물 포트폴리오 요약에 서지 포지션이 병합됨."""
    from unittest.mock import MagicMock
    from core.models import Position
    from api.portfolio import _merge_surge_positions
    from api.dependencies import engine_registry as reg

    # 서지 포지션 생성
    pos = Position(
        exchange="binance_surge",
        symbol="SOL/USDT",
        quantity=1.0,
        average_buy_price=100.0,
        total_invested=33.33,
        direction="long",
        leverage=3,
        is_surge=True,
        entered_at=datetime.now(timezone.utc),
    )
    session.add(pos)
    await session.flush()

    # 기본 선물 요약
    summary = {
        "exchange": "binance_futures",
        "total_value_krw": 300.0,
        "cash_balance_krw": 250.0,
        "invested_value_krw": 50.0,
        "unrealized_pnl": 0.0,
        "positions": [],
    }

    # engine_registry mock — 서지 엔진 없음 (가격 fallback)
    original_get = reg.get_engine
    reg.get_engine = MagicMock(return_value=None)
    try:
        result = await _merge_surge_positions(summary, session)
    finally:
        reg.get_engine = original_get

    assert len(result["positions"]) == 1
    assert result["positions"][0]["symbol"] == "SOL/USDT"
    assert result["positions"][0]["is_surge"] is True
    assert result["invested_value_krw"] > 50.0  # 서지 투자금 합산


@pytest.mark.asyncio
async def test_merge_surge_no_positions(session):
    """서지 포지션 없으면 요약 변경 없음."""
    from api.portfolio import _merge_surge_positions

    summary = {
        "exchange": "binance_futures",
        "total_value_krw": 300.0,
        "positions": [],
    }

    result = await _merge_surge_positions(summary, session)
    assert len(result["positions"]) == 0
    assert result["total_value_krw"] == 300.0
