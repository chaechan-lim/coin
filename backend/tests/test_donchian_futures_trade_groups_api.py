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
                exchange="binance_donchian_futures",
                symbol="BTC/USDT",
                side="sell",
                order_type="market",
                status="filled",
                requested_price=70000,
                executed_price=70000,
                requested_quantity=0.001,
                executed_quantity=0.001,
                fee=0.02,
                fee_currency="USDT",
                is_paper=False,
                direction="short",
                leverage=2,
                margin_used=35,
                strategy_name="donchian_futures_bi",
                signal_reason="donchian_futures_bi_entry:trade=don1:symbol=BTC/USDT:direction=short",
                trade_group_id="don1",
                trade_group_type="donchian_futures_entry",
                created_at=now,
                filled_at=now,
            ),
            Order(
                exchange="binance_donchian_futures",
                symbol="BTC/USDT",
                side="buy",
                order_type="market",
                status="filled",
                requested_price=68000,
                executed_price=68000,
                requested_quantity=0.001,
                executed_quantity=0.001,
                fee=0.02,
                fee_currency="USDT",
                is_paper=False,
                direction="short",
                leverage=2,
                margin_used=35,
                realized_pnl=1.98,
                realized_pnl_pct=2.85,
                entry_price=70000,
                strategy_name="donchian_futures_bi",
                signal_reason="donchian_futures_bi_exit:trade=don1:symbol=BTC/USDT:direction=short",
                trade_group_id="don1",
                trade_group_type="donchian_futures_exit",
                created_at=now,
                filled_at=now,
            ),
            ServerEvent(
                level="info",
                category="donchian_futures_trade",
                title="Donchian futures entry opened: BTC/USDT",
                detail="direction=short qty=0.001",
                metadata_={"trade_id": "don1", "stage": "entry_opened", "exchange": "binance_donchian_futures"},
                created_at=now,
            ),
            ServerEvent(
                level="info",
                category="donchian_futures_trade",
                title="Donchian futures exit closed: BTC/USDT",
                detail="realized=1.98 USDT",
                metadata_={"trade_id": "don1", "stage": "exit_closed", "exchange": "binance_donchian_futures"},
                created_at=now,
            ),
        ])
        await session.commit()

    try:
        yield app
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_donchian_futures_trade_groups_endpoint(test_app):
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        resp = await client.get("/trades/donchian-futures/groups")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["trade_id"] == "don1"
    assert data[0]["status"] == "closed"
    assert data[0]["symbol"] == "BTC/USDT"
    assert data[0]["direction"] == "short"


@pytest.mark.asyncio
async def test_donchian_futures_trade_group_detail_includes_journal(test_app):
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        resp = await client.get("/trades/donchian-futures/groups/don1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["trade_id"] == "don1"
    assert len(data["orders"]) == 2
    assert len(data["journal"]) == 2
    assert data["journal"][0]["metadata"]["trade_id"] == "don1"
