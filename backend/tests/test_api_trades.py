"""
Tests for trade summary calculation (api/trades.py).
"""
from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from collections import defaultdict

from core.models import Order
from tests.conftest import make_order


async def _insert_orders(session: AsyncSession, orders: list[Order]):
    for o in orders:
        session.add(o)
    await session.flush()


async def _calc_trade_summary(session: AsyncSession, period_days: int | None = 7):
    """Replicate the trade summary calculation from api/trades.py."""
    from core.utils import utcnow

    start = utcnow() - timedelta(days=period_days) if period_days else None

    result = await session.execute(
        select(Order).where(Order.status == "filled").order_by(Order.created_at)
    )
    all_orders = list(result.scalars().all())

    positions: dict[str, dict] = defaultdict(lambda: {"qty": 0.0, "cost": 0.0})
    buy_count = 0
    sell_count = 0
    winning = 0
    losing = 0
    total_pnl = 0.0

    for order in all_orders:
        sym = order.symbol
        qty = order.executed_quantity or order.requested_quantity
        price = order.executed_price or order.requested_price
        fee = order.fee or 0
        in_period = start is None or order.created_at >= start

        if not price or not qty:
            continue

        if order.side == "buy":
            positions[sym]["cost"] += price * qty + fee
            positions[sym]["qty"] += qty
            if in_period:
                buy_count += 1
        elif order.side == "sell":
            pos = positions[sym]
            if pos["qty"] > 0:
                avg_buy = pos["cost"] / pos["qty"]
                sell_qty = min(qty, pos["qty"])
                pnl = (price - avg_buy) * sell_qty - fee

                if in_period:
                    sell_count += 1
                    total_pnl += pnl
                    if pnl > 0:
                        winning += 1
                    else:
                        losing += 1

                pos["cost"] -= avg_buy * sell_qty
                pos["qty"] -= sell_qty

    return {
        "total_trades": buy_count + sell_count,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "winning_trades": winning,
        "losing_trades": losing,
        "win_rate": round(winning / (winning + losing) * 100, 1) if (winning + losing) > 0 else 0,
        "total_pnl": round(total_pnl, 0),
    }


@pytest.mark.asyncio
async def test_summary_profitable_round_trip(session):
    """Buy then sell at profit → positive P&L, win."""
    now = datetime.now(timezone.utc)
    await _insert_orders(session, [
        make_order(side="buy", executed_price=50_000_000, fee=150,
                   created_at=now - timedelta(hours=2)),
        make_order(side="sell", executed_price=51_000_000, fee=153,
                   created_at=now - timedelta(hours=1)),
    ])
    await session.commit()

    result = await _calc_trade_summary(session)
    assert result["buy_count"] == 1
    assert result["sell_count"] == 1
    assert result["total_trades"] == 2
    assert result["winning_trades"] == 1
    assert result["total_pnl"] > 0


@pytest.mark.asyncio
async def test_summary_losing_round_trip(session):
    """Buy then sell at loss → negative P&L."""
    now = datetime.now(timezone.utc)
    await _insert_orders(session, [
        make_order(side="buy", executed_price=50_000_000, fee=150,
                   created_at=now - timedelta(hours=2)),
        make_order(side="sell", executed_price=49_000_000, fee=147,
                   created_at=now - timedelta(hours=1)),
    ])
    await session.commit()

    result = await _calc_trade_summary(session)
    assert result["losing_trades"] == 1
    assert result["total_pnl"] < 0


@pytest.mark.asyncio
async def test_summary_buy_only(session):
    """Only buys → no wins/losses, zero P&L."""
    now = datetime.now(timezone.utc)
    await _insert_orders(session, [
        make_order(side="buy", executed_price=50_000_000, fee=150, created_at=now),
        make_order(side="buy", executed_price=51_000_000, fee=153,
                   strategy_name="ma_crossover", created_at=now),
    ])
    await session.commit()

    result = await _calc_trade_summary(session)
    assert result["buy_count"] == 2
    assert result["sell_count"] == 0
    assert result["total_pnl"] == 0


@pytest.mark.asyncio
async def test_summary_cancelled_orders_excluded(session):
    """Cancelled orders should not appear in summary."""
    now = datetime.now(timezone.utc)
    await _insert_orders(session, [
        make_order(side="buy", status="cancelled", created_at=now),
        make_order(side="buy", status="filled", executed_price=50_000_000,
                   fee=150, created_at=now),
    ])
    await session.commit()

    result = await _calc_trade_summary(session)
    assert result["total_trades"] == 1  # only the filled one


@pytest.mark.asyncio
async def test_summary_multiple_symbols(session):
    """P&L calculated per-symbol, not mixed."""
    now = datetime.now(timezone.utc)
    await _insert_orders(session, [
        # BTC: buy 50M, sell 52M → profit
        make_order(symbol="BTC/KRW", side="buy", executed_price=50_000_000,
                   fee=150, created_at=now - timedelta(hours=4)),
        make_order(symbol="BTC/KRW", side="sell", executed_price=52_000_000,
                   fee=156, created_at=now - timedelta(hours=3)),
        # ETH: buy 4M, sell 3.5M → loss
        make_order(symbol="ETH/KRW", side="buy", executed_price=4_000_000,
                   requested_quantity=0.01, executed_quantity=0.01,
                   fee=120, created_at=now - timedelta(hours=2)),
        make_order(symbol="ETH/KRW", side="sell", executed_price=3_500_000,
                   requested_quantity=0.01, executed_quantity=0.01,
                   fee=105, created_at=now - timedelta(hours=1)),
    ])
    await session.commit()

    result = await _calc_trade_summary(session)
    assert result["winning_trades"] == 1
    assert result["losing_trades"] == 1
    assert result["win_rate"] == 50.0
