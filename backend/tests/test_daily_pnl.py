"""
Tests for DailyPnL recording (PortfolioManager.record_daily_pnl).
"""
from datetime import datetime, timezone, timedelta, date
import pytest
from sqlalchemy import select

from core.models import PortfolioSnapshot, Order, Position, DailyPnL, CapitalTransaction
from engine.portfolio_manager import PortfolioManager
from api.portfolio import _compute_daily_pnl_from_orders, _related_order_exchanges


def _utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_record_daily_pnl_basic(session):
    """Basic daily PnL from snapshots."""
    target = date(2026, 3, 1)

    # Create snapshots for the day
    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=500_000,
        cash_balance_krw=500_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 1, 0, 5),
    ))
    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=505_000,
        cash_balance_krw=505_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 1, 23, 55),
    ))
    await session.flush()

    record = await PortfolioManager.record_daily_pnl(session, "bithumb", target)
    assert record is not None
    assert record.open_value == 500_000
    assert record.close_value == 505_000
    assert record.daily_pnl == 5_000
    assert round(record.daily_pnl_pct, 2) == 1.0
    assert record.trade_count == 0


@pytest.mark.asyncio
async def test_record_daily_pnl_with_trades(session):
    """Daily PnL with buy/sell orders."""
    target = date(2026, 3, 2)

    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=500_000,
        cash_balance_krw=500_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 2, 0, 5),
    ))
    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=510_000,
        cash_balance_krw=510_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 2, 23, 50),
    ))

    # Buy order
    session.add(Order(
        exchange="bithumb",
        symbol="BTC/KRW",
        side="buy",
        order_type="limit",
        status="filled",
        requested_price=50_000_000,
        executed_price=50_000_000,
        requested_quantity=0.001,
        executed_quantity=0.001,
        fee=150,
        is_paper=True,
        strategy_name="rsi",
        signal_confidence=0.7,
        created_at=_utc(2026, 3, 2, 10, 0),
    ))
    # Sell order
    session.add(Order(
        exchange="bithumb",
        symbol="BTC/KRW",
        side="sell",
        order_type="limit",
        status="filled",
        requested_price=51_000_000,
        executed_price=51_000_000,
        requested_quantity=0.001,
        executed_quantity=0.001,
        fee=150,
        is_paper=True,
        strategy_name="rsi",
        signal_confidence=0.7,
        created_at=_utc(2026, 3, 2, 15, 0),
    ))
    await session.flush()

    record = await PortfolioManager.record_daily_pnl(session, "bithumb", target)
    assert record is not None
    assert record.trade_count == 2
    assert record.buy_count == 1
    assert record.sell_count == 1
    assert record.daily_pnl == 10_000


@pytest.mark.asyncio
async def test_record_daily_pnl_no_snapshots(session):
    """Returns None when no snapshots exist for the day."""
    target = date(2026, 1, 1)
    record = await PortfolioManager.record_daily_pnl(session, "bithumb", target)
    assert record is None


@pytest.mark.asyncio
async def test_record_daily_pnl_upsert(session):
    """Re-running updates existing record instead of duplicating."""
    target = date(2026, 3, 3)

    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=500_000,
        cash_balance_krw=500_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 3, 1, 0),
    ))
    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=502_000,
        cash_balance_krw=502_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 3, 23, 0),
    ))
    await session.flush()

    # First run
    r1 = await PortfolioManager.record_daily_pnl(session, "bithumb", target)
    assert r1 is not None
    assert r1.daily_pnl == 2_000

    # Add another snapshot to change close value
    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=508_000,
        cash_balance_krw=508_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 3, 23, 55),
    ))
    await session.flush()

    # Second run — should update, not duplicate
    r2 = await PortfolioManager.record_daily_pnl(session, "bithumb", target)
    assert r2 is not None
    assert r2.daily_pnl == 8_000

    # Only one record
    result = await session.execute(
        select(DailyPnL).where(DailyPnL.exchange == "bithumb", DailyPnL.date == target)
    )
    assert len(list(result.scalars().all())) == 1


@pytest.mark.asyncio
async def test_record_daily_pnl_exchange_isolation(session):
    """Different exchanges produce separate records."""
    target = date(2026, 3, 4)

    for ex, val in [("bithumb", 500_000), ("binance_futures", 1000)]:
        session.add(PortfolioSnapshot(
            exchange=ex,
            total_value_krw=val,
            cash_balance_krw=val,
            invested_value_krw=0,
            snapshot_at=_utc(2026, 3, 4, 0, 5),
        ))
        session.add(PortfolioSnapshot(
            exchange=ex,
            total_value_krw=val + 100,
            cash_balance_krw=val + 100,
            invested_value_krw=0,
            snapshot_at=_utc(2026, 3, 4, 23, 55),
        ))
    await session.flush()

    r_bithumb = await PortfolioManager.record_daily_pnl(session, "bithumb", target)
    r_binance = await PortfolioManager.record_daily_pnl(session, "binance_futures", target)

    assert r_bithumb.daily_pnl == 100
    assert r_binance.daily_pnl == 100
    assert r_bithumb.open_value == 500_000
    assert r_binance.open_value == 1000


@pytest.mark.asyncio
async def test_record_daily_pnl_excludes_capital(session):
    """Deposits/withdrawals should not count as PnL."""
    target = date(2026, 3, 5)

    # open=500K, close=700K (200K 증가)
    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=500_000,
        cash_balance_krw=500_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 5, 0, 5),
    ))
    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=700_000,
        cash_balance_krw=700_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 5, 23, 55),
    ))

    # 150K 입금 → 순수 매매 수익은 200K - 150K = 50K
    session.add(CapitalTransaction(
        exchange="bithumb",
        tx_type="deposit",
        amount=150_000,
        currency="KRW",
        source="manual",
        confirmed=True,
        created_at=_utc(2026, 3, 5, 12, 0),
    ))
    await session.flush()

    record = await PortfolioManager.record_daily_pnl(session, "bithumb", target)
    assert record is not None
    assert record.daily_pnl == 50_000
    assert round(record.daily_pnl_pct, 2) == 10.0  # 50K / 500K * 100


@pytest.mark.asyncio
async def test_record_daily_pnl_withdrawal(session):
    """Withdrawal should not count as loss."""
    target = date(2026, 3, 6)

    # open=500K, close=300K (200K 감소)
    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=500_000,
        cash_balance_krw=500_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 6, 0, 5),
    ))
    session.add(PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=300_000,
        cash_balance_krw=300_000,
        invested_value_krw=0,
        snapshot_at=_utc(2026, 3, 6, 23, 55),
    ))

    # 200K 출금 → 순수 매매 손익은 -200K - (-200K) = 0
    session.add(CapitalTransaction(
        exchange="bithumb",
        tx_type="withdrawal",
        amount=200_000,
        currency="KRW",
        source="manual",
        confirmed=True,
        created_at=_utc(2026, 3, 6, 10, 0),
    ))
    await session.flush()

    record = await PortfolioManager.record_daily_pnl(session, "bithumb", target)
    assert record is not None
    assert record.daily_pnl == 0  # 출금 보정 → 손익 없음


# ── R&D 주문 기반 daily PnL 테스트 ──


def test_related_order_exchanges():
    """binance_futures/spot에 대해 올바른 R&D 엔진 목록 반환."""
    futures = _related_order_exchanges("binance_futures")
    assert "binance_donchian_futures" in futures
    assert "binance_breakout_pb" in futures
    assert "binance_surge" in futures

    spot = _related_order_exchanges("binance_spot")
    assert "binance_donchian" in spot
    assert "binance_fgdca" in spot

    assert _related_order_exchanges("bithumb") == ()


@pytest.mark.asyncio
async def test_compute_daily_pnl_from_orders_basic(session):
    """R&D 주문 데이터에서 일별 PnL 집계."""
    target_date = date(2026, 4, 10)

    # 매수 주문
    session.add(Order(
        exchange="binance_momentum", symbol="ETH/USDT", side="buy",
        order_type="market", status="filled",
        executed_price=3000.0, executed_quantity=0.1,
        fee=0.12, is_paper=False, strategy_name="momentum_rotation",
        created_at=_utc(2026, 4, 10, 8, 0),
    ))
    # 매도 주문 (수익)
    session.add(Order(
        exchange="binance_momentum", symbol="ETH/USDT", side="sell",
        order_type="market", status="filled",
        executed_price=3200.0, executed_quantity=0.1,
        fee=0.13, realized_pnl=19.75, is_paper=False,
        strategy_name="momentum_rotation",
        created_at=_utc(2026, 4, 10, 16, 0),
    ))
    # 매도 주문 (손실, 다른 엔진)
    session.add(Order(
        exchange="binance_hmm", symbol="BTC/USDT", side="sell",
        order_type="market", status="filled",
        executed_price=60000.0, executed_quantity=0.001,
        fee=0.024, realized_pnl=-5.0, is_paper=False,
        strategy_name="hmm_regime",
        created_at=_utc(2026, 4, 10, 20, 0),
    ))
    # 관계없는 엔진 주문 (포함되면 안 됨)
    session.add(Order(
        exchange="bithumb", symbol="BTC/KRW", side="sell",
        order_type="market", status="filled",
        executed_price=80000000, executed_quantity=0.001,
        fee=200, realized_pnl=5000, is_paper=False,
        strategy_name="rsi",
        created_at=_utc(2026, 4, 10, 12, 0),
    ))
    await session.flush()

    results = await _compute_daily_pnl_from_orders(
        session,
        ("binance_momentum", "binance_hmm"),
        date(2026, 4, 1),
    )

    assert len(results) == 1
    row = results[0]
    assert abs(row["realized_pnl"] - 14.75) < 0.01  # 19.75 + (-5.0)
    assert row["trade_count"] == 3
    assert row["buy_count"] == 1
    assert row["sell_count"] == 2
    assert row["win_count"] == 1
    assert row["loss_count"] == 1
    assert abs(row["total_fees"] - 0.274) < 0.01


@pytest.mark.asyncio
async def test_compute_daily_pnl_from_orders_multi_day(session):
    """여러 날에 걸친 주문 분리 집계."""
    for day, pnl in [(10, 20.0), (10, -5.0), (12, 15.0)]:
        session.add(Order(
            exchange="binance_donchian_futures", symbol="BTC/USDT", side="sell",
            order_type="market", status="filled",
            executed_price=60000, executed_quantity=0.001,
            fee=0.024, realized_pnl=pnl, is_paper=False,
            strategy_name="donchian_futures",
            created_at=_utc(2026, 4, day, 12, 0),
        ))
    await session.flush()

    results = await _compute_daily_pnl_from_orders(
        session,
        ("binance_donchian_futures",),
        date(2026, 4, 1),
    )

    assert len(results) == 2
    # 4/10: 20 + (-5) = 15
    assert abs(results[0]["realized_pnl"] - 15.0) < 0.01
    assert results[0]["win_count"] == 1
    assert results[0]["loss_count"] == 1
    # 4/12: 15
    assert abs(results[1]["realized_pnl"] - 15.0) < 0.01


@pytest.mark.asyncio
async def test_compute_daily_pnl_excludes_unfilled(session):
    """미체결 주문은 집계에서 제외."""
    session.add(Order(
        exchange="binance_pairs", symbol="ETH/USDT", side="sell",
        order_type="market", status="cancelled",
        executed_price=3000, executed_quantity=0,
        fee=0, realized_pnl=0, is_paper=False,
        strategy_name="pairs_trading",
        created_at=_utc(2026, 4, 10, 12, 0),
    ))
    await session.flush()

    results = await _compute_daily_pnl_from_orders(
        session, ("binance_pairs",), date(2026, 4, 1),
    )
    assert len(results) == 0
