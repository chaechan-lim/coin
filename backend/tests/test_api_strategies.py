"""
Tests for strategy performance calculation (api/strategies.py).

These validate the core P&L matching logic independently of FastAPI.
Uses realized_pnl-based calculation (not FIFO lot matching).
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
    session: AsyncSession,
    strategy_name: str,
    days: int = 30,
    exchange: str = "bithumb",
):
    """Replicate the realized_pnl-based strategy performance calculation from api/strategies.py."""
    from core.utils import utcnow

    start = utcnow() - timedelta(days=days)
    is_futures = "futures" in exchange

    result = await session.execute(
        select(Order)
        .where(
            Order.status == "filled",
            Order.exchange == exchange,
            Order.strategy_name == strategy_name,
            Order.created_at >= start,
        )
        .order_by(Order.created_at)
    )
    orders = list(result.scalars().all())

    winning = 0
    losing = 0
    total_pnl = 0.0
    returns: list[float] = []
    trade_count = 0

    for order in orders:
        qty = order.executed_quantity or order.requested_quantity
        price = order.executed_price or order.requested_price

        if not price or not qty:
            continue

        direction = getattr(order, "direction", None) or "long"
        is_short = is_futures and direction == "short"

        is_entry = (is_short and order.side == "sell") or (
            not is_short and order.side == "buy"
        )
        is_close = (is_short and order.side == "buy") or (
            not is_short and order.side == "sell"
        )

        if is_entry:
            trade_count += 1
        elif is_close and order.realized_pnl is not None:
            pnl = order.realized_pnl
            total_pnl += pnl
            pnl_pct = (
                order.realized_pnl_pct if order.realized_pnl_pct is not None else 0.0
            )
            returns.append(pnl_pct)
            if pnl > 0:
                winning += 1
            else:
                losing += 1

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


# ── Basic PnL Tests (Spot, realized_pnl based) ──


@pytest.mark.asyncio
async def test_profitable_sell_shows_positive_pnl(session):
    """Buy at 50M, sell at 52M → positive P&L via realized_pnl."""
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            make_order(
                side="buy",
                executed_price=50_000_000,
                fee=150,
                created_at=now - timedelta(hours=2),
            ),
            make_order(
                side="sell",
                executed_price=52_000_000,
                fee=156,
                realized_pnl=1694.0,
                realized_pnl_pct=3.39,
                created_at=now - timedelta(hours=1),
            ),
        ],
    )
    await session.commit()

    result = await _calc_performance(session, "rsi")
    assert result["total_pnl"] > 0, f"Expected positive PnL, got {result['total_pnl']}"
    assert result["winning"] == 1
    assert result["losing"] == 0
    assert result["win_rate"] == 100.0


@pytest.mark.asyncio
async def test_loss_sell_shows_negative_pnl(session):
    """Buy at 50M, sell at 48M → negative P&L via realized_pnl."""
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            make_order(
                side="buy",
                executed_price=50_000_000,
                fee=150,
                created_at=now - timedelta(hours=2),
            ),
            make_order(
                side="sell",
                executed_price=48_000_000,
                fee=144,
                realized_pnl=-2144.0,
                realized_pnl_pct=-4.27,
                created_at=now - timedelta(hours=1),
            ),
        ],
    )
    await session.commit()

    result = await _calc_performance(session, "rsi")
    assert result["total_pnl"] < 0, f"Expected negative PnL, got {result['total_pnl']}"
    assert result["winning"] == 0
    assert result["losing"] == 1


@pytest.mark.asyncio
async def test_no_sell_orders_means_no_pnl(session):
    """Only buy orders → no P&L, just trade count."""
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            make_order(side="buy", executed_price=50_000_000, fee=150, created_at=now),
        ],
    )
    await session.commit()

    result = await _calc_performance(session, "rsi")
    assert result["total_pnl"] == 0
    assert result["trade_count"] == 1  # buy counted as entry
    assert result["winning"] == 0
    assert result["losing"] == 0


@pytest.mark.asyncio
async def test_empty_orders_returns_zero(session):
    """No orders at all → everything zero."""
    result = await _calc_performance(session, "rsi")
    assert result["total_pnl"] == 0
    assert result["trade_count"] == 0
    assert result["win_rate"] == 0


@pytest.mark.asyncio
async def test_fee_included_in_realized_pnl(session):
    """Fees are reflected in realized_pnl (already included by engine)."""
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            make_order(
                side="buy",
                executed_price=50_000_000,
                requested_quantity=0.001,
                executed_quantity=0.001,
                fee=500,
                created_at=now - timedelta(hours=2),
            ),
            make_order(
                side="sell",
                executed_price=50_000_000,
                requested_quantity=0.001,
                executed_quantity=0.001,
                fee=500,
                realized_pnl=-1000.0,
                realized_pnl_pct=-2.0,
                created_at=now - timedelta(hours=1),
            ),
        ],
    )
    await session.commit()

    result = await _calc_performance(session, "rsi")
    assert result["total_pnl"] < 0, "Fees should cause net loss on flat trade"
    assert result["losing"] == 1


@pytest.mark.asyncio
async def test_multiple_buys_single_sell(session):
    """Two buys at different prices, one sell → realized_pnl on sell determines outcome."""
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            make_order(
                side="buy",
                executed_price=50_000_000,
                requested_quantity=0.001,
                executed_quantity=0.001,
                fee=150,
                created_at=now - timedelta(hours=3),
            ),
            make_order(
                side="buy",
                executed_price=52_000_000,
                requested_quantity=0.001,
                executed_quantity=0.001,
                fee=156,
                created_at=now - timedelta(hours=2),
            ),
            make_order(
                side="sell",
                executed_price=53_000_000,
                requested_quantity=0.001,
                executed_quantity=0.001,
                fee=159,
                realized_pnl=1694.0,
                realized_pnl_pct=3.39,
                created_at=now - timedelta(hours=1),
            ),
        ],
    )
    await session.commit()

    result = await _calc_performance(session, "rsi")
    assert result["total_pnl"] > 0
    assert result["winning"] == 1
    assert result["trade_count"] == 2  # 2 entries (buys)


# ── Cross-Strategy Attribution Tests ──


@pytest.mark.asyncio
async def test_cross_strategy_pnl_on_closing_strategy(session):
    """Buy from strategy A, sell from strategy B → PnL attributed to closing strategy B."""
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            make_order(
                side="buy",
                strategy_name="ma_crossover",
                executed_price=50_000_000,
                fee=150,
                created_at=now - timedelta(hours=2),
            ),
            make_order(
                side="sell",
                strategy_name="rsi",
                executed_price=52_000_000,
                fee=156,
                realized_pnl=1694.0,
                realized_pnl_pct=3.39,
                created_at=now - timedelta(hours=1),
            ),
        ],
    )
    await session.commit()

    # With realized_pnl approach: PnL on closing order → attributed to rsi (closing strategy)
    result_entry = await _calc_performance(session, "ma_crossover")
    assert result_entry["trade_count"] == 1  # 1 entry only
    assert result_entry["total_pnl"] == 0  # no close orders for ma_crossover
    assert result_entry["winning"] == 0

    result_exit = await _calc_performance(session, "rsi")
    assert result_exit["total_pnl"] > 0  # close order has realized_pnl
    assert result_exit["winning"] == 1


@pytest.mark.asyncio
async def test_same_strategy_entry_exit(session):
    """Same strategy for entry and exit → PnL correctly attributed."""
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            make_order(
                side="buy",
                strategy_name="bollinger_rsi",
                executed_price=50_000_000,
                fee=150,
                created_at=now - timedelta(hours=2),
            ),
            make_order(
                side="sell",
                strategy_name="bollinger_rsi",
                executed_price=52_000_000,
                fee=156,
                realized_pnl=1694.0,
                realized_pnl_pct=3.39,
                created_at=now - timedelta(hours=1),
            ),
        ],
    )
    await session.commit()

    result = await _calc_performance(session, "bollinger_rsi")
    assert result["trade_count"] == 1  # 1 entry
    assert result["total_pnl"] > 0
    assert result["winning"] == 1
    assert result["losing"] == 0


@pytest.mark.asyncio
async def test_stop_loss_exit_pnl_on_stop_loss(session):
    """Entry by bollinger_rsi, exit by stop_loss → PnL on stop_loss, not bollinger_rsi."""
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            make_order(
                symbol="ETH/USDT",
                side="buy",
                strategy_name="bollinger_rsi",
                executed_price=3000,
                requested_quantity=0.1,
                executed_quantity=0.1,
                fee=0.12,
                exchange="binance_futures",
                direction="long",
                created_at=now - timedelta(hours=3),
            ),
            make_order(
                symbol="ETH/USDT",
                side="sell",
                strategy_name="stop_loss",
                executed_price=2800,
                requested_quantity=0.1,
                executed_quantity=0.1,
                fee=0.11,
                exchange="binance_futures",
                direction="long",
                realized_pnl=-20.11,
                realized_pnl_pct=-6.7,
                created_at=now - timedelta(hours=2),
            ),
        ],
    )
    await session.commit()

    result_entry = await _calc_performance(
        session, "bollinger_rsi", exchange="binance_futures"
    )
    assert result_entry["trade_count"] == 1  # entry only
    assert result_entry["total_pnl"] == 0
    assert result_entry["winning"] == 0
    assert result_entry["losing"] == 0

    result_stop = await _calc_performance(
        session, "stop_loss", exchange="binance_futures"
    )
    assert result_stop["total_pnl"] < 0
    assert result_stop["losing"] == 1


# ── Futures Short PnL Tests ──


@pytest.mark.asyncio
async def test_futures_short_profitable(session):
    """선물 숏: 높은 가격에 진입(sell), 낮은 가격에 청산(buy) → 수익."""
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            # 숏 진입: sell at 100,000
            make_order(
                symbol="BTC/USDT",
                side="sell",
                executed_price=100_000,
                requested_quantity=0.01,
                executed_quantity=0.01,
                fee=0.04,
                exchange="binance_futures",
                direction="short",
                created_at=now - timedelta(hours=2),
            ),
            # 숏 청산: buy at 95,000
            make_order(
                symbol="BTC/USDT",
                side="buy",
                executed_price=95_000,
                requested_quantity=0.01,
                executed_quantity=0.01,
                fee=0.038,
                exchange="binance_futures",
                direction="short",
                realized_pnl=49.962,
                realized_pnl_pct=5.0,
                created_at=now - timedelta(hours=1),
            ),
        ],
    )
    await session.commit()

    result = await _calc_performance(session, "rsi", exchange="binance_futures")
    assert result["total_pnl"] > 0, (
        f"Short should be profitable, got {result['total_pnl']}"
    )
    assert result["winning"] == 1
    assert result["losing"] == 0


@pytest.mark.asyncio
async def test_futures_short_loss(session):
    """선물 숏: 낮은 가격에 진입, 높은 가격에 청산 → 손실."""
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            make_order(
                symbol="ETH/USDT",
                side="sell",
                executed_price=3000,
                requested_quantity=0.1,
                executed_quantity=0.1,
                fee=0.12,
                exchange="binance_futures",
                direction="short",
                created_at=now - timedelta(hours=2),
            ),
            make_order(
                symbol="ETH/USDT",
                side="buy",
                executed_price=3200,
                requested_quantity=0.1,
                executed_quantity=0.1,
                fee=0.128,
                exchange="binance_futures",
                direction="short",
                realized_pnl=-20.128,
                realized_pnl_pct=-6.71,
                created_at=now - timedelta(hours=1),
            ),
        ],
    )
    await session.commit()

    result = await _calc_performance(session, "rsi", exchange="binance_futures")
    assert result["total_pnl"] < 0, f"Short should be loss, got {result['total_pnl']}"
    assert result["winning"] == 0
    assert result["losing"] == 1


@pytest.mark.asyncio
async def test_futures_long_still_works(session):
    """선물 롱은 기존과 동일하게 작동."""
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            make_order(
                symbol="BTC/USDT",
                side="buy",
                executed_price=90_000,
                requested_quantity=0.01,
                executed_quantity=0.01,
                fee=0.036,
                exchange="binance_futures",
                direction="long",
                created_at=now - timedelta(hours=2),
            ),
            make_order(
                symbol="BTC/USDT",
                side="sell",
                executed_price=95_000,
                requested_quantity=0.01,
                executed_quantity=0.01,
                fee=0.038,
                exchange="binance_futures",
                direction="long",
                realized_pnl=49.962,
                realized_pnl_pct=5.55,
                created_at=now - timedelta(hours=1),
            ),
        ],
    )
    await session.commit()

    result = await _calc_performance(session, "rsi", exchange="binance_futures")
    assert result["total_pnl"] > 0
    assert result["winning"] == 1


@pytest.mark.asyncio
async def test_futures_mixed_long_short(session):
    """선물 롱+숏 혼합 시 각각 독립 계산."""
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            # 롱: buy 90k → sell 95k → profit
            make_order(
                symbol="BTC/USDT",
                side="buy",
                executed_price=90_000,
                requested_quantity=0.01,
                executed_quantity=0.01,
                fee=0,
                exchange="binance_futures",
                direction="long",
                created_at=now - timedelta(hours=4),
            ),
            make_order(
                symbol="BTC/USDT",
                side="sell",
                executed_price=95_000,
                requested_quantity=0.01,
                executed_quantity=0.01,
                fee=0,
                exchange="binance_futures",
                direction="long",
                realized_pnl=50.0,
                realized_pnl_pct=5.56,
                created_at=now - timedelta(hours=3),
            ),
            # 숏: sell 95k → buy 93k → profit
            make_order(
                symbol="BTC/USDT",
                side="sell",
                executed_price=95_000,
                requested_quantity=0.01,
                executed_quantity=0.01,
                fee=0,
                exchange="binance_futures",
                direction="short",
                created_at=now - timedelta(hours=2),
            ),
            make_order(
                symbol="BTC/USDT",
                side="buy",
                executed_price=93_000,
                requested_quantity=0.01,
                executed_quantity=0.01,
                fee=0,
                exchange="binance_futures",
                direction="short",
                realized_pnl=20.0,
                realized_pnl_pct=2.11,
                created_at=now - timedelta(hours=1),
            ),
        ],
    )
    await session.commit()

    result = await _calc_performance(session, "rsi", exchange="binance_futures")
    # 롱 PnL: 50.0, 숏 PnL: 20.0, 합계: 70.0
    assert result["total_pnl"] == pytest.approx(70, abs=1)
    assert result["winning"] == 2
    assert result["losing"] == 0


# ── V1 Orphan Lot Bug Fix Tests (COIN-35) ──


@pytest.mark.asyncio
async def test_orphan_lots_dont_affect_v2_strategy(session):
    """V1 고아 로트가 V2 전략 성과에 영향을 주지 않음 (COIN-35 핵심 버그).

    시나리오:
    - V1 stochastic_rsi: 숏 진입 0.014 ETH (미청산 고아 로트)
    - V1 obv_divergence: 숏 진입 0.054 ETH (미청산 고아 로트)
    - V2 cis_momentum: 숏 진입 0.055 ETH → 숏 청산 0.055 ETH (손실 -1.98%)
    - V2 cis_momentum: 숏 진입 0.01 BTC → 숏 청산 0.01 BTC (수익 +0.45%)

    기존 FIFO 방식: 청산 #340이 고아 로트를 흡수 → cis_momentum 승률 100% (오류)
    realized_pnl 방식: 청산 주문의 realized_pnl 직접 사용 → cis_momentum 1승 1패 (정확)
    """
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            # V1 고아 로트: stochastic_rsi 숏 진입 (미청산)
            make_order(
                symbol="ETH/USDT",
                side="sell",
                strategy_name="stochastic_rsi",
                executed_price=2025.99,
                requested_quantity=0.014,
                executed_quantity=0.014,
                fee=0.011,
                exchange="binance_futures",
                direction="short",
                created_at=now - timedelta(days=17),
            ),
            # V1 고아 로트: obv_divergence 숏 진입 (미청산)
            make_order(
                symbol="ETH/USDT",
                side="sell",
                strategy_name="obv_divergence",
                executed_price=1980.53,
                requested_quantity=0.054,
                executed_quantity=0.054,
                fee=0.043,
                exchange="binance_futures",
                direction="short",
                created_at=now - timedelta(days=11),
            ),
            # V2 cis_momentum: BTC 숏 진입 → 청산 (수익)
            make_order(
                symbol="BTC/USDT",
                side="sell",
                strategy_name="cis_momentum",
                executed_price=95_000,
                requested_quantity=0.01,
                executed_quantity=0.01,
                fee=0.038,
                exchange="binance_futures",
                direction="short",
                created_at=now - timedelta(hours=48),
            ),
            make_order(
                symbol="BTC/USDT",
                side="buy",
                strategy_name="cis_momentum",
                executed_price=94_572,
                requested_quantity=0.01,
                executed_quantity=0.01,
                fee=0.038,
                exchange="binance_futures",
                direction="short",
                realized_pnl=4.24,
                realized_pnl_pct=0.45,
                created_at=now - timedelta(hours=24),
            ),
            # V2 cis_momentum: ETH 숏 진입 → 청산 (손실)
            make_order(
                symbol="ETH/USDT",
                side="sell",
                strategy_name="cis_momentum",
                executed_price=2115.64,
                requested_quantity=0.055,
                executed_quantity=0.055,
                fee=0.047,
                exchange="binance_futures",
                direction="short",
                created_at=now - timedelta(hours=6),
            ),
            make_order(
                symbol="ETH/USDT",
                side="buy",
                strategy_name="cis_momentum",
                executed_price=2150.68,
                requested_quantity=0.055,
                executed_quantity=0.055,
                fee=0.047,
                exchange="binance_futures",
                direction="short",
                realized_pnl=-1.93,
                realized_pnl_pct=-1.98,
                created_at=now - timedelta(hours=3),
            ),
        ],
    )
    await session.commit()

    # cis_momentum should have 1 win and 1 loss (NOT 100% win rate)
    result = await _calc_performance(
        session, "cis_momentum", exchange="binance_futures"
    )
    assert result["winning"] == 1, f"Expected 1 win, got {result['winning']}"
    assert result["losing"] == 1, f"Expected 1 loss, got {result['losing']}"
    assert result["win_rate"] == 50.0, (
        f"Expected 50% win rate, got {result['win_rate']}"
    )
    assert result["trade_count"] == 2  # 2 entries (BTC + ETH)

    # V1 orphan strategies should only show entries, no closes
    result_stoch = await _calc_performance(
        session, "stochastic_rsi", exchange="binance_futures"
    )
    assert result_stoch["trade_count"] == 1  # 1 entry only
    assert result_stoch["winning"] == 0
    assert result_stoch["losing"] == 0

    result_obv = await _calc_performance(
        session, "obv_divergence", exchange="binance_futures"
    )
    assert result_obv["trade_count"] == 1  # 1 entry only
    assert result_obv["winning"] == 0
    assert result_obv["losing"] == 0


@pytest.mark.asyncio
async def test_realized_pnl_none_close_order_skipped(session):
    """Close order without realized_pnl is not counted in win/loss stats."""
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            make_order(
                side="buy",
                executed_price=50_000_000,
                fee=150,
                created_at=now - timedelta(hours=2),
            ),
            # Sell without realized_pnl (legacy order or data gap)
            make_order(
                side="sell",
                executed_price=52_000_000,
                fee=156,
                realized_pnl=None,
                realized_pnl_pct=None,
                created_at=now - timedelta(hours=1),
            ),
        ],
    )
    await session.commit()

    result = await _calc_performance(session, "rsi")
    assert result["trade_count"] == 1  # 1 entry
    assert result["winning"] == 0  # close skipped (no realized_pnl)
    assert result["losing"] == 0
    assert result["total_pnl"] == 0


@pytest.mark.asyncio
async def test_realized_pnl_zero_is_loss(session):
    """Breakeven trade (realized_pnl=0) is counted as a loss (not > 0)."""
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            make_order(
                side="buy",
                executed_price=50_000_000,
                fee=0,
                created_at=now - timedelta(hours=2),
            ),
            make_order(
                side="sell",
                executed_price=50_000_000,
                fee=0,
                realized_pnl=0.0,
                realized_pnl_pct=0.0,
                created_at=now - timedelta(hours=1),
            ),
        ],
    )
    await session.commit()

    result = await _calc_performance(session, "rsi")
    assert result["losing"] == 1  # breakeven → losing (pnl <= 0)
    assert result["winning"] == 0


@pytest.mark.asyncio
async def test_avg_return_pct_uses_realized_pnl_pct(session):
    """avg_return_pct is computed from realized_pnl_pct on closing orders."""
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            # Trade 1: +5%
            make_order(
                symbol="BTC/USDT",
                side="buy",
                strategy_name="rsi",
                executed_price=90_000,
                requested_quantity=0.01,
                executed_quantity=0.01,
                fee=0,
                exchange="binance_futures",
                direction="long",
                created_at=now - timedelta(hours=6),
            ),
            make_order(
                symbol="BTC/USDT",
                side="sell",
                strategy_name="rsi",
                executed_price=94_500,
                requested_quantity=0.01,
                executed_quantity=0.01,
                fee=0,
                exchange="binance_futures",
                direction="long",
                realized_pnl=45.0,
                realized_pnl_pct=5.0,
                created_at=now - timedelta(hours=5),
            ),
            # Trade 2: -3%
            make_order(
                symbol="ETH/USDT",
                side="sell",
                strategy_name="rsi",
                executed_price=3000,
                requested_quantity=0.1,
                executed_quantity=0.1,
                fee=0,
                exchange="binance_futures",
                direction="short",
                created_at=now - timedelta(hours=4),
            ),
            make_order(
                symbol="ETH/USDT",
                side="buy",
                strategy_name="rsi",
                executed_price=3090,
                requested_quantity=0.1,
                executed_quantity=0.1,
                fee=0,
                exchange="binance_futures",
                direction="short",
                realized_pnl=-9.0,
                realized_pnl_pct=-3.0,
                created_at=now - timedelta(hours=3),
            ),
        ],
    )
    await session.commit()

    result = await _calc_performance(session, "rsi", exchange="binance_futures")
    # avg_return = (5.0 + (-3.0)) / 2 = 1.0
    assert result["avg_return"] == 1.0
    assert result["winning"] == 1
    assert result["losing"] == 1


@pytest.mark.asyncio
async def test_period_filtering_excludes_old_orders(session):
    """Orders outside the period are not counted."""
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            # Old trade (40 days ago, outside 30d period)
            make_order(
                side="buy",
                executed_price=50_000_000,
                fee=150,
                created_at=now - timedelta(days=40),
            ),
            make_order(
                side="sell",
                executed_price=52_000_000,
                fee=156,
                realized_pnl=1694.0,
                realized_pnl_pct=3.39,
                created_at=now - timedelta(days=39),
            ),
            # Recent trade (2 days ago, within 30d period)
            make_order(
                side="buy",
                executed_price=50_000_000,
                fee=150,
                created_at=now - timedelta(days=2),
            ),
            make_order(
                side="sell",
                executed_price=48_000_000,
                fee=144,
                realized_pnl=-2144.0,
                realized_pnl_pct=-4.27,
                created_at=now - timedelta(days=1),
            ),
        ],
    )
    await session.commit()

    result = await _calc_performance(session, "rsi", days=30)
    # Only the recent (loss) trade should be counted
    assert result["trade_count"] == 1  # 1 entry in period
    assert result["winning"] == 0
    assert result["losing"] == 1
    assert result["total_pnl"] < 0


@pytest.mark.asyncio
async def test_multiple_strategies_independent(session):
    """Each strategy's performance is computed independently from realized_pnl."""
    now = datetime.now(timezone.utc)
    await _insert_orders(
        session,
        [
            # Strategy A: winning trade
            make_order(
                symbol="BTC/USDT",
                side="sell",
                strategy_name="bollinger_rsi",
                executed_price=100_000,
                requested_quantity=0.01,
                executed_quantity=0.01,
                fee=0,
                exchange="binance_futures",
                direction="short",
                created_at=now - timedelta(hours=4),
            ),
            make_order(
                symbol="BTC/USDT",
                side="buy",
                strategy_name="bollinger_rsi",
                executed_price=97_000,
                requested_quantity=0.01,
                executed_quantity=0.01,
                fee=0,
                exchange="binance_futures",
                direction="short",
                realized_pnl=30.0,
                realized_pnl_pct=3.0,
                created_at=now - timedelta(hours=3),
            ),
            # Strategy B: losing trade
            make_order(
                symbol="ETH/USDT",
                side="buy",
                strategy_name="rsi",
                executed_price=3000,
                requested_quantity=0.1,
                executed_quantity=0.1,
                fee=0,
                exchange="binance_futures",
                direction="long",
                created_at=now - timedelta(hours=2),
            ),
            make_order(
                symbol="ETH/USDT",
                side="sell",
                strategy_name="rsi",
                executed_price=2900,
                requested_quantity=0.1,
                executed_quantity=0.1,
                fee=0,
                exchange="binance_futures",
                direction="long",
                realized_pnl=-10.0,
                realized_pnl_pct=-3.33,
                created_at=now - timedelta(hours=1),
            ),
        ],
    )
    await session.commit()

    result_a = await _calc_performance(
        session, "bollinger_rsi", exchange="binance_futures"
    )
    assert result_a["winning"] == 1
    assert result_a["losing"] == 0
    assert result_a["total_pnl"] > 0

    result_b = await _calc_performance(session, "rsi", exchange="binance_futures")
    assert result_b["winning"] == 0
    assert result_b["losing"] == 1
    assert result_b["total_pnl"] < 0
