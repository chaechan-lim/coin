"""
Tests for PortfolioManager (engine/portfolio_manager.py).
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock
import pytest
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
async def test_futures_sync_cash_excludes_unrealized_pnl(session):
    """선물 sync에서 cash_balance는 walletBalance - margin (unrealizedPnL 제외)."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({"BTC/USDT": 100_000}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )

    # Mock exchange adapter
    adapter = AsyncMock()
    # Binance USDT: free=280 (wallet+unPnl-margin), used=30 (margin), total=320 (wallet+unPnl)
    # wallet=300, unPnl=20, margin=40
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=280, used=40, total=320),
    })
    # Mock futures positions: 1 position with 20 USDT unrealized PnL
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

    # DB에 이미 포지션 존재
    pos = Position(
        exchange="binance_futures", symbol="BTC/USDT",
        quantity=0.001, average_buy_price=95000,
        total_invested=40, is_paper=False,
        direction="long", leverage=3, margin_used=40,
    )
    session.add(pos)
    await session.flush()

    await pm.sync_exchange_positions(session, adapter, ["BTC/USDT"])

    # cash = wallet(300) - margin(40) = 260, NOT 280 (which includes unPnL)
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

    await pm.sync_exchange_positions(session, adapter, ["ETH/USDT"])

    # cash = wallet(300) - margin(50) = 250
    assert pm.cash_balance == pytest.approx(250, abs=1)

    summary = await pm.get_portfolio_summary(session)
    # total = cash(250) + position_value(margin+unPnL = 50+5 = 55) = 305
    # = wallet(300) + unPnL(5) = 305 (equity) ✓
    assert summary["total_value_krw"] == pytest.approx(305, abs=2)


@pytest.mark.asyncio
async def test_futures_sync_no_positions_cash_equals_wallet(session):
    """선물 포지션 없을 때 cash = wallet balance 전체."""
    from exchange.base import Balance

    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=300,
        exchange_name="binance_futures",
    )

    adapter = AsyncMock()
    # No positions: free=300, used=0, total=300 (wallet=300, unPnl=0)
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=300, used=0, total=300),
    })
    adapter._exchange = AsyncMock()
    adapter._exchange.fetch_positions = AsyncMock(return_value=[])

    await pm.sync_exchange_positions(session, adapter, [])

    # cash = wallet(300) - margin(0) = 300
    assert pm.cash_balance == pytest.approx(300, abs=1)


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
