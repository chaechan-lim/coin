from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from datetime import timedelta
from core.utils import utcnow

from db.session import get_db
from core.models import PortfolioSnapshot
from core.schemas import PortfolioSummaryResponse, PortfolioHistoryPoint
from api.dependencies import engine_registry

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

# Legacy setter for backward compatibility
_portfolio_manager = None


def set_portfolio_manager(pm):
    global _portfolio_manager
    _portfolio_manager = pm


def _get_pm(exchange: str):
    pm = engine_registry.get_portfolio_manager(exchange)
    if pm:
        return pm
    return _portfolio_manager


@router.get("/summary", response_model=PortfolioSummaryResponse)
async def get_portfolio_summary(
    exchange: str = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    pm = _get_pm(exchange)
    if not pm:
        return PortfolioSummaryResponse(
            exchange=exchange,
            total_value_krw=0, cash_balance_krw=0, invested_value_krw=0,
            initial_balance_krw=0,
            realized_pnl=0, unrealized_pnl=0, total_pnl=0, total_pnl_pct=0,
            total_fees=0, trade_count=0,
            peak_value=0, drawdown_pct=0, positions=[],
        )
    summary = await pm.get_portfolio_summary(session)
    return PortfolioSummaryResponse(**summary)


@router.get("/history", response_model=list[PortfolioHistoryPoint])
async def get_portfolio_history(
    period: str = Query("7d", pattern="^(1d|7d|30d|90d|all)$"),
    exchange: str = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    now = utcnow()
    period_map = {
        "1d": timedelta(days=1),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
        "90d": timedelta(days=90),
    }

    query = (
        select(PortfolioSnapshot)
        .where(PortfolioSnapshot.exchange == exchange)
        .order_by(desc(PortfolioSnapshot.snapshot_at))
    )

    if period != "all" and period in period_map:
        start = now - period_map[period]
        query = query.where(PortfolioSnapshot.snapshot_at >= start)

    query = query.limit(1000)
    result = await session.execute(query)
    snapshots = result.scalars().all()

    return [
        PortfolioHistoryPoint(
            timestamp=s.snapshot_at,
            total_value=s.total_value_krw,
            cash_balance=s.cash_balance_krw,
            unrealized_pnl=s.unrealized_pnl,
            drawdown_pct=s.drawdown_pct,
        )
        for s in reversed(list(snapshots))
    ]
