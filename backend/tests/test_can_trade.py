"""
Tests for TradingEngine._can_trade() smart trade limiting.
"""
import os
os.environ.setdefault("DB_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("EXCHANGE_API_KEY", "test")
os.environ.setdefault("EXCHANGE_API_SECRET", "test")
os.environ.setdefault("TRADING_MODE", "paper")

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock

import pytest

from config import AppConfig
from engine.trading_engine import TradingEngine


def _make_engine(**trading_overrides) -> TradingEngine:
    """Create a TradingEngine with minimal mocks for unit testing _can_trade."""
    config = AppConfig()
    for k, v in trading_overrides.items():
        setattr(config.trading, k, v)
    engine = TradingEngine(
        config=config,
        exchange=MagicMock(),
        market_data=MagicMock(),
        order_manager=MagicMock(),
        portfolio_manager=MagicMock(),
        combiner=MagicMock(),
    )
    return engine


# ── 매수 제한 ──────────────────────────────────────────────────


class TestDailyBuyLimit:
    def test_buy_allowed_under_limit(self):
        engine = _make_engine(daily_buy_limit=5)
        engine._daily_buy_count = 4
        ok, reason = engine._can_trade("BTC/KRW", side="buy")
        assert ok is True

    def test_buy_blocked_at_limit(self):
        engine = _make_engine(daily_buy_limit=5)
        engine._daily_buy_count = 5
        ok, reason = engine._can_trade("BTC/KRW", side="buy")
        assert ok is False
        assert "Daily buy limit" in reason

    def test_buy_blocked_over_limit(self):
        engine = _make_engine(daily_buy_limit=5)
        engine._daily_buy_count = 10
        ok, reason = engine._can_trade("BTC/KRW", side="buy")
        assert ok is False


class TestCoinDailyBuyLimit:
    def test_coin_buy_allowed_under_limit(self):
        engine = _make_engine(max_daily_coin_buys=3)
        engine._daily_coin_buy_count["BTC/KRW"] = 2
        ok, reason = engine._can_trade("BTC/KRW", side="buy")
        assert ok is True

    def test_coin_buy_blocked_at_limit(self):
        engine = _make_engine(max_daily_coin_buys=3)
        engine._daily_coin_buy_count["BTC/KRW"] = 3
        ok, reason = engine._can_trade("BTC/KRW", side="buy")
        assert ok is False
        assert "Coin daily buy limit" in reason

    def test_other_coin_unaffected(self):
        """BTC가 상한에 도달해도 ETH는 매수 가능."""
        engine = _make_engine(max_daily_coin_buys=3)
        engine._daily_coin_buy_count["BTC/KRW"] = 3
        ok, reason = engine._can_trade("ETH/KRW", side="buy")
        assert ok is True


# ── 매도는 무제한 ──────────────────────────────────────────────


class TestSellUnlimited:
    def test_sell_allowed_despite_daily_buy_limit(self):
        engine = _make_engine(daily_buy_limit=1)
        engine._daily_buy_count = 100
        ok, reason = engine._can_trade("BTC/KRW", side="sell")
        assert ok is True
        assert reason == "OK"

    def test_sell_allowed_despite_coin_buy_limit(self):
        engine = _make_engine(max_daily_coin_buys=1)
        engine._daily_coin_buy_count["BTC/KRW"] = 99
        ok, reason = engine._can_trade("BTC/KRW", side="sell")
        assert ok is True

    def test_sell_allowed_despite_cooldown(self):
        engine = _make_engine(min_trade_interval_sec=9999)
        engine._last_trade_time["BTC/KRW"] = datetime.now(timezone.utc)
        ok, reason = engine._can_trade("BTC/KRW", side="sell")
        assert ok is True

    def test_sell_allowed_despite_paused(self):
        engine = _make_engine()
        engine._paused_coins.add("BTC/KRW")
        ok, reason = engine._can_trade("BTC/KRW", side="sell")
        assert ok is True


# ── 쿨다운 ────────────────────────────────────────────────────


class TestCooldown:
    def test_buy_blocked_during_cooldown(self):
        engine = _make_engine(min_trade_interval_sec=3600)
        engine._last_trade_time["BTC/KRW"] = datetime.now(timezone.utc) - timedelta(seconds=100)
        ok, reason = engine._can_trade("BTC/KRW", side="buy")
        assert ok is False
        assert "cooldown" in reason.lower()

    def test_buy_allowed_after_cooldown(self):
        engine = _make_engine(min_trade_interval_sec=3600)
        engine._last_trade_time["BTC/KRW"] = datetime.now(timezone.utc) - timedelta(seconds=3601)
        ok, reason = engine._can_trade("BTC/KRW", side="buy")
        assert ok is True

    def test_no_cooldown_for_new_coin(self):
        engine = _make_engine(min_trade_interval_sec=3600)
        ok, reason = engine._can_trade("NEW/KRW", side="buy")
        assert ok is True


# ── 리스크 에이전트 일시중지 ──────────────────────────────────


class TestPausedCoins:
    def test_buy_blocked_for_paused_coin(self):
        engine = _make_engine()
        engine._paused_coins.add("BTC/KRW")
        ok, reason = engine._can_trade("BTC/KRW", side="buy")
        assert ok is False
        assert "paused" in reason.lower()

    def test_buy_allowed_for_unpaused_coin(self):
        engine = _make_engine()
        engine._paused_coins.add("BTC/KRW")
        ok, reason = engine._can_trade("ETH/KRW", side="buy")
        assert ok is True


# ── 일일 리셋 ─────────────────────────────────────────────────


class TestDailyReset:
    def test_counters_reset_on_new_day(self):
        engine = _make_engine(daily_buy_limit=5)
        engine._daily_buy_count = 5
        engine._daily_coin_buy_count["BTC/KRW"] = 3
        engine._daily_trade_count = 10
        # 날짜를 어제로 세팅
        engine._daily_reset_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        ok, reason = engine._can_trade("BTC/KRW", side="buy")
        assert ok is True
        assert engine._daily_buy_count == 0
        assert engine._daily_coin_buy_count == {}
        assert engine._daily_trade_count == 0

    def test_no_reset_same_day(self):
        engine = _make_engine(daily_buy_limit=5)
        engine._daily_buy_count = 5
        engine._daily_reset_date = datetime.now(timezone.utc).date()
        ok, reason = engine._can_trade("BTC/KRW", side="buy")
        assert ok is False  # limit still enforced


# ── 매도 후 재매수 대기 (washout) ──────────────────────────────


class TestPostSellWashout:
    def test_buy_blocked_during_washout(self):
        """매도 직후 재매수 차단."""
        engine = _make_engine(cooldown_after_sell_sec=14400)
        engine._last_sell_time["BTC/KRW"] = datetime.now(timezone.utc) - timedelta(hours=1)
        ok, reason = engine._can_trade("BTC/KRW", side="buy")
        assert ok is False
        assert "washout" in reason.lower()

    def test_buy_allowed_after_washout(self):
        """대기 시간 경과 후 매수 가능."""
        engine = _make_engine(cooldown_after_sell_sec=14400)
        engine._last_sell_time["BTC/KRW"] = datetime.now(timezone.utc) - timedelta(hours=5)
        ok, reason = engine._can_trade("BTC/KRW", side="buy")
        assert ok is True

    def test_sell_not_affected_by_washout(self):
        """매도는 washout 영향 안 받음."""
        engine = _make_engine(cooldown_after_sell_sec=14400)
        engine._last_sell_time["BTC/KRW"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        ok, reason = engine._can_trade("BTC/KRW", side="sell")
        assert ok is True

    def test_other_coin_not_affected(self):
        """다른 코인은 영향 없음."""
        engine = _make_engine(cooldown_after_sell_sec=14400)
        engine._last_sell_time["BTC/KRW"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        ok, reason = engine._can_trade("ETH/KRW", side="buy")
        assert ok is True
