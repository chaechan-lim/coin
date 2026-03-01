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

    long_lots: dict[str, list[dict]] = defaultdict(list)
    short_lots: dict[str, list[dict]] = defaultdict(list)
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

        if is_short:
            if order.side == "sell":
                short_lots[sym].append({
                    "strategy": order.strategy_name,
                    "qty": qty,
                    "cost": price * qty + fee,
                    "time": order.created_at,
                })
                if order.strategy_name == strategy_name and ensure_aware(order.created_at) >= start:
                    trade_count += 1
            elif order.side == "buy":
                remaining = qty
                fee_remaining = fee
                while remaining > 0 and short_lots[sym]:
                    lot = short_lots[sym][0]
                    close_qty = min(remaining, lot["qty"])
                    avg_entry = lot["cost"] / lot["qty"]
                    lot_fee = fee_remaining * (close_qty / qty) if qty > 0 else 0
                    pnl = (avg_entry - price) * close_qty - lot_fee

                    in_period = lot["strategy"] == strategy_name and ensure_aware(lot["time"]) >= start
                    if in_period:
                        total_pnl += pnl
                        ret_pct = pnl / (avg_entry * close_qty) * 100 if avg_entry > 0 else 0
                        returns.append(ret_pct)
                        trade_count += 1
                        if pnl > 0:
                            winning += 1
                        else:
                            losing += 1

                    lot["qty"] -= close_qty
                    lot["cost"] -= avg_entry * close_qty
                    fee_remaining -= lot_fee
                    remaining -= close_qty
                    if lot["qty"] <= 1e-12:
                        short_lots[sym].pop(0)
        else:
            if order.side == "buy":
                long_lots[sym].append({
                    "strategy": order.strategy_name,
                    "qty": qty,
                    "cost": price * qty + fee,
                    "time": order.created_at,
                })
                if order.strategy_name == strategy_name and ensure_aware(order.created_at) >= start:
                    trade_count += 1
            elif order.side == "sell":
                remaining = qty
                fee_remaining = fee
                while remaining > 0 and long_lots[sym]:
                    lot = long_lots[sym][0]
                    close_qty = min(remaining, lot["qty"])
                    avg_entry = lot["cost"] / lot["qty"]
                    lot_fee = fee_remaining * (close_qty / qty) if qty > 0 else 0
                    pnl = (price - avg_entry) * close_qty - lot_fee

                    in_period = lot["strategy"] == strategy_name and ensure_aware(lot["time"]) >= start
                    if in_period:
                        total_pnl += pnl
                        ret_pct = pnl / (avg_entry * close_qty) * 100 if avg_entry > 0 else 0
                        returns.append(ret_pct)
                        trade_count += 1
                        if pnl > 0:
                            winning += 1
                        else:
                            losing += 1

                    lot["qty"] -= close_qty
                    lot["cost"] -= avg_entry * close_qty
                    fee_remaining -= lot_fee
                    remaining -= close_qty
                    if lot["qty"] <= 1e-12:
                        long_lots[sym].pop(0)

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
    """Buy from strategy A, sell from strategy B → PnL attributed to entry strategy A."""
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

    # PnL is now attributed to the entry strategy (ma_crossover), not exit (rsi)
    result_entry = await _calc_performance(session, "ma_crossover")
    assert result_entry["total_pnl"] > 0
    assert result_entry["winning"] == 1
    assert result_entry["trade_count"] == 2  # 1 entry + 1 exit(PnL)

    result_exit = await _calc_performance(session, "rsi")
    assert result_exit["total_pnl"] == 0
    assert result_exit["trade_count"] == 0  # rsi has no entries/exits attributed


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


# ── Entry Strategy PnL Attribution Tests ──


@pytest.mark.asyncio
async def test_entry_strategy_gets_pnl_not_exit(session):
    """ma_crossover 매수 → futures_stop 매도 → PnL은 ma_crossover에 귀속."""
    now = datetime.now(timezone.utc)
    await _insert_orders(session, [
        make_order(
            symbol="BTC/USDT", side="buy", strategy_name="ma_crossover",
            executed_price=90_000, requested_quantity=0.01, executed_quantity=0.01,
            fee=0.036, exchange="binance_futures", direction="long",
            created_at=now - timedelta(hours=2),
        ),
        make_order(
            symbol="BTC/USDT", side="sell", strategy_name="futures_stop",
            executed_price=95_000, requested_quantity=0.01, executed_quantity=0.01,
            fee=0.038, exchange="binance_futures", direction="long",
            created_at=now - timedelta(hours=1),
        ),
    ])
    await session.commit()

    # PnL attributed to entry strategy (ma_crossover)
    result_entry = await _calc_performance(session, "ma_crossover", exchange="binance_futures")
    assert result_entry["total_pnl"] > 0, f"Entry strategy should have PnL, got {result_entry['total_pnl']}"
    assert result_entry["winning"] == 1
    assert result_entry["trade_count"] == 2  # 1 entry + 1 exit(PnL)

    # Exit strategy (futures_stop) should have 0 trades/PnL
    result_exit = await _calc_performance(session, "futures_stop", exchange="binance_futures")
    assert result_exit["total_pnl"] == 0
    assert result_exit["trade_count"] == 0
    assert result_exit["winning"] == 0


@pytest.mark.asyncio
async def test_multiple_entry_strategies_fifo(session):
    """2개 전략이 각각 매수 → FIFO로 첫 번째 전략에 먼저 PnL 귀속."""
    now = datetime.now(timezone.utc)
    await _insert_orders(session, [
        # 1st entry: ma_crossover buys 0.01 at 90k
        make_order(
            symbol="BTC/USDT", side="buy", strategy_name="ma_crossover",
            executed_price=90_000, requested_quantity=0.01, executed_quantity=0.01,
            fee=0, exchange="binance_futures", direction="long",
            created_at=now - timedelta(hours=3),
        ),
        # 2nd entry: rsi buys 0.01 at 92k
        make_order(
            symbol="BTC/USDT", side="buy", strategy_name="rsi",
            executed_price=92_000, requested_quantity=0.01, executed_quantity=0.01,
            fee=0, exchange="binance_futures", direction="long",
            created_at=now - timedelta(hours=2),
        ),
        # Exit: sell 0.02 at 95k (closes both lots)
        make_order(
            symbol="BTC/USDT", side="sell", strategy_name="futures_stop",
            executed_price=95_000, requested_quantity=0.02, executed_quantity=0.02,
            fee=0, exchange="binance_futures", direction="long",
            created_at=now - timedelta(hours=1),
        ),
    ])
    await session.commit()

    # ma_crossover: entry + FIFO exit PnL = (95k-90k)*0.01 = 50
    result_ma = await _calc_performance(session, "ma_crossover", exchange="binance_futures")
    assert result_ma["total_pnl"] == pytest.approx(50, abs=1)
    assert result_ma["winning"] == 1
    assert result_ma["trade_count"] == 2  # 1 entry + 1 exit

    # rsi: entry + FIFO exit PnL = (95k-92k)*0.01 = 30
    result_rsi = await _calc_performance(session, "rsi", exchange="binance_futures")
    assert result_rsi["total_pnl"] == pytest.approx(30, abs=1)
    assert result_rsi["winning"] == 1
    assert result_rsi["trade_count"] == 2  # 1 entry + 1 exit

    # futures_stop: no entries → 0
    result_stop = await _calc_performance(session, "futures_stop", exchange="binance_futures")
    assert result_stop["total_pnl"] == 0
    assert result_stop["trade_count"] == 0


@pytest.mark.asyncio
async def test_exit_only_strategy_shows_zero(session):
    """청산 전용 전략(futures_stop)은 0 trades, 0 PnL."""
    now = datetime.now(timezone.utc)
    await _insert_orders(session, [
        make_order(
            symbol="ETH/USDT", side="buy", strategy_name="bollinger_rsi",
            executed_price=3000, requested_quantity=0.1, executed_quantity=0.1,
            fee=0.12, exchange="binance_futures", direction="long",
            created_at=now - timedelta(hours=3),
        ),
        make_order(
            symbol="ETH/USDT", side="sell", strategy_name="futures_stop",
            executed_price=3100, requested_quantity=0.1, executed_quantity=0.1,
            fee=0.124, exchange="binance_futures", direction="long",
            created_at=now - timedelta(hours=2),
        ),
        make_order(
            symbol="BTC/USDT", side="buy", strategy_name="rsi",
            executed_price=90_000, requested_quantity=0.01, executed_quantity=0.01,
            fee=0.036, exchange="binance_futures", direction="long",
            created_at=now - timedelta(hours=1),
        ),
        make_order(
            symbol="BTC/USDT", side="sell", strategy_name="futures_stop",
            executed_price=88_000, requested_quantity=0.01, executed_quantity=0.01,
            fee=0.035, exchange="binance_futures", direction="long",
            created_at=now - timedelta(minutes=30),
        ),
    ])
    await session.commit()

    result_stop = await _calc_performance(session, "futures_stop", exchange="binance_futures")
    assert result_stop["total_pnl"] == 0
    assert result_stop["trade_count"] == 0
    assert result_stop["winning"] == 0
    assert result_stop["losing"] == 0
