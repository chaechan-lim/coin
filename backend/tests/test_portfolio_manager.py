"""
Tests for PortfolioManager (engine/portfolio_manager.py).
"""
from datetime import datetime, timezone
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
        highest_price=52_000_000,
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
        highest_price=pos.highest_price or pos.average_buy_price,
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
    assert tracker.highest_price == pytest.approx(4_500_000)


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
    pm._sync_guard = True

    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=999, used=0, total=999),
    })

    await pm.sync_exchange_positions(session, adapter, [])
    # fetch_balance가 호출되지 않아야 함 (guard에서 return)
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
    pm._sync_guard = False

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
