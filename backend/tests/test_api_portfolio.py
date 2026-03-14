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

    # total_value_krw 검증:
    # - 서지 진입 시 futures_pm.cash_balance에서 margin이 차감됨 (summary["cash_balance_krw"] 이미 반영됨)
    # - 따라서 merge 시 current_value(= invested + unrealized)를 더해야 총 자산이 올바름
    # - 케이스: entry=100, current=100(가격 없어 fallback), unrealized=0, invested=33.33
    # - 기대: 300.0 + (33.33 + 0) = 333.33
    expected_total = round(300.0 + 33.33, 2)
    assert result["total_value_krw"] == expected_total, (
        f"total_value_krw should be {expected_total} (300.0 base + 33.33 surge margin), "
        f"got {result['total_value_krw']}. "
        "Bug: if only unrealized(=0) is added, total stays at 300.0 losing the surge margin."
    )


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


@pytest.mark.asyncio
async def test_merge_surge_total_value_includes_margin_with_pnl(session):
    """서지 포지션 총 자산 = futures 총 자산 + 서지 current_value (invested + unrealized).

    재현 시나리오:
    - 서지 마진 50 USDT로 롱 진입 (entry=100), 현재 가격=110 (+10%)
    - unrealized = 50 * 3 * (110-100)/100 = 15 USDT
    - current_value = 50 + 15 = 65 USDT
    - futures PM cash는 이미 50 USDT 차감된 상태
    - 따라서 total_value_krw 에는 current_value(65) 전체를 더해야 함
    """
    from unittest.mock import MagicMock
    from core.models import Position
    from api.portfolio import _merge_surge_positions
    from api.dependencies import engine_registry as reg

    pos = Position(
        exchange="binance_surge",
        symbol="BTC/USDT",
        quantity=0.05,
        average_buy_price=100.0,
        total_invested=50.0,
        direction="long",
        leverage=3,
        is_surge=True,
        entered_at=datetime.now(timezone.utc),
    )
    session.add(pos)
    await session.flush()

    # 서지 엔진 mock — 현재 가격 110 반환
    mock_state = MagicMock()
    mock_state.last_price = 110.0
    mock_surge_eng = MagicMock()
    mock_surge_eng._symbol_states = {"BTC/USDT": mock_state}

    summary = {
        "exchange": "binance_futures",
        "total_value_krw": 200.0,
        "cash_balance_krw": 150.0,  # 이미 서지 마진 50 차감된 상태
        "invested_value_krw": 50.0,
        "unrealized_pnl": 0.0,
        "positions": [],
    }

    original_get = reg.get_engine
    reg.get_engine = MagicMock(return_value=mock_surge_eng)
    try:
        result = await _merge_surge_positions(summary, session)
    finally:
        reg.get_engine = original_get

    # unrealized = 50 * 3 * (110-100)/100 = 15
    # current_value = 50 + 15 = 65
    # total_value_krw = 200.0 + 65 = 265.0
    assert result["unrealized_pnl"] == 15.0
    assert result["total_value_krw"] == 265.0, (
        f"Expected 265.0 (200 base + 65 surge current_value), got {result['total_value_krw']}. "
        "Bug: unrealized(15)만 더하면 215.0이 됨 — 마진 50 USDT가 누락됨."
    )
    assert result["invested_value_krw"] == 100.0  # 50 futures + 50 surge
