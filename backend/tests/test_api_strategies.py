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


async def _calc_performance(
    session: AsyncSession, strategy_name: str, days: int = 30, exchange: str = "bithumb",
):
    """Replicate the strategy performance calculation from api/strategies.py."""
    from collections import defaultdict
    from core.utils import utcnow, ensure_aware

    start = utcnow() - timedelta(days=days)
    is_futures = "futures" in exchange

    result = await session.execute(
        select(Order)
        .where(Order.status == "filled", Order.exchange == exchange)
        .order_by(Order.created_at)
    )
    all_orders = list(result.scalars().all())

    long_positions: dict[str, dict] = defaultdict(lambda: {"qty": 0.0, "cost": 0.0})
    short_positions: dict[str, dict] = defaultdict(lambda: {"qty": 0.0, "cost": 0.0})
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

        direction = getattr(order, "direction", None) or "long"
        is_short = is_futures and direction == "short"
        in_period = order.strategy_name == strategy_name and ensure_aware(order.created_at) >= start

        if is_short:
            if order.side == "sell":
                short_positions[sym]["cost"] += price * qty + fee
                short_positions[sym]["qty"] += qty
                if in_period:
                    trade_count += 1
            elif order.side == "buy":
                pos = short_positions[sym]
                if pos["qty"] > 0:
                    avg_entry = pos["cost"] / pos["qty"]
                    close_qty = min(qty, pos["qty"])
                    pnl = (avg_entry - price) * close_qty - fee

                    if in_period:
                        total_pnl += pnl
                        ret_pct = pnl / (avg_entry * close_qty) * 100 if avg_entry > 0 else 0
                        returns.append(ret_pct)
                        trade_count += 1
                        if pnl > 0:
                            winning += 1
                        else:
                            losing += 1

                    pos["cost"] -= avg_entry * close_qty
                    pos["qty"] -= close_qty
        else:
            if order.side == "buy":
                long_positions[sym]["cost"] += price * qty + fee
                long_positions[sym]["qty"] += qty
                if in_period:
                    trade_count += 1
            elif order.side == "sell":
                pos = long_positions[sym]
                if pos["qty"] > 0:
                    avg_buy = pos["cost"] / pos["qty"]
                    sell_qty = min(qty, pos["qty"])
                    pnl = (price - avg_buy) * sell_qty - fee

                    if in_period:
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


# ── Futures Short PnL Tests ──


@pytest.mark.asyncio
async def test_futures_short_profitable(session):
    """선물 숏: 높은 가격에 진입(sell), 낮은 가격에 청산(buy) → 수익."""
    now = datetime.now(timezone.utc)
    await _insert_orders(session, [
        # 숏 진입: sell at 100,000
        make_order(
            symbol="BTC/USDT", side="sell", executed_price=100_000,
            requested_quantity=0.01, executed_quantity=0.01,
            fee=0.04, exchange="binance_futures", direction="short",
            created_at=now - timedelta(hours=2),
        ),
        # 숏 청산: buy at 95,000
        make_order(
            symbol="BTC/USDT", side="buy", executed_price=95_000,
            requested_quantity=0.01, executed_quantity=0.01,
            fee=0.038, exchange="binance_futures", direction="short",
            created_at=now - timedelta(hours=1),
        ),
    ])
    await session.commit()

    result = await _calc_performance(session, "rsi", exchange="binance_futures")
    # PnL = (100000 - 95000) * 0.01 - 0.038 = 49.962
    assert result["total_pnl"] > 0, f"Short should be profitable, got {result['total_pnl']}"
    assert result["winning"] == 1
    assert result["losing"] == 0


@pytest.mark.asyncio
async def test_futures_short_loss(session):
    """선물 숏: 낮은 가격에 진입, 높은 가격에 청산 → 손실."""
    now = datetime.now(timezone.utc)
    await _insert_orders(session, [
        make_order(
            symbol="ETH/USDT", side="sell", executed_price=3000,
            requested_quantity=0.1, executed_quantity=0.1,
            fee=0.12, exchange="binance_futures", direction="short",
            created_at=now - timedelta(hours=2),
        ),
        make_order(
            symbol="ETH/USDT", side="buy", executed_price=3200,
            requested_quantity=0.1, executed_quantity=0.1,
            fee=0.128, exchange="binance_futures", direction="short",
            created_at=now - timedelta(hours=1),
        ),
    ])
    await session.commit()

    result = await _calc_performance(session, "rsi", exchange="binance_futures")
    # PnL = (3000 - 3200) * 0.1 - 0.128 = -20.128
    assert result["total_pnl"] < 0, f"Short should be loss, got {result['total_pnl']}"
    assert result["winning"] == 0
    assert result["losing"] == 1


@pytest.mark.asyncio
async def test_futures_long_still_works(session):
    """선물 롱은 기존과 동일하게 작동."""
    now = datetime.now(timezone.utc)
    await _insert_orders(session, [
        make_order(
            symbol="BTC/USDT", side="buy", executed_price=90_000,
            requested_quantity=0.01, executed_quantity=0.01,
            fee=0.036, exchange="binance_futures", direction="long",
            created_at=now - timedelta(hours=2),
        ),
        make_order(
            symbol="BTC/USDT", side="sell", executed_price=95_000,
            requested_quantity=0.01, executed_quantity=0.01,
            fee=0.038, exchange="binance_futures", direction="long",
            created_at=now - timedelta(hours=1),
        ),
    ])
    await session.commit()

    result = await _calc_performance(session, "rsi", exchange="binance_futures")
    assert result["total_pnl"] > 0
    assert result["winning"] == 1


@pytest.mark.asyncio
async def test_futures_mixed_long_short(session):
    """선물 롱+숏 혼합 시 각각 독립 계산."""
    now = datetime.now(timezone.utc)
    await _insert_orders(session, [
        # 롱: buy 90k → sell 95k → profit
        make_order(
            symbol="BTC/USDT", side="buy", executed_price=90_000,
            requested_quantity=0.01, executed_quantity=0.01,
            fee=0, exchange="binance_futures", direction="long",
            created_at=now - timedelta(hours=4),
        ),
        make_order(
            symbol="BTC/USDT", side="sell", executed_price=95_000,
            requested_quantity=0.01, executed_quantity=0.01,
            fee=0, exchange="binance_futures", direction="long",
            created_at=now - timedelta(hours=3),
        ),
        # 숏: sell 95k → buy 93k → profit
        make_order(
            symbol="BTC/USDT", side="sell", executed_price=95_000,
            requested_quantity=0.01, executed_quantity=0.01,
            fee=0, exchange="binance_futures", direction="short",
            created_at=now - timedelta(hours=2),
        ),
        make_order(
            symbol="BTC/USDT", side="buy", executed_price=93_000,
            requested_quantity=0.01, executed_quantity=0.01,
            fee=0, exchange="binance_futures", direction="short",
            created_at=now - timedelta(hours=1),
        ),
    ])
    await session.commit()

    result = await _calc_performance(session, "rsi", exchange="binance_futures")
    # 롱 PnL: (95k-90k)*0.01 = 50
    # 숏 PnL: (95k-93k)*0.01 = 20
    # 합계: 70
    assert result["total_pnl"] == pytest.approx(70, abs=1)
    assert result["winning"] == 2
    assert result["losing"] == 0
