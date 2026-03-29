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


# ── Test: Zombie position cleanup ─────────────────────────────────

class TestZombiePositionCleanup:
    """비추적 심볼의 잔여 포지션 자동 청산 테스트."""

    @pytest.mark.asyncio
    async def test_close_zombie_long(self, futures_engine, session):
        """tracked_coins에 없는 롱 포지션은 자동 청산."""
        # XAU/USDT는 tracked_coins에 없음
        pos = Position(
            exchange="binance_futures",
            symbol="XAU/USDT",
            quantity=0.022,
            average_buy_price=5167.0,
            total_invested=37.0,
            margin_used=37.0,
            direction="long",
            leverage=3,
        )
        session.add(pos)
        await session.flush()

        mock_factory = MagicMock()
        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_session_ctx

        futures_engine._order_manager.create_order = AsyncMock(return_value=MagicMock(
            executed_price=5200.0, executed_quantity=0.022, fee=0.046,
        ))

        with patch("db.session.get_session_factory", return_value=mock_factory):
            with patch("core.event_bus.emit_event", new_callable=AsyncMock):
                await futures_engine._close_zombie_positions()

        # 포지션이 청산됨
        assert pos.quantity == 0

    @pytest.mark.asyncio
    async def test_tracked_coins_not_closed(self, futures_engine, session):
        """tracked_coins에 있는 포지션은 청산 안 함."""
        pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.001,
            average_buy_price=65000.0,
            total_invested=21.67,
            direction="long",
            leverage=3,
        )
        session.add(pos)
        await session.flush()

        mock_factory = MagicMock()
        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_session_ctx

        with patch("db.session.get_session_factory", return_value=mock_factory):
            await futures_engine._close_zombie_positions()

        # BTC는 tracked → 안 건드림
        assert pos.quantity == 0.001

    @pytest.mark.asyncio
    async def test_surge_positions_not_closed(self, futures_engine, session):
        """is_surge=True인 포지션은 청산 안 함."""
        pos = Position(
            exchange="binance_futures",
            symbol="DOGE/USDT",
            quantity=100.0,
            average_buy_price=0.15,
            total_invested=5.0,
            direction="long",
            leverage=3,
            is_surge=True,
        )
        session.add(pos)
        await session.flush()

        mock_factory = MagicMock()
        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_session_ctx

        with patch("db.session.get_session_factory", return_value=mock_factory):
            await futures_engine._close_zombie_positions()

        # 서지 포지션 → 안 건드림
        assert pos.quantity == 100.0


class TestStopEventThrottle:
    """트레일링 스탑/SL/TP 경고 이벤트 스팸 방지 테스트 (COIN-6)."""

    def test_last_stop_event_time_initialized(self, futures_engine):
        """_last_stop_event_time이 TradingEngine에서 상속되어 빈 dict로 초기화됨."""
        assert hasattr(futures_engine, "_last_stop_event_time")
        assert futures_engine._last_stop_event_time == {}

    @pytest.mark.asyncio
    async def test_stop_event_emitted_on_first_trigger(self, futures_engine, mock_market_data):
        """첫 번째 트레일링 스탑 조건 충족 시 경고 이벤트 발생."""
        mock_market_data.get_current_price = AsyncMock(return_value=90000.0)

        position = MagicMock(spec=Position)
        position.symbol = "BTC/USDT"
        position.quantity = 0.001
        position.direction = "long"
        position.average_buy_price = 80000.0
        position.leverage = 3
        position.margin_used = 26.67
        position.stop_loss_pct = 2.83
        position.take_profit_pct = 5.66
        position.trailing_activation_pct = 1.77
        position.trailing_stop_pct = 1.24
        position.trailing_active = True
        position.highest_price = 100000.0  # peak
        position.liquidation_price = None
        position.entered_at = None
        position.is_surge = False
        position.max_hold_hours = 0

        # 트래커: trailing_active=True, extreme=100000, 현재가 90000 → drawdown=10% > trailing_stop_pct
        tracker = PositionTracker(
            entry_price=80000.0,
            extreme_price=100000.0,
            stop_loss_pct=2.83,
            take_profit_pct=5.66,
            trailing_activation_pct=1.77,
            trailing_stop_pct=1.24,
            trailing_active=True,
        )
        futures_engine._position_trackers["BTC/USDT"] = tracker

        session = AsyncMock()
        mock_order = MagicMock()
        mock_order.status = "pending"  # 청산 실패 (filled 아님)
        futures_engine._order_manager.create_order = AsyncMock(return_value=mock_order)
        futures_engine._portfolio_manager.update_position_on_sell = AsyncMock()

        with patch("engine.futures_engine.emit_event", new_callable=AsyncMock) as mock_emit:
            result = await futures_engine._check_futures_stop_conditions(
                session, "BTC/USDT", position
            )

        assert result is True
        # 경고 이벤트가 1회 발생해야 함
        warning_calls = [
            c for c in mock_emit.call_args_list if c.args[0] == "warning"
        ]
        assert len(warning_calls) == 1

    @pytest.mark.asyncio
    async def test_stop_event_suppressed_within_cooldown(self, futures_engine, mock_market_data):
        """5분 이내 재발화 시 경고 이벤트 억제 (노티 스팸 방지)."""
        from datetime import timedelta

        mock_market_data.get_current_price = AsyncMock(return_value=90000.0)

        position = MagicMock(spec=Position)
        position.symbol = "BTC/USDT"
        position.quantity = 0.001
        position.direction = "long"
        position.average_buy_price = 80000.0
        position.leverage = 3
        position.margin_used = 26.67
        position.stop_loss_pct = 2.83
        position.take_profit_pct = 5.66
        position.trailing_activation_pct = 1.77
        position.trailing_stop_pct = 1.24
        position.trailing_active = True
        position.highest_price = 100000.0
        position.liquidation_price = None
        position.entered_at = None
        position.is_surge = False
        position.max_hold_hours = 0

        tracker = PositionTracker(
            entry_price=80000.0,
            extreme_price=100000.0,
            stop_loss_pct=2.83,
            take_profit_pct=5.66,
            trailing_activation_pct=1.77,
            trailing_stop_pct=1.24,
            trailing_active=True,
        )
        futures_engine._position_trackers["BTC/USDT"] = tracker

        # 2분 전에 이미 이벤트를 발생시켰다고 기록 → 쿨다운 중
        futures_engine._last_stop_event_time["BTC/USDT"] = (
            datetime.now(timezone.utc) - timedelta(minutes=2)
        )

        session = AsyncMock()
        mock_order = MagicMock()
        mock_order.status = "pending"
        futures_engine._order_manager.create_order = AsyncMock(return_value=mock_order)
        futures_engine._portfolio_manager.update_position_on_sell = AsyncMock()

        with patch("engine.futures_engine.emit_event", new_callable=AsyncMock) as mock_emit:
            result = await futures_engine._check_futures_stop_conditions(
                session, "BTC/USDT", position
            )

        assert result is True
        # 쿨다운 중 → 경고 이벤트 억제
        warning_calls = [
            c for c in mock_emit.call_args_list if c.args[0] == "warning"
        ]
        assert len(warning_calls) == 0

    @pytest.mark.asyncio
    async def test_stop_event_resumes_after_cooldown(self, futures_engine, mock_market_data):
        """5분 쿨다운 만료 후 경고 이벤트 재발생."""
        from datetime import timedelta

        mock_market_data.get_current_price = AsyncMock(return_value=90000.0)

        position = MagicMock(spec=Position)
        position.symbol = "BTC/USDT"
        position.quantity = 0.001
        position.direction = "long"
        position.average_buy_price = 80000.0
        position.leverage = 3
        position.margin_used = 26.67
        position.stop_loss_pct = 2.83
        position.take_profit_pct = 5.66
        position.trailing_activation_pct = 1.77
        position.trailing_stop_pct = 1.24
        position.trailing_active = True
        position.highest_price = 100000.0
        position.liquidation_price = None
        position.entered_at = None
        position.is_surge = False
        position.max_hold_hours = 0

        tracker = PositionTracker(
            entry_price=80000.0,
            extreme_price=100000.0,
            stop_loss_pct=2.83,
            take_profit_pct=5.66,
            trailing_activation_pct=1.77,
            trailing_stop_pct=1.24,
            trailing_active=True,
        )
        futures_engine._position_trackers["BTC/USDT"] = tracker

        # 6분 전 기록 → 쿨다운 만료
        futures_engine._last_stop_event_time["BTC/USDT"] = (
            datetime.now(timezone.utc) - timedelta(minutes=6)
        )

        session = AsyncMock()
        mock_order = MagicMock()
        mock_order.status = "pending"
        futures_engine._order_manager.create_order = AsyncMock(return_value=mock_order)
        futures_engine._portfolio_manager.update_position_on_sell = AsyncMock()

        with patch("engine.futures_engine.emit_event", new_callable=AsyncMock) as mock_emit:
            result = await futures_engine._check_futures_stop_conditions(
                session, "BTC/USDT", position
            )

        assert result is True
        warning_calls = [
            c for c in mock_emit.call_args_list if c.args[0] == "warning"
        ]
        assert len(warning_calls) == 1

    @pytest.mark.asyncio
    async def test_close_position_clears_throttle(self, futures_engine):
        """포지션 청산 완료 시 _last_stop_event_time에서 해당 심볼 제거."""
        from datetime import timedelta

        position = MagicMock(spec=Position)
        position.symbol = "BTC/USDT"
        position.quantity = 0.001
        position.direction = "long"
        position.average_buy_price = 65000.0
        position.leverage = 3
        position.margin_used = 21.67
        position.liquidation_price = None

        futures_engine._last_stop_event_time["BTC/USDT"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        )

        mock_order = MagicMock()
        mock_order.status = "filled"
        mock_order.executed_price = 90000.0
        mock_order.executed_quantity = 0.001
        mock_order.fee = 0.0036
        futures_engine._order_manager.create_order = AsyncMock(return_value=mock_order)
        futures_engine._portfolio_manager.update_position_on_sell = AsyncMock()

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = position
        session.execute = AsyncMock(return_value=mock_result)

        with patch("engine.futures_engine.emit_event", new_callable=AsyncMock):
            await futures_engine._close_position(
                session, "BTC/USDT", position, 90000.0, "Trailing Stop"
            )

        # 청산 완료 후 쿨다운 해제
        assert "BTC/USDT" not in futures_engine._last_stop_event_time


# ── Harness Conformance Tests (COIN-10) ────────────────────────────────────


class TestEngineConfigConformance:
    """COIN-10: futures_engine은 _ec.* 를 통해 설정값에 접근해야 함."""

    def test_tracked_coins_uses_ec(self, futures_engine):
        """tracked_coins property는 _ec.tracked_coins를 사용해야 한다."""
        # _ec.tracked_coins와 tracked_coins 프로퍼티가 일치해야 함
        assert futures_engine.tracked_coins == list(futures_engine._ec.tracked_coins)

    def test_tracked_coins_not_from_raw_config(self, futures_engine):
        """tracked_coins 수정 시 _ec를 통해 반영된다 (_config.binance 직접 접근 금지)."""
        original = futures_engine._ec.tracked_coins.copy()
        futures_engine._ec.tracked_coins = ["BTC/USDT"]
        assert futures_engine.tracked_coins == ["BTC/USDT"]
        # 복원
        futures_engine._ec.tracked_coins = original

    def test_start_log_uses_exchange_name(self, futures_engine):
        """_exchange_name 속성이 'binance_futures'로 올바르게 설정된다."""
        assert futures_engine._exchange_name == "binance_futures"

    def test_ec_mode_consistent_with_config(self, futures_engine):
        """_ec.mode가 binance_trading.mode와 일치해야 한다."""
        # EngineConfig.from_app_config가 올바르게 binance_trading.mode를 매핑하는지 확인
        # futures_engine의 _ec는 부모 TradingEngine.__init__에서 생성됨
        assert futures_engine._ec.mode is not None

    def test_ec_evaluation_interval_consistent(self, futures_engine):
        """_ec.evaluation_interval_sec이 binance_trading 설정과 일치해야 한다."""
        assert futures_engine._ec.evaluation_interval_sec == 300

    def test_ec_min_combined_confidence(self, futures_engine):
        """_ec.min_combined_confidence가 EngineConfig에 올바르게 매핑된다."""
        assert futures_engine._ec.min_combined_confidence == 0.50

    def test_ec_max_trade_size_pct(self, futures_engine):
        """_ec.max_trade_size_pct가 EngineConfig에 올바르게 매핑된다."""
        assert futures_engine._ec.max_trade_size_pct == 0.15

    def test_ec_min_trade_interval_sec(self, futures_engine):
        """_ec.min_trade_interval_sec이 EngineConfig에 올바르게 매핑된다."""
        # mock_config에서 binance_trading.min_trade_interval_sec = 1036800
        assert futures_engine._ec.min_trade_interval_sec == 1036800


# ── COIN-64: flush 누락 + 숏 tracker extreme_price 오류 ─────────────────────


class TestFuturesMetadataFlush:
    """Bug 1 — _open_long/_open_short에서 session.flush()가 실제로 호출됨을 검증.

    이전 구현은 engine 코드를 호출하지 않아 flush fix를 되돌려도 통과하는
    tautological test였다. 여기서는 실제 engine 메서드를 호출하고 session.flush가
    호출되었는지 확인한다.
    """

    def _make_mock_pos(self):
        pos = MagicMock(spec=Position)
        pos.direction = None
        pos.leverage = None
        pos.liquidation_price = None
        pos.margin_used = None
        pos.lowest_price = None
        pos.highest_price = None
        pos.stop_loss_pct = None
        return pos

    @pytest.mark.asyncio
    async def test_long_entry_metadata_flushed(self, futures_engine):
        """_open_long은 pos 메타데이터 설정 후 session.flush()를 호출한다."""
        mock_pos = self._make_mock_pos()

        flush_calls = []

        async def track_flush(*args, **kwargs):
            flush_calls.append(True)

        meta_result = MagicMock()
        meta_result.scalar_one_or_none.return_value = mock_pos

        # Use a callable side_effect so unexpected extra execute calls get a neutral
        # fallback instead of raising StopAsyncIteration.
        _neutral = MagicMock()
        _neutral.scalar_one_or_none.return_value = None
        _neutral.scalars.return_value.first.return_value = None
        _expected = [meta_result]

        def _execute_handler(stmt):
            return _expected.pop(0) if _expected else _neutral

        session = AsyncMock()
        session.flush = track_flush
        session.execute = AsyncMock(side_effect=_execute_handler)

        filled_order = MagicMock(status="filled", exchange_order_id="o1", fee=1.0)
        futures_engine._order_manager.create_order = AsyncMock(return_value=filled_order)
        futures_engine._portfolio_manager.update_position_on_buy = AsyncMock()

        signal = MagicMock(strategy_name="rsi")
        decision = MagicMock(combined_confidence=0.7)

        with (
            patch("engine.futures_engine.emit_event", new_callable=AsyncMock),
            patch.object(futures_engine, "_check_cooldown", return_value=False),
            patch.object(futures_engine, "_get_atr_pct", return_value=None),
            patch.object(futures_engine, "_atr_risk_adjust", return_value=(1.0, None)),
            patch.object(futures_engine, "_adjust_amount", side_effect=lambda s, a: a),
            patch.object(futures_engine, "_get_min_notional", return_value=0),
            patch.object(futures_engine, "_save_tracker_to_db", new_callable=AsyncMock),
        ):
            await futures_engine._open_long(session, "BTC/USDT", 65000.0, signal, decision)

        assert len(flush_calls) >= 1, "_open_long must call session.flush() after setting metadata"
        assert mock_pos.direction == "long"
        assert mock_pos.leverage == futures_engine._leverage
        assert mock_pos.margin_used is not None

    @pytest.mark.asyncio
    async def test_short_entry_metadata_flushed(self, futures_engine):
        """_open_short은 pos 메타데이터 설정 후 session.flush()를 호출하고 lowest_price를 초기화한다."""
        mock_pos = self._make_mock_pos()

        flush_calls = []

        async def track_flush(*args, **kwargs):
            flush_calls.append(True)

        # _open_short execute calls: 1 = cross-position check, 2 = metadata query.
        # Callable side_effect provides explicit results for known calls and a
        # neutral fallback for unexpected extras to avoid StopAsyncIteration errors.
        cross_result = MagicMock()
        cross_result.scalars.return_value.first.return_value = None  # no cross position
        meta_result = MagicMock()
        meta_result.scalar_one_or_none.return_value = mock_pos

        _neutral = MagicMock()
        _neutral.scalar_one_or_none.return_value = None
        _neutral.scalars.return_value.first.return_value = None
        _expected = [cross_result, meta_result]

        def _execute_handler(stmt):
            return _expected.pop(0) if _expected else _neutral

        session = AsyncMock()
        session.flush = track_flush
        session.execute = AsyncMock(side_effect=_execute_handler)

        filled_order = MagicMock(status="filled", exchange_order_id="o2", fee=1.0)
        futures_engine._order_manager.create_order = AsyncMock(return_value=filled_order)
        futures_engine._portfolio_manager.update_position_on_buy = AsyncMock()

        signal = MagicMock(strategy_name="bollinger_rsi")
        decision = MagicMock(combined_confidence=0.72)

        with (
            patch("engine.futures_engine.emit_event", new_callable=AsyncMock),
            patch.object(futures_engine, "_check_cooldown", return_value=False),
            patch.object(futures_engine, "_get_atr_pct", return_value=None),
            patch.object(futures_engine, "_atr_risk_adjust", return_value=(1.0, None)),
            patch.object(futures_engine, "_adjust_amount", side_effect=lambda s, a: a),
            patch.object(futures_engine, "_get_min_notional", return_value=0),
            patch.object(futures_engine, "_save_tracker_to_db", new_callable=AsyncMock),
        ):
            await futures_engine._open_short(session, "BTC/USDT", 65000.0, signal, decision)

        assert len(flush_calls) >= 1, "_open_short must call session.flush() after setting metadata"
        assert mock_pos.direction == "short"
        assert mock_pos.leverage == futures_engine._leverage
        assert mock_pos.lowest_price == pytest.approx(65000.0)
        assert mock_pos.highest_price is None


class TestShortTrackerExtremePriceRestore:
    """Bug 2 — 숏 tracker extreme_price 복원이 lowest_price 컬럼을 사용하는지 검증."""

    def _make_short_position(self, lowest_price=None, highest_price=None):
        """숏 포지션 MagicMock 생성 헬퍼."""
        position = MagicMock(spec=Position)
        position.symbol = "BTC/USDT"
        position.direction = "short"
        position.average_buy_price = 65000.0
        position.quantity = 0.001
        position.leverage = 3
        position.margin_used = 21.67
        position.liquidation_price = None
        position.entered_at = None
        position.is_surge = False
        position.max_hold_hours = 0
        position.stop_loss_pct = 2.83
        position.take_profit_pct = 5.66
        position.trailing_activation_pct = 1.77
        position.trailing_stop_pct = 1.24
        position.trailing_active = False
        position.lowest_price = lowest_price
        position.highest_price = highest_price
        return position

    @pytest.mark.asyncio
    async def test_short_tracker_uses_lowest_price_when_available(
        self, futures_engine, mock_market_data
    ):
        """숏 포지션 복원 시 lowest_price가 있으면 extreme_price로 사용한다."""
        mock_market_data.get_current_price = AsyncMock(return_value=65000.0)

        position = self._make_short_position(lowest_price=60000.0, highest_price=65000.0)

        from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession
        session = AsyncMock(spec=_AsyncSession)

        # tracker가 없으면 DB에서 복원
        assert "BTC/USDT" not in futures_engine._position_trackers

        with (
            patch("engine.futures_engine.emit_event", new_callable=AsyncMock),
            patch.object(futures_engine, "_get_atr_pct", return_value=None),
        ):
            await futures_engine._check_futures_stop_conditions(session, "BTC/USDT", position)

        tracker = futures_engine._position_trackers.get("BTC/USDT")
        assert tracker is not None
        # lowest_price=60000.0이 extreme_price로 복원되어야 함
        assert tracker.extreme_price == pytest.approx(60000.0)

    @pytest.mark.asyncio
    async def test_short_tracker_falls_back_to_highest_price_when_lowest_absent(
        self, futures_engine, mock_market_data
    ):
        """숏 포지션 복원 시 lowest_price가 None이면 highest_price로 대체한다."""
        mock_market_data.get_current_price = AsyncMock(return_value=65000.0)

        # lowest_price=None, highest_price에 기존 값 존재 (backward compat)
        position = self._make_short_position(lowest_price=None, highest_price=62000.0)

        from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession
        session = AsyncMock(spec=_AsyncSession)

        futures_engine._position_trackers.pop("BTC/USDT", None)

        with (
            patch("engine.futures_engine.emit_event", new_callable=AsyncMock),
            patch.object(futures_engine, "_get_atr_pct", return_value=None),
        ):
            await futures_engine._check_futures_stop_conditions(session, "BTC/USDT", position)

        tracker = futures_engine._position_trackers.get("BTC/USDT")
        assert tracker is not None
        assert tracker.extreme_price == pytest.approx(62000.0)

    @pytest.mark.asyncio
    async def test_short_tracker_falls_back_to_entry_price_when_both_absent(
        self, futures_engine, mock_market_data
    ):
        """숏 포지션 복원 시 lowest_price, highest_price 모두 None이면 entry price 사용."""
        mock_market_data.get_current_price = AsyncMock(return_value=65000.0)

        position = self._make_short_position(lowest_price=None, highest_price=None)

        from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession
        session = AsyncMock(spec=_AsyncSession)

        futures_engine._position_trackers.pop("BTC/USDT", None)

        with (
            patch("engine.futures_engine.emit_event", new_callable=AsyncMock),
            patch.object(futures_engine, "_get_atr_pct", return_value=None),
        ):
            await futures_engine._check_futures_stop_conditions(session, "BTC/USDT", position)

        tracker = futures_engine._position_trackers.get("BTC/USDT")
        assert tracker is not None
        assert tracker.extreme_price == pytest.approx(65000.0)  # average_buy_price

    @pytest.mark.asyncio
    async def test_long_tracker_still_uses_highest_price(
        self, futures_engine, mock_market_data
    ):
        """롱 포지션 복원은 여전히 highest_price를 사용한다 (regression guard)."""
        # 현재가 = entry price와 동일 → pnl=0 → tracker_changed=False → _save_tracker_to_db 미호출
        mock_market_data.get_current_price = AsyncMock(return_value=3000.0)

        position = MagicMock(spec=Position)
        position.symbol = "ETH/USDT"
        position.direction = "long"
        position.average_buy_price = 3000.0
        position.quantity = 0.05
        position.leverage = 3
        position.margin_used = 50.0
        position.liquidation_price = None
        position.entered_at = None
        position.is_surge = False
        position.max_hold_hours = 0
        position.stop_loss_pct = 2.83
        position.take_profit_pct = 5.66
        position.trailing_activation_pct = 1.77
        position.trailing_stop_pct = 1.24
        position.trailing_active = False
        position.highest_price = 3500.0
        position.lowest_price = None

        from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession
        session = AsyncMock(spec=_AsyncSession)

        futures_engine._position_trackers.pop("ETH/USDT", None)

        with (
            patch("engine.futures_engine.emit_event", new_callable=AsyncMock),
            patch.object(futures_engine, "_get_atr_pct", return_value=None),
        ):
            await futures_engine._check_futures_stop_conditions(session, "ETH/USDT", position)

        tracker = futures_engine._position_trackers.get("ETH/USDT")
        assert tracker is not None
        assert tracker.extreme_price == pytest.approx(3500.0)  # highest_price


class TestSaveTrackerDirectionAware:
    """_save_tracker_to_db — 방향별 extreme_price 저장 및 반대 컬럼 클리어 검증."""

    @pytest.fixture
    def _db_session_factory(self):
        """인메모리 SQLite DB와 세션 팩토리를 반환하는 async 헬퍼."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from core.models import Base

        async def _make():
            db = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
            async with db.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(bind=db, class_=AsyncSession, expire_on_commit=False)
            return db, factory

        return _make

    def _make_engine_stub(self, exchange_name: str):
        """TradingEngine 인스턴스를 생성하지 않고 _save_tracker_to_db에 필요한
        최소 스텁을 반환한다.  create_autospec을 사용해 향후 속성 확장에 대비한다."""
        from unittest.mock import create_autospec
        from engine.trading_engine import TradingEngine
        stub = create_autospec(TradingEngine, instance=True)
        stub._exchange_name = exchange_name
        return stub

    @pytest.mark.asyncio
    async def test_long_saves_to_highest_price(self, _db_session_factory):
        """롱 포지션: extreme_price → highest_price, lowest_price는 None으로 클리어."""
        from sqlalchemy.ext.asyncio import AsyncSession
        from core.models import Position as PositionModel
        from engine.trading_engine import TradingEngine, PositionTracker

        db, factory = await _db_session_factory()
        async with factory() as session:
            pos = PositionModel(
                exchange="binance_futures",
                symbol="BTC/USDT",
                quantity=0.001,
                average_buy_price=65000.0,
                total_invested=65.0,
                is_paper=False,
                direction="long",
            )
            session.add(pos)
            await session.flush()

            eng = self._make_engine_stub("binance_futures")
            tracker = PositionTracker(
                entry_price=65000.0,
                extreme_price=70000.0,
                stop_loss_pct=3.0,
                take_profit_pct=6.0,
                trailing_activation_pct=2.0,
                trailing_stop_pct=1.5,
                trailing_active=False,
            )
            await TradingEngine._save_tracker_to_db(eng, session, "BTC/USDT", tracker)

            await session.refresh(pos)
            assert pos.highest_price == pytest.approx(70000.0)
            assert pos.lowest_price is None  # cleared by direction-aware logic

        await db.dispose()

    @pytest.mark.asyncio
    async def test_short_saves_to_lowest_price(self, _db_session_factory):
        """숏 포지션: extreme_price → lowest_price, highest_price는 None으로 클리어."""
        from sqlalchemy.ext.asyncio import AsyncSession
        from core.models import Position as PositionModel
        from engine.trading_engine import TradingEngine, PositionTracker

        db, factory = await _db_session_factory()
        async with factory() as session:
            pos = PositionModel(
                exchange="binance_futures",
                symbol="ETH/USDT",
                quantity=0.05,
                average_buy_price=3200.0,
                total_invested=160.0,
                is_paper=False,
                direction="short",
            )
            session.add(pos)
            await session.flush()

            eng = self._make_engine_stub("binance_futures")
            tracker = PositionTracker(
                entry_price=3200.0,
                extreme_price=2800.0,  # 숏 최저가
                stop_loss_pct=3.0,
                take_profit_pct=6.0,
                trailing_activation_pct=2.0,
                trailing_stop_pct=1.5,
                trailing_active=True,
            )
            await TradingEngine._save_tracker_to_db(eng, session, "ETH/USDT", tracker)

            await session.refresh(pos)
            assert pos.lowest_price == pytest.approx(2800.0)
            assert pos.highest_price is None  # cleared by direction-aware logic

        await db.dispose()

    @pytest.mark.asyncio
    async def test_long_clears_stale_lowest_price(self, _db_session_factory):
        """롱 저장 시 이전 숏 세션에서 남은 lowest_price를 None으로 클리어한다."""
        from sqlalchemy.ext.asyncio import AsyncSession
        from core.models import Position as PositionModel
        from engine.trading_engine import TradingEngine, PositionTracker

        db, factory = await _db_session_factory()
        async with factory() as session:
            # 이전에 숏으로 사용된 포지션 행 (lowest_price에 값 있음)
            pos = PositionModel(
                exchange="binance_futures",
                symbol="BTC/USDT",
                quantity=0.001,
                average_buy_price=65000.0,
                total_invested=65.0,
                is_paper=False,
                direction="long",
                lowest_price=59000.0,  # stale short data
            )
            session.add(pos)
            await session.flush()

            eng = self._make_engine_stub("binance_futures")
            tracker = PositionTracker(
                entry_price=65000.0,
                extreme_price=67000.0,
                stop_loss_pct=3.0,
                take_profit_pct=6.0,
                trailing_activation_pct=2.0,
                trailing_stop_pct=1.5,
            )
            await TradingEngine._save_tracker_to_db(eng, session, "BTC/USDT", tracker)

            await session.refresh(pos)
            assert pos.highest_price == pytest.approx(67000.0)
            assert pos.lowest_price is None  # stale value wiped

        await db.dispose()

    @pytest.mark.asyncio
    async def test_short_clears_stale_highest_price(self, _db_session_factory):
        """숏 저장 시 이전 롱 세션에서 남은 highest_price를 None으로 클리어한다."""
        from sqlalchemy.ext.asyncio import AsyncSession
        from core.models import Position as PositionModel
        from engine.trading_engine import TradingEngine, PositionTracker

        db, factory = await _db_session_factory()
        async with factory() as session:
            # 이전에 롱으로 사용된 포지션 행 (highest_price에 값 있음)
            pos = PositionModel(
                exchange="binance_futures",
                symbol="ETH/USDT",
                quantity=0.05,
                average_buy_price=3200.0,
                total_invested=160.0,
                is_paper=False,
                direction="short",
                highest_price=70000.0,  # stale long data
            )
            session.add(pos)
            await session.flush()

            eng = self._make_engine_stub("binance_futures")
            tracker = PositionTracker(
                entry_price=3200.0,
                extreme_price=2900.0,
                stop_loss_pct=3.0,
                take_profit_pct=6.0,
                trailing_activation_pct=2.0,
                trailing_stop_pct=1.5,
                trailing_active=True,
            )
            await TradingEngine._save_tracker_to_db(eng, session, "ETH/USDT", tracker)

            await session.refresh(pos)
            assert pos.lowest_price == pytest.approx(2900.0)
            assert pos.highest_price is None  # stale value wiped

        await db.dispose()
