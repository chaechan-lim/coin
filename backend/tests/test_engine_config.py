"""Unit tests for EngineConfig — exchange-agnostic engine configuration."""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import asdict

from engine.trading_engine import EngineConfig, TradingEngine


# ── EngineConfig 기본 동작 ─────────────────────────────────────────


class TestEngineConfigDefaults:
    def test_default_values(self):
        ec = EngineConfig()
        assert ec.exchange_name == "bithumb"
        assert ec.mode == "paper"
        assert ec.quote_currency == "KRW"
        assert ec.min_order_amount == 5000
        assert ec.fee_margin == 1.003
        assert ec.min_fallback_amount == 5000
        assert ec.rotation_enabled is True
        assert ec.asymmetric_mode is True

    def test_quote_suffix(self):
        ec_krw = EngineConfig(quote_currency="KRW")
        assert ec_krw.quote_suffix == "/KRW"

        ec_usdt = EngineConfig(quote_currency="USDT")
        assert ec_usdt.quote_suffix == "/USDT"

    def test_btc_symbol(self):
        ec_krw = EngineConfig(quote_currency="KRW")
        assert ec_krw.btc_symbol == "BTC/KRW"

        ec_usdt = EngineConfig(quote_currency="USDT")
        assert ec_usdt.btc_symbol == "BTC/USDT"


# ── from_app_config 팩토리 ─────────────────────────────────────────


def _make_mock_app_config():
    """AppConfig 모킹."""
    config = MagicMock()

    # TradingConfig (bithumb)
    config.trading.mode = "live"
    config.trading.tracked_coins = ["BTC/KRW", "ETH/KRW", "XRP/KRW"]
    config.trading.evaluation_interval_sec = 300
    config.trading.min_combined_confidence = 0.50
    config.trading.daily_buy_limit = 20
    config.trading.max_daily_coin_buys = 3
    config.trading.min_trade_interval_sec = 3600
    config.trading.cooldown_after_sell_sec = 14400
    config.trading.min_profit_vs_fee_ratio = 2.0
    config.trading.asymmetric_mode = True
    config.trading.rotation_enabled = True
    config.trading.rotation_coins = ["DOGE/KRW", "AVAX/KRW"]
    config.trading.surge_threshold = 3.0
    config.trading.rotation_cooldown_sec = 7200

    # RiskConfig
    config.risk.max_trade_size_pct = 0.20
    config.risk.max_single_coin_pct = 0.40
    config.risk.rebalancing_enabled = True
    config.risk.rebalancing_target_pct = 0.35

    # BinanceConfig
    config.binance.tracked_coins = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

    # BinanceTradingConfig
    config.binance_trading.mode = "live"
    config.binance_trading.evaluation_interval_sec = 300
    config.binance_trading.min_combined_confidence = 0.55
    config.binance_trading.daily_buy_limit = 15
    config.binance_trading.max_daily_coin_buys = 3
    config.binance_trading.max_trade_size_pct = 0.35
    config.binance_trading.ws_price_monitor = True

    # BinanceSpotTradingConfig
    config.binance_spot_trading.mode = "live"
    config.binance_spot_trading.evaluation_interval_sec = 300
    config.binance_spot_trading.min_combined_confidence = 0.50
    config.binance_spot_trading.daily_buy_limit = 20
    config.binance_spot_trading.max_daily_coin_buys = 3
    config.binance_spot_trading.max_trade_size_pct = 0.20
    config.binance_spot_trading.cooldown_after_buy_sec = 1800
    config.binance_spot_trading.cooldown_after_sell_sec = 14400
    config.binance_spot_trading.rotation_enabled = True

    return config


class TestFromAppConfig:
    def test_bithumb_config(self):
        config = _make_mock_app_config()
        ec = EngineConfig.from_app_config(config, "bithumb")

        assert ec.exchange_name == "bithumb"
        assert ec.mode == "live"
        assert ec.quote_currency == "KRW"
        assert ec.tracked_coins == ["BTC/KRW", "ETH/KRW", "XRP/KRW"]
        assert ec.min_order_amount == 5000
        assert ec.fee_margin == 1.003
        assert ec.min_fallback_amount == 5000
        assert ec.rotation_enabled is True
        assert ec.rotation_coins == ["DOGE/KRW", "AVAX/KRW"]
        assert ec.surge_threshold == 3.0
        assert ec.rotation_cooldown_sec == 7200
        assert ec.quote_suffix == "/KRW"
        assert ec.btc_symbol == "BTC/KRW"

    def test_binance_spot_config(self):
        config = _make_mock_app_config()
        ec = EngineConfig.from_app_config(config, "binance_spot")

        assert ec.exchange_name == "binance_spot"
        assert ec.mode == "live"
        assert ec.quote_currency == "USDT"
        assert ec.tracked_coins == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
        assert ec.min_order_amount == 5.0
        assert ec.fee_margin == 1.002
        assert ec.min_fallback_amount == 10.0
        assert ec.rotation_enabled is True
        assert ec.rotation_coins == []
        assert ec.quote_suffix == "/USDT"
        assert ec.btc_symbol == "BTC/USDT"
        assert "USDC/USDT" in ec.stablecoins

    def test_binance_futures_config(self):
        config = _make_mock_app_config()
        ec = EngineConfig.from_app_config(config, "binance_futures")

        assert ec.exchange_name == "binance_futures"
        assert ec.mode == "live"
        assert ec.quote_currency == "USDT"
        assert ec.tracked_coins == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
        assert ec.min_order_amount == 5.0
        assert ec.rotation_enabled is False
        assert ec.asymmetric_mode is False
        assert ec.rebalancing_enabled is False

    def test_unknown_exchange_raises(self):
        config = _make_mock_app_config()
        with pytest.raises(ValueError, match="Unknown exchange"):
            EngineConfig.from_app_config(config, "kraken")


# ── TradingEngine 에 EngineConfig 통합 ─────────────────────────────


def _make_engine(exchange_name="bithumb", **ec_kwargs):
    """간단한 TradingEngine 생성 (EngineConfig 직접 주입)."""
    ec = EngineConfig(exchange_name=exchange_name, **ec_kwargs)
    config = _make_mock_app_config()
    engine = TradingEngine(
        config=config,
        exchange=MagicMock(),
        market_data=MagicMock(),
        order_manager=MagicMock(),
        portfolio_manager=MagicMock(),
        combiner=MagicMock(),
        exchange_name=exchange_name,
        engine_config=ec,
    )
    return engine


class TestEngineUsesEngineConfig:
    def test_tracked_coins_from_ec(self):
        engine = _make_engine(tracked_coins=["BTC/KRW", "ETH/KRW"])
        assert engine.tracked_coins == ["BTC/KRW", "ETH/KRW"]

    def test_min_order_amount_bithumb(self):
        engine = _make_engine(min_order_amount=5000)
        assert engine._min_order_amount == 5000

    def test_min_order_amount_binance(self):
        engine = _make_engine(exchange_name="binance_spot", min_order_amount=5.0)
        assert engine._min_order_amount == 5.0

    def test_fee_margin(self):
        engine = _make_engine(fee_margin=1.002)
        assert engine._fee_margin == 1.002

    def test_min_fallback_amount(self):
        engine = _make_engine(min_fallback_amount=10.0)
        assert engine._min_fallback_amount == 10.0

    def test_rotation_status_uses_ec(self):
        engine = _make_engine(
            rotation_enabled=True,
            surge_threshold=3.0,
            rotation_cooldown_sec=7200,
            tracked_coins=["BTC/KRW"],
        )
        status = engine.rotation_status
        assert status["rotation_enabled"] is True
        assert status["surge_threshold"] == 3.0
        assert status["rotation_cooldown_sec"] == 7200
        assert status["tracked_coins"] == ["BTC/KRW"]

    def test_no_exchange_string_comparison(self):
        """Engine should not check exchange_name strings for config values."""
        # binance_spot with KRW settings — engine doesn't care about exchange name
        engine = _make_engine(
            exchange_name="binance_spot",
            min_order_amount=5.0,
            fee_margin=1.002,
            quote_currency="USDT",
        )
        assert engine._min_order_amount == 5.0
        assert engine._fee_margin == 1.002
        assert engine._ec.quote_suffix == "/USDT"

    def test_engine_config_override_tracked_coins(self):
        """tracked_coins 파라미터가 engine_config를 오버라이드."""
        ec = EngineConfig(tracked_coins=["BTC/KRW"])
        config = _make_mock_app_config()
        engine = TradingEngine(
            config=config,
            exchange=MagicMock(),
            market_data=MagicMock(),
            order_manager=MagicMock(),
            portfolio_manager=MagicMock(),
            combiner=MagicMock(),
            engine_config=ec,
            tracked_coins=["ETH/KRW", "XRP/KRW"],
        )
        assert engine.tracked_coins == ["ETH/KRW", "XRP/KRW"]

    def test_engine_config_override_eval_interval(self):
        """evaluation_interval_sec 파라미터가 engine_config를 오버라이드."""
        ec = EngineConfig(evaluation_interval_sec=300)
        config = _make_mock_app_config()
        engine = TradingEngine(
            config=config,
            exchange=MagicMock(),
            market_data=MagicMock(),
            order_manager=MagicMock(),
            portfolio_manager=MagicMock(),
            combiner=MagicMock(),
            engine_config=ec,
            evaluation_interval_sec=60,
        )
        assert engine._ec.evaluation_interval_sec == 60

    def test_auto_engine_config_from_app_config(self):
        """engine_config 미전달 시 from_app_config 자동 생성."""
        config = _make_mock_app_config()
        engine = TradingEngine(
            config=config,
            exchange=MagicMock(),
            market_data=MagicMock(),
            order_manager=MagicMock(),
            portfolio_manager=MagicMock(),
            combiner=MagicMock(),
            exchange_name="bithumb",
        )
        assert engine._ec.exchange_name == "bithumb"
        assert engine._ec.mode == "live"
        assert engine._ec.tracked_coins == ["BTC/KRW", "ETH/KRW", "XRP/KRW"]


# ── can_trade 관련 EngineConfig 사용 ──────────────────────────────


class TestCanTradeWithEngineConfig:
    def test_daily_buy_limit_from_ec(self):
        engine = _make_engine(daily_buy_limit=5)
        engine._daily_buy_count = 5
        engine._daily_reset_date = __import__('datetime').datetime.now(
            __import__('datetime').timezone.utc
        ).date()
        can, reason = engine._can_trade("BTC/KRW")
        assert can is False
        assert "Daily buy limit" in reason

    def test_coin_buy_limit_from_ec(self):
        engine = _make_engine(max_daily_coin_buys=2)
        engine._daily_coin_buy_count["BTC/KRW"] = 2
        engine._daily_reset_date = __import__('datetime').datetime.now(
            __import__('datetime').timezone.utc
        ).date()
        can, reason = engine._can_trade("BTC/KRW")
        assert can is False
        assert "Coin daily buy limit" in reason

    def test_cooldown_from_ec(self):
        from datetime import datetime, timezone
        engine = _make_engine(min_trade_interval_sec=3600)
        engine._daily_reset_date = datetime.now(timezone.utc).date()
        engine._last_trade_time["BTC/KRW"] = datetime.now(timezone.utc)
        can, reason = engine._can_trade("BTC/KRW")
        assert can is False
        assert "cooldown" in reason.lower() or "Cooldown" in reason


# ── 로테이션 관련 EngineConfig 사용 ──────────────────────────────


class TestRotationWithEngineConfig:
    def test_rotation_coins_fallback(self):
        engine = _make_engine(rotation_coins=["DOGE/KRW", "AVAX/KRW"])
        coins = engine._get_rotation_coins()
        assert coins == ["DOGE/KRW", "AVAX/KRW"]

    def test_rotation_coins_dynamic_priority(self):
        engine = _make_engine(rotation_coins=["DOGE/KRW"])
        engine._dynamic_rotation_coins = ["SOL/KRW", "LINK/KRW"]
        coins = engine._get_rotation_coins()
        assert coins == ["SOL/KRW", "LINK/KRW"]

    def test_empty_rotation_coins(self):
        engine = _make_engine(rotation_coins=[])
        engine._dynamic_rotation_coins = []
        coins = engine._get_rotation_coins()
        assert coins == []


# ── 거래소별 EngineConfig 격리 테스트 ────────────────────────────


class TestExchangeIsolation:
    def test_bithumb_and_binance_spot_different_configs(self):
        bithumb = _make_engine(
            exchange_name="bithumb",
            min_order_amount=5000,
            fee_margin=1.003,
            quote_currency="KRW",
        )
        spot = _make_engine(
            exchange_name="binance_spot",
            min_order_amount=5.0,
            fee_margin=1.002,
            quote_currency="USDT",
        )
        assert bithumb._min_order_amount == 5000
        assert spot._min_order_amount == 5.0
        assert bithumb._fee_margin == 1.003
        assert spot._fee_margin == 1.002
        assert bithumb._ec.quote_suffix == "/KRW"
        assert spot._ec.quote_suffix == "/USDT"

    def test_engine_does_not_check_exchange_name_for_sizing(self):
        """거래소명 무관하게 EngineConfig 값만 사용하는지 확인."""
        # 의도적으로 빗썸에 USDT 설정
        engine = _make_engine(
            exchange_name="bithumb",
            min_order_amount=5.0,
            fee_margin=1.002,
            quote_currency="USDT",
        )
        assert engine._min_order_amount == 5.0
        assert engine._fee_margin == 1.002
        assert engine._ec.btc_symbol == "BTC/USDT"
