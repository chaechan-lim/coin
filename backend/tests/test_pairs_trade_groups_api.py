from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.trades import router as trades_router
from core.models import Base, Order, ServerEvent


def _make_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(trades_router)
    return app


@pytest.fixture
async def test_app():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def _db_override():
        async with factory() as session:
            yield session

    app = _make_test_app()
    from db.session import get_db

    app.dependency_overrides[get_db] = _db_override

    async with factory() as session:
        now = datetime.now(timezone.utc)
        session.add_all([
            Order(
                exchange="binance_pairs",
                symbol="BTC/USDT",
                side="buy",
                order_type="market",
                status="filled",
                requested_price=70000,
                executed_price=70000,
                requested_quantity=0.001,
                executed_quantity=0.001,
                fee=0.02,
                fee_currency="USDT",
                is_paper=False,
                direction="long",
                leverage=2,
                margin_used=35,
                strategy_name="pairs_trading_live",
                signal_reason="pairs_entry:trade=abc123:def=ignored:pair_direction=long_a_short_b:leg=a",
                created_at=now,
                filled_at=now,
            ),
            Order(
                exchange="binance_pairs",
                symbol="ETH/USDT",
                side="sell",
                order_type="market",
                status="filled",
                requested_price=2200,
                executed_price=2200,
                requested_quantity=0.03,
                executed_quantity=0.03,
                fee=0.02,
                fee_currency="USDT",
                is_paper=False,
                direction="short",
                leverage=2,
                margin_used=15,
                strategy_name="pairs_trading_live",
                signal_reason="pairs_entry:trade=abc123:pair_direction=long_a_short_b:leg=b",
                created_at=now,
                filled_at=now,
            ),
            Order(
                exchange="binance_pairs",
                symbol="BTC/USDT",
                side="sell",
                order_type="market",
                status="filled",
                requested_price=71000,
                executed_price=71000,
                requested_quantity=0.001,
                executed_quantity=0.001,
                fee=0.02,
                fee_currency="USDT",
                is_paper=False,
                direction="long",
                leverage=2,
                margin_used=35,
                realized_pnl=0.98,
                realized_pnl_pct=1.42,
                entry_price=70000,
                strategy_name="pairs_trading_live",
                signal_reason="pairs_exit:trade=abc123:pair_direction=long_a_short_b:leg=a",
                created_at=now,
                filled_at=now,
            ),
            Order(
                exchange="binance_pairs",
                symbol="ETH/USDT",
                side="buy",
                order_type="market",
                status="filled",
                requested_price=2100,
                executed_price=2100,
                requested_quantity=0.03,
                executed_quantity=0.03,
                fee=0.02,
                fee_currency="USDT",
                is_paper=False,
                direction="short",
                leverage=2,
                margin_used=15,
                realized_pnl=2.98,
                realized_pnl_pct=1.42,
                entry_price=2200,
                strategy_name="pairs_trading_live",
                signal_reason="pairs_exit:trade=abc123:pair_direction=long_a_short_b:leg=b",
                created_at=now,
                filled_at=now,
            ),
            ServerEvent(
                level="info",
                category="pairs_trade",
                title="Pairs entry opened: BTC/USDT-ETH/USDT",
                detail="long_a_short_b z=2.1",
                metadata_={"trade_id": "abc123", "stage": "entry_opened"},
                created_at=now,
            ),
            ServerEvent(
                level="info",
                category="pairs_trade",
                title="Pairs exit closed: BTC/USDT-ETH/USDT",
                detail="long_a_short_b realized=3.96 USDT",
                metadata_={"trade_id": "abc123", "stage": "exit_closed"},
                created_at=now,
            ),
        ])
        await session.commit()

    try:
        yield app
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pairs_trade_groups_endpoint(test_app):
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        resp = await client.get("/trades/pairs/groups")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["trade_id"] == "abc123"
    assert data[0]["status"] == "closed"
    assert set(data[0]["symbols"]) == {"BTC/USDT", "ETH/USDT"}


@pytest.mark.asyncio
async def test_pairs_trade_group_detail_includes_journal(test_app):
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        resp = await client.get("/trades/pairs/groups/abc123")
    assert resp.status_code == 200
    data = resp.json()
    assert data["trade_id"] == "abc123"
    assert len(data["orders"]) == 4
    assert len(data["journal"]) == 2
    assert data["journal"][0]["metadata"]["trade_id"] == "abc123"
