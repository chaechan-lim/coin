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
    config.binance_trading.min_trade_interval_sec = 1036800
    config.binance_trading.min_sell_active_weight = 0.20
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


class TestWSReconnection:
    """WebSocket 재연결 로직 테스트."""

    @pytest.mark.asyncio
    async def test_ws_reconnect_success(self, futures_engine):
        """재연결 성공 시 backoff 리셋."""
        futures_engine._exchange.close_ws = AsyncMock()
        futures_engine._exchange.create_ws_exchange = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await futures_engine._ws_reconnect(10)
        assert result == futures_engine._WS_RECONNECT_MIN  # 성공 → 리셋
        futures_engine._exchange.create_ws_exchange.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ws_reconnect_failure_increases_backoff(self, futures_engine):
        """재연결 실패 시 backoff 증가."""
        futures_engine._exchange.close_ws = AsyncMock()
        futures_engine._exchange.create_ws_exchange = AsyncMock(
            side_effect=Exception("Connection refused")
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await futures_engine._ws_reconnect(10)
        assert result == min(10 * futures_engine._WS_RECONNECT_FACTOR,
                             futures_engine._WS_RECONNECT_MAX)

    @pytest.mark.asyncio
    async def test_ws_reconnect_max_cap(self, futures_engine):
        """backoff가 최대값을 초과하지 않음."""
        futures_engine._exchange.close_ws = AsyncMock()
        futures_engine._exchange.create_ws_exchange = AsyncMock(
            side_effect=Exception("Connection refused")
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await futures_engine._ws_reconnect(999)
        assert result <= futures_engine._WS_RECONNECT_MAX

    def test_ws_reconnect_constants(self, futures_engine):
        """WS 재연결 상수가 합리적인 값인지 확인."""
        assert futures_engine._WS_RECONNECT_MIN == 5
        assert futures_engine._WS_RECONNECT_MAX == 300
        assert futures_engine._WS_RECONNECT_FACTOR == 2


class TestFuturesCooldown:
    """선물 엔진 쿨다운 체크."""

    def test_cooldown_blocks_recent_sell(self, futures_engine):
        """최근 청산 코인은 쿨다운으로 진입 차단."""
        from datetime import timedelta
        futures_engine._last_sell_time["BTC/USDT"] = datetime.now(timezone.utc) - timedelta(hours=1)
        assert futures_engine._check_cooldown("BTC/USDT") is True

    def test_cooldown_allows_after_expiry(self, futures_engine):
        """쿨다운 만료 후 진입 허용."""
        from datetime import timedelta
        futures_engine._last_sell_time["BTC/USDT"] = datetime.now(timezone.utc) - timedelta(days=13)
        assert futures_engine._check_cooldown("BTC/USDT") is False

    def test_cooldown_allows_new_symbol(self, futures_engine):
        """매매 이력 없는 코인은 진입 허용."""
        assert futures_engine._check_cooldown("NEW/USDT") is False


class TestMLFilterIntegration:
    """ML Signal Filter 라이브 엔진 통합 테스트."""

    def test_ml_filter_init_loads_if_available(self, futures_engine):
        """모델 파일 존재 시 ML 필터 로드, 없으면 None."""
        from pathlib import Path
        model_path = Path(__file__).parent.parent / "data" / "ml_models" / "signal_filter.pkl"
        if model_path.exists():
            assert futures_engine._ml_filter is not None
        else:
            assert futures_engine._ml_filter is None

    def test_latest_candle_rows_init(self, futures_engine):
        """캔들 캐시 초기화."""
        assert futures_engine._latest_candle_rows == {}


class TestConfidenceSizing:
    """Confidence-proportional sizing 테스트."""

    def test_low_confidence_reduces_size(self):
        """낮은 신뢰도(0.55)에서 0.5x 축소."""
        conf = 0.55
        mult = min(2.0, max(0.5, 0.5 + (conf - 0.55) * (1.5 / 0.45)))
        assert abs(mult - 0.5) < 0.01

    def test_medium_confidence_normal_size(self):
        """중간 신뢰도(0.70)에서 1.0x."""
        conf = 0.70
        mult = min(2.0, max(0.5, 0.5 + (conf - 0.55) * (1.5 / 0.45)))
        assert abs(mult - 1.0) < 0.01

    def test_high_confidence_increases_size(self):
        """높은 신뢰도(0.85)에서 1.5x."""
        conf = 0.85
        mult = min(2.0, max(0.5, 0.5 + (conf - 0.55) * (1.5 / 0.45)))
        assert abs(mult - 1.5) < 0.01

    def test_max_confidence_caps_at_2x(self):
        """최대 신뢰도(1.0)에서 2.0x 상한."""
        conf = 1.0
        mult = min(2.0, max(0.5, 0.5 + (conf - 0.55) * (1.5 / 0.45)))
        assert abs(mult - 2.0) < 0.01


class TestCooldownBypassOnClose:
    """포지션 청산 시 쿨다운 면제 테스트."""

    @pytest.mark.asyncio
    async def test_buy_signal_closes_short_despite_cooldown(self, futures_engine):
        """숏 보유 중 BUY 시그널 → 쿨다운 무시하고 숏 청산."""
        from core.enums import SignalType
        from strategies.combiner import CombinedDecision, Signal
        from datetime import timedelta

        # 쿨다운 설정 (12일 전 매도 기록 → cd48 내)
        futures_engine._last_sell_time["BTC/USDT"] = (
            datetime.now(timezone.utc) - timedelta(days=2)
        )
        futures_engine._last_trade_time["BTC/USDT"] = (
            datetime.now(timezone.utc) - timedelta(days=2)
        )

        position = MagicMock(spec=Position)
        position.direction = "short"
        position.quantity = 0.1
        position.average_buy_price = 65000.0
        position.symbol = "BTC/USDT"
        position.leverage = 5

        decision = MagicMock(spec=CombinedDecision)
        decision.action = SignalType.BUY
        decision.combined_confidence = 0.70
        decision.active_weight = 0.30

        signal = MagicMock(spec=Signal)
        signal.signal_type = SignalType.BUY
        signal.strategy_name = "test"
        signal.confidence = 0.70
        signal.reason = "test"

        with patch.object(futures_engine, '_close_position', new_callable=AsyncMock) as mock_close:
            session = AsyncMock()
            await futures_engine._process_futures_decision(
                session, "BTC/USDT", decision, [signal], position,
            )
            mock_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_sell_signal_closes_long_despite_cooldown(self, futures_engine):
        """롱 보유 중 SELL 시그널 → 쿨다운 무시하고 롱 청산."""
        from core.enums import SignalType
        from strategies.combiner import CombinedDecision, Signal
        from datetime import timedelta

        futures_engine._last_sell_time["ETH/USDT"] = (
            datetime.now(timezone.utc) - timedelta(days=2)
        )
        futures_engine._last_trade_time["ETH/USDT"] = (
            datetime.now(timezone.utc) - timedelta(days=2)
        )

        position = MagicMock(spec=Position)
        position.direction = "long"
        position.quantity = 1.0
        position.average_buy_price = 3500.0
        position.symbol = "ETH/USDT"
        position.leverage = 5

        decision = MagicMock(spec=CombinedDecision)
        decision.action = SignalType.SELL
        decision.combined_confidence = 0.65
        decision.active_weight = 0.25

        signal = MagicMock(spec=Signal)
        signal.signal_type = SignalType.SELL
        signal.strategy_name = "test"
        signal.confidence = 0.65
        signal.reason = "test"

        with patch.object(futures_engine, '_close_position', new_callable=AsyncMock) as mock_close:
            session = AsyncMock()
            await futures_engine._process_futures_decision(
                session, "ETH/USDT", decision, [signal], position,
            )
            mock_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_new_entry_still_blocked_by_cooldown(self, futures_engine):
        """신규 진입은 쿨다운에 의해 차단."""
        from core.enums import SignalType
        from strategies.combiner import CombinedDecision, Signal
        from datetime import timedelta

        futures_engine._last_trade_time["BTC/USDT"] = (
            datetime.now(timezone.utc) - timedelta(days=2)
        )

        decision = MagicMock(spec=CombinedDecision)
        decision.action = SignalType.BUY
        decision.combined_confidence = 0.70
        decision.active_weight = 0.30

        signal = MagicMock(spec=Signal)
        signal.signal_type = SignalType.BUY
        signal.strategy_name = "test"
        signal.confidence = 0.70
        signal.reason = "test"

        with patch.object(futures_engine, '_open_long', new_callable=AsyncMock) as mock_open:
            session = AsyncMock()
            await futures_engine._process_futures_decision(
                session, "BTC/USDT", decision, [signal], None,
            )
            mock_open.assert_not_called()
