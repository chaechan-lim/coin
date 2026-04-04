"""Tests for futures data models (OpenInterest, MarkPriceInfo, LongShortRatio)
and the corresponding base adapter method stubs.
"""

import pytest
from datetime import datetime, timezone

from exchange.data_models import OpenInterest, MarkPriceInfo, LongShortRatio
from exchange.base import ExchangeAdapter


# ── OpenInterest ──────────────────────────────────────────────────


class TestOpenInterest:
    def test_basic_construction(self):
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        oi = OpenInterest(
            symbol="BTC/USDT",
            open_interest_value=1_500_000_000.0,
            timestamp=ts,
        )
        assert oi.symbol == "BTC/USDT"
        assert oi.open_interest_value == 1_500_000_000.0
        assert oi.timestamp == ts

    def test_zero_value(self):
        oi = OpenInterest(
            symbol="ETH/USDT",
            open_interest_value=0.0,
            timestamp=datetime.now(timezone.utc),
        )
        assert oi.open_interest_value == 0.0

    def test_large_value(self):
        oi = OpenInterest(
            symbol="BTC/USDT",
            open_interest_value=99_999_999_999.99,
            timestamp=datetime.now(timezone.utc),
        )
        assert oi.open_interest_value == 99_999_999_999.99


# ── MarkPriceInfo ─────────────────────────────────────────────────


class TestMarkPriceInfo:
    def test_basic_construction(self):
        """premium_pct is auto-computed from mark_price and index_price."""
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        mp = MarkPriceInfo(
            symbol="BTC/USDT",
            mark_price=65100.0,
            index_price=65000.0,
            last_funding_rate=0.0001,
            next_funding_time=ts,
            timestamp=ts,
        )
        assert mp.symbol == "BTC/USDT"
        assert mp.mark_price == 65100.0
        assert mp.index_price == 65000.0
        assert mp.last_funding_rate == 0.0001
        # (65100 - 65000) / 65000 * 100 ≈ 0.1538%
        assert mp.premium_pct == pytest.approx(0.15384615, rel=1e-5)

    def test_negative_premium(self):
        """Mark < index → negative premium (discount)."""
        mp = MarkPriceInfo(
            symbol="BTC/USDT",
            mark_price=64900.0,
            index_price=65000.0,
            last_funding_rate=-0.0002,
            next_funding_time=datetime.now(timezone.utc),
            timestamp=datetime.now(timezone.utc),
        )
        assert mp.premium_pct < 0
        assert mp.premium_pct == pytest.approx(-0.15384615, rel=1e-5)

    def test_zero_index_price(self):
        """When index is 0, premium_pct must be 0.0 (no ZeroDivisionError)."""
        mp = MarkPriceInfo(
            symbol="BTC/USDT",
            mark_price=65000.0,
            index_price=0.0,
            last_funding_rate=0.0,
            next_funding_time=datetime.now(timezone.utc),
            timestamp=datetime.now(timezone.utc),
        )
        assert mp.premium_pct == 0.0

    def test_equal_prices_zero_premium(self):
        """Mark == index → premium 0."""
        mp = MarkPriceInfo(
            symbol="BTC/USDT",
            mark_price=65000.0,
            index_price=65000.0,
            last_funding_rate=0.0001,
            next_funding_time=datetime.now(timezone.utc),
            timestamp=datetime.now(timezone.utc),
        )
        assert mp.premium_pct == 0.0

    def test_explicit_premium_pct_overridden(self):
        """Any explicitly passed premium_pct is overwritten by __post_init__."""
        mp = MarkPriceInfo(
            symbol="BTC/USDT",
            mark_price=65100.0,
            index_price=65000.0,
            last_funding_rate=0.0,
            next_funding_time=datetime.now(timezone.utc),
            timestamp=datetime.now(timezone.utc),
            premium_pct=999.0,  # should be overwritten
        )
        assert mp.premium_pct == pytest.approx(0.15384615, rel=1e-5)


# ── LongShortRatio ────────────────────────────────────────────────


class TestLongShortRatio:
    def test_basic_construction(self):
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        lsr = LongShortRatio(
            symbol="BTC/USDT",
            long_account_ratio=0.55,
            short_account_ratio=0.45,
            long_position_ratio=0.60,
            short_position_ratio=0.40,
            timestamp=ts,
        )
        assert lsr.symbol == "BTC/USDT"
        assert lsr.long_account_ratio == 0.55
        assert lsr.short_account_ratio == 0.45
        assert lsr.long_position_ratio == 0.60
        assert lsr.short_position_ratio == 0.40
        assert lsr.timestamp == ts

    def test_ratios_sum_to_one(self):
        lsr = LongShortRatio(
            symbol="ETH/USDT",
            long_account_ratio=0.52,
            short_account_ratio=0.48,
            long_position_ratio=0.58,
            short_position_ratio=0.42,
            timestamp=datetime.now(timezone.utc),
        )
        assert lsr.long_account_ratio + lsr.short_account_ratio == pytest.approx(1.0)
        assert lsr.long_position_ratio + lsr.short_position_ratio == pytest.approx(1.0)

    def test_extreme_long_bias(self):
        lsr = LongShortRatio(
            symbol="BTC/USDT",
            long_account_ratio=0.95,
            short_account_ratio=0.05,
            long_position_ratio=0.90,
            short_position_ratio=0.10,
            timestamp=datetime.now(timezone.utc),
        )
        assert lsr.long_account_ratio > lsr.short_account_ratio


# ── Base adapter stubs raise NotImplementedError ──────────────────


class _ConcreteAdapter(ExchangeAdapter):
    """Minimal concrete subclass to test the optional futures method stubs."""

    async def initialize(self) -> None: ...
    async def close(self) -> None: ...
    async def fetch_ticker(self, symbol): ...
    async def fetch_ohlcv(self, symbol, timeframe="1h", limit=100, since=None): ...
    async def fetch_orderbook(self, symbol, limit=20): ...
    async def fetch_balance(self): ...
    async def create_limit_buy(self, symbol, amount, price): ...
    async def create_limit_sell(self, symbol, amount, price): ...
    async def create_market_buy(self, symbol, amount): ...
    async def create_market_sell(self, symbol, amount): ...
    async def cancel_order(self, order_id, symbol): ...
    async def fetch_order(self, order_id, symbol): ...


class TestBaseAdapterStubs:
    @pytest.fixture
    def adapter(self):
        return _ConcreteAdapter()

    @pytest.mark.asyncio
    async def test_fetch_open_interest_not_implemented(self, adapter):
        with pytest.raises(NotImplementedError, match="Futures not supported"):
            await adapter.fetch_open_interest("BTC/USDT")

    @pytest.mark.asyncio
    async def test_fetch_open_interest_history_not_implemented(self, adapter):
        with pytest.raises(NotImplementedError, match="Futures not supported"):
            await adapter.fetch_open_interest_history("BTC/USDT")

    @pytest.mark.asyncio
    async def test_fetch_mark_price_not_implemented(self, adapter):
        with pytest.raises(NotImplementedError, match="Futures not supported"):
            await adapter.fetch_mark_price("BTC/USDT")

    @pytest.mark.asyncio
    async def test_fetch_long_short_ratio_not_implemented(self, adapter):
        with pytest.raises(NotImplementedError, match="Futures not supported"):
            await adapter.fetch_long_short_ratio("BTC/USDT")
