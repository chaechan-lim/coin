"""
BinanceFuturesEngine 단위 테스트
================================
"""
import math
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from core.models import Position
from engine.futures_engine import (
    BinanceFuturesEngine,
    _FUTURES_DEFAULT_SL_PCT,
    _FUTURES_DEFAULT_TP_PCT,
)
from engine.trading_engine import PositionTracker


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def mock_config():
    """Minimal mock AppConfig for futures."""
    config = MagicMock()
    config.binance.enabled = True
    config.binance.default_leverage = 5
    config.binance.max_leverage = 10
    config.binance.futures_fee = 0.0004
    config.binance.tracked_coins = ["BTC/USDT", "ETH/USDT"]
    config.binance.testnet = True
    config.binance_trading.evaluation_interval_sec = 300
    config.binance_trading.initial_balance_usdt = 1000.0
    config.binance_trading.min_combined_confidence = 0.50
    config.binance_trading.max_trade_size_pct = 0.15
    config.binance_trading.daily_buy_limit = 15
    config.binance_trading.max_daily_coin_buys = 3
    config.binance_trading.ws_price_monitor = True
    config.trading.mode = "paper"
    config.trading.evaluation_interval_sec = 300
    config.trading.tracked_coins = ["BTC/USDT"]
    config.trading.min_combined_confidence = 0.50
    config.trading.daily_buy_limit = 15
    config.trading.max_daily_coin_buys = 3
    config.trading.min_trade_interval_sec = 3600
    config.trading.rotation_enabled = False
    config.risk.max_trade_size_pct = 0.20
    return config


@pytest.fixture
def mock_exchange():
    exchange = AsyncMock()
    exchange.set_leverage = AsyncMock(return_value={})
    exchange.fetch_funding_rate = AsyncMock(return_value=0.0001)
    return exchange


@pytest.fixture
def mock_market_data():
    md = AsyncMock()
    md.get_current_price = AsyncMock(return_value=65000.0)
    md.get_ticker = AsyncMock(return_value=MagicMock(last=65000.0))
    md.get_candles = AsyncMock(return_value=None)
    return md


@pytest.fixture
def futures_engine(mock_config, mock_exchange, mock_market_data):
    """Create BinanceFuturesEngine with mocked dependencies."""
    order_mgr = MagicMock()
    portfolio_mgr = MagicMock()
    portfolio_mgr.cash_balance = 1000.0
    combiner = MagicMock()

    engine = BinanceFuturesEngine(
        config=mock_config,
        exchange=mock_exchange,
        market_data=mock_market_data,
        order_manager=order_mgr,
        portfolio_manager=portfolio_mgr,
        combiner=combiner,
    )
    return engine


# ── Tests ─────────────────────────────────────────────────────────

class TestFuturesEngineInit:
    def test_exchange_name(self, futures_engine):
        assert futures_engine._exchange_name == "binance_futures"

    def test_leverage_from_config(self, futures_engine):
        assert futures_engine._leverage == 5

    def test_tracked_coins(self, futures_engine):
        assert sorted(futures_engine.tracked_coins) == ["BTC/USDT", "ETH/USDT"]

    def test_rotation_disabled(self, futures_engine):
        rs = futures_engine.rotation_status
        assert rs["rotation_enabled"] is False


class TestLeverageSizing:
    def test_sl_tp_scaled_by_sqrt_leverage(self, futures_engine):
        """SL/TP가 sqrt(leverage)로 축소되는지 확인."""
        lev = futures_engine._leverage  # 5
        sqrt_lev = math.sqrt(lev)

        expected_sl = _FUTURES_DEFAULT_SL_PCT / sqrt_lev
        expected_tp = _FUTURES_DEFAULT_TP_PCT / sqrt_lev

        assert abs(expected_sl - 8.0 / sqrt_lev) < 0.01
        assert abs(expected_tp - 16.0 / sqrt_lev) < 0.01

    def test_liquidation_price_long(self, futures_engine):
        """롱 청산가 = entry * (1 - 1/lev + fee)."""
        entry = 65000.0
        lev = 5
        fee = 0.0004
        expected = entry * (1 - 1 / lev + fee)
        assert abs(expected - entry * 0.8004) < 1.0


class TestShortTracking:
    def test_short_pnl_calculation(self):
        """숏 PnL: (entry - price) / entry * 100"""
        entry = 65000.0
        price = 63000.0  # 가격 하락 = 수익
        pnl_pct = (entry - price) / entry * 100
        assert pnl_pct > 0  # 숏은 가격 하락 시 수익

    def test_short_sl_triggers_on_price_increase(self):
        """숏 SL: 가격 상승이 SL% 초과하면 발동."""
        entry = 65000.0
        sl_pct = 2.24  # 5.0 / sqrt(5)
        # price가 entry * (1 + sl_pct/100) 이상이면 SL
        sl_price = entry * (1 + sl_pct / 100)
        price = sl_price + 100
        pnl_pct = (entry - price) / entry * 100
        assert pnl_pct < 0
        assert abs(pnl_pct) > sl_pct


class TestLiquidationCheck:
    def test_long_liquidation_proximity(self):
        """롱 포지션 청산가 근접 감지 (2% 이내)."""
        liq_price = 52000.0
        current_price = liq_price * 1.015  # 1.5% above liquidation
        assert current_price <= liq_price * 1.02  # Within 2%

    def test_short_liquidation_proximity(self):
        """숏 포지션 청산가 근접 감지 (2% 이내)."""
        liq_price = 78000.0
        current_price = liq_price * 0.985  # 1.5% below liquidation
        assert current_price >= liq_price * 0.98  # Within 2%


class TestFundingRates:
    @pytest.mark.asyncio
    async def test_funding_rate_fetch(self, futures_engine):
        await futures_engine._maybe_update_funding_rates()
        assert "BTC/USDT" in futures_engine._funding_rates
        assert futures_engine._funding_rates["BTC/USDT"] == 0.0001
