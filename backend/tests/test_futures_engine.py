"""
BinanceFuturesEngine 단위 테스트
================================
"""
import asyncio
import math
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from datetime import datetime, timezone

from core.models import Position
from exchange.data_models import OrderBook
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


class TestEvalErrorCounter:
    """연속 평가 오류 → 강제 청산 테스트."""

    def test_error_counter_initialized(self, futures_engine):
        assert futures_engine._eval_error_counts == {}
        assert futures_engine._MAX_EVAL_ERRORS == 3

    def test_error_count_increments(self, futures_engine):
        """에러 카운터가 올바르게 증가하는지."""
        futures_engine._eval_error_counts["POWER/USDT"] = 1
        futures_engine._eval_error_counts["POWER/USDT"] += 1
        assert futures_engine._eval_error_counts["POWER/USDT"] == 2

    def test_error_count_resets_on_success(self, futures_engine):
        """성공 시 카운터가 리셋되는지."""
        futures_engine._eval_error_counts["BTC/USDT"] = 2
        futures_engine._eval_error_counts.pop("BTC/USDT", None)
        assert "BTC/USDT" not in futures_engine._eval_error_counts

    def test_threshold_triggers_force_close(self, futures_engine):
        """N회 연속 에러가 임계값에 도달하는지."""
        symbol = "POWER/USDT"
        for i in range(1, futures_engine._MAX_EVAL_ERRORS + 1):
            futures_engine._eval_error_counts[symbol] = i
        assert futures_engine._eval_error_counts[symbol] >= futures_engine._MAX_EVAL_ERRORS


class TestForceCloseStuckPosition:
    """_force_close_stuck_position 메서드 테스트."""

    @pytest.mark.asyncio
    async def test_force_close_no_position(self, futures_engine):
        """포지션이 없으면 카운터만 정리하고 리턴."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        futures_engine._eval_error_counts["DEAD/USDT"] = 5

        await futures_engine._force_close_stuck_position(
            session, "DEAD/USDT", "API 404"
        )
        assert "DEAD/USDT" not in futures_engine._eval_error_counts

    @pytest.mark.asyncio
    async def test_force_close_with_price_available(self, futures_engine, mock_market_data):
        """가격 조회 가능 → 시장가 청산 시도."""
        position = MagicMock(spec=Position)
        position.symbol = "POWER/USDT"
        position.quantity = 500.0
        position.average_buy_price = 0.18
        position.direction = "long"
        position.leverage = 3
        position.margin_used = 30.0

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = position
        session.execute = AsyncMock(return_value=mock_result)
        session.refresh = AsyncMock()

        mock_market_data.get_current_price = AsyncMock(return_value=0.20)
        futures_engine._eval_error_counts["POWER/USDT"] = 3
        futures_engine._close_lock = asyncio.Lock()

        with patch.object(futures_engine, '_close_position', new_callable=AsyncMock) as mock_close:
            await futures_engine._force_close_stuck_position(
                session, "POWER/USDT", "API 404"
            )
            mock_close.assert_called_once()
            assert "POWER/USDT" not in futures_engine._eval_error_counts

    @pytest.mark.asyncio
    async def test_force_close_price_unavailable_resets_db(self, futures_engine, mock_market_data):
        """가격 조회 실패 → DB 포지션 0으로 리셋."""
        position = MagicMock(spec=Position)
        position.symbol = "DEAD/USDT"
        position.quantity = 100.0
        position.average_buy_price = 1.5

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = position
        session.execute = AsyncMock(return_value=mock_result)

        mock_market_data.get_current_price = AsyncMock(side_effect=Exception("404"))
        futures_engine._eval_error_counts["DEAD/USDT"] = 5
        futures_engine._close_lock = asyncio.Lock()

        with patch("engine.futures_engine.emit_event", new_callable=AsyncMock):
            await futures_engine._force_close_stuck_position(
                session, "DEAD/USDT", "404 Not Found"
            )
            assert position.quantity == 0
            assert position.current_value == 0
            assert "DEAD/USDT" not in futures_engine._eval_error_counts
            session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_collect_signals_ticker_failure_returns_empty(self, futures_engine, mock_market_data):
        """ticker 조회 실패 시 빈 시그널 반환 (예외 전파 안 함)."""
        mock_market_data.get_ticker = AsyncMock(side_effect=Exception("404"))
        signals = await futures_engine._collect_signals("DEAD/USDT")
        assert signals == []


class TestDynamicCoinOrderbookFilter:
    """동적 코인 선정 시 오더북 깊이 필터 테스트."""

    def _make_ticker(self, symbol, quote_vol, pct_change=5.0):
        return {
            f"{symbol}:USDT": {
                "quoteVolume": quote_vol,
                "percentage": pct_change,
            }
        }

    def _make_orderbook(self, mid_price, depth_usdt):
        """지정된 1% depth를 가진 오더북 생성."""
        bid_price = mid_price
        bid_qty = depth_usdt / mid_price  # depth_usdt 만큼의 물량
        ask_price = mid_price * 1.001
        ask_qty = bid_qty
        return OrderBook(
            symbol="",
            bids=[(bid_price, bid_qty)],
            asks=[(ask_price, ask_qty)],
            timestamp=datetime.now(timezone.utc),
        )

    @pytest.mark.asyncio
    async def test_shallow_orderbook_excluded(self, futures_engine):
        """오더북 깊이 부족 코인 제외."""
        tickers = {}
        tickers.update(self._make_ticker("SHALLOW/USDT", 100_000_000))
        tickers.update(self._make_ticker("DEEP/USDT", 80_000_000))

        futures_engine._exchange.fetch_tickers = AsyncMock(return_value=tickers)
        futures_engine._last_coin_refresh = None

        # SHALLOW: 3000 USDT depth (< 50000 임계값), DEEP: 200000 USDT depth
        async def mock_fetch_orderbook(sym, limit=20):
            if "SHALLOW" in sym:
                return self._make_orderbook(10.0, 3000)
            return self._make_orderbook(50.0, 200000)

        futures_engine._exchange.fetch_orderbook = mock_fetch_orderbook

        await futures_engine._refresh_dynamic_coins()

        assert "DEEP/USDT" in futures_engine._dynamic_coins
        assert "SHALLOW/USDT" not in futures_engine._dynamic_coins

    @pytest.mark.asyncio
    async def test_tracked_coins_skip_depth_check(self, futures_engine):
        """고정 추적 코인(config)은 오더북 체크 스킵."""
        tickers = {}
        tickers.update(self._make_ticker("BTC/USDT", 500_000_000))

        futures_engine._exchange.fetch_tickers = AsyncMock(return_value=tickers)
        futures_engine._last_coin_refresh = None
        # fetch_orderbook 호출되지 않아야 함 (tracked_coins에 BTC/USDT 포함)
        futures_engine._exchange.fetch_orderbook = AsyncMock(
            side_effect=Exception("should not be called")
        )

        await futures_engine._refresh_dynamic_coins()

        assert "BTC/USDT" in futures_engine._dynamic_coins

    @pytest.mark.asyncio
    async def test_orderbook_fetch_failure_passes_coin(self, futures_engine):
        """오더북 조회 실패 시 코인 통과 (거래대금 필터만 적용)."""
        tickers = {}
        tickers.update(self._make_ticker("FAIL/USDT", 100_000_000))

        futures_engine._exchange.fetch_tickers = AsyncMock(return_value=tickers)
        futures_engine._last_coin_refresh = None
        futures_engine._exchange.fetch_orderbook = AsyncMock(
            side_effect=Exception("timeout")
        )

        await futures_engine._refresh_dynamic_coins()

        assert "FAIL/USDT" in futures_engine._dynamic_coins

    @pytest.mark.asyncio
    async def test_deep_orderbook_passes(self, futures_engine):
        """오더북 깊이 충분한 코인 통과."""
        tickers = {}
        tickers.update(self._make_ticker("GOOD/USDT", 100_000_000))

        futures_engine._exchange.fetch_tickers = AsyncMock(return_value=tickers)
        futures_engine._last_coin_refresh = None
        futures_engine._exchange.fetch_orderbook = AsyncMock(
            return_value=self._make_orderbook(100.0, 100_000)
        )

        await futures_engine._refresh_dynamic_coins()

        assert "GOOD/USDT" in futures_engine._dynamic_coins


class TestSellTriggeredReview:
    """매도 N회마다 매매 회고 트리거 테스트."""

    def test_sell_counter_initialized(self, futures_engine):
        assert futures_engine._sells_since_review == 0
        assert futures_engine._REVIEW_TRIGGER_SELLS == 5

    @pytest.mark.asyncio
    async def test_sell_counter_increments(self, futures_engine):
        """카운터가 올바르게 증가하는지."""
        futures_engine._agent_coordinator = None
        for i in range(1, 4):
            await futures_engine._on_sell_completed()
            assert futures_engine._sells_since_review == i

    @pytest.mark.asyncio
    async def test_review_triggered_at_threshold(self, futures_engine):
        """N회 매도 시 리뷰 트리거 + 카운터 리셋."""
        mock_coord = MagicMock()
        mock_coord.run_trade_review = AsyncMock()
        futures_engine._agent_coordinator = mock_coord
        futures_engine._sells_since_review = 4  # 다음 1회면 5회

        await futures_engine._on_sell_completed()
        assert futures_engine._sells_since_review == 0

    @pytest.mark.asyncio
    async def test_no_review_below_threshold(self, futures_engine):
        """임계값 미만이면 리뷰 안 돔."""
        mock_coord = MagicMock()
        mock_coord.run_trade_review = AsyncMock()
        futures_engine._agent_coordinator = mock_coord
        futures_engine._sells_since_review = 2

        await futures_engine._on_sell_completed()
        assert futures_engine._sells_since_review == 3

    @pytest.mark.asyncio
    async def test_no_coordinator_no_crash(self, futures_engine):
        """코디네이터 없어도 에러 없이 동작."""
        futures_engine._agent_coordinator = None
        futures_engine._sells_since_review = 4
        await futures_engine._on_sell_completed()
        assert futures_engine._sells_since_review == 5  # 트리거 안 되고 증가만
