"""
Shared test fixtures: in-memory SQLite, async sessions, FastAPI test client.
"""
import os
import asyncio
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from httpx import AsyncClient, ASGITransport

# Override env before any config import
os.environ.setdefault("DB_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("EXCHANGE_API_KEY", "test")
os.environ.setdefault("EXCHANGE_API_SECRET", "test")
os.environ.setdefault("TRADING_MODE", "paper")

from core.models import Base, Order, Trade, Position, PortfolioSnapshot


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_engine():
    """Create a fresh in-memory SQLite engine per test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(db_engine):
    """Provide a fresh async session per test."""
    factory = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
        await sess.rollback()


@pytest_asyncio.fixture
async def session_factory(db_engine):
    """Provide the session factory itself."""
    return async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)


# ── Helpers to create test data ──────────────────────────────

def make_order(
    *,
    symbol="BTC/KRW",
    side="buy",
    strategy_name="rsi",
    requested_price=50_000_000,
    executed_price=50_000_000,
    requested_quantity=0.001,
    executed_quantity=0.001,
    fee=150.0,
    status="filled",
    created_at=None,
    exchange="bithumb",
    direction=None,
) -> Order:
    return Order(
        exchange=exchange,
        symbol=symbol,
        side=side,
        order_type="limit",
        status=status,
        requested_price=requested_price,
        executed_price=executed_price,
        requested_quantity=requested_quantity,
        executed_quantity=executed_quantity,
        fee=fee,
        fee_currency="KRW",
        is_paper=True,
        strategy_name=strategy_name,
        signal_confidence=0.7,
        signal_reason="test",
        created_at=created_at or datetime.now(timezone.utc),
        direction=direction,
    )


def make_position(
    *,
    symbol="BTC/KRW",
    quantity=0.001,
    average_buy_price=50_000_000,
    total_invested=50_000,
    exchange="bithumb",
) -> Position:
    return Position(
        exchange=exchange,
        symbol=symbol,
        quantity=quantity,
        average_buy_price=average_buy_price,
        total_invested=total_invested,
        is_paper=True,
    )
