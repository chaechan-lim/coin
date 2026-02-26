"""
Tests for strategy performance calculation (api/strategies.py).

These validate the core P&L matching logic independently of FastAPI.
"""
from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Order
from tests.conftest import make_order


async def _insert_orders(session: AsyncSession, orders: list[Order]):
    for o in orders:
        session.add(o)
    await session.flush()


async def _calc_performance(session: AsyncSession, strategy_name: str, days: int = 30):
    """Replicate the strategy performance calculation from api/strategies.py."""
    from collections import defaultdict
    from core.utils import utcnow

    start = utcnow() - timedelta(days=days)

    result = await session.execute(
        select(Order).where(Order.status == "filled").order_by(Order.created_at)
    )
    all_orders = list(result.scalars().all())

    positions: dict[str, dict] = defaultdict(lambda: {"qty": 0.0, "cost": 0.0})
    winning = 0
    losing = 0
    total_pnl = 0.0
    returns: list[float] = []
    trade_count = 0

    for order in all_orders:
        sym = order.symbol
        qty = order.executed_quantity or order.requested_quantity
        price = order.executed_price or order.requested_price
        fee = order.fee or 0

        if not price or not qty:
            continue

        from core.utils import ensure_aware
        if order.side == "buy":
            positions[sym]["cost"] += price * qty + fee
            positions[sym]["qty"] += qty
            if order.strategy_name == strategy_name and ensure_aware(order.created_at) >= start:
                trade_count += 1
        elif order.side == "sell":
            pos = positions[sym]
            if pos["qty"] > 0:
                avg_buy = pos["cost"] / pos["qty"]
                sell_qty = min(qty, pos["qty"])
                pnl = (price - avg_buy) * sell_qty - fee

                if order.strategy_name == strategy_name and ensure_aware(order.created_at) >= start:
                    total_pnl += pnl
                    ret_pct = pnl / (avg_buy * sell_qty) * 100 if avg_buy > 0 else 0
                    returns.append(ret_pct)
                    trade_count += 1
                    if pnl > 0:
                        winning += 1
                    else:
                        losing += 1

                pos["cost"] -= avg_buy * sell_qty
                pos["qty"] -= sell_qty

    win_rate = winning / (winning + losing) * 100 if (winning + losing) > 0 else 0
    avg_return = sum(returns) / len(returns) if returns else 0

    return {
        "trade_count": trade_count,
        "winning": winning,
        "losing": losing,
        "total_pnl": round(total_pnl, 0),
        "win_rate": round(win_rate, 1),
        "avg_return": round(avg_return, 2),
    }


@pytest.mark.asyncio
async def test_profitable_sell_shows_positive_pnl(session):
    """Buy at 50M, sell at 52M → positive P&L."""
    now = datetime.now(timezone.utc)
    await _insert_orders(session, [
        make_order(side="buy", executed_price=50_000_000, fee=150,
                   created_at=now - timedelta(hours=2)),
        make_order(side="sell", executed_price=52_000_000, fee=156,
                   created_at=now - timedelta(hours=1)),
    ])
    await session.commit()

    result = await _calc_performance(session, "rsi")
    assert result["total_pnl"] > 0, f"Expected positive PnL, got {result['total_pnl']}"
    assert result["winning"] == 1
    assert result["losing"] == 0
    assert result["win_rate"] == 100.0


@pytest.mark.asyncio
async def test_loss_sell_shows_negative_pnl(session):
    """Buy at 50M, sell at 48M → negative P&L."""
    now = datetime.now(timezone.utc)
    await _insert_orders(session, [
        make_order(side="buy", executed_price=50_000_000, fee=150,
                   created_at=now - timedelta(hours=2)),
        make_order(side="sell", executed_price=48_000_000, fee=144,
                   created_at=now - timedelta(hours=1)),
    ])
    await session.commit()

    result = await _calc_performance(session, "rsi")
    assert result["total_pnl"] < 0, f"Expected negative PnL, got {result['total_pnl']}"
    assert result["winning"] == 0
    assert result["losing"] == 1


@pytest.mark.asyncio
async def test_no_sell_orders_means_no_pnl(session):
    """Only buy orders → no P&L, just trade count."""
    now = datetime.now(timezone.utc)
    await _insert_orders(session, [
        make_order(side="buy", executed_price=50_000_000, fee=150, created_at=now),
    ])
    await session.commit()

    result = await _calc_performance(session, "rsi")
    assert result["total_pnl"] == 0
    assert result["trade_count"] == 1  # buy counted
    assert result["winning"] == 0
    assert result["losing"] == 0


@pytest.mark.asyncio
async def test_cross_strategy_buy_cost_basis(session):
    """Buy from strategy A, sell from strategy B → B uses correct cost basis."""
    now = datetime.now(timezone.utc)
    await _insert_orders(session, [
        make_order(side="buy", strategy_name="ma_crossover",
                   executed_price=50_000_000, fee=150,
                   created_at=now - timedelta(hours=2)),
        make_order(side="sell", strategy_name="rsi",
                   executed_price=52_000_000, fee=156,
                   created_at=now - timedelta(hours=1)),
    ])
    await session.commit()

    result = await _calc_performance(session, "rsi")
    # sell at 52M, cost basis from ma_crossover buy at 50M
    assert result["total_pnl"] > 0
    assert result["winning"] == 1
    assert result["trade_count"] == 1  # only the sell is rsi's


@pytest.mark.asyncio
async def test_multiple_buys_averaged_cost(session):
    """Two buys at different prices → average cost used for sell P&L."""
    now = datetime.now(timezone.utc)
    await _insert_orders(session, [
        make_order(side="buy", executed_price=50_000_000,
                   requested_quantity=0.001, executed_quantity=0.001,
                   fee=150, created_at=now - timedelta(hours=3)),
        make_order(side="buy", executed_price=52_000_000,
                   requested_quantity=0.001, executed_quantity=0.001,
                   fee=156, created_at=now - timedelta(hours=2)),
        # Sell 0.001 at 53M
        make_order(side="sell", executed_price=53_000_000,
                   requested_quantity=0.001, executed_quantity=0.001,
                   fee=159, created_at=now - timedelta(hours=1)),
    ])
    await session.commit()

    result = await _calc_performance(session, "rsi")
    # avg buy = (50M*0.001+150 + 52M*0.001+156) / 0.002 = ~51,153,000
    # pnl = (53M - ~51.15M)*0.001 - 159 = ~1,694
    assert result["total_pnl"] > 0
    assert result["winning"] == 1


@pytest.mark.asyncio
async def test_empty_orders_returns_zero(session):
    """No orders at all → everything zero."""
    result = await _calc_performance(session, "rsi")
    assert result["total_pnl"] == 0
    assert result["trade_count"] == 0
    assert result["win_rate"] == 0


@pytest.mark.asyncio
async def test_fee_included_in_cost_basis(session):
    """Fees are added to buy cost and subtracted from sell proceeds."""
    now = datetime.now(timezone.utc)
    # Buy at 50M, fee 500, sell at exactly 50M, fee 500
    # Net loss = buy_fee_portion_in_avg + sell_fee
    await _insert_orders(session, [
        make_order(side="buy", executed_price=50_000_000,
                   requested_quantity=0.001, executed_quantity=0.001,
                   fee=500, created_at=now - timedelta(hours=2)),
        make_order(side="sell", executed_price=50_000_000,
                   requested_quantity=0.001, executed_quantity=0.001,
                   fee=500, created_at=now - timedelta(hours=1)),
    ])
    await session.commit()

    result = await _calc_performance(session, "rsi")
    # buy cost per unit = (50M*0.001 + 500) / 0.001 = 50,500,000
    # sell pnl = (50M - 50.5M)*0.001 - 500 = -500 - 500 = -1000
    assert result["total_pnl"] < 0, "Fees should cause net loss on flat trade"
    assert result["losing"] == 1
