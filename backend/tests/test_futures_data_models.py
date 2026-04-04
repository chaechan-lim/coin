"""Tests for futures data models: OpenInterest, MarkPriceInfo, LongShortRatio."""

import pytest
from datetime import datetime, timezone

from exchange.data_models import OpenInterest, MarkPriceInfo, LongShortRatio


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
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        mp = MarkPriceInfo(
            symbol="BTC/USDT",
            mark_price=65100.50,
            index_price=65000.00,
            last_funding_rate=0.0001,
            next_funding_time=ts,
            premium_pct=0.1546,
            timestamp=ts,
        )
        assert mp.symbol == "BTC/USDT"
        assert mp.mark_price == 65100.50
        assert mp.index_price == 65000.00
        assert mp.last_funding_rate == 0.0001
        assert mp.premium_pct == pytest.approx(0.1546)

    def test_premium_pct_calculation(self):
        """Verify manual premium_pct = (mark - index) / index * 100."""
        mark = 65100.0
        index = 65000.0
        expected_premium = (mark - index) / index * 100  # ~0.1538%

        mp = MarkPriceInfo(
            symbol="BTC/USDT",
            mark_price=mark,
            index_price=index,
            last_funding_rate=0.0001,
            next_funding_time=datetime.now(timezone.utc),
            premium_pct=expected_premium,
            timestamp=datetime.now(timezone.utc),
        )
        assert mp.premium_pct == pytest.approx(0.15384615, rel=1e-5)

    def test_negative_premium(self):
        """Mark < index → negative premium (discount)."""
        mark = 64900.0
        index = 65000.0
        premium = (mark - index) / index * 100

        mp = MarkPriceInfo(
            symbol="BTC/USDT",
            mark_price=mark,
            index_price=index,
            last_funding_rate=-0.0002,
            next_funding_time=datetime.now(timezone.utc),
            premium_pct=premium,
            timestamp=datetime.now(timezone.utc),
        )
        assert mp.premium_pct < 0
        assert mp.premium_pct == pytest.approx(-0.15384615, rel=1e-5)

    def test_zero_index_price(self):
        """When index is 0, premium should be set to 0."""
        mp = MarkPriceInfo(
            symbol="BTC/USDT",
            mark_price=65000.0,
            index_price=0.0,
            last_funding_rate=0.0,
            next_funding_time=datetime.now(timezone.utc),
            premium_pct=0.0,
            timestamp=datetime.now(timezone.utc),
        )
        assert mp.premium_pct == 0.0

    def test_equal_prices_zero_premium(self):
        """Mark == index → premium 0."""
        mark = index = 65000.0
        premium = (mark - index) / index * 100

        mp = MarkPriceInfo(
            symbol="BTC/USDT",
            mark_price=mark,
            index_price=index,
            last_funding_rate=0.0001,
            next_funding_time=datetime.now(timezone.utc),
            premium_pct=premium,
            timestamp=datetime.now(timezone.utc),
        )
        assert mp.premium_pct == 0.0


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
