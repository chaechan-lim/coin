"""
Tests for PortfolioManager (engine/portfolio_manager.py).
"""
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Position, Order, PortfolioSnapshot, CapitalTransaction
from engine.portfolio_manager import PortfolioManager


def _make_market_data(prices: dict[str, float]):
    """Create a mock MarketDataService with predefined prices."""
    md = AsyncMock()
    md.get_current_price = AsyncMock(side_effect=lambda sym: prices.get(sym, 0))
    return md


@pytest.mark.asyncio
async def test_initial_state(session):
    """Fresh portfolio has correct initial values."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
        is_paper=True,
    )
    assert pm.cash_balance == 500_000
    assert pm.realized_pnl == 0


@pytest.mark.asyncio
async def test_buy_reduces_cash(session):
    """Buying reduces cash by cost + fee."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    await pm.update_position_on_buy(
        session, "BTC/KRW",
        quantity=0.001, price=50_000_000, cost=50_000, fee=150,
    )
    assert pm.cash_balance == 500_000 - 50_000 - 150


@pytest.mark.asyncio
async def test_sell_increases_cash_and_realized_pnl(session):
    """Selling increases cash and records realized P&L."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    # Buy first
    await pm.update_position_on_buy(
        session, "BTC/KRW",
        quantity=0.001, price=50_000_000, cost=50_000, fee=150,
    )
    # Sell at higher price
    await pm.update_position_on_sell(
        session, "BTC/KRW",
        quantity=0.001, price=52_000_000, cost=52_000, fee=156,
    )
    # Cash: 500_000 - 50_150 + (52_000 - 156) = 501_694
    assert pm.cash_balance == pytest.approx(501_694, abs=1)
    # Realized P&L: (52_000 - 156) - 50_000 = 1_844
    assert pm.realized_pnl > 0


@pytest.mark.asyncio
async def test_sell_more_than_position_is_rejected(session):
    """Cannot sell more than held quantity."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    # No position exists
    await pm.update_position_on_sell(
        session, "BTC/KRW",
        quantity=0.001, price=50_000_000, cost=50_000, fee=150,
    )
    # Cash should be unchanged
    assert pm.cash_balance == 500_000


@pytest.mark.asyncio
async def test_portfolio_summary_with_positions(session):
    """Summary correctly calculates unrealized P&L and total value."""
    prices = {"BTC/KRW": 52_000_000}
    pm = PortfolioManager(
        market_data=_make_market_data(prices),
        initial_balance_krw=500_000,
    )

    # Create a position in DB
    pos = Position(
        symbol="BTC/KRW",
        quantity=0.001,
        average_buy_price=50_000_000,
        total_invested=50_150,
        is_paper=True,
    )
    session.add(pos)

    # Also add an order for fee tracking
    order = Order(
        symbol="BTC/KRW", side="buy", order_type="limit", status="filled",
        requested_price=50_000_000, executed_price=50_000_000,
        requested_quantity=0.001, executed_quantity=0.001,
        fee=150, is_paper=True, strategy_name="rsi",
    )
    session.add(order)
    await session.flush()

    # Simulate cash deduction
    pm._cash_balance = 500_000 - 50_150

    summary = await pm.get_portfolio_summary(session)

    # total_current_value(52000) - total_invested(50150) = 1850
    assert summary["unrealized_pnl"] == pytest.approx(1850, abs=1)
    assert summary["total_value_krw"] > 0
    assert summary["initial_balance_krw"] == 500_000
    assert summary["trade_count"] == 1
    assert summary["total_fees"] == 150
    assert len(summary["positions"]) == 1
    assert summary["positions"][0]["symbol"] == "BTC/KRW"


@pytest.mark.asyncio
async def test_portfolio_summary_empty(session):
    """No positions → summary has zero invested, only cash."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    summary = await pm.get_portfolio_summary(session)
    assert summary["total_value_krw"] == 500_000
    assert summary["cash_balance_krw"] == 500_000
    assert summary["invested_value_krw"] == 0
    assert summary["positions"] == []


@pytest.mark.asyncio
async def test_drawdown_tracking(session):
    """Peak and drawdown are tracked correctly."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    # First call: total = 500k, peak = 500k, drawdown = 0
    s1 = await pm.get_portfolio_summary(session)
    assert s1["drawdown_pct"] == 0

    # Simulate loss: reduce cash
    pm._cash_balance = 450_000
    s2 = await pm.get_portfolio_summary(session)
    # Peak stays 500k, drawdown = (500-450)/500 * 100 = 10%
    assert s2["peak_value"] == 500_000
    assert s2["drawdown_pct"] == pytest.approx(10.0, abs=0.1)


@pytest.mark.asyncio
async def test_reconcile_cash(session):
    """reconcile_cash_from_db corrects in-memory cash from DB positions."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    # Artificially wrong cash
    pm._cash_balance = 999_999

    # Add a position in DB
    pos = Position(
        symbol="BTC/KRW", quantity=0.001, average_buy_price=50_000_000,
        total_invested=50_150, is_paper=True,
    )
    session.add(pos)
    await session.flush()

    await pm.reconcile_cash_from_db(session)
    assert pm.cash_balance == pytest.approx(500_000 - 50_150, abs=1)


@pytest.mark.asyncio
async def test_reconcile_cash_skipped_for_futures(session):
    """reconcile_cash_from_db is a no-op for futures (funding fees not in formula)."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300.0,
        exchange_name="binance_futures",
    )
    # Simulate exchange sync set cash to 318 USDT
    pm._cash_balance = 318.0

    # Add a position so formula would give different result
    pos = Position(
        symbol="BTC/USDT", quantity=0.01, average_buy_price=95000.0,
        total_invested=100.0, is_paper=True, exchange="binance_futures",
    )
    session.add(pos)
    await session.flush()

    # reconcile should NOT override for futures
    await pm.reconcile_cash_from_db(session)
    assert pm.cash_balance == 318.0  # unchanged — exchange sync is authoritative


@pytest.mark.asyncio
async def test_average_buy_price_updates_on_additional_buy(session):
    """Multiple buys update the average buy price correctly."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=1_000_000,
    )
    # First buy: 0.001 BTC at 50M
    await pm.update_position_on_buy(
        session, "BTC/KRW",
        quantity=0.001, price=50_000_000, cost=50_000, fee=150,
    )
    # Second buy: 0.001 BTC at 52M
    await pm.update_position_on_buy(
        session, "BTC/KRW",
        quantity=0.001, price=52_000_000, cost=52_000, fee=156,
    )

    from sqlalchemy import select
    from core.models import Position
    result = await session.execute(select(Position).where(Position.symbol == "BTC/KRW"))
    pos = result.scalar_one()

    assert pos.quantity == pytest.approx(0.002)
    # Average: (50M*0.001 + 52M*0.001) / 0.002 = 51M
    assert pos.average_buy_price == pytest.approx(51_000_000, rel=0.01)


@pytest.mark.asyncio
async def test_partial_sell_reduces_total_invested(session):
    """Partial sell must reduce total_invested proportionally."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    # Buy 1000 ADA at 400
    await pm.update_position_on_buy(
        session, "ADA/KRW",
        quantity=1000, price=400, cost=400_000, fee=1000,
    )

    from sqlalchemy import select
    from core.models import Position
    result = await session.execute(select(Position).where(Position.symbol == "ADA/KRW"))
    pos = result.scalar_one()
    assert pos.total_invested == pytest.approx(401_000)  # cost + fee

    # Partial sell: 500 ADA (50%)
    await pm.update_position_on_sell(
        session, "ADA/KRW",
        quantity=500, price=420, cost=210_000, fee=525,
    )

    await session.refresh(pos)
    assert pos.quantity == pytest.approx(500)
    # total_invested should be halved (50% sold)
    assert pos.total_invested == pytest.approx(200_500, abs=1)


@pytest.mark.asyncio
async def test_partial_sell_unrealized_pnl_correct(session):
    """After partial sell, portfolio unrealized PnL reflects only remaining position."""
    prices = {"ADA/KRW": 420}
    pm = PortfolioManager(
        market_data=_make_market_data(prices),
        initial_balance_krw=500_000,
    )
    # Buy 1000 ADA at 400
    await pm.update_position_on_buy(
        session, "ADA/KRW",
        quantity=1000, price=400, cost=400_000, fee=1000,
    )
    # Partial sell: 500 ADA at 420
    await pm.update_position_on_sell(
        session, "ADA/KRW",
        quantity=500, price=420, cost=210_000, fee=525,
    )

    summary = await pm.get_portfolio_summary(session)
    # Remaining: 500 ADA at avg 400, current 420
    # unrealized = 500*420 - (total_invested/2 ≈ 200500) = 210000 - 200500 = 9500
    assert summary["unrealized_pnl"] == pytest.approx(9500, abs=100)
    assert len(summary["positions"]) == 1
    assert summary["positions"][0]["quantity"] == pytest.approx(500)


# ── Capital Transaction + Peak Adjustment Tests ──


@pytest.mark.asyncio
async def test_load_initial_balance_from_deposits(session):
    """initial_balance is recalculated from confirmed CapitalTransactions."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    # Seed deposit
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=500_000,
        currency="KRW", source="seed", confirmed=True,
    ))
    await session.flush()

    await pm.load_initial_balance_from_db(session)
    assert pm._initial_balance == pytest.approx(500_000)


@pytest.mark.asyncio
async def test_withdrawal_reduces_initial_balance(session):
    """Withdrawal reduces initial_balance."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=500_000,
        currency="KRW", source="seed", confirmed=True,
    ))
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="withdrawal", amount=200_000,
        currency="KRW", source="manual", confirmed=True,
    ))
    await session.flush()

    await pm.load_initial_balance_from_db(session)
    assert pm._initial_balance == pytest.approx(300_000)


@pytest.mark.asyncio
async def test_withdrawal_adjusts_peak_proportionally(session):
    """Peak value is scaled down proportionally on withdrawal."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    pm._peak_value = 520_000  # Simulate a peak above initial

    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=500_000,
        currency="KRW", source="seed", confirmed=True,
    ))
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="withdrawal", amount=200_000,
        currency="KRW", source="manual", confirmed=True,
    ))
    await session.flush()

    await pm.load_initial_balance_from_db(session)

    # Ratio = 300_000 / 500_000 = 0.6
    # New peak = 520_000 * 0.6 = 312_000
    assert pm._initial_balance == pytest.approx(300_000)
    assert pm._peak_value == pytest.approx(312_000, abs=1)


@pytest.mark.asyncio
async def test_withdrawal_peak_prevents_fake_drawdown(session):
    """After withdrawal, drawdown reflects actual loss, not capital movement."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    pm._peak_value = 500_000

    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=500_000,
        currency="KRW", source="seed", confirmed=True,
    ))
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="withdrawal", amount=200_000,
        currency="KRW", source="manual", confirmed=True,
    ))
    await session.flush()

    await pm.load_initial_balance_from_db(session)
    # Peak should be 300_000, cash is still 500_000 (not adjusted here)
    # In real flow, sync_exchange_positions adjusts cash
    pm._cash_balance = 300_000

    summary = await pm.get_portfolio_summary(session)
    # total_value == cash == 300_000, peak == 300_000 → drawdown ~0%
    assert summary["drawdown_pct"] == pytest.approx(0, abs=0.1)


@pytest.mark.asyncio
async def test_no_withdrawal_peak_unchanged(session):
    """Deposit only → peak is not adjusted."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    pm._peak_value = 520_000

    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=500_000,
        currency="KRW", source="seed", confirmed=True,
    ))
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=100_000,
        currency="KRW", source="manual", confirmed=True,
    ))
    await session.flush()

    await pm.load_initial_balance_from_db(session)
    # No withdrawal → ratio > 1 → peak not changed
    assert pm._initial_balance == pytest.approx(600_000)
    assert pm._peak_value == pytest.approx(520_000)


@pytest.mark.asyncio
async def test_unconfirmed_transactions_ignored(session):
    """Unconfirmed transactions don't affect initial_balance."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=500_000,
        currency="KRW", source="seed", confirmed=True,
    ))
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=100_000,
        currency="KRW", source="auto_detected", confirmed=False,
    ))
    await session.flush()

    await pm.load_initial_balance_from_db(session)
    assert pm._initial_balance == pytest.approx(500_000)


@pytest.mark.asyncio
async def test_restore_state_from_snapshot(session):
    """restore_state_from_db restores peak and realized_pnl from snapshot."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    snapshot = PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=480_000,
        cash_balance_krw=200_000,
        invested_value_krw=280_000,
        peak_value=510_000,
        realized_pnl=5_000,
    )
    session.add(snapshot)
    await session.flush()

    await pm.restore_state_from_db(session)
    assert pm._peak_value == pytest.approx(510_000)
    assert pm._realized_pnl == pytest.approx(5_000)


@pytest.mark.asyncio
async def test_restore_state_no_snapshot_uses_cash(session):
    """No snapshot → peak set to current cash_balance."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    pm._cash_balance = 300_000

    await pm.restore_state_from_db(session)
    assert pm._peak_value == pytest.approx(300_000)


# ── Peak 이중 조정 방지 (재시작 시 restore + load_initial) ──


@pytest.mark.asyncio
async def test_restore_then_load_no_double_peak_adjustment(session):
    """restore_state_from_db 후 load_initial_balance_from_db → peak 이중 조정 안 됨."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    # 스냅샷에 이미 출금 조정된 peak 저장
    snapshot = PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=300_000,
        cash_balance_krw=300_000,
        invested_value_krw=0,
        peak_value=312_000,  # 이미 0.6 ratio 적용된 값
        realized_pnl=0,
    )
    session.add(snapshot)
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=500_000,
        currency="KRW", source="seed", confirmed=True,
    ))
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="withdrawal", amount=200_000,
        currency="KRW", source="manual", confirmed=True,
    ))
    await session.flush()

    # 재시작 시 순서: restore → load_initial
    await pm.restore_state_from_db(session)
    assert pm._peak_value == pytest.approx(312_000)

    await pm.load_initial_balance_from_db(session)
    # 이중 조정 방지: peak는 312_000 유지 (187_200이 되면 안 됨)
    assert pm._peak_value == pytest.approx(312_000)
    assert pm._initial_balance == pytest.approx(300_000)


@pytest.mark.asyncio
async def test_first_run_withdrawal_adjusts_peak(session):
    """스냅샷 없는 최초 실행 시에는 peak 조정이 정상 적용."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    pm._peak_value = 520_000

    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=500_000,
        currency="KRW", source="seed", confirmed=True,
    ))
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="withdrawal", amount=200_000,
        currency="KRW", source="manual", confirmed=True,
    ))
    await session.flush()

    # 스냅샷 없이 restore → peak_already_adjusted = False
    await pm.restore_state_from_db(session)

    await pm.load_initial_balance_from_db(session)
    # 최초 실행: restore에서 peak=cash=500_000, ratio=0.6 → peak=300_000
    assert pm._peak_value == pytest.approx(300_000, abs=1)


# ── Futures Cash Balance (unrealized PnL double-count fix) Tests ──


@pytest.mark.asyncio
async def test_futures_sync_does_not_overwrite_cash(session):
    """선물 sync는 cash를 덮어쓰지 않음 (내부 장부 기반)."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"BTC/USDT": 100_000}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )

    # Mock exchange adapter
    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=280, used=40, total=320),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[
        {
            "symbol": "BTC/USDT:USDT",
            "contracts": 0.001,
            "side": "long",
            "initialMargin": 40,
            "leverage": "3",
            "entryPrice": 95000,
            "liquidationPrice": 60000,
            "notional": 120,
            "unrealizedPnl": 20,
        }
    ])

    pos = Position(
        exchange="binance_futures", symbol="BTC/USDT",
        quantity=0.001, average_buy_price=95000,
        total_invested=40, is_paper=False,
        direction="long", leverage=3, margin_used=40,
    )
    session.add(pos)
    await session.flush()

    await pm.sync_exchange_positions(session, adapter, ["BTC/USDT"])

    # 선물 sync는 cash를 변경하지 않음 → 초기값 유지
    assert pm.cash_balance == pytest.approx(300, abs=1)


@pytest.mark.asyncio
async def test_futures_initialize_cash_from_exchange(session):
    """initialize_cash_from_exchange로 cash = wallet - margin 설정."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"BTC/USDT": 100_000}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )

    adapter = AsyncMock()
    # wallet=300, unPnl=20, margin=40 → total=320
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=280, used=40, total=320),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[
        {
            "symbol": "BTC/USDT:USDT",
            "contracts": 0.001,
            "initialMargin": 40,
            "unrealizedPnl": 20,
        }
    ])

    await pm.initialize_cash_from_exchange(adapter)

    # cash = wallet(300) - margin(40) = 260
    assert pm.cash_balance == pytest.approx(260, abs=1)


@pytest.mark.asyncio
async def test_futures_total_value_no_double_unrealized_pnl(session):
    """선물 total_value = wallet + unrealizedPnL (이중 계산 없음)."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"ETH/USDT": 3500}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )

    adapter = AsyncMock()
    # contracts=0.01, entry=3000, current=3500 → unPnl = 0.01*(3500-3000) = 5
    # wallet=300, margin=50 → free = 300+5-50 = 255, total = 300+5 = 305
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=255, used=50, total=305),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[
        {
            "symbol": "ETH/USDT:USDT",
            "contracts": 0.01,
            "side": "long",
            "initialMargin": 50,
            "leverage": "3",
            "entryPrice": 3000,
            "liquidationPrice": 2000,
            "notional": 150,
            "unrealizedPnl": 5,
        }
    ])

    pos = Position(
        exchange="binance_futures", symbol="ETH/USDT",
        quantity=0.01, average_buy_price=3000,
        total_invested=50, is_paper=False,
        direction="long", leverage=3, margin_used=50,
    )
    session.add(pos)
    await session.flush()

    # initialize_cash_from_exchange로 cash 설정
    await pm.initialize_cash_from_exchange(adapter)

    # cash = wallet(300) - margin(50) = 250
    assert pm.cash_balance == pytest.approx(250, abs=1)

    summary = await pm.get_portfolio_summary(session)
    # total = cash(250) + position_value(margin+unPnL = 50+5 = 55) = 305
    # = wallet(300) + unPnL(5) = 305 (equity) ✓
    assert summary["total_value_krw"] == pytest.approx(305, abs=2)


@pytest.mark.asyncio
async def test_futures_sync_no_positions_cash_unchanged(session):
    """선물 포지션 없을 때 sync는 cash를 변경하지 않음."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=300, used=0, total=300),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, [])

    # 선물 sync는 cash를 변경하지 않음 → 초기값 유지
    assert pm.cash_balance == pytest.approx(300, abs=1)


@pytest.mark.asyncio
async def test_futures_initialize_cash_no_positions(session):
    """initialize_cash_from_exchange 포지션 없을 때 cash = wallet 전체."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=100,
        exchange_name="binance_futures",
    )

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=300, used=0, total=300),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])

    await pm.initialize_cash_from_exchange(adapter)

    # cash = wallet(300) - margin(0) = 300
    assert pm.cash_balance == pytest.approx(300, abs=1)


@pytest.mark.asyncio
async def test_apply_income_funding_fee(session):
    """apply_income으로 펀딩비가 cash에 반영된다."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )

    adapter = AsyncMock()
    adapter.fetch_income = AsyncMock(return_value=[
        {"income_type": "FUNDING_FEE", "income": -0.5, "asset": "USDT",
         "time": 1709900000000, "symbol": "BTCUSDT"},
        {"income_type": "FUNDING_FEE", "income": 0.3, "asset": "USDT",
         "time": 1709928800000, "symbol": "ETHUSDT"},
    ])

    result = await pm.apply_income(adapter)

    assert result == pytest.approx(-0.2, abs=0.01)
    assert pm.cash_balance == pytest.approx(299.8, abs=0.01)
    # _last_income_time_ms 업데이트 확인
    assert pm._last_income_time_ms == 1709928800000


@pytest.mark.asyncio
async def test_apply_income_not_futures(session):
    """현물 엔진에서는 apply_income이 아무것도 하지 않는다."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300,
        exchange_name="bithumb",
    )

    adapter = AsyncMock()
    result = await pm.apply_income(adapter)

    assert result == 0.0
    assert pm.cash_balance == 300
    adapter.fetch_income.assert_not_called()


@pytest.mark.asyncio
async def test_initialize_cash_not_futures(session):
    """현물 엔진에서는 initialize_cash_from_exchange가 아무것도 하지 않는다."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300,
        exchange_name="bithumb",
    )

    adapter = AsyncMock()
    await pm.initialize_cash_from_exchange(adapter)

    assert pm.cash_balance == 300
    adapter.fetch_balance.assert_not_called()


@pytest.mark.asyncio
async def test_spot_sync_cash_uses_free_balance(session):
    """현물 sync에서는 기존대로 free balance 사용."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
        exchange_name="bithumb",
    )

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "KRW": Balance(currency="KRW", free=450_000, used=50_000, total=500_000),
    })

    await pm.sync_exchange_positions(session, adapter, [])

    # 현물은 free 그대로
    assert pm.cash_balance == pytest.approx(450_000, abs=1)


@pytest.mark.asyncio
async def test_sync_clears_position_not_on_exchange(session):
    """거래소에 없는 포지션(수동 매도)은 quantity=0으로 정리."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"MOCA/KRW": 23.0}),
        initial_balance_krw=300_000,
        exchange_name="bithumb",
    )

    # DB에 MOCA/KRW 포지션 존재
    session.add(Position(
        exchange="bithumb", symbol="MOCA/KRW",
        quantity=43.56, average_buy_price=23.11,
        total_invested=1007, is_paper=False,
    ))
    await session.flush()

    # 거래소에는 KRW만 있고 MOCA 없음 (수동 매도됨)
    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "KRW": Balance(currency="KRW", free=315_000, used=0, total=315_000),
    })

    await pm.sync_exchange_positions(session, adapter, ["BTC/KRW"])
    await session.flush()

    # MOCA/KRW quantity가 0으로 정리됨
    result = await session.execute(
        select(Position).where(Position.symbol == "MOCA/KRW", Position.exchange == "bithumb")
    )
    pos = result.scalar_one()
    assert pos.quantity == 0

    # _cleared_positions에 기록됨
    assert len(pm._cleared_positions) == 1
    cp = pm._cleared_positions[0]
    assert cp["symbol"] == "MOCA/KRW"
    assert cp["direction"] == "long"
    assert cp["invested"] == 1007

    # Order 기록이 생성됨 (거래 이력 추적 가능)
    from core.models import Order as OrderModel
    order_result = await session.execute(
        select(OrderModel).where(
            OrderModel.symbol == "MOCA/KRW",
            OrderModel.strategy_name == "position_sync",
        )
    )
    order = order_result.scalar_one_or_none()
    assert order is not None
    assert order.side == "sell"
    assert order.status == "filled"
    assert order.executed_quantity == 43.56


@pytest.mark.asyncio
async def test_sync_clears_zombie_with_dust_on_exchange(session):
    """거래소에 dust(가치 미만) 잔고만 남은 포지션은 좀비로 정리돼야 한다."""
    from exchange.base import Balance
    from core.models import Order as OrderModel

    # DB에 MOCA/USDT 포지션 존재 (qty=50, 가치 $100)
    session.add(Position(
        exchange="binance_spot", symbol="MOCA/USDT",
        quantity=50.0, average_buy_price=2.0,
        total_invested=100.0, is_paper=False,
    ))
    await session.flush()

    # 거래소: MOCA가 dust 수준(0.0001개, 가격 $2 → $0.0002 < $1)만 남음
    pm = PortfolioManager(
        market_data=_make_market_data({"MOCA/USDT": 2.0}),
        initial_balance_krw=1000,
        is_paper=False,
        exchange_name="binance_spot",
    )
    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=900, used=0, total=900),
        "MOCA": Balance(currency="MOCA", free=0.0001, used=0, total=0.0001),
    })

    await pm.sync_exchange_positions(session, adapter, ["MOCA/USDT"])
    await session.flush()

    # MOCA/USDT quantity가 0으로 정리됨 (dust는 zombie로 처리)
    result = await session.execute(
        select(Position).where(Position.symbol == "MOCA/USDT", Position.exchange == "binance_spot")
    )
    pos = result.scalar_one()
    assert pos.quantity == 0

    # _cleared_positions에 기록됨
    assert len(pm._cleared_positions) == 1
    assert pm._cleared_positions[0]["symbol"] == "MOCA/USDT"

    # Order 기록이 생성됨
    order_result = await session.execute(
        select(OrderModel).where(
            OrderModel.symbol == "MOCA/USDT",
            OrderModel.strategy_name == "position_sync",
        )
    )
    assert order_result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_sync_keeps_position_with_normal_balance(session):
    """거래소에 정상 잔고가 있으면 좀비로 정리되지 않아야 한다."""
    from exchange.base import Balance

    session.add(Position(
        exchange="binance_spot", symbol="BTC/USDT",
        quantity=0.01, average_buy_price=50000.0,
        total_invested=500.0, is_paper=False,
    ))
    await session.flush()

    # 거래소: BTC가 $500 이상 (정상 잔고)
    pm = PortfolioManager(
        market_data=_make_market_data({"BTC/USDT": 50000.0}),
        initial_balance_krw=1000,
        is_paper=False,
        exchange_name="binance_spot",
    )
    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=500, used=0, total=500),
        "BTC": Balance(currency="BTC", free=0.01, used=0, total=0.01),
    })

    await pm.sync_exchange_positions(session, adapter, ["BTC/USDT"])
    await session.flush()

    # 포지션이 그대로 유지됨
    result = await session.execute(
        select(Position).where(Position.symbol == "BTC/USDT", Position.exchange == "binance_spot")
    )
    pos = result.scalar_one()
    assert pos.quantity == pytest.approx(0.01)
    assert len(pm._cleared_positions) == 0


@pytest.mark.asyncio
async def test_sync_cleared_position_futures_liquidation(session):
    """선물 포지션이 거래소에서 사라지면 _cleared_positions에 기록."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"ETH/USDT": 1300.0}),
        initial_balance_krw=500,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # DB에 ETH/USDT long 포지션 (entry 2000, 현재가 1300, lev 3 → -105% = 강제청산)
    session.add(Position(
        exchange="binance_futures", symbol="ETH/USDT",
        quantity=0.1, average_buy_price=2000.0,
        total_invested=66.7, is_paper=False,
        direction="long", leverage=3, margin_used=66.7,
    ))
    await session.flush()

    # 거래소: USDT만 있고 ETH 포지션 없음 (청산됨)
    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=200, used=0, total=200),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    # Income API: INSURANCE_CLEAR 없음 → PnL 기반 강제청산 추정
    adapter.fetch_income = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, ["ETH/USDT"])
    await session.flush()

    # DB 포지션 0으로 정리됨
    result = await session.execute(
        select(Position).where(Position.symbol == "ETH/USDT", Position.exchange == "binance_futures")
    )
    pos = result.scalar_one()
    assert pos.quantity == 0
    assert pos.last_sell_at is not None

    # _cleared_positions에 기록됨 (큰 손실 → 강제청산 추정)
    assert len(pm._cleared_positions) == 1
    cp = pm._cleared_positions[0]
    assert cp["symbol"] == "ETH/USDT"
    assert cp["direction"] == "long"
    assert cp["leverage"] == 3
    assert "청산" in cp["reason"]

    # Order 기록이 생성됨 (strategy_name=forced_liquidation)
    from core.models import Order as OrderModel
    order_result = await session.execute(
        select(OrderModel).where(
            OrderModel.symbol == "ETH/USDT",
            OrderModel.exchange == "binance_futures",
            OrderModel.strategy_name == "forced_liquidation",
        )
    )
    order = order_result.scalar_one_or_none()
    assert order is not None
    assert order.side == "sell"
    assert order.status == "filled"
    assert order.realized_pnl is not None
    assert order.realized_pnl_pct < -80  # 강제청산 수준


# ── COIN-56: 서지 포지션 미포함으로 오진 청산 방지 테스트 ──────────────────────────


@pytest.mark.asyncio
async def test_sync_does_not_clear_futures_when_surge_active(session):
    """서지 엔진이 같은 심볼 활성 포지션 보유 시 선물 DB 포지션 오진 청산 방지.

    서지와 선물 엔진은 같은 물리 계정을 공유하므로, 서지가 포지션을 닫으면
    exchange_symbols에서 해당 심볼이 사라져 선물 DB 포지션이 거짓 청산될 수 있다.
    서지 DB에 qty>0 포지션이 있으면 exchange_symbols에 포함되어야 한다.
    """
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"BTC/USDT": 50000.0}),
        initial_balance_krw=1000,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # 선물 DB 포지션: BTC/USDT qty=0.01 (active)
    session.add(Position(
        exchange="binance_futures", symbol="BTC/USDT",
        quantity=0.01, average_buy_price=50000.0,
        total_invested=166.7, is_paper=False,
        direction="long", leverage=3, margin_used=166.7,
    ))
    # 서지 DB 포지션: 같은 BTC/USDT, qty=0.005 (서지 엔진이 활성 보유 중)
    session.add(Position(
        exchange="binance_surge", symbol="BTC/USDT",
        quantity=0.005, average_buy_price=50000.0,
        total_invested=83.3, is_paper=False,
        direction="long", leverage=3,
    ))
    await session.flush()

    # 거래소 API: 포지션 없음 (서지가 이미 닫았거나, 조회 타이밍 문제)
    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=500, used=0, total=500),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, ["BTC/USDT"])
    await session.flush()

    # 선물 DB 포지션은 그대로 유지돼야 한다 (오진 청산 금지)
    result = await session.execute(
        select(Position).where(
            Position.symbol == "BTC/USDT",
            Position.exchange == "binance_futures",
        )
    )
    pos = result.scalar_one()
    assert pos.quantity == 0.01, "surge 활성 포지션이 있으면 futures 포지션을 오진 청산하면 안 됨"

    # _cleared_positions에 기록되지 않아야 함
    assert len(pm._cleared_positions) == 0, "오진 청산이 발생해서는 안 됨"


@pytest.mark.asyncio
async def test_sync_clears_futures_when_surge_also_closed(session):
    """서지 포지션도 닫혀 있으면(qty=0) 선물 DB 포지션은 정상적으로 청산돼야 한다."""
    from exchange.base import Balance
    from core.models import Order as OrderModel

    pm = PortfolioManager(
        market_data=_make_market_data({"ETH/USDT": 1500.0}),
        initial_balance_krw=1000,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # 선물 DB 포지션: ETH/USDT qty=0.1 (active)
    session.add(Position(
        exchange="binance_futures", symbol="ETH/USDT",
        quantity=0.1, average_buy_price=2000.0,
        total_invested=66.7, is_paper=False,
        direction="long", leverage=3, margin_used=66.7,
    ))
    # 서지 DB 포지션: 같은 ETH/USDT, qty=0 (서지도 이미 닫음)
    session.add(Position(
        exchange="binance_surge", symbol="ETH/USDT",
        quantity=0.0, average_buy_price=2000.0,
        total_invested=0.0, is_paper=False,
        direction="long", leverage=3,
    ))
    await session.flush()

    # 거래소 API: ETH 포지션 없음
    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=300, used=0, total=300),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, ["ETH/USDT"])
    await session.flush()

    # 선물 DB 포지션은 청산돼야 한다 (서지도 qty=0이므로 보호 불필요)
    result = await session.execute(
        select(Position).where(
            Position.symbol == "ETH/USDT",
            Position.exchange == "binance_futures",
        )
    )
    pos = result.scalar_one()
    assert pos.quantity == 0, "서지도 닫혀 있으면 선물 포지션은 정상 청산돼야 함"

    # _cleared_positions에 기록돼야 함
    assert len(pm._cleared_positions) == 1
    assert pm._cleared_positions[0]["symbol"] == "ETH/USDT"

    # Order 기록이 생성돼야 함
    order_result = await session.execute(
        select(OrderModel).where(
            OrderModel.symbol == "ETH/USDT",
            OrderModel.exchange == "binance_futures",
        )
    )
    order = order_result.scalar_one_or_none()
    assert order is not None


@pytest.mark.asyncio
async def test_sync_clears_futures_when_no_surge_position(session):
    """서지 포지션이 아예 없는 경우 선물 ghost 포지션은 정상 청산돼야 한다."""
    from exchange.base import Balance
    from core.models import Order as OrderModel

    pm = PortfolioManager(
        market_data=_make_market_data({"SOL/USDT": 20.0}),
        initial_balance_krw=1000,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # 선물 DB 포지션만 존재, 서지 DB 포지션 없음
    session.add(Position(
        exchange="binance_futures", symbol="SOL/USDT",
        quantity=5.0, average_buy_price=25.0,
        total_invested=41.7, is_paper=False,
        direction="long", leverage=3, margin_used=41.7,
    ))
    await session.flush()

    # 거래소 API: SOL 포지션 없음 (ghost position)
    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=100, used=0, total=100),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, ["SOL/USDT"])
    await session.flush()

    # 선물 DB 포지션은 청산돼야 한다
    result = await session.execute(
        select(Position).where(
            Position.symbol == "SOL/USDT",
            Position.exchange == "binance_futures",
        )
    )
    pos = result.scalar_one()
    assert pos.quantity == 0, "서지 포지션이 없으면 ghost 선물 포지션은 정상 청산돼야 함"

    assert len(pm._cleared_positions) == 1
    assert pm._cleared_positions[0]["symbol"] == "SOL/USDT"

    order_result = await session.execute(
        select(OrderModel).where(
            OrderModel.symbol == "SOL/USDT",
            OrderModel.exchange == "binance_futures",
        )
    )
    assert order_result.scalar_one_or_none() is not None


# ── COIN-14: 포지션 종료 사유 판별 테스트 ──────────────────────────


@pytest.mark.asyncio
async def test_determine_close_reason_stop_loss(session):
    """SL 수준 이하 PnL → stop_loss로 판별."""
    from exchange.base import Balance
    from core.models import Order as OrderModel

    pm = PortfolioManager(
        market_data=_make_market_data({"BTC/USDT": 47500.0}),
        initial_balance_krw=1000,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # DB에 BTC/USDT long 포지션 (entry 50000, SL 5%)
    # 현재가 47500 → PnL = (47500-50000)/50000 * 3 * 100 = -15% (lev 3)
    session.add(Position(
        exchange="binance_futures", symbol="BTC/USDT",
        quantity=0.01, average_buy_price=50000.0,
        total_invested=167.0, is_paper=False,
        direction="long", leverage=3, margin_used=167.0,
        stop_loss_pct=5.0, take_profit_pct=10.0,
    ))
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=800, used=0, total=800),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, ["BTC/USDT"])
    await session.flush()

    # strategy_name이 "stop_loss"로 기록됨
    order_result = await session.execute(
        select(OrderModel).where(
            OrderModel.symbol == "BTC/USDT",
            OrderModel.exchange == "binance_futures",
        )
    )
    order = order_result.scalar_one()
    assert order.strategy_name == "stop_loss"
    assert "SL" in order.signal_reason
    assert order.realized_pnl_pct < -5  # SL 수준 이하

    cp = pm._cleared_positions[0]
    assert "SL" in cp["reason"]


@pytest.mark.asyncio
async def test_determine_close_reason_take_profit(session):
    """TP 수준 이상 PnL → take_profit로 판별."""
    from exchange.base import Balance
    from core.models import Order as OrderModel

    pm = PortfolioManager(
        market_data=_make_market_data({"ETH/USDT": 2200.0}),
        initial_balance_krw=1000,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # DB에 ETH/USDT long 포지션 (entry 2000, TP 8%)
    # 현재가 2200 → PnL = (2200-2000)/2000 * 3 * 100 = +30% (lev 3)
    session.add(Position(
        exchange="binance_futures", symbol="ETH/USDT",
        quantity=0.1, average_buy_price=2000.0,
        total_invested=67.0, is_paper=False,
        direction="long", leverage=3, margin_used=67.0,
        stop_loss_pct=5.0, take_profit_pct=8.0,
        trailing_active=False,
    ))
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=900, used=0, total=900),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, ["ETH/USDT"])
    await session.flush()

    order_result = await session.execute(
        select(OrderModel).where(
            OrderModel.symbol == "ETH/USDT",
            OrderModel.exchange == "binance_futures",
        )
    )
    order = order_result.scalar_one()
    assert order.strategy_name == "take_profit"
    assert "TP" in order.signal_reason
    assert order.realized_pnl_pct > 8  # TP 수준 이상


@pytest.mark.asyncio
async def test_determine_close_reason_trailing_stop(session):
    """트레일링 스탑 활성 + 하락 → trailing_stop로 판별."""
    from exchange.base import Balance
    from core.models import Order as OrderModel

    pm = PortfolioManager(
        market_data=_make_market_data({"SOL/USDT": 105.0}),
        initial_balance_krw=1000,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # DB에 SOL/USDT long 포지션 (entry 100, trailing 활성, highest 115)
    # 현재가 105 → PnL = (105-100)/100 * 3 * 100 = +15%
    # highest 115 → drawdown = (115-105)/115 * 100 = 8.7%
    session.add(Position(
        exchange="binance_futures", symbol="SOL/USDT",
        quantity=1.0, average_buy_price=100.0,
        total_invested=33.3, is_paper=False,
        direction="long", leverage=3, margin_used=33.3,
        stop_loss_pct=5.0, take_profit_pct=10.0,
        trailing_activation_pct=5.0, trailing_stop_pct=4.0,
        trailing_active=True, highest_price=115.0,
    ))
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=900, used=0, total=900),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, ["SOL/USDT"])
    await session.flush()

    order_result = await session.execute(
        select(OrderModel).where(
            OrderModel.symbol == "SOL/USDT",
            OrderModel.exchange == "binance_futures",
        )
    )
    order = order_result.scalar_one()
    assert order.strategy_name == "trailing_stop"
    assert "트레일링" in order.signal_reason


@pytest.mark.asyncio
async def test_determine_close_reason_income_api_liquidation(session):
    """Income API에서 INSURANCE_CLEAR 확인 → forced_liquidation으로 판별."""
    from exchange.base import Balance
    from core.models import Order as OrderModel

    pm = PortfolioManager(
        market_data=_make_market_data({"FIL/USDT": 4.0}),
        initial_balance_krw=1000,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # DB에 FIL/USDT long 포지션 (중간 손실, PnL < -80 아님)
    # entry 5.0, 현재가 4.0 → PnL = (4-5)/5 * 3 * 100 = -60% (강제청산 추정 기준 미달)
    session.add(Position(
        exchange="binance_futures", symbol="FIL/USDT",
        quantity=10.0, average_buy_price=5.0,
        total_invested=16.7, is_paper=False,
        direction="long", leverage=3, margin_used=16.7,
        stop_loss_pct=5.0,
    ))
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=400, used=0, total=400),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    # Income API에서 INSURANCE_CLEAR 이벤트 반환 → 확정 강제청산
    adapter.fetch_income = AsyncMock(return_value=[
        {"income_type": "INSURANCE_CLEAR", "income": -16.7, "symbol": "FILUSDT", "time": 0, "asset": "USDT"},
    ])

    await pm.sync_exchange_positions(session, adapter, ["FIL/USDT"])
    await session.flush()

    order_result = await session.execute(
        select(OrderModel).where(
            OrderModel.symbol == "FIL/USDT",
            OrderModel.exchange == "binance_futures",
        )
    )
    order = order_result.scalar_one()
    assert order.strategy_name == "forced_liquidation"
    assert "Income API" in order.signal_reason


@pytest.mark.asyncio
async def test_determine_close_reason_time_expiry(session):
    """보유 시간 초과 → time_expiry로 판별."""
    from exchange.base import Balance
    from core.models import Order as OrderModel

    pm = PortfolioManager(
        market_data=_make_market_data({"DOGE/USDT": 0.10}),
        initial_balance_krw=1000,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # DB에 DOGE/USDT 서지 포지션 (max_hold 48h, 50시간 전 진입)
    # 현재가 = entry → PnL 0% (SL/TP 히트 안됨)
    entered = datetime.now(timezone.utc) - timedelta(hours=50)
    session.add(Position(
        exchange="binance_futures", symbol="DOGE/USDT",
        quantity=100.0, average_buy_price=0.10,
        total_invested=3.3, is_paper=False,
        direction="long", leverage=3, margin_used=3.3,
        is_surge=True, max_hold_hours=48.0, entered_at=entered,
    ))
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=900, used=0, total=900),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, ["DOGE/USDT"])
    await session.flush()

    order_result = await session.execute(
        select(OrderModel).where(
            OrderModel.symbol == "DOGE/USDT",
            OrderModel.exchange == "binance_futures",
        )
    )
    order = order_result.scalar_one()
    assert order.strategy_name == "time_expiry"
    assert "시간 초과" in order.signal_reason


@pytest.mark.asyncio
async def test_determine_close_reason_fallback(session):
    """SL/TP/trailing/시간초과/강제청산 어디에도 해당하지 않으면 position_sync."""
    from exchange.base import Balance
    from core.models import Order as OrderModel

    pm = PortfolioManager(
        market_data=_make_market_data({"RENDER/USDT": 7.5}),
        initial_balance_krw=1000,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # DB에 RENDER/USDT long 포지션 (SL/TP 수준 미설정, 작은 손실)
    # entry 8.0, 현재가 7.5 → PnL = (7.5-8)/8 * 3 * 100 = -18.75%
    session.add(Position(
        exchange="binance_futures", symbol="RENDER/USDT",
        quantity=5.0, average_buy_price=8.0,
        total_invested=13.3, is_paper=False,
        direction="long", leverage=3, margin_used=13.3,
        # SL/TP 미설정
    ))
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=900, used=0, total=900),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, ["RENDER/USDT"])
    await session.flush()

    order_result = await session.execute(
        select(OrderModel).where(
            OrderModel.symbol == "RENDER/USDT",
            OrderModel.exchange == "binance_futures",
        )
    )
    order = order_result.scalar_one()
    assert order.strategy_name == "position_sync"
    assert "다운타임" in order.signal_reason


@pytest.mark.asyncio
async def test_determine_close_reason_short_stop_loss(session):
    """숏 포지션 SL 히트 → stop_loss로 판별."""
    from exchange.base import Balance
    from core.models import Order as OrderModel

    pm = PortfolioManager(
        market_data=_make_market_data({"ETH/USDT": 2200.0}),
        initial_balance_krw=1000,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # DB에 ETH/USDT short 포지션 (entry 2000, SL 5%)
    # 현재가 2200 → PnL = (2000-2200)/2000 * 3 * 100 = -30% (SL 5% 초과)
    session.add(Position(
        exchange="binance_futures", symbol="ETH/USDT",
        quantity=0.1, average_buy_price=2000.0,
        total_invested=67.0, is_paper=False,
        direction="short", leverage=3, margin_used=67.0,
        stop_loss_pct=5.0, take_profit_pct=10.0,
    ))
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=900, used=0, total=900),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, ["ETH/USDT"])
    await session.flush()

    order_result = await session.execute(
        select(OrderModel).where(
            OrderModel.symbol == "ETH/USDT",
            OrderModel.exchange == "binance_futures",
        )
    )
    order = order_result.scalar_one()
    assert order.strategy_name == "stop_loss"
    assert "SL" in order.signal_reason
    # 숏은 buy로 청산
    assert order.side == "buy"


@pytest.mark.asyncio
async def test_determine_close_reason_income_api_failure_falls_back(session):
    """Income API 실패 시 PnL 기반 추정으로 폴백."""
    from exchange.base import Balance
    from core.models import Order as OrderModel

    pm = PortfolioManager(
        market_data=_make_market_data({"ETH/USDT": 500.0}),
        initial_balance_krw=1000,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # DB에 ETH/USDT long 포지션 (entry 2000, 현재가 500 → PnL = -225% → 강제청산)
    session.add(Position(
        exchange="binance_futures", symbol="ETH/USDT",
        quantity=0.1, average_buy_price=2000.0,
        total_invested=67.0, is_paper=False,
        direction="long", leverage=3, margin_used=67.0,
    ))
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=50, used=0, total=50),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    # Income API가 예외 발생
    adapter.fetch_income = AsyncMock(side_effect=Exception("API timeout"))

    await pm.sync_exchange_positions(session, adapter, ["ETH/USDT"])
    await session.flush()

    order_result = await session.execute(
        select(OrderModel).where(
            OrderModel.symbol == "ETH/USDT",
            OrderModel.exchange == "binance_futures",
        )
    )
    order = order_result.scalar_one()
    # Income API 실패 → PnL 기반 강제청산 추정
    assert order.strategy_name == "forced_liquidation"
    assert "추정" in order.signal_reason


@pytest.mark.asyncio
async def test_downtime_stops_check(session):
    """_check_downtime_stops: 시작 시 보유 포지션의 SL/TP 즉시 체크."""
    from engine.trading_engine import TradingEngine
    from unittest.mock import patch
    from contextlib import asynccontextmanager

    # 엔진 최소 셋업
    engine = TradingEngine.__new__(TradingEngine)
    engine._exchange_name = "binance_spot"
    engine._is_running = True
    engine._position_trackers = {}

    # mock _check_stop_conditions
    checked_symbols = []

    async def mock_check(sess, sym, pos):
        checked_symbols.append(sym)
        return False

    engine._check_stop_conditions = mock_check

    # DB에 보유 포지션 2개 추가
    session.add(Position(
        exchange="binance_spot", symbol="BTC/USDT",
        quantity=0.01, average_buy_price=60000, total_invested=600, is_paper=False,
    ))
    session.add(Position(
        exchange="binance_spot", symbol="ETH/USDT",
        quantity=0.1, average_buy_price=2000, total_invested=200, is_paper=False,
    ))
    await session.flush()

    @asynccontextmanager
    async def mock_session_ctx():
        yield session

    with patch("db.session.get_session_factory", return_value=mock_session_ctx):
        await engine._check_downtime_stops()

    assert "BTC/USDT" in checked_symbols
    assert "ETH/USDT" in checked_symbols


# ── PositionTracker DB Persistence Tests ──


@pytest.mark.asyncio
async def test_save_tracker_to_db(session):
    """_save_tracker_to_db writes tracker fields to Position record."""
    from engine.trading_engine import TradingEngine, PositionTracker

    pos = Position(
        exchange="bithumb", symbol="BTC/KRW",
        quantity=0.001, average_buy_price=50_000_000,
        total_invested=50_000, is_paper=True,
    )
    session.add(pos)
    await session.flush()

    engine = TradingEngine.__new__(TradingEngine)
    engine._exchange_name = "bithumb"

    tracker = PositionTracker(
        entry_price=50_000_000,
        extreme_price=52_000_000,
        stop_loss_pct=6.5,
        take_profit_pct=12.0,
        trailing_activation_pct=4.0,
        trailing_stop_pct=3.5,
        trailing_active=True,
        max_hold_hours=48,
    )
    await engine._save_tracker_to_db(session, "BTC/KRW", tracker)

    await session.refresh(pos)
    assert pos.stop_loss_pct == pytest.approx(6.5)
    assert pos.take_profit_pct == pytest.approx(12.0)
    assert pos.trailing_activation_pct == pytest.approx(4.0)
    assert pos.trailing_stop_pct == pytest.approx(3.5)
    assert pos.trailing_active is True
    assert pos.highest_price == pytest.approx(52_000_000)
    assert pos.max_hold_hours == pytest.approx(48)


@pytest.mark.asyncio
async def test_tracker_restore_from_db(session):
    """트래커가 없을 때 DB의 stop_loss_pct가 있으면 DB 값으로 복원."""
    from engine.trading_engine import PositionTracker

    pos = Position(
        exchange="bithumb", symbol="ETH/KRW",
        quantity=1.0, average_buy_price=4_000_000,
        total_invested=4_000_000, is_paper=True,
        stop_loss_pct=7.0,
        take_profit_pct=15.0,
        trailing_activation_pct=5.0,
        trailing_stop_pct=4.0,
        trailing_active=True,
        highest_price=4_500_000,
        max_hold_hours=0,
    )
    session.add(pos)
    await session.flush()

    # Simulate tracker restoration logic (from _check_stop_conditions)
    tracker = PositionTracker(
        entry_price=pos.average_buy_price,
        extreme_price=pos.highest_price or pos.average_buy_price,
        stop_loss_pct=pos.stop_loss_pct,
        take_profit_pct=pos.take_profit_pct or 10.0,
        trailing_activation_pct=pos.trailing_activation_pct or 3.0,
        trailing_stop_pct=pos.trailing_stop_pct or 3.0,
        trailing_active=pos.trailing_active or False,
        is_surge=pos.is_surge or False,
        max_hold_hours=pos.max_hold_hours or 0,
    )

    assert tracker.stop_loss_pct == pytest.approx(7.0)
    assert tracker.take_profit_pct == pytest.approx(15.0)
    assert tracker.trailing_activation_pct == pytest.approx(5.0)
    assert tracker.trailing_stop_pct == pytest.approx(4.0)
    assert tracker.trailing_active is True
    assert tracker.extreme_price == pytest.approx(4_500_000)


@pytest.mark.asyncio
async def test_tracker_fallback_when_no_db_values(session):
    """DB에 stop_loss_pct가 None이면 기존 폴백 로직 사용."""
    pos = Position(
        exchange="bithumb", symbol="ADA/KRW",
        quantity=100, average_buy_price=500,
        total_invested=50_000, is_paper=True,
        # stop_loss_pct is None → 마이그레이션 전 포지션
    )
    session.add(pos)
    await session.flush()

    # stop_loss_pct is None → fallback
    assert pos.stop_loss_pct is None


@pytest.mark.asyncio
async def test_portfolio_summary_includes_sl_tp_prices(session):
    """포트폴리오 서머리에 SL/TP 가격이 포함됨."""
    prices = {"BTC/KRW": 52_000_000}
    pm = PortfolioManager(
        market_data=_make_market_data(prices),
        initial_balance_krw=500_000,
    )

    pos = Position(
        symbol="BTC/KRW",
        quantity=0.001,
        average_buy_price=50_000_000,
        total_invested=50_000,
        is_paper=True,
        stop_loss_pct=5.0,
        take_profit_pct=10.0,
        trailing_active=True,
        is_surge=False,
    )
    session.add(pos)
    session.add(Order(
        symbol="BTC/KRW", side="buy", order_type="limit", status="filled",
        requested_price=50_000_000, executed_price=50_000_000,
        requested_quantity=0.001, executed_quantity=0.001,
        fee=150, is_paper=True, strategy_name="rsi",
    ))
    await session.flush()

    pm._cash_balance = 500_000 - 50_150
    summary = await pm.get_portfolio_summary(session)

    p = summary["positions"][0]
    # SL: 50M * (1 - 5/100) = 47,500,000
    assert p["stop_loss_price"] == pytest.approx(47_500_000, abs=1)
    # TP: 50M * (1 + 10/100) = 55,000,000
    assert p["take_profit_price"] == pytest.approx(55_000_000, abs=1)
    assert p["trailing_active"] is True
    assert p["is_surge"] is False


@pytest.mark.asyncio
async def test_portfolio_summary_short_sl_tp_reversed(session):
    """선물 숏 포지션의 SL/TP 가격은 방향 반전."""
    prices = {"BTC/USDT": 95_000}
    pm = PortfolioManager(
        market_data=_make_market_data(prices),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )

    pos = Position(
        exchange="binance_futures",
        symbol="BTC/USDT",
        quantity=0.01,
        average_buy_price=100_000,
        total_invested=333.33,
        is_paper=True,
        direction="short",
        leverage=3,
        stop_loss_pct=8.0,
        take_profit_pct=16.0,
    )
    session.add(pos)
    session.add(Order(
        exchange="binance_futures",
        symbol="BTC/USDT", side="sell", order_type="market", status="filled",
        requested_price=100_000, executed_price=100_000,
        requested_quantity=0.01, executed_quantity=0.01,
        fee=0.04, is_paper=True, strategy_name="rsi",
        direction="short",
    ))
    await session.flush()

    pm._cash_balance = 0
    summary = await pm.get_portfolio_summary(session)
    p = summary["positions"][0]

    # 숏 SL: entry * (1 + sl_pct/100) = 100,000 * 1.08 = 108,000
    assert p["stop_loss_price"] == pytest.approx(108_000, abs=1)
    # 숏 TP: entry * (1 - tp_pct/100) = 100,000 * 0.84 = 84,000
    assert p["take_profit_price"] == pytest.approx(84_000, abs=1)


@pytest.mark.asyncio
async def test_portfolio_summary_no_sl_tp_when_null(session):
    """DB에 SL/TP가 null이면 API에서 None 반환."""
    prices = {"ADA/KRW": 500}
    pm = PortfolioManager(
        market_data=_make_market_data(prices),
        initial_balance_krw=500_000,
    )

    pos = Position(
        symbol="ADA/KRW",
        quantity=100,
        average_buy_price=450,
        total_invested=45_000,
        is_paper=True,
        # stop_loss_pct is None
    )
    session.add(pos)
    session.add(Order(
        symbol="ADA/KRW", side="buy", order_type="limit", status="filled",
        requested_price=450, executed_price=450,
        requested_quantity=100, executed_quantity=100,
        fee=113, is_paper=True, strategy_name="rsi",
    ))
    await session.flush()

    pm._cash_balance = 500_000 - 45_113
    summary = await pm.get_portfolio_summary(session)
    p = summary["positions"][0]

    assert p["stop_loss_price"] is None
    assert p["take_profit_price"] is None


# ── Trade Timestamp Persistence ──


@pytest.mark.asyncio
async def test_buy_records_last_trade_at(session):
    """매수 시 Position.last_trade_at이 기록됨."""
    pm = PortfolioManager(
        market_data=_make_market_data({"BTC/KRW": 50_000_000}),
        initial_balance_krw=500_000,
        is_paper=True,
    )
    await pm.update_position_on_buy(
        session, "BTC/KRW", 0.001, 50_000_000, 50_000, 125,
    )
    result = await session.execute(
        select(Position).where(Position.symbol == "BTC/KRW")
    )
    pos = result.scalar_one()
    assert pos.last_trade_at is not None
    assert pos.last_sell_at is None


@pytest.mark.asyncio
async def test_sell_records_both_timestamps(session):
    """매도 시 last_trade_at + last_sell_at 모두 기록됨."""
    pm = PortfolioManager(
        market_data=_make_market_data({"BTC/KRW": 55_000_000}),
        initial_balance_krw=500_000,
        is_paper=True,
    )
    await pm.update_position_on_buy(
        session, "BTC/KRW", 0.001, 50_000_000, 50_000, 125,
    )
    await pm.update_position_on_sell(
        session, "BTC/KRW", 0.001, 55_000_000, 55_000, 137,
    )
    result = await session.execute(
        select(Position).where(Position.symbol == "BTC/KRW")
    )
    pos = result.scalar_one()
    assert pos.last_trade_at is not None
    assert pos.last_sell_at is not None
    # 매도 후 last_sell_at >= last_trade_at (실제로 같은 시점)
    assert pos.last_sell_at >= pos.last_trade_at


# ── Sync Guard Tests ──


@pytest.mark.asyncio
async def test_sync_guard_skips_during_eval(session):
    """sync_guard=True → sync_exchange_positions는 아무것도 하지 않음."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )
    pm._cash_balance = 260.0

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=999, used=0, total=999),
    })

    # sync_lock이 잠겨 있으면 sync를 스킵해야 함
    async with pm._sync_lock:
        await pm.sync_exchange_positions(session, adapter, [])
    # fetch_balance가 호출되지 않아야 함 (lock에서 return)
    adapter.fetch_balance.assert_not_called()
    assert pm.cash_balance == 260.0  # 불변


@pytest.mark.asyncio
async def test_sync_guard_allows_normal(session):
    """sync_guard=False → sync_exchange_positions 정상 동작."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )
    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=300, used=0, total=300),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, [])
    adapter.fetch_balance.assert_called_once()
    assert pm.cash_balance == pytest.approx(300, abs=1)


# ── Spike Detection Tests ──


@pytest.mark.asyncio
async def test_spike_clamps_peak(session):
    """66% 점프 → peak 업데이트 건너뜀."""
    prices = {"BTC/USDT": 100_000}
    pm = PortfolioManager(
        market_data=_make_market_data(prices),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )
    pm._peak_value = 300
    pm._cash_balance = 250
    pm._last_total_value = 300  # 이전 총자산

    # 포지션 추가 → 자산이 갑자기 500으로 점프 (66%)
    pos = Position(
        exchange="binance_futures", symbol="BTC/USDT",
        quantity=0.01, average_buy_price=95_000,
        total_invested=250, is_paper=True,
        direction="long", leverage=3,
    )
    session.add(pos)
    session.add(Order(
        exchange="binance_futures",
        symbol="BTC/USDT", side="buy", order_type="market", status="filled",
        requested_price=95_000, executed_price=95_000,
        requested_quantity=0.01, executed_quantity=0.01,
        fee=0.1, is_paper=True, strategy_name="rsi",
    ))
    await session.flush()

    summary = await pm.get_portfolio_summary(session)
    # peak는 300 유지 (스파이크로 인한 업데이트 차단)
    assert pm._peak_value == 300


@pytest.mark.asyncio
async def test_normal_growth_updates_peak(session):
    """3% 성장 → peak 정상 갱신."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    pm._peak_value = 500_000
    pm._last_total_value = 500_000

    # 소폭 상승
    pm._cash_balance = 515_000
    summary = await pm.get_portfolio_summary(session)
    # 3% 상승 → peak 갱신됨
    assert pm._peak_value == 515_000
    assert pm._last_total_value == 515_000


@pytest.mark.asyncio
async def test_spike_logs_warning(session):
    """스파이크 감지 시 peak 업데이트 안 됨 + _last_total_value 불변."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )
    pm._peak_value = 300
    pm._cash_balance = 600  # 100% 점프
    pm._last_total_value = 300

    await pm.get_portfolio_summary(session)

    # 스파이크 → peak 불변, _last_total_value도 갱신 안 됨
    assert pm._peak_value == 300
    assert pm._last_total_value == 300


@pytest.mark.asyncio
async def test_first_summary_initializes_last_total(session):
    """첫 호출 시 _last_total_value가 None → 초기화."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500_000,
    )
    assert pm._last_total_value is None

    await pm.get_portfolio_summary(session)
    assert pm._last_total_value == 500_000


@pytest.mark.asyncio
async def test_snapshot_skipped_on_cash_spike(session):
    """직전 스냅샷 대비 cash가 >20% 급변 시 스냅샷 건너뜀 (sync 오염 방어)."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )
    pm._peak_value = 300
    pm._cash_balance = 300
    pm._last_total_value = 300

    # 정상 스냅샷 먼저 기록 (cash=300)
    snap1 = await pm.take_snapshot(session)
    assert snap1 is not None
    assert snap1.cash_balance_krw == 300

    # sync가 cash를 오염시킨 상황: cash 66% 급등
    pm._cash_balance = 500
    pm._last_total_value = 300  # peak guard용

    snap2 = await pm.take_snapshot(session)
    # cash 스파이크 → None 반환, DB에 기록되지 않음
    assert snap2 is None


@pytest.mark.asyncio
async def test_snapshot_recorded_on_normal_cash_change(session):
    """cash 정상 변동(20% 이하)은 스냅샷 정상 기록."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )
    pm._peak_value = 300
    pm._cash_balance = 300
    pm._last_total_value = 300

    # 정상 스냅샷
    snap1 = await pm.take_snapshot(session)
    assert snap1 is not None

    # 소폭 cash 변동 (5%) — 정상
    pm._cash_balance = 315
    pm._last_total_value = 315

    snap2 = await pm.take_snapshot(session)
    assert snap2 is not None
    assert snap2.total_value_krw == 315


@pytest.mark.asyncio
async def test_snapshot_passes_on_market_surge(session):
    """시장 급등(invested 증가)은 cash 불변이므로 정상 기록."""
    prices = {"BTC/USDT": 100_000}
    pm = PortfolioManager(
        market_data=_make_market_data(prices),
        initial_balance_krw=250,
        exchange_name="binance_futures",
    )
    pm._peak_value = 300
    pm._cash_balance = 250
    pm._last_total_value = 300

    # 정상 스냅샷 (cash=250, invested=포지션가치)
    pos = Position(
        exchange="binance_futures", symbol="BTC/USDT",
        quantity=0.01, average_buy_price=95_000,
        total_invested=50, is_paper=True,
        direction="long", leverage=3,
    )
    session.add(pos)
    session.add(Order(
        exchange="binance_futures",
        symbol="BTC/USDT", side="buy", order_type="market", status="filled",
        requested_price=95_000, executed_price=95_000,
        requested_quantity=0.01, executed_quantity=0.01,
        fee=0.1, is_paper=True, strategy_name="rsi",
    ))
    await session.flush()

    snap1 = await pm.take_snapshot(session)
    assert snap1 is not None

    # 시장 급등: BTC 가격 30% 상승 → invested 증가, cash 불변
    pm._market_data = _make_market_data({"BTC/USDT": 130_000})
    pm._last_total_value = snap1.total_value_krw

    snap2 = await pm.take_snapshot(session)
    # cash 변동 없음 → 정상 기록됨 (시장 급등은 차단하지 않음)
    assert snap2 is not None
    assert snap2.total_value_krw > snap1.total_value_krw


# ── Snapshot Total Spike + Cash Delta Check Tests ──


@pytest.mark.asyncio
async def test_snapshot_blocked_total_spike_with_cash_change(session):
    """total 10%+ 변동 + cash 3%+ 변동 → 스냅샷 차단 (매매 직후 sync 오염)."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )
    pm._peak_value = 300
    pm._cash_balance = 300
    pm._last_total_value = 300

    # 정상 스냅샷 기록 (baseline=300)
    snap1 = await pm.take_snapshot(session)
    assert snap1 is not None

    # sync 오염: cash가 급등 → total도 >10% 상승
    # new_total = cash(350) + invested(0) = 350, baseline=300, +16.7%
    # cash_delta = |350-300|/300 = 16.7% > 3% → 차단
    pm._cash_balance = 350
    pm._last_total_value = 350

    snap2 = await pm.take_snapshot(session)
    assert snap2 is None  # 차단됨


@pytest.mark.asyncio
async def test_snapshot_allowed_total_spike_without_cash_change(session):
    """total 12% 변동이지만 cash 변동 <3% → 시장 변동으로 판단, 스냅샷 허용."""
    prices = {"BTC/USDT": 100_000}
    pm = PortfolioManager(
        market_data=_make_market_data(prices),
        initial_balance_krw=250,
        exchange_name="binance_futures",
    )
    pm._peak_value = 350
    pm._cash_balance = 200
    pm._last_total_value = 350

    # baseline 스냅샷: total=350, cash=200
    snap1 = PortfolioSnapshot(
        exchange="binance_futures",
        total_value_krw=350,
        cash_balance_krw=200,
        invested_value_krw=150,
    )
    session.add(snap1)
    await session.flush()

    # 시장 급등: invested만 커짐, cash 불변 → total 12% 변동
    pos = Position(
        exchange="binance_futures", symbol="BTC/USDT",
        quantity=0.01, average_buy_price=95_000,
        total_invested=150, is_paper=True,
        direction="long", leverage=3,
    )
    session.add(pos)
    session.add(Order(
        exchange="binance_futures",
        symbol="BTC/USDT", side="buy", order_type="market", status="filled",
        requested_price=95_000, executed_price=95_000,
        requested_quantity=0.01, executed_quantity=0.01,
        fee=0.1, is_paper=True, strategy_name="rsi",
    ))
    await session.flush()

    snap2 = await pm.take_snapshot(session)
    # cash delta < 3% → 시장 변동으로 허용
    assert snap2 is not None


@pytest.mark.asyncio
async def test_snapshot_allowed_small_total_change(session):
    """total 5% 변동(10% 미만) → 무조건 허용."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )
    pm._peak_value = 300
    pm._cash_balance = 300
    pm._last_total_value = 300

    snap1 = await pm.take_snapshot(session)
    assert snap1 is not None

    # 5% 변동 + cash도 변동 → 10% 미만이라 허용
    pm._cash_balance = 315  # +5%
    pm._last_total_value = 315

    snap2 = await pm.take_snapshot(session)
    assert snap2 is not None


@pytest.mark.asyncio
async def test_snapshot_blocked_invested_zero_spike(session):
    """invested가 0으로 급락 (sync 실패) → 스냅샷 차단."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )
    pm._peak_value = 330
    pm._cash_balance = 30
    pm._last_total_value = 330

    # baseline 스냅샷: total=330, cash=30, invested=300
    snap_prev = PortfolioSnapshot(
        exchange="binance_futures",
        total_value_krw=330,
        cash_balance_krw=30,
        invested_value_krw=300,
    )
    session.add(snap_prev)
    await session.flush()

    # sync 실패: 포지션이 사라져 invested=0, cash는 거의 불변
    # total = cash(30) = 30, invested=0
    # cash spike: |30-30|/30 = 0% → 통과
    # total spike: |30-330|/330 = 91% > 10%, but cash_delta = 0% < 3% → 기존에는 통과
    # invested zero check: prev_invested=300 > 10, new_invested=0 < 1 → 차단!
    snap = await pm.take_snapshot(session)
    assert snap is None  # invested→0 스파이크 차단


@pytest.mark.asyncio
async def test_snapshot_uses_median_baseline(session):
    """3개 이전 스냅샷의 중앙값을 baseline으로 사용."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )
    pm._peak_value = 310
    pm._last_total_value = 310

    # 3개 스냅샷: 300, 305, 310 → 중앙값=305
    for total in [300, 305, 310]:
        snap = PortfolioSnapshot(
            exchange="binance_futures",
            total_value_krw=total,
            cash_balance_krw=total,
            invested_value_krw=0,
        )
        session.add(snap)
    await session.flush()

    # baseline=305, total=345(+13.1%), cash_delta=13.1% → 차단
    pm._cash_balance = 345
    snap = await pm.take_snapshot(session)
    assert snap is None


# ── cleanup_spike_snapshots Tests ──


@pytest.mark.asyncio
async def test_cleanup_corrects_isolated_spike(session):
    """고립 스파이크: 좌우 이웃 유사, 해당 포인트만 이탈 → 보정."""
    # 10개 스냅샷: 정상-정상-정상-스파이크-정상-정상-정상-정상-정상-정상
    normals = [100, 101, 102, 200, 103, 104, 101, 102, 103, 105]
    for i, val in enumerate(normals):
        snap = PortfolioSnapshot(
            exchange="bithumb",
            total_value_krw=val,
            cash_balance_krw=val,
            invested_value_krw=0,
        )
        session.add(snap)
    await session.flush()

    fixed = await PortfolioManager.cleanup_spike_snapshots(session, "bithumb")
    assert fixed == 1  # 인덱스3(200)이 보정됨

    # 보정된 값 확인
    result = await session.execute(
        select(PortfolioSnapshot)
        .where(PortfolioSnapshot.exchange == "bithumb")
        .order_by(PortfolioSnapshot.snapshot_at.asc())
    )
    snapshots = list(result.scalars().all())
    # 인덱스3: left_med≈101, right_med≈103 → corrected≈102
    assert abs(snapshots[3].total_value_krw - 102) < 5


@pytest.mark.asyncio
async def test_cleanup_preserves_level_shift(session):
    """레벨 시프트(출금): 좌우 이웃 수준이 다름 → 보정하지 않음."""
    # 10개: 500-505-510-515-300-305-310-300-305-310 (출금으로 레벨 이동)
    values = [500, 505, 510, 515, 300, 305, 310, 300, 305, 310]
    for val in values:
        snap = PortfolioSnapshot(
            exchange="bithumb",
            total_value_krw=val,
            cash_balance_krw=val,
            invested_value_krw=0,
        )
        session.add(snap)
    await session.flush()

    fixed = await PortfolioManager.cleanup_spike_snapshots(session, "bithumb")
    assert fixed == 0  # 레벨 시프트 → 보정 없음


@pytest.mark.asyncio
async def test_cleanup_corrects_multiple_spikes(session):
    """여러 개의 고립 스파이크 모두 보정."""
    # 12개: 정상 흐름에 2개 스파이크 (인덱스3, 인덱스7)
    values = [100, 101, 102, 250, 103, 104, 105, 50, 106, 107, 108, 109]
    for val in values:
        snap = PortfolioSnapshot(
            exchange="bithumb",
            total_value_krw=val,
            cash_balance_krw=val,
            invested_value_krw=0,
        )
        session.add(snap)
    await session.flush()

    fixed = await PortfolioManager.cleanup_spike_snapshots(session, "bithumb")
    assert fixed == 2  # 2개 모두 보정


@pytest.mark.asyncio
async def test_cleanup_too_few_snapshots(session):
    """스냅샷 7개 미만 → 보정하지 않음."""
    for val in [100, 200, 100, 100, 100]:
        snap = PortfolioSnapshot(
            exchange="bithumb",
            total_value_krw=val,
            cash_balance_krw=val,
            invested_value_krw=0,
        )
        session.add(snap)
    await session.flush()

    fixed = await PortfolioManager.cleanup_spike_snapshots(session, "bithumb")
    assert fixed == 0


@pytest.mark.asyncio
async def test_cleanup_no_spikes_no_changes(session):
    """정상 데이터 → 보정 0건."""
    for val in [100, 102, 104, 106, 108, 110, 112, 114]:
        snap = PortfolioSnapshot(
            exchange="bithumb",
            total_value_krw=val,
            cash_balance_krw=val,
            invested_value_krw=0,
        )
        session.add(snap)
    await session.flush()

    fixed = await PortfolioManager.cleanup_spike_snapshots(session, "bithumb")
    assert fixed == 0


@pytest.mark.asyncio
async def test_cleanup_edge_first_last_three_untouched(session):
    """처음/끝 3개 스냅샷은 이웃 부족으로 절대 수정하지 않음."""
    # 첫 번째와 마지막이 스파이크여도 수정 안 됨
    values = [999, 100, 101, 102, 103, 104, 105, 106, 107, 999]
    for val in values:
        snap = PortfolioSnapshot(
            exchange="bithumb",
            total_value_krw=val,
            cash_balance_krw=val,
            invested_value_krw=0,
        )
        session.add(snap)
    await session.flush()

    fixed = await PortfolioManager.cleanup_spike_snapshots(session, "bithumb")
    assert fixed == 0  # 처음/끝 3개는 수정 불가


@pytest.mark.asyncio
async def test_cleanup_exchange_isolation(session):
    """다른 거래소 스냅샷에 영향 없음."""
    # bithumb: 스파이크 포함
    for val in [100, 101, 102, 300, 103, 104, 105, 106, 107, 108]:
        session.add(PortfolioSnapshot(
            exchange="bithumb",
            total_value_krw=val,
            cash_balance_krw=val,
            invested_value_krw=0,
        ))
    # binance_futures: 정상
    for val in [200, 201, 202, 203, 204, 205, 206, 207, 208, 209]:
        session.add(PortfolioSnapshot(
            exchange="binance_futures",
            total_value_krw=val,
            cash_balance_krw=val,
            invested_value_krw=0,
        ))
    await session.flush()

    # bithumb만 보정
    fixed_bithumb = await PortfolioManager.cleanup_spike_snapshots(session, "bithumb")
    assert fixed_bithumb == 1

    fixed_futures = await PortfolioManager.cleanup_spike_snapshots(session, "binance_futures")
    assert fixed_futures == 0


# ── Sync Margin Grace Period Tests ──


@pytest.mark.asyncio
async def test_sync_margin_grace_protects_recent_trade(session):
    """최근 10분 이내 거래 포지션의 margin은 sync에서 덮어쓰지 않음."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"BTC/USDT": 100_000}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )

    # DB 포지션: 2분 전 거래, margin=40
    pos = Position(
        exchange="binance_futures", symbol="BTC/USDT",
        quantity=0.001, average_buy_price=95000,
        total_invested=40, is_paper=False,
        direction="long", leverage=3, margin_used=40,
        last_trade_at=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    session.add(pos)
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=250, used=50, total=310),
        "BTC": Balance(currency="BTC", free=0.001, used=0, total=0.001),
    })
    adapter._exchange = AsyncMock()
    # 거래소가 일시적으로 잘못된 margin(80) 반환
    adapter._exchange.fetch_positions = AsyncMock(return_value=[
        {
            "symbol": "BTC/USDT:USDT",
            "contracts": 0.001,
            "side": "long",
            "initialMargin": 80,  # 실제=40, 거래소 임시 오류=80
            "leverage": "3",
            "entryPrice": 95000,
            "liquidationPrice": 60000,
            "notional": 240,
            "unrealizedPnl": 5,
        }
    ])

    await pm.sync_exchange_positions(session, adapter, ["BTC/USDT"])

    await session.refresh(pos)
    # grace period 보호: margin이 40으로 유지 (80으로 덮어쓰지 않음)
    assert pos.total_invested == pytest.approx(40, abs=1)
    assert pos.margin_used == pytest.approx(40, abs=1)


@pytest.mark.asyncio
async def test_sync_margin_updates_old_trade(session):
    """10분 이상 지난 포지션의 margin은 정상적으로 업데이트."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"BTC/USDT": 100_000}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )

    # DB 포지션: 30분 전 거래 (grace period 밖)
    pos = Position(
        exchange="binance_futures", symbol="BTC/USDT",
        quantity=0.001, average_buy_price=95000,
        total_invested=40, is_paper=False,
        direction="long", leverage=3, margin_used=40,
        last_trade_at=datetime.now(timezone.utc) - timedelta(minutes=30),
    )
    session.add(pos)
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=250, used=50, total=310),
        "BTC": Balance(currency="BTC", free=0.001, used=0, total=0.001),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[
        {
            "symbol": "BTC/USDT:USDT",
            "contracts": 0.001,
            "side": "long",
            "initialMargin": 50,  # 거래소 정상 값
            "leverage": "3",
            "entryPrice": 95000,
            "liquidationPrice": 60000,
            "notional": 150,
            "unrealizedPnl": 5,
        }
    ])

    await pm.sync_exchange_positions(session, adapter, ["BTC/USDT"])

    await session.refresh(pos)
    # grace period 밖: margin 정상 업데이트
    assert pos.total_invested == pytest.approx(50, abs=1)
    assert pos.margin_used == pytest.approx(50, abs=1)


@pytest.mark.asyncio
async def test_sync_margin_grace_spot_no_effect(session):
    """현물은 grace period 로직 무관 (is_futures=False)."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"BTC/KRW": 50_000_000}),
        initial_balance_krw=500_000,
        exchange_name="bithumb",
    )

    pos = Position(
        exchange="bithumb", symbol="BTC/KRW",
        quantity=0.001, average_buy_price=50_000_000,
        total_invested=50_000, is_paper=False,
        last_trade_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    session.add(pos)
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "KRW": Balance(currency="KRW", free=450_000, used=0, total=450_000),
        "BTC": Balance(currency="BTC", free=0.002, used=0, total=0.002),
    })

    await pm.sync_exchange_positions(session, adapter, ["BTC/KRW"])

    await session.refresh(pos)
    # 현물: 수량 불일치 시 ratio 적용 (grace period 무관)
    assert pos.quantity == pytest.approx(0.002)
    # total_invested = 50_000 * (0.002/0.001) = 100_000
    assert pos.total_invested == pytest.approx(100_000, abs=1)


@pytest.mark.asyncio
async def test_sync_margin_grace_no_last_trade_at(session):
    """last_trade_at이 None인 포지션은 grace period 보호 안 됨."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"ETH/USDT": 3500}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )

    # last_trade_at = None (마이그레이션 전 포지션)
    pos = Position(
        exchange="binance_futures", symbol="ETH/USDT",
        quantity=0.01, average_buy_price=3000,
        total_invested=30, is_paper=False,
        direction="long", leverage=3, margin_used=30,
        last_trade_at=None,
    )
    session.add(pos)
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=250, used=50, total=310),
        "ETH": Balance(currency="ETH", free=0.01, used=0, total=0.01),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[
        {
            "symbol": "ETH/USDT:USDT",
            "contracts": 0.01,
            "side": "long",
            "initialMargin": 50,
            "leverage": "3",
            "entryPrice": 3000,
            "liquidationPrice": 2000,
            "notional": 150,
            "unrealizedPnl": 5,
        }
    ])

    await pm.sync_exchange_positions(session, adapter, ["ETH/USDT"])

    await session.refresh(pos)
    # last_trade_at=None → 보호 없음 → margin 업데이트됨
    assert pos.total_invested == pytest.approx(50, abs=1)
    assert pos.margin_used == pytest.approx(50, abs=1)


# ── Consecutive Skip Force-Record Tests ──


@pytest.mark.asyncio
async def test_snapshot_forced_after_3_consecutive_cash_skips(session):
    """cash 20%+ 변동이 3회 연속 → 실제 변화로 판단, 스냅샷 강제 기록."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )
    pm._peak_value = 300
    pm._cash_balance = 300
    pm._last_total_value = 300

    # 정상 스냅샷 기록 (cash=300)
    snap1 = await pm.take_snapshot(session)
    assert snap1 is not None
    assert pm._snapshot_skip_count == 0

    # 포지션 청산으로 cash 25% 증가 (정상적 변화)
    pm._cash_balance = 375
    pm._last_total_value = 375

    # 1회차: 스킵
    snap2 = await pm.take_snapshot(session)
    assert snap2 is None
    assert pm._snapshot_skip_count == 1

    # 2회차: 여전히 스킵
    snap3 = await pm.take_snapshot(session)
    assert snap3 is None
    assert pm._snapshot_skip_count == 2

    # 3회차: 강제 기록!
    snap4 = await pm.take_snapshot(session)
    assert snap4 is not None
    assert snap4.total_value_krw == 375
    assert pm._snapshot_skip_count == 0  # 리셋됨


@pytest.mark.asyncio
async def test_snapshot_skip_count_resets_on_normal(session):
    """정상 스냅샷이 기록되면 skip_count가 0으로 리셋."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )
    pm._peak_value = 300
    pm._cash_balance = 300
    pm._last_total_value = 300

    snap1 = await pm.take_snapshot(session)
    assert snap1 is not None

    # 스파이크 1회 → 스킵
    pm._cash_balance = 400
    pm._last_total_value = 400
    snap2 = await pm.take_snapshot(session)
    assert snap2 is None
    assert pm._snapshot_skip_count == 1

    # cash가 정상으로 돌아옴
    pm._cash_balance = 310
    pm._last_total_value = 310
    snap3 = await pm.take_snapshot(session)
    assert snap3 is not None
    assert pm._snapshot_skip_count == 0  # 리셋


@pytest.mark.asyncio
async def test_snapshot_total_spike_also_counts_skip(session):
    """total spike도 연속 스킵에 카운트됨."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )
    pm._peak_value = 300
    pm._cash_balance = 300
    pm._last_total_value = 300

    # baseline 스냅샷 3개 (total=300)
    for _ in range(3):
        snap = PortfolioSnapshot(
            exchange="binance_futures",
            total_value_krw=300,
            cash_balance_krw=300,
            invested_value_krw=0,
        )
        session.add(snap)
    await session.flush()

    # total 20% 급등 + cash 15% 변동 → total spike
    pm._cash_balance = 345
    pm._last_total_value = 360

    skip1 = await pm.take_snapshot(session)
    assert skip1 is None
    assert pm._snapshot_skip_count == 1

    skip2 = await pm.take_snapshot(session)
    assert skip2 is None
    assert pm._snapshot_skip_count == 2

    # 3회차 → 강제 기록
    snap = await pm.take_snapshot(session)
    assert snap is not None
    assert pm._snapshot_skip_count == 0


# ── 선물 매도 cash 정산 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_futures_sell_returns_margin_not_notional(session):
    """선물 매도 시 margin + leveraged PnL만 반환 (notional이 아님)."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=1000,
        exchange_name="binance_futures",
    )
    # 롱 포지션: margin 100, 3x 레버리지, entry 50000
    pos = Position(
        exchange="binance_futures", symbol="BTC/USDT",
        quantity=0.006, average_buy_price=50000,
        total_invested=100, is_paper=False,
        direction="long", leverage=3, margin_used=100,
    )
    session.add(pos)
    await session.flush()

    pm._cash_balance = 900  # 1000 - 100 margin

    # 10% 가격 상승 → 55000
    await pm.update_position_on_sell(
        session, "BTC/USDT", 0.006, 55000,
        0.006 * 55000, 0.13,  # cost=notional (330), fee=0.13
    )
    # 반환: margin(100) + leveraged_pnl(100 * 3 * 0.10 = 30) - fee(0.13) = 129.87
    assert pm.cash_balance == pytest.approx(900 + 129.87, abs=0.1)
    assert pm.realized_pnl == pytest.approx(30 - 0.13, abs=0.1)


@pytest.mark.asyncio
async def test_futures_short_sell_returns_margin_plus_pnl(session):
    """선물 숏 청산 시 하락 수익이 cash에 올바르게 반영."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=1000,
        exchange_name="binance_futures",
    )
    # 숏 포지션: margin 100, 3x, entry 50000
    pos = Position(
        exchange="binance_futures", symbol="ETH/USDT",
        quantity=0.006, average_buy_price=50000,
        total_invested=100, is_paper=False,
        direction="short", leverage=3, margin_used=100,
    )
    session.add(pos)
    await session.flush()

    pm._cash_balance = 900

    # 10% 가격 하락 → 45000 (숏 수익)
    await pm.update_position_on_sell(
        session, "ETH/USDT", 0.006, 45000,
        0.006 * 45000, 0.11,
    )
    # 숏 PnL: 100 * 3 * (50000-45000)/50000 = 30
    # 반환: 100 + 30 - 0.11 = 129.89
    assert pm.cash_balance == pytest.approx(900 + 129.89, abs=0.1)
    assert pm.realized_pnl == pytest.approx(30 - 0.11, abs=0.1)


@pytest.mark.asyncio
async def test_futures_sell_loss_returns_less_than_margin(session):
    """선물 손절 시 margin에서 손실분 차감."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=1000,
        exchange_name="binance_futures",
    )
    pos = Position(
        exchange="binance_futures", symbol="SOL/USDT",
        quantity=1.0, average_buy_price=100,
        total_invested=100, is_paper=False,
        direction="long", leverage=3, margin_used=100,
    )
    session.add(pos)
    await session.flush()

    pm._cash_balance = 900

    # 5% 하락 → 95 (손실)
    await pm.update_position_on_sell(
        session, "SOL/USDT", 1.0, 95,
        1.0 * 95, 0.04,
    )
    # PnL: 100 * 3 * (-0.05) = -15
    # 반환: 100 + (-15) - 0.04 = 84.96
    assert pm.cash_balance == pytest.approx(900 + 84.96, abs=0.1)
    assert pm.realized_pnl == pytest.approx(-15 - 0.04, abs=0.1)


@pytest.mark.asyncio
async def test_spot_sell_unchanged_notional_based(session):
    """현물 매도는 기존 notional 기반 그대로 동작."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500000,
        exchange_name="bithumb",
        is_paper=True,
    )
    pos = Position(
        exchange="bithumb", symbol="BTC/KRW",
        quantity=0.001, average_buy_price=50_000_000,
        total_invested=50000, is_paper=True,
    )
    session.add(pos)
    await session.flush()

    pm._cash_balance = 450000

    # 10% 상승
    await pm.update_position_on_sell(
        session, "BTC/KRW", 0.001, 55_000_000,
        0.001 * 55_000_000, 137.5,
    )
    # 현물: proceeds = 55000 - 137.5 = 54862.5
    assert pm.cash_balance == pytest.approx(450000 + 54862.5, abs=1)


# ── 일일 매수 카운터 DB 복원 테스트 ───────────────────────

@pytest.mark.asyncio
async def test_daily_buy_count_restored_from_orders(session):
    """재시작 시 오늘 Order로부터 일일 매수 카운터가 복원되는지 확인."""
    from unittest.mock import MagicMock, patch
    from engine.trading_engine import TradingEngine

    # 오늘 buy 주문 3개 생성
    now = datetime.now(timezone.utc)
    for i, sym in enumerate(["BTC/KRW", "BTC/KRW", "ETH/KRW"]):
        order = Order(
            exchange="bithumb", symbol=sym, side="buy",
            order_type="market", status="filled",
            requested_price=50_000_000,
            executed_price=50_000_000,
            requested_quantity=0.001, executed_quantity=0.001,
            strategy_name="test",
            created_at=now - timedelta(minutes=i),
        )
        session.add(order)
    await session.flush()

    # 엔진 생성 (최소 mock)
    config = MagicMock()
    config.trading.mode = "paper"
    config.trading.evaluation_interval_sec = 300
    config.trading.tracked_coins = ["BTC/KRW"]
    config.trading.rotation_enabled = False
    config.trading.min_combined_confidence = 0.5
    config.trading.daily_buy_limit = 20
    config.trading.max_daily_coin_buys = 3
    config.trading.min_trade_interval_sec = 3600
    config.risk.max_trade_size_pct = 0.2

    engine = TradingEngine(
        config=config,
        exchange=MagicMock(),
        market_data=MagicMock(),
        order_manager=MagicMock(),
        portfolio_manager=MagicMock(),
        combiner=MagicMock(),
    )

    # session fixture는 이미 모든 테이블이 생성된 상태
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_session_ctx():
        yield session

    with patch("db.session.get_session_factory", return_value=mock_session_ctx):
        await engine._restore_trade_timestamps()

    assert engine._daily_buy_count == 3
    assert engine._daily_coin_buy_count.get("BTC/KRW") == 2
    assert engine._daily_coin_buy_count.get("ETH/KRW") == 1


# ── _is_futures 캐싱 테스트 (COIN-10) ──────────────────────────────────────


class TestIsFuturesCaching:
    """COIN-10: _is_futures 플래그가 __init__에서 캐싱되어야 함."""

    def test_is_futures_true_for_binance_futures(self):
        """binance_futures exchange_name → _is_futures=True."""
        pm = PortfolioManager(
            market_data=_make_market_data({}),
            initial_balance_krw=1000.0,
            exchange_name="binance_futures",
        )
        assert pm._is_futures is True

    def test_is_futures_false_for_bithumb(self):
        """bithumb exchange_name → _is_futures=False."""
        pm = PortfolioManager(
            market_data=_make_market_data({}),
            initial_balance_krw=500_000,
            exchange_name="bithumb",
        )
        assert pm._is_futures is False

    def test_is_futures_false_for_binance_spot(self):
        """binance_spot exchange_name → _is_futures=False."""
        pm = PortfolioManager(
            market_data=_make_market_data({}),
            initial_balance_krw=1000.0,
            exchange_name="binance_spot",
        )
        assert pm._is_futures is False

    def test_is_futures_default_bithumb(self):
        """기본 exchange_name='bithumb' → _is_futures=False."""
        pm = PortfolioManager(
            market_data=_make_market_data({}),
            initial_balance_krw=500_000,
        )
        assert pm._is_futures is False

    @pytest.mark.asyncio
    async def test_reconcile_skips_for_futures(self, session):
        """선물 PM은 reconcile_cash_from_db를 즉시 건너뜀 (_is_futures=True)."""
        pm = PortfolioManager(
            market_data=_make_market_data({}),
            initial_balance_krw=1000.0,
            exchange_name="binance_futures",
        )
        original_cash = pm.cash_balance
        # 선물은 reconcile을 건너뜀 → cash 변화 없음
        await pm.reconcile_cash_from_db(session)
        assert pm.cash_balance == original_cash


# ── COIN-18: 선물 position_sync 청산 시 cash 반환 테스트 ─────────────


@pytest.mark.asyncio
async def test_futures_sync_returns_cash_on_tp_clearance(session):
    """선물 포지션이 TP로 청산되면 margin + PnL이 cash에 반환되어야 한다."""
    from exchange.base import Balance

    initial_cash = 1000.0
    pm = PortfolioManager(
        market_data=_make_market_data({"ADA/USDT": 0.55}),
        initial_balance_krw=initial_cash,
        is_paper=False,
        exchange_name="binance_futures",
    )
    # cash를 포지션 진입 후 상태로 설정 (마진 차감)
    invested = 8.65
    pm.cash_balance = initial_cash - invested  # 991.35

    # DB에 ADA/USDT long 포지션 (entry 0.50, 현재가 0.55, lev 3)
    # pnl_pct = (0.55-0.50)/0.50 * 3 * 100 = +30%
    session.add(Position(
        exchange="binance_futures", symbol="ADA/USDT",
        quantity=51.9, average_buy_price=0.50,
        total_invested=invested, is_paper=False,
        direction="long", leverage=3, margin_used=invested,
        stop_loss_pct=5.0, take_profit_pct=10.0,
    ))
    await session.flush()

    # 거래소: 포지션 없음 (TP로 청산됨)
    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=1000, used=0, total=1000),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    cash_before = pm.cash_balance
    await pm.sync_exchange_positions(session, adapter, ["ADA/USDT"])
    await session.flush()

    # pnl_amount = 8.65 * 30 / 100 = 2.595
    # cash_returned = 8.65 + 2.595 = 11.245
    expected_pnl = invested * 30.0 / 100
    expected_cash_returned = invested + expected_pnl
    assert pm.cash_balance == pytest.approx(cash_before + expected_cash_returned, abs=0.01)
    # realized_pnl도 업데이트됨
    assert pm._realized_pnl == pytest.approx(expected_pnl, abs=0.01)


@pytest.mark.asyncio
async def test_futures_sync_returns_cash_on_sl_clearance(session):
    """선물 포지션이 SL로 청산되면 margin - 손실이 cash에 반환되어야 한다."""
    from exchange.base import Balance

    initial_cash = 500.0
    pm = PortfolioManager(
        market_data=_make_market_data({"AVAX/USDT": 19.0}),
        initial_balance_krw=initial_cash,
        is_paper=False,
        exchange_name="binance_futures",
    )
    invested = 6.9
    pm.cash_balance = initial_cash - invested  # 493.1

    # DB에 AVAX/USDT long 포지션 (entry 20, 현재가 19, lev 3)
    # pnl_pct = (19-20)/20 * 3 * 100 = -15%
    session.add(Position(
        exchange="binance_futures", symbol="AVAX/USDT",
        quantity=1.035, average_buy_price=20.0,
        total_invested=invested, is_paper=False,
        direction="long", leverage=3, margin_used=invested,
        stop_loss_pct=5.0, take_profit_pct=10.0,
    ))
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=499, used=0, total=499),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    cash_before = pm.cash_balance
    await pm.sync_exchange_positions(session, adapter, ["AVAX/USDT"])
    await session.flush()

    # pnl_amount = 6.9 * (-15) / 100 = -1.035
    # cash_returned = 6.9 + (-1.035) = 5.865 (margin minus loss)
    expected_pnl = invested * (-15.0) / 100
    expected_cash_returned = invested + expected_pnl
    assert expected_cash_returned > 0  # SL loss is less than margin
    assert pm.cash_balance == pytest.approx(cash_before + expected_cash_returned, abs=0.01)
    assert pm._realized_pnl == pytest.approx(expected_pnl, abs=0.01)


@pytest.mark.asyncio
async def test_futures_sync_cash_zero_on_liquidation(session):
    """강제청산(PnL > -100%)이면 cash 반환 0 (max(invested+pnl, 0))."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"ETH/USDT": 1300.0}),
        initial_balance_krw=500,
        is_paper=False,
        exchange_name="binance_futures",
    )

    invested = 66.7
    pm.cash_balance = 500 - invested  # 433.3

    # entry 2000, 현재가 1300, lev 3 → pnl_pct = -105%
    session.add(Position(
        exchange="binance_futures", symbol="ETH/USDT",
        quantity=0.1, average_buy_price=2000.0,
        total_invested=invested, is_paper=False,
        direction="long", leverage=3, margin_used=invested,
    ))
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=200, used=0, total=200),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    cash_before = pm.cash_balance
    await pm.sync_exchange_positions(session, adapter, ["ETH/USDT"])
    await session.flush()

    # pnl_pct = -105%, pnl_amount = 66.7 * -105/100 = -70.035
    # cash_returned = max(66.7 + (-70.035), 0) = max(-3.335, 0) = 0
    assert pm.cash_balance == pytest.approx(cash_before, abs=0.01)
    # realized_pnl still updated with the loss
    pnl_amount = invested * (-105.0) / 100
    assert pm._realized_pnl == pytest.approx(pnl_amount, abs=0.5)


@pytest.mark.asyncio
async def test_futures_sync_returns_cash_multiple_positions(session):
    """여러 선물 포지션이 동시에 청산되면 각각의 margin+PnL이 반환되어야 한다."""
    from exchange.base import Balance

    initial_cash = 500.0
    pm = PortfolioManager(
        market_data=_make_market_data({
            "ADA/USDT": 0.55,   # pnl +30% (long, entry 0.50, lev 3)
            "FIL/USDT": 5.5,    # pnl +10% (long, entry 5.33, lev 3)
        }),
        initial_balance_krw=initial_cash,
        is_paper=False,
        exchange_name="binance_futures",
    )

    invested_ada = 8.65
    invested_fil = 8.62
    total_invested = invested_ada + invested_fil
    pm.cash_balance = initial_cash - total_invested  # 482.73

    session.add(Position(
        exchange="binance_futures", symbol="ADA/USDT",
        quantity=51.9, average_buy_price=0.50,
        total_invested=invested_ada, is_paper=False,
        direction="long", leverage=3, margin_used=invested_ada,
        stop_loss_pct=5.0, take_profit_pct=10.0,
    ))
    session.add(Position(
        exchange="binance_futures", symbol="FIL/USDT",
        quantity=4.85, average_buy_price=5.33,
        total_invested=invested_fil, is_paper=False,
        direction="long", leverage=3, margin_used=invested_fil,
        stop_loss_pct=5.0, take_profit_pct=10.0,
    ))
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=500, used=0, total=500),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    cash_before = pm.cash_balance
    await pm.sync_exchange_positions(session, adapter, ["ADA/USDT", "FIL/USDT"])
    await session.flush()

    # ADA: pnl_pct = (0.55-0.50)/0.50*3*100 = +30%, pnl_amount = 8.65*30/100 = 2.595
    # FIL: pnl_pct = (5.5-5.33)/5.33*3*100 ≈ +9.57%, pnl_amount = 8.62*9.57/100 ≈ 0.825
    pnl_ada = invested_ada * ((0.55 - 0.50) / 0.50 * 3 * 100) / 100
    pnl_fil = invested_fil * ((5.5 - 5.33) / 5.33 * 3 * 100) / 100
    total_cash_returned = (invested_ada + pnl_ada) + (invested_fil + pnl_fil)

    assert pm.cash_balance == pytest.approx(cash_before + total_cash_returned, abs=0.1)
    assert pm._realized_pnl == pytest.approx(pnl_ada + pnl_fil, abs=0.1)
    assert len(pm._cleared_positions) == 2


@pytest.mark.asyncio
async def test_futures_sync_short_position_cash_return(session):
    """숏 포지션이 청산되면 올바른 PnL 방향으로 cash 반환."""
    from exchange.base import Balance

    initial_cash = 1000.0
    pm = PortfolioManager(
        market_data=_make_market_data({"BTC/USDT": 48000.0}),
        initial_balance_krw=initial_cash,
        is_paper=False,
        exchange_name="binance_futures",
    )
    invested = 50.0
    pm.cash_balance = initial_cash - invested  # 950

    # short entry 50000, 현재가 48000 → 수익
    # pnl_pct = (50000-48000)/50000 * 3 * 100 = +12%
    session.add(Position(
        exchange="binance_futures", symbol="BTC/USDT",
        quantity=0.003, average_buy_price=50000.0,
        total_invested=invested, is_paper=False,
        direction="short", leverage=3, margin_used=invested,
        stop_loss_pct=5.0, take_profit_pct=10.0,
    ))
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=1000, used=0, total=1000),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    cash_before = pm.cash_balance
    await pm.sync_exchange_positions(session, adapter, ["BTC/USDT"])
    await session.flush()

    # pnl_pct = (50000-48000)/50000 * 3 * 100 = +12%
    # pnl_amount = 50 * 12 / 100 = 6.0
    # cash_returned = 50 + 6 = 56
    expected_pnl = invested * 12.0 / 100
    expected_cash_returned = invested + expected_pnl
    assert pm.cash_balance == pytest.approx(cash_before + expected_cash_returned, abs=0.01)
    assert pm._realized_pnl == pytest.approx(expected_pnl, abs=0.01)


@pytest.mark.asyncio
async def test_spot_sync_no_cash_return_on_clearance(session):
    """현물 position_sync 청산은 기존 방식(actual_cash 덮어쓰기)으로 처리."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"MOCA/KRW": 23.1}),
        initial_balance_krw=500_000,
        is_paper=False,
        exchange_name="bithumb",
    )

    session.add(Position(
        exchange="bithumb", symbol="MOCA/KRW",
        quantity=43.56, average_buy_price=23.1,
        total_invested=1007, is_paper=False,
    ))
    await session.flush()

    # 거래소에는 KRW만 있고 MOCA 없음
    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "KRW": Balance(currency="KRW", free=315_000, used=0, total=315_000),
    })

    await pm.sync_exchange_positions(session, adapter, ["BTC/KRW"])
    await session.flush()

    # 현물: actual_cash로 덮어씀 (315,000)
    assert pm.cash_balance == 315_000


@pytest.mark.asyncio
async def test_futures_sync_skip_already_closed_position(session):
    """레이스 컨디션: 엔진이 이미 청산한 포지션을 sync가 이중 청산하지 않아야 한다.

    시나리오:
    1. sync_exchange_positions()가 DB 포지션 스냅샷을 읽음 (qty=3.0)
    2. 서지 엔진이 포지션을 청산 (qty=0) + cash 반환
    3. sync가 거래소에서 포지션이 없음을 확인
    4. sync가 DB를 다시 읽으면 qty=0 → 이중 청산 스킵
    """
    from exchange.base import Balance

    initial_cash = 1000.0
    invested = 10.0
    pm = PortfolioManager(
        market_data=_make_market_data({"AVAX/USDT": 10.2}),
        initial_balance_krw=initial_cash,
        is_paper=False,
        exchange_name="binance_futures",
    )
    # 서지 엔진이 이미 포지션을 닫고 cash를 반환한 상태
    pm.cash_balance = initial_cash - invested + invested  # 마진 차감 후 반환 완료

    # DB에 포지션이 있지만 qty=0 (서지 엔진이 이미 청산)
    session.add(Position(
        exchange="binance_futures", symbol="AVAX/USDT",
        quantity=0, average_buy_price=10.194,
        total_invested=invested, is_paper=False,
        direction="short", leverage=3, margin_used=invested,
    ))
    await session.flush()

    # 거래소: 포지션 없음
    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=1000, used=0, total=1000),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    cash_before = pm.cash_balance
    await pm.sync_exchange_positions(session, adapter, ["AVAX/USDT"])
    await session.flush()

    # cash가 변하지 않아야 함 — qty=0이므로 이미 청산된 포지션
    assert pm.cash_balance == pytest.approx(cash_before, abs=0.01)


@pytest.mark.asyncio
async def test_futures_sync_race_condition_no_double_cash(session):
    """sync 도중 엔진이 포지션을 닫으면 이중 cash 반환이 발생하지 않아야 한다.

    sync가 DB 스냅샷(qty>0) 읽은 후, 실제 DB 재확인 시 qty=0이면 스킵.
    """
    from exchange.base import Balance

    initial_cash = 240.0
    invested = 10.23
    pm = PortfolioManager(
        market_data=_make_market_data({"AVAX/USDT": 10.215}),
        initial_balance_krw=300.0,
        is_paper=False,
        exchange_name="binance_futures",
    )
    pm.cash_balance = initial_cash  # 엔진이 이미 포지션 청산 후 cash 반환

    # DB 포지션: qty>0로 시작 (sync 스냅샷 시점)
    pos = Position(
        exchange="binance_futures", symbol="AVAX/USDT",
        quantity=3.0, average_buy_price=10.194,
        total_invested=invested, is_paper=False,
        direction="short", leverage=3, margin_used=invested,
    )
    session.add(pos)
    await session.flush()

    # 서지 엔진이 sync 실행 중에 포지션을 닫는 것을 시뮬레이션:
    # fetch_balance 호출 후 (sync 시작), DB의 포지션 qty를 0으로 변경
    original_fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=240, used=0, total=240),
    })

    async def fetch_balance_and_close_position():
        """fetch_balance 호출 시 엔진이 포지션을 닫는 것을 시뮬레이션."""
        result = await original_fetch_balance()
        # 엔진이 포지션을 닫음
        pos.quantity = 0
        await session.flush()
        return result

    adapter = AsyncMock()
    adapter.fetch_balance = fetch_balance_and_close_position
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    cash_before = pm.cash_balance
    await pm.sync_exchange_positions(session, adapter, ["AVAX/USDT"])
    await session.flush()

    # cash가 변하지 않아야 함 — 엔진이 이미 처리
    assert pm.cash_balance == pytest.approx(cash_before, abs=0.01)


@pytest.mark.asyncio
async def test_sync_does_not_revive_closed_position_from_fetch_positions(session):
    """닫힌 포지션(qty=0)이 fetch_positions 데이터로 부활하지 않아야 함.

    시나리오: SurgeEngine이 생성한 포지션이 거래소에 존재하지만,
    FuturesEngine PM의 DB에는 같은 심볼의 닫힌 레코드(qty=0)가 있을 때,
    sync가 이 레코드를 거래소 값으로 업데이트하면 안 됨.
    """
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"JUP/USDT": 0.14}),
        initial_balance_krw=250.0,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # DB에 닫힌 포지션 (다른 엔진이 사용 중인 거래소 포지션의 잔재)
    closed_pos = Position(
        exchange="binance_futures", symbol="JUP/USDT",
        quantity=0, average_buy_price=0.14,
        total_invested=0, is_paper=False,
        direction="short", leverage=3, margin_used=0,
    )
    session.add(closed_pos)
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=250, used=0, total=250),
    })
    # 거래소에 JUP 포지션 존재 (SurgeEngine이 생성)
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[{
        "symbol": "JUP/USDT:USDT",
        "contracts": 347.0,
        "initialMargin": 16.6,
        "entryPrice": 0.1431,
        "liquidationPrice": 0.19,
        "side": "short",
        "leverage": "3",
        "notional": -49.8,
    }])
    adapter.fetch_income = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, ["JUP/USDT"])
    await session.flush()

    # qty=0인 닫힌 포지션이 부활하지 않아야 함
    await session.refresh(closed_pos)
    assert closed_pos.quantity == 0, "Closed position must not be revived by sync"


@pytest.mark.asyncio
async def test_sync_skips_duplicate_order_when_recent_engine_order_exists(session):
    """엔진이 최근 청산한 심볼에 대해 sync가 중복 Order를 생성하지 않아야 함.

    시나리오: vol_breakout이 BTC 숏을 SL 청산 → 1분 후 sync 실행 →
    거래소에 BTC 포지션 없음 → sync가 "사라진 포지션" 감지하지만,
    최근 5분 이내 Order가 있으므로 스킵.
    """
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"BTC/USDT": 66680.0}),
        initial_balance_krw=3000.0,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # DB에 활성 포지션 (아직 qty > 0 — 엔진이 DB 업데이트 전)
    pos = Position(
        exchange="binance_futures", symbol="BTC/USDT",
        quantity=0.019, average_buy_price=66610.0,
        total_invested=421.8, is_paper=False,
        direction="short", leverage=3, margin_used=421.8,
    )
    session.add(pos)

    # 최근 엔진이 생성한 청산 Order (1분 전)
    recent_order = Order(
        exchange="binance_futures", symbol="BTC/USDT",
        side="buy", order_type="market", status="filled",
        requested_price=66682.0, executed_price=66682.0,
        requested_quantity=0.019, executed_quantity=0.019,
        fee=0.507, fee_currency="USDT", is_paper=False,
        direction="short", leverage=3, margin_used=0,
        strategy_name="vol_breakout",
        realized_pnl=-1.93, realized_pnl_pct=-0.3,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    session.add(recent_order)
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=3000, used=0, total=3000),
    })
    # 거래소에 BTC 포지션 없음 (이미 청산됨)
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, ["BTC/USDT"])
    await session.flush()

    # sync가 중복 Order를 생성하지 않아야 함
    result = await session.execute(
        select(Order).where(
            Order.exchange == "binance_futures",
            Order.symbol == "BTC/USDT",
        )
    )
    orders = result.scalars().all()
    assert len(orders) == 1, f"Expected 1 order (engine only), got {len(orders)}"
    assert orders[0].strategy_name == "vol_breakout"


@pytest.mark.asyncio
async def test_sync_creates_order_when_no_recent_engine_order(session):
    """최근 5분 이내 엔진 Order가 없으면 sync가 정상적으로 Order를 생성해야 함.

    시나리오: 다운타임 중 수동 청산 — 엔진 Order 없음 → sync가 기록.
    """
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"ETH/USDT": 2010.0}),
        initial_balance_krw=3000.0,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # DB에 활성 포지션
    pos = Position(
        exchange="binance_futures", symbol="ETH/USDT",
        quantity=0.5, average_buy_price=2000.0,
        total_invested=333.3, is_paper=False,
        direction="long", leverage=3, margin_used=333.3,
    )
    session.add(pos)
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=3000, used=0, total=3000),
    })
    # 거래소에 ETH 포지션 없음 (다운타임 중 수동 청산)
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, ["ETH/USDT"])
    await session.flush()

    # 최근 엔진 Order 없으므로 sync가 Order를 생성해야 함
    result = await session.execute(
        select(Order).where(
            Order.exchange == "binance_futures",
            Order.symbol == "ETH/USDT",
        )
    )
    orders = result.scalars().all()
    assert len(orders) == 1, f"Expected 1 sync order, got {len(orders)}"
    assert orders[0].strategy_name == "position_sync"


@pytest.mark.asyncio
async def test_sync_does_not_create_position_for_active_surge(session):
    """서지 엔진이 보유 중인 포지션을 선물 PM sync가 신규 생성하지 않아야 함.

    시나리오: SurgeEngine이 SUI 숏 보유 중 → 선물 PM sync가 거래소에서
    SUI 포지션 감지 → binance_surge에 활성 포지션이 있으므로 스킵.
    """
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"SUI/USDT": 0.86}),
        initial_balance_krw=3000.0,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # 서지 엔진의 활성 포지션
    surge_pos = Position(
        exchange="binance_surge", symbol="SUI/USDT",
        quantity=616.6, average_buy_price=0.85,
        total_invested=175.7, is_paper=False,
        direction="short", leverage=3, margin_used=175.7,
    )
    session.add(surge_pos)
    await session.flush()

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=3000, used=0, total=3000),
    })
    # 거래소에 SUI 포지션 존재 (서지 엔진 소유)
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[{
        "symbol": "SUI/USDT:USDT",
        "contracts": 616.6,
        "initialMargin": 175.7,
        "entryPrice": 0.85,
        "liquidationPrice": 1.13,
        "side": "short",
        "leverage": "3",
        "notional": -527.0,
    }])
    adapter.fetch_income = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, ["SUI/USDT"])
    await session.flush()

    # binance_futures로 신규 포지션이 생성되지 않아야 함
    result = await session.execute(
        select(Position).where(
            Position.exchange == "binance_futures",
            Position.symbol == "SUI/USDT",
        )
    )
    futures_positions = result.scalars().all()
    assert len(futures_positions) == 0, (
        f"Surge position must not be duplicated as futures position, "
        f"got {len(futures_positions)}"
    )


@pytest.mark.asyncio
async def test_futures_sync_no_cash_return_when_fresh_qty_zero_at_cash_return(session):
    """TOCTOU 방지: await 이후 DB qty=0이면 cash 반환 스킵.

    시나리오:
    1. sync가 DB 스냅샷(qty>0) 읽고 초기 fresh_qty 체크 통과 (qty>0)
    2. get_current_price / _determine_close_reason await 중 엔진이 포지션 청산 (DB qty=0, cash 반환)
    3. sync가 cash 반환 직전 fresh_qty 재확인 → qty=0 감지 → cash 반환 스킵
    4. 이중 cash 반환 없음
    """
    from exchange.base import Balance

    initial_cash = 300.0
    invested = 20.0
    pm = PortfolioManager(
        market_data=_make_market_data({"LINK/USDT": 14.5}),
        initial_balance_krw=500.0,
        is_paper=False,
        exchange_name="binance_futures",
    )
    # 엔진이 포지션을 닫고 이미 cash를 반환한 상태
    pm.cash_balance = initial_cash

    # DB에 활성 포지션 (qty>0 — 초기 fresh_qty 체크 통과)
    pos = Position(
        exchange="binance_futures", symbol="LINK/USDT",
        quantity=5.0, average_buy_price=14.2,
        total_invested=invested, is_paper=False,
        direction="long", leverage=3, margin_used=invested,
    )
    session.add(pos)
    await session.flush()

    # get_current_price 호출 시 엔진이 포지션을 닫는 것을 시뮬레이션
    original_price = 14.5

    async def get_price_and_close_position(sym):
        """가격 조회 중 엔진이 포지션을 닫음 (TOCTOU 시뮬레이션)."""
        # 엔진이 포지션 청산 — DB qty=0으로 변경
        pos.quantity = 0
        await session.flush()
        return original_price

    market_data = AsyncMock()
    market_data.get_current_price = get_price_and_close_position
    pm._market_data = market_data

    # _determine_close_reason을 모킹하여 실제 TOCTOU 재확인 경로를 통과하도록 보장
    # (모킹하지 않으면 qty=0 상태에서 실제 메서드가 일찍 종료해 재확인이 실행 안 될 수 있음)
    async def mock_close_reason(sym, db_pos, current_price, pnl_pct, adapter):
        return "rsi", "tp_hit"

    pm._determine_close_reason = mock_close_reason

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=300, used=0, total=300),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    cash_before = pm.cash_balance
    await pm.sync_exchange_positions(session, adapter, ["LINK/USDT"])
    await session.flush()

    # cash가 변하지 않아야 함 — 재확인에서 qty=0 감지, 이중 반환 방지
    assert pm.cash_balance == pytest.approx(cash_before, abs=0.01), (
        f"Expected no cash change (double return prevented), "
        f"but cash changed from {cash_before} to {pm.cash_balance}"
    )


@pytest.mark.asyncio
async def test_futures_sync_no_cash_return_when_engine_order_created_during_await(session):
    """TOCTOU 방지: await 기간 중 엔진 Order가 생성됐으면 cash 반환 스킵.

    시나리오:
    1. sync가 초기 fresh_qty + recent Order 체크 통과
    2. _determine_close_reason await 중 엔진이 현재 시각으로 청산 Order 생성
       (= _processing_start 이후 생성)
    3. sync가 TOCTOU 재확인 → 엔진 Order 감지(_processing_start 이후) → 전체 스킵
    4. 이중 cash 반환 없음
    """
    from exchange.base import Balance

    initial_cash = 500.0
    invested = 15.0
    pm = PortfolioManager(
        market_data=_make_market_data({"DOT/USDT": 7.8}),
        initial_balance_krw=700.0,
        is_paper=False,
        exchange_name="binance_futures",
    )
    pm.cash_balance = initial_cash

    # DB에 활성 포지션 (qty>0)
    pos = Position(
        exchange="binance_futures", symbol="DOT/USDT",
        quantity=4.0, average_buy_price=7.5,
        total_invested=invested, is_paper=False,
        direction="long", leverage=3, margin_used=invested,
    )
    session.add(pos)
    await session.flush()

    # _determine_close_reason 호출 시 엔진이 현재 시각으로 Order를 생성
    # (processing_start 이후 생성 → TOCTOU 재확인에 걸림)
    engine_order_created = False
    captured_engine_order = None

    async def determine_close_reason_and_create_order(sym, db_pos, current_price, pnl_pct, adapter):
        """청산 사유 판별 중 엔진이 Order를 생성함 (TOCTOU 시뮬레이션).
        created_at을 명시적으로 지정해 DB 기본값 타이밍 의존성 제거.
        """
        nonlocal engine_order_created, captured_engine_order
        if not engine_order_created:
            engine_order_created = True
            # 엔진이 현재 시각으로 Order 생성 (processing_start 이후임이 보장됨)
            engine_order = Order(
                exchange="binance_futures", symbol="DOT/USDT",
                side="sell", order_type="market", status="filled",
                requested_price=7.8, executed_price=7.8,
                requested_quantity=4.0, executed_quantity=4.0,
                fee=0.0, fee_currency="USDT", is_paper=False,
                direction="long", leverage=3, margin_used=0,
                strategy_name="rsi",
                realized_pnl=0.36, realized_pnl_pct=2.4,
                created_at=datetime.now(timezone.utc),  # 명시적 Python-side 타임스탬프
            )
            session.add(engine_order)
            await session.flush()
            captured_engine_order = engine_order
        return "rsi", "tp_hit"

    pm._determine_close_reason = determine_close_reason_and_create_order

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=500, used=0, total=500),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    from datetime import datetime, timezone
    processing_start_approx = datetime.now(timezone.utc)
    cash_before = pm.cash_balance
    await pm.sync_exchange_positions(session, adapter, ["DOT/USDT"])
    await session.flush()

    # 엔진 Order가 실제로 생성됐는지 + guard 전제 조건 검증
    assert captured_engine_order is not None, "Engine order was not created during simulate"
    assert captured_engine_order.created_at >= processing_start_approx, (
        f"engine_order.created_at ({captured_engine_order.created_at}) must be >= "
        f"processing_start_approx ({processing_start_approx}) — "
        "guard depends on order timestamp falling inside the recheck window"
    )

    # cash가 변하지 않아야 함 — 재확인에서 엔진 Order 감지, 이중 반환 방지
    assert pm.cash_balance == pytest.approx(cash_before, abs=0.01), (
        f"Expected no cash change (double return prevented), "
        f"but cash changed from {cash_before} to {pm.cash_balance}"
    )


@pytest.mark.asyncio
async def test_futures_sync_cash_returns_normally_when_toctou_checks_pass(session):
    """TOCTOU 재확인 통과 시 정상적으로 cash 반환 (정상 플로우 회귀 테스트).

    시나리오: await 중 엔진 개입 없음 → 두 재확인 모두 통과 → cash 정상 반환.
    """
    from exchange.base import Balance

    initial_cash = 200.0
    invested = 12.0
    entry_price = 8.0
    current_price = 8.4  # 5% 상승, 3x → 15% PnL

    pm = PortfolioManager(
        market_data=_make_market_data({"OP/USDT": current_price}),
        initial_balance_krw=400.0,
        is_paper=False,
        exchange_name="binance_futures",
    )
    pm.cash_balance = initial_cash

    # DB에 활성 포지션 (qty>0)
    pos = Position(
        exchange="binance_futures", symbol="OP/USDT",
        quantity=4.5, average_buy_price=entry_price,
        total_invested=invested, is_paper=False,
        direction="long", leverage=3, margin_used=invested,
    )
    session.add(pos)
    await session.flush()

    # cash 반환 경로가 실행되도록 _determine_close_reason 모킹
    # (실제 메서드가 (None, None)을 반환해 일찍 종료하면 cash 반환 경로를 검증 못함)
    async def mock_close_reason(sym, db_pos, current_price, pnl_pct, adapter):
        return "rsi", "tp_hit"

    pm._determine_close_reason = mock_close_reason

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=200, used=0, total=200),
    })
    # 거래소에 OP 포지션 없음 (수동 청산)
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    cash_before = pm.cash_balance
    await pm.sync_exchange_positions(session, adapter, ["OP/USDT"])
    await session.flush()

    # 최소한 invested 이상은 반환됐어야 함 (PnL 양수이므로 invested 초과)
    # 구체적 PnL 공식은 구현 내부에 위임 — 이중 반환 방지 이후 cash 증가 여부만 검증
    assert pm.cash_balance > cash_before + invested, (
        f"Expected cash_balance > {cash_before + invested:.2f} (cash_before + invested), "
        f"got {pm.cash_balance:.2f}"
    )
    # 반환된 cash는 invested의 2배를 넘으면 안 됨 (합리적 상한)
    assert pm.cash_balance < cash_before + invested * 2, (
        f"Cash returned seems unexpectedly large: {pm.cash_balance - cash_before:.2f}"
    )


@pytest.mark.asyncio
async def test_sync_balances_loop_skips_surge_symbol(session):
    """balances 루프: 서지 포지션이 활성일 때 신규 포지션 생성 차단.

    시나리오: SurgeEngine이 ARB를 숏 보유 중 → fetch_balance에 ARB 잔고 반영 →
    balances 루프가 신규 포지션 생성하려 할 때 binance_surge에 활성 포지션이
    있으므로 스킵.
    """
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"ARB/USDT": 0.65}),
        initial_balance_krw=3000.0,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # 서지 엔진의 활성 포지션 (ARB 숏)
    surge_pos = Position(
        exchange="binance_surge", symbol="ARB/USDT",
        quantity=100.0, average_buy_price=0.65,
        total_invested=32.5, is_paper=False,
        direction="short", leverage=3, margin_used=32.5,
    )
    session.add(surge_pos)
    await session.flush()

    # fetch_balance에 ARB 잔고 반영됨 (서지 엔진이 보유)
    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=3000, used=0, total=3000),
        "ARB": Balance(currency="ARB", free=100, used=0, total=100),
    })
    # fetch_positions는 빈 상태 (ARB는 마진 데이터가 없다고 가정)
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, ["ARB/USDT"])
    await session.flush()

    # balances 루프에서 신규 ARB 포지션이 생성되지 않아야 함
    result = await session.execute(
        select(Position).where(
            Position.exchange == "binance_futures",
            Position.symbol == "ARB/USDT",
        )
    )
    futures_positions = result.scalars().all()
    assert len(futures_positions) == 0, (
        f"Balances loop must skip ARB when surge position exists, "
        f"but got {len(futures_positions)} futures position(s)"
    )


@pytest.mark.asyncio
async def test_sync_balances_loop_creates_position_when_no_surge(session):
    """balances 루프: 서지 포지션이 없을 때 신규 포지션 생성.

    시나리오: SurgeEngine이 DOGE를 보유하지 않음 → fetch_balance에 DOGE 잔고 반영 →
    balances 루프가 신규 포지션 생성 (정상).
    """
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"DOGE/USDT": 0.15}),
        initial_balance_krw=3000.0,
        is_paper=False,
        exchange_name="binance_futures",
    )

    # 서지 엔진에는 DOGE 포지션 없음

    # fetch_balance에 DOGE 잔고 반영됨 (어디선가 보유)
    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=3000, used=0, total=3000),
        "DOGE": Balance(currency="DOGE", free=200, used=0, total=200),
    })
    # fetch_positions는 빈 상태
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])
    adapter.fetch_income = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, ["DOGE/USDT"])
    await session.flush()

    # balances 루프에서 신규 DOGE 포지션이 생성되어야 함
    result = await session.execute(
        select(Position).where(
            Position.exchange == "binance_futures",
            Position.symbol == "DOGE/USDT",
        )
    )
    futures_positions = result.scalars().all()
    assert len(futures_positions) == 1, (
        f"Balances loop must create position when no surge exists, "
        f"expected 1, got {len(futures_positions)}"
    )
    pos = futures_positions[0]
    assert pos.quantity == pytest.approx(200, abs=0.01)
    assert pos.average_buy_price == pytest.approx(0.15, abs=0.001)
