"""
거래소별 데이터 격리 테스트
========================
같은 심볼이라도 exchange가 다르면 별도 레코드.
"""
import pytest
import pytest_asyncio
from sqlalchemy import select, func

from core.models import Position, Order
from tests.conftest import make_order, make_position


@pytest.mark.asyncio
async def test_position_isolation_by_exchange(session):
    """같은 심볼 다른 거래소 포지션이 독립적으로 존재."""
    p1 = make_position(symbol="BTC/KRW", exchange="bithumb", quantity=0.01)
    p2 = make_position(symbol="BTC/KRW", exchange="binance_futures", quantity=0.05)
    session.add_all([p1, p2])
    await session.flush()

    result = await session.execute(select(Position).where(Position.quantity > 0))
    positions = list(result.scalars().all())
    assert len(positions) == 2

    bithumb_pos = [p for p in positions if p.exchange == "bithumb"]
    binance_pos = [p for p in positions if p.exchange == "binance_futures"]
    assert len(bithumb_pos) == 1
    assert len(binance_pos) == 1
    assert bithumb_pos[0].quantity == 0.01
    assert binance_pos[0].quantity == 0.05


@pytest.mark.asyncio
async def test_position_exchange_filtered_query(session):
    """exchange 필터로 해당 거래소 포지션만 조회."""
    session.add_all([
        make_position(symbol="BTC/KRW", exchange="bithumb"),
        make_position(symbol="ETH/KRW", exchange="bithumb"),
        make_position(symbol="BTC/USDT", exchange="binance_futures"),
    ])
    await session.flush()

    result = await session.execute(
        select(Position).where(Position.exchange == "bithumb", Position.quantity > 0)
    )
    assert len(list(result.scalars().all())) == 2

    result = await session.execute(
        select(Position).where(Position.exchange == "binance_futures", Position.quantity > 0)
    )
    assert len(list(result.scalars().all())) == 1


@pytest.mark.asyncio
async def test_order_count_isolation_by_exchange(session):
    """거래소별 주문 카운트 격리."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    session.add_all([
        make_order(exchange="bithumb", symbol="BTC/KRW", created_at=now),
        make_order(exchange="bithumb", symbol="ETH/KRW", created_at=now),
        make_order(exchange="binance_futures", symbol="BTC/USDT", created_at=now),
    ])
    await session.flush()

    # Bithumb orders
    result = await session.execute(
        select(func.count(Order.id)).where(Order.exchange == "bithumb")
    )
    assert result.scalar() == 2

    # Binance orders
    result = await session.execute(
        select(func.count(Order.id)).where(Order.exchange == "binance_futures")
    )
    assert result.scalar() == 1


@pytest.mark.asyncio
async def test_position_default_exchange(session):
    """exchange 기본값은 bithumb."""
    p = Position(symbol="XRP/KRW", quantity=100, average_buy_price=500, total_invested=50000)
    session.add(p)
    await session.flush()

    result = await session.execute(select(Position).where(Position.symbol == "XRP/KRW"))
    pos = result.scalar_one()
    assert pos.exchange == "bithumb"


@pytest.mark.asyncio
async def test_position_futures_fields(session):
    """선물 포지션에 direction, leverage, liquidation_price 저장."""
    p = make_position(symbol="BTC/USDT", exchange="binance_futures")
    p.direction = "short"
    p.leverage = 5
    p.liquidation_price = 72000.0
    p.margin_used = 200.0
    session.add(p)
    await session.flush()

    result = await session.execute(
        select(Position).where(Position.exchange == "binance_futures")
    )
    pos = result.scalar_one()
    assert pos.direction == "short"
    assert pos.leverage == 5
    assert pos.liquidation_price == 72000.0
    assert pos.margin_used == 200.0
