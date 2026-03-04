"""
Tests for DailyPnL recording (PortfolioManager.record_daily_pnl).
"""
from datetime import datetime, timezone, timedelta, date
import pytest
from sqlalchemy import select

from core.models import PortfolioSnapshot, Order, Position, DailyPnL
from engine.portfolio_manager import PortfolioManager


def _utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_record_daily_pnl_basic(session):
    """Basic daily PnL from snapshots."""
    target = date(2026, 3, 1)

    # Create snapshots for the day
    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=500_000,
        cash_balance_krw=500_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 1, 0, 5),
    ))
    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=505_000,
        cash_balance_krw=505_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 1, 23, 55),
    ))
    await session.flush()

    record = await PortfolioManager.record_daily_pnl(session, "bithumb", target)
    assert record is not None
    assert record.open_value == 500_000
    assert record.close_value == 505_000
    assert record.daily_pnl == 5_000
    assert round(record.daily_pnl_pct, 2) == 1.0
    assert record.trade_count == 0


@pytest.mark.asyncio
async def test_record_daily_pnl_with_trades(session):
    """Daily PnL with buy/sell orders."""
    target = date(2026, 3, 2)

    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=500_000,
        cash_balance_krw=500_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 2, 0, 5),
    ))
    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=510_000,
        cash_balance_krw=510_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 2, 23, 50),
    ))

    # Buy order
    session.add(Order(
        exchange="bithumb",
        symbol="BTC/KRW",
        side="buy",
        order_type="limit",
        status="filled",
        requested_price=50_000_000,
        executed_price=50_000_000,
        requested_quantity=0.001,
        executed_quantity=0.001,
        fee=150,
        is_paper=True,
        strategy_name="rsi",
        signal_confidence=0.7,
        created_at=_utc(2026, 3, 2, 10, 0),
    ))
    # Sell order
    session.add(Order(
        exchange="bithumb",
        symbol="BTC/KRW",
        side="sell",
        order_type="limit",
        status="filled",
        requested_price=51_000_000,
        executed_price=51_000_000,
        requested_quantity=0.001,
        executed_quantity=0.001,
        fee=150,
        is_paper=True,
        strategy_name="rsi",
        signal_confidence=0.7,
        created_at=_utc(2026, 3, 2, 15, 0),
    ))
    await session.flush()

    record = await PortfolioManager.record_daily_pnl(session, "bithumb", target)
    assert record is not None
    assert record.trade_count == 2
    assert record.buy_count == 1
    assert record.sell_count == 1
    assert record.daily_pnl == 10_000


@pytest.mark.asyncio
async def test_record_daily_pnl_no_snapshots(session):
    """Returns None when no snapshots exist for the day."""
    target = date(2026, 1, 1)
    record = await PortfolioManager.record_daily_pnl(session, "bithumb", target)
    assert record is None


@pytest.mark.asyncio
async def test_record_daily_pnl_upsert(session):
    """Re-running updates existing record instead of duplicating."""
    target = date(2026, 3, 3)

    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=500_000,
        cash_balance_krw=500_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 3, 1, 0),
    ))
    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=502_000,
        cash_balance_krw=502_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 3, 23, 0),
    ))
    await session.flush()

    # First run
    r1 = await PortfolioManager.record_daily_pnl(session, "bithumb", target)
    assert r1 is not None
    assert r1.daily_pnl == 2_000

    # Add another snapshot to change close value
    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=508_000,
        cash_balance_krw=508_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 3, 23, 55),
    ))
    await session.flush()

    # Second run — should update, not duplicate
    r2 = await PortfolioManager.record_daily_pnl(session, "bithumb", target)
    assert r2 is not None
    assert r2.daily_pnl == 8_000

    # Only one record
    result = await session.execute(
        select(DailyPnL).where(DailyPnL.exchange == "bithumb", DailyPnL.date == target)
    )
    assert len(list(result.scalars().all())) == 1


@pytest.mark.asyncio
async def test_record_daily_pnl_exchange_isolation(session):
    """Different exchanges produce separate records."""
    target = date(2026, 3, 4)

    for ex, val in [("bithumb", 500_000), ("binance_futures", 1000)]:
        session.add(PortfolioSnapshot(
            exchange=ex,
            total_value_krw=val,
            cash_balance_krw=val,
            invested_value_krw=0,
            snapshot_at=_utc(2026, 3, 4, 0, 5),
        ))
        session.add(PortfolioSnapshot(
            exchange=ex,
            total_value_krw=val + 100,
            cash_balance_krw=val + 100,
            invested_value_krw=0,
            snapshot_at=_utc(2026, 3, 4, 23, 55),
        ))
    await session.flush()

    r_bithumb = await PortfolioManager.record_daily_pnl(session, "bithumb", target)
    r_binance = await PortfolioManager.record_daily_pnl(session, "binance_futures", target)

    assert r_bithumb.daily_pnl == 100
    assert r_binance.daily_pnl == 100
    assert r_bithumb.open_value == 500_000
    assert r_binance.open_value == 1000
