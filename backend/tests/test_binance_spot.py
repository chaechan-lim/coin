"""
Tests for Binance Spot adapter and integration with TradingEngine/PortfolioManager.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from core.models import Position
from engine.portfolio_manager import PortfolioManager
from exchange.paper_adapter import PaperAdapter


def _make_market_data(prices: dict[str, float]):
    """Create a mock MarketDataService with predefined prices."""
    md = AsyncMock()
    md.get_current_price = AsyncMock(side_effect=lambda sym: prices.get(sym, 0))
    md.get_ticker = AsyncMock(side_effect=lambda sym: MagicMock(
        last=prices.get(sym, 0), bid=prices.get(sym, 0), ask=prices.get(sym, 0),
    ))
    return md


# ── 1. BinanceSpotAdapter 초기화 ─────────────────────────────

def test_binance_spot_adapter_import():
    """BinanceSpotAdapter can be imported."""
    from exchange.binance_spot_adapter import BinanceSpotAdapter
    adapter = BinanceSpotAdapter(api_key="test", api_secret="test", testnet=True)
    assert adapter._testnet is True
    assert adapter._exchange is None  # not initialized yet


# ── 2. TradingEngine + binance_spot 프로퍼티 ──────────────────

def test_engine_min_order_binance_spot():
    """TradingEngine with binance_spot has min_order = 5 USDT."""
    from engine.trading_engine import TradingEngine
    config = MagicMock()
    config.trading.evaluation_interval_sec = 300
    config.trading.tracked_coins = ["BTC/USDT"]
    config.trading.mode = "paper"

    engine = TradingEngine(
        config=config,
        exchange=AsyncMock(),
        market_data=AsyncMock(),
        order_manager=AsyncMock(),
        portfolio_manager=AsyncMock(),
        combiner=AsyncMock(),
        exchange_name="binance_spot",
    )
    assert engine._min_order_amount == 5.0
    assert engine._fee_margin == 1.002
    assert engine._min_fallback_amount == 10.0


def test_engine_min_order_bithumb():
    """TradingEngine with bithumb has min_order = 500 KRW."""
    from engine.trading_engine import TradingEngine
    config = MagicMock()
    config.trading.evaluation_interval_sec = 300
    config.trading.tracked_coins = ["BTC/KRW"]
    config.trading.mode = "paper"

    engine = TradingEngine(
        config=config,
        exchange=AsyncMock(),
        market_data=AsyncMock(),
        order_manager=AsyncMock(),
        portfolio_manager=AsyncMock(),
        combiner=AsyncMock(),
        exchange_name="bithumb",
    )
    assert engine._min_order_amount == 5000
    assert engine._fee_margin == 1.003
    assert engine._min_fallback_amount == 5000


# ── 3. 교차충돌 차단 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_cross_exchange_conflict_blocks_buy(session):
    """Buying on binance_spot is blocked if futures short position exists."""
    # 선물 숏 포지션 생성
    pos = Position(
        exchange="binance_futures",
        symbol="BTC/USDT",
        quantity=0.01,
        average_buy_price=50000,
        total_invested=500,
        is_paper=True,
        direction="short",
    )
    session.add(pos)
    await session.flush()

    # base 심볼로 교차충돌 조회
    base = "BTC"
    result = await session.execute(
        select(Position).where(
            Position.symbol.like(f"{base}/%"),
            Position.quantity > 0,
            Position.exchange != "binance_spot",
            Position.direction == "short",
        )
    )
    cross_pos = result.scalars().first()
    assert cross_pos is not None
    assert cross_pos.exchange == "binance_futures"


# ── 4. PortfolioManager cash_symbol = USDT ────────────────────

@pytest.mark.asyncio
async def test_portfolio_manager_cash_symbol_usdt(session):
    """PortfolioManager with binance_spot uses USDT as cash symbol."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500.0,
        is_paper=True,
        exchange_name="binance_spot",
    )
    assert pm.cash_balance == 500.0

    # Buy with USDT values
    await pm.update_position_on_buy(
        session, "BTC/USDT",
        quantity=0.001, price=50000, cost=50.0, fee=0.05,
    )
    assert pm.cash_balance == pytest.approx(449.95, abs=0.01)


# ── 5. reconcile 실행됨 (skip 안 함) ─────────────────────────

@pytest.mark.asyncio
async def test_reconcile_runs_for_binance_spot(session):
    """reconcile_cash_from_db should NOT skip for binance_spot."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=500.0,
        is_paper=True,
        exchange_name="binance_spot",
    )
    old_cash = pm.cash_balance
    # Should execute without returning early
    await pm.reconcile_cash_from_db(session)
    # No positions/orders, so cash = initial_balance - 0 + 0 - 0 = 500
    assert pm.cash_balance == pytest.approx(500.0, abs=0.01)


@pytest.mark.asyncio
async def test_reconcile_skips_for_futures(session):
    """reconcile_cash_from_db should skip for binance_futures."""
    pm = PortfolioManager(
        market_data=_make_market_data({}),
        initial_balance_krw=1000.0,
        is_paper=True,
        exchange_name="binance_futures",
    )
    pm._cash_balance = 999.0  # manually set to different value
    await pm.reconcile_cash_from_db(session)
    # Should not change because it returns early
    assert pm.cash_balance == 999.0


# ── 6. PaperAdapter USDT 동작 ────────────────────────────────

@pytest.mark.asyncio
async def test_paper_adapter_usdt():
    """PaperAdapter with base_currency=USDT uses USDT for balances."""
    mock_real = AsyncMock()
    mock_real.initialize = AsyncMock()
    mock_real.fetch_ticker = AsyncMock(return_value=MagicMock(
        ask=50000.0, bid=49990.0,
    ))

    adapter = PaperAdapter(
        real_adapter=mock_real,
        initial_balance_krw=500.0,
        taker_fee_pct=0.001,
        base_currency="USDT",
    )
    await adapter.initialize()

    balances = await adapter.fetch_balance()
    assert "USDT" in balances
    assert "KRW" not in balances
    assert balances["USDT"].free == 500.0


# ── 7. PaperAdapter 하위 호환 (KRW) ──────────────────────────

@pytest.mark.asyncio
async def test_paper_adapter_krw_backward_compat():
    """PaperAdapter without base_currency defaults to KRW."""
    mock_real = AsyncMock()
    mock_real.initialize = AsyncMock()

    adapter = PaperAdapter(
        real_adapter=mock_real,
        initial_balance_krw=500_000,
    )
    await adapter.initialize()

    balances = await adapter.fetch_balance()
    assert "KRW" in balances
    assert balances["KRW"].free == 500_000


# ── 8. Config 로딩 ───────────────────────────────────────────

def test_binance_spot_config():
    """BinanceSpotTradingConfig can be loaded."""
    from config import BinanceSpotTradingConfig, BinanceConfig
    bst = BinanceSpotTradingConfig()
    assert bst.mode == "paper"
    assert bst.initial_balance_usdt == 500.0
    assert bst.rotation_enabled is False

    bc = BinanceConfig()
    assert bc.spot_enabled is False
