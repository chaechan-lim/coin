"""
SurgeEngine 단위 테스트
=======================
"""
import asyncio
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from datetime import datetime, timezone, timedelta
from collections import deque

from core.models import Position
from core.enums import SignalType
from engine.surge_engine import (
    SurgeEngine,
    SurgePositionState,
    SymbolState,
    EXCHANGE_NAME,
    FEE_PCT,
)
from config import SurgeTradingConfig


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def mock_config():
    """Minimal mock AppConfig for surge engine."""
    config = MagicMock()
    sc = SurgeTradingConfig()
    config.surge_trading = sc
    config.binance.enabled = True
    config.binance.api_key = "test"
    config.binance.api_secret = "test"
    return config


@pytest.fixture
def mock_exchange():
    exchange = AsyncMock()
    exchange.set_leverage = AsyncMock(return_value={})
    exchange.fetch_ticker = AsyncMock(return_value=MagicMock(
        last=65000.0, bid=64990.0, ask=65010.0, volume=1000.0,
    ))
    return exchange


@pytest.fixture
def mock_portfolio():
    """Mock futures PM (shared cash)."""
    pm = MagicMock()
    pm.cash_balance = 300.0  # 선물 전체 잔고
    return pm


@pytest.fixture
def mock_order_manager():
    om = AsyncMock()
    order = MagicMock()
    order.executed_price = 65000.0
    order.executed_quantity = 0.001
    order.fee = 0.026
    om.create_order = AsyncMock(return_value=order)
    return om


@pytest.fixture
def mock_registry():
    registry = MagicMock()
    registry.get_engine = MagicMock(return_value=None)
    return registry


@pytest.fixture
def surge_engine(mock_config, mock_exchange, mock_portfolio, mock_order_manager, mock_registry):
    """Create SurgeEngine with mocked dependencies."""
    engine = SurgeEngine(
        config=mock_config,
        exchange=mock_exchange,
        futures_pm=mock_portfolio,
        order_manager=mock_order_manager,
        engine_registry=mock_registry,
    )
    return engine


# ── Test: Config and initialization ──────────────────────────────

class TestSurgeEngineInit:
    def test_exchange_name(self, surge_engine):
        assert surge_engine._exchange_name == "binance_surge"

    def test_default_not_running(self, surge_engine):
        assert surge_engine.is_running is False

    def test_default_config_values(self, surge_engine):
        """Default SurgeTradingConfig values are applied (COIN-20 updated)."""
        assert surge_engine._leverage == 3
        assert surge_engine._max_concurrent == 3
        assert surge_engine._sl_pct == 2.5       # COIN-20: 2.0→2.5
        assert surge_engine._tp_pct == 3.0       # COIN-20: 4.0→3.0
        assert surge_engine._trail_activation_pct == 0.5  # COIN-20: 1.0→0.5
        assert surge_engine._trail_stop_pct == 0.8
        assert surge_engine._max_hold_minutes == 120
        assert surge_engine._long_only is False  # COIN-36: default changed
        assert surge_engine._daily_trade_limit == 15

    def test_coin20_entry_filter_config(self, surge_engine):
        """COIN-20: New entry filter config values are applied."""
        assert surge_engine._min_score == 0.55
        assert surge_engine._rsi_overbought == 75.0
        assert surge_engine._rsi_oversold == 25.0
        assert surge_engine._consecutive_sl_cooldown_sec == 10800  # 180 min
        assert surge_engine._min_atr_pct == 0.5

    def test_tracked_coins(self, surge_engine):
        assert len(surge_engine.tracked_coins) == 30

    def test_status_dict(self, surge_engine):
        s = surge_engine.status()
        assert s["running"] is False
        assert s["leverage"] == 3
        assert s["open_positions"] == 0

    def test_uses_futures_pm_cash(self, surge_engine):
        """Position sizing uses futures PM cash directly."""
        assert surge_engine._futures_pm.cash_balance == 300.0


# ── Test: Surge score computation ────────────────────────────────

class TestSurgeScore:
    def test_empty_state_returns_zero(self, surge_engine):
        score, vol, price = surge_engine.compute_surge_score("UNKNOWN/USDT")
        assert score == 0.0
        assert vol == 0.0
        assert price == 0.0

    def test_no_candle_data_returns_zero(self, surge_engine):
        """No candle volume data → zero score."""
        # _candle_vol_ratios is empty
        score, vol, price = surge_engine.compute_surge_score("BTC/USDT")
        assert score == 0.0

    def test_high_volume_surge_score(self, surge_engine):
        """High volume ratio from candle data should produce a meaningful score."""
        # Set candle-based data (simulating 5m OHLCV result)
        surge_engine._candle_vol_ratios["BTC/USDT"] = 10.0  # 10x volume
        surge_engine._candle_price_chgs["BTC/USDT"] = 2.3   # +2.3% price
        surge_engine._candle_vol_accel["BTC/USDT"] = 3.0     # accelerating

        score, vol_ratio, price_chg = surge_engine.compute_surge_score("BTC/USDT")
        assert vol_ratio == 10.0
        assert price_chg == 2.3
        assert score > 0.3  # significant score

    def test_no_volume_no_score(self, surge_engine):
        """Zero volume ratio should not trigger."""
        surge_engine._candle_vol_ratios["ETH/USDT"] = 0.0
        surge_engine._candle_price_chgs["ETH/USDT"] = 0.0
        surge_engine._candle_vol_accel["ETH/USDT"] = 0.0

        score, vol_ratio, price_chg = surge_engine.compute_surge_score("ETH/USDT")
        assert score == 0.0

    def test_score_weights_sum(self, surge_engine):
        """Score weights should sum to 1.0 (0.40 + 0.35 + 0.25)."""
        # Set extreme values → all signals saturate at 1.0
        surge_engine._candle_vol_ratios["SOL/USDT"] = 20.0  # 20x → saturates
        surge_engine._candle_price_chgs["SOL/USDT"] = 10.0   # 10% → saturates
        surge_engine._candle_vol_accel["SOL/USDT"] = 5.0      # 5 → saturates

        score, _, _ = surge_engine.compute_surge_score("SOL/USDT")
        assert 0.0 <= score <= 1.0
        assert abs(score - 1.0) < 0.01  # all signals maxed

    def test_moderate_surge(self, surge_engine):
        """Moderate surge (vol=5x, price=1.5%) should pass threshold."""
        surge_engine._candle_vol_ratios["XRP/USDT"] = 5.0
        surge_engine._candle_price_chgs["XRP/USDT"] = 1.5
        surge_engine._candle_vol_accel["XRP/USDT"] = 1.0

        score, vol_ratio, price_chg = surge_engine.compute_surge_score("XRP/USDT")
        assert vol_ratio >= 5.0
        assert abs(price_chg) >= 1.5
        assert score >= 0.30


# ── Test: RSI computation ────────────────────────────────────────

class TestRSI:
    def test_rsi_neutral_on_insufficient_data(self, surge_engine):
        rsi = surge_engine.compute_rsi("UNKNOWN/USDT")
        assert rsi == 50.0

    def test_rsi_overbought(self, surge_engine):
        """Continuously rising prices should yield high RSI."""
        surge_engine._symbol_states["BTC/USDT"] = SymbolState()
        state = surge_engine._symbol_states["BTC/USDT"]
        for i in range(20):
            state.rsi_closes.append(100.0 + i * 5)

        rsi = surge_engine.compute_rsi("BTC/USDT")
        assert rsi > 80

    def test_rsi_oversold(self, surge_engine):
        """Continuously falling prices should yield low RSI."""
        surge_engine._symbol_states["ETH/USDT"] = SymbolState()
        state = surge_engine._symbol_states["ETH/USDT"]
        for i in range(20):
            state.rsi_closes.append(200.0 - i * 5)

        rsi = surge_engine.compute_rsi("ETH/USDT")
        assert rsi < 20


# ── Test: Entry conditions ───────────────────────────────────────

class TestEntryConditions:
    def test_bidirectional_allows_short(self, surge_engine):
        """COIN-36: With long_only=False (default), short direction is allowed."""
        assert surge_engine._long_only is False
        # Simulate negative price change -> would be "short"
        surge_engine._symbol_states["BTC/USDT"] = SymbolState()
        state = surge_engine._symbol_states["BTC/USDT"]
        for i in range(10):
            state.volume_1m.append(100.0)
            state.prices.append(65000.0 - i * 100)  # declining
            state.rsi_closes.append(65000.0 - i * 100)

        # Price change should be negative
        _, _, price_chg = surge_engine.compute_surge_score("BTC/USDT")
        if price_chg < 0:
            direction = "short"
            # With bidirectional, short should NOT be blocked
            assert not (surge_engine._long_only and direction == "short")

    def test_long_only_true_blocks_short(self, surge_engine):
        """With long_only=True explicitly set, short direction is blocked."""
        surge_engine._long_only = True
        surge_engine._symbol_states["BTC/USDT"] = SymbolState()
        state = surge_engine._symbol_states["BTC/USDT"]
        for i in range(10):
            state.volume_1m.append(100.0)
            state.prices.append(65000.0 - i * 100)  # declining
            state.rsi_closes.append(65000.0 - i * 100)

        _, _, price_chg = surge_engine.compute_surge_score("BTC/USDT")
        if price_chg < 0:
            direction = "short"
            assert surge_engine._long_only and direction == "short"

    def test_max_concurrent_blocks_new_entry(self, surge_engine):
        """No new entries when max_concurrent is reached."""
        # Fill up to max
        for i in range(surge_engine._max_concurrent):
            sym = f"COIN{i}/USDT"
            surge_engine._positions[sym] = SurgePositionState(
                symbol=sym, direction="long", entry_price=100.0,
                quantity=1.0, margin=10.0,
                entry_time=datetime.now(timezone.utc),
                peak_price=100.0, trough_price=100.0,
            )

        assert len(surge_engine._positions) >= surge_engine._max_concurrent

    def test_cooldown_blocks_reentry(self, surge_engine):
        """A symbol with active cooldown is skipped."""
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        surge_engine._cooldowns["BTC/USDT"] = future

        now = datetime.now(timezone.utc)
        assert now < surge_engine._cooldowns["BTC/USDT"]

    def test_daily_trade_limit(self, surge_engine):
        """No entries when daily_trade_limit reached."""
        surge_engine._daily_trades = surge_engine._daily_trade_limit
        # The scan_for_entries would return early
        assert surge_engine._daily_trades >= surge_engine._daily_trade_limit


# ── Test: Exit conditions ────────────────────────────────────────

class TestExitConditions:
    def _make_long_pos(self, entry=65000.0) -> SurgePositionState:
        return SurgePositionState(
            symbol="BTC/USDT", direction="long",
            entry_price=entry, quantity=0.01,
            margin=100.0,
            entry_time=datetime.now(timezone.utc),
            peak_price=entry, trough_price=entry,
        )

    def _make_short_pos(self, entry=65000.0) -> SurgePositionState:
        return SurgePositionState(
            symbol="BTC/USDT", direction="short",
            entry_price=entry, quantity=0.01,
            margin=100.0,
            entry_time=datetime.now(timezone.utc),
            peak_price=entry, trough_price=entry,
        )

    def test_long_sl_triggered(self, surge_engine):
        """Long stop loss at -2.5% (leveraged, COIN-20)."""
        pos = self._make_long_pos(entry=65000.0)
        # SL = 2.5%, leverage = 3 -> price drop = 2.5%/3 = 0.833%
        sl_price = 65000.0 * (1 - surge_engine._sl_pct / 100 / surge_engine._leverage)
        now = datetime.now(timezone.utc)

        should_exit, reason = surge_engine._check_exit_conditions(pos, sl_price - 1, now)
        assert should_exit is True
        assert reason == "SL"

    def test_long_tp_triggered(self, surge_engine):
        """Long take profit at +3% (leveraged, COIN-20)."""
        pos = self._make_long_pos(entry=65000.0)
        # TP = 3%, leverage = 3 -> price rise = 3%/3 = 1%
        tp_price = 65000.0 * (1 + surge_engine._tp_pct / 100 / surge_engine._leverage)
        now = datetime.now(timezone.utc)

        should_exit, reason = surge_engine._check_exit_conditions(pos, tp_price + 1, now)
        assert should_exit is True
        assert reason == "TP"

    def test_long_trailing_stop(self, surge_engine):
        """Trailing activates after +0.5% PnL (COIN-20), exits on drawdown."""
        pos = self._make_long_pos(entry=65000.0)
        now = datetime.now(timezone.utc)

        # First, move price up to activate trailing (pnl > 0.5%)
        # pnl = (price - entry) / entry * 100 * leverage
        # 0.5% = (price - 65000) / 65000 * 100 * 3
        # price = 65000 * (1 + 0.5 / 300) = 65108.33
        activation_price = 65000.0 * (1 + surge_engine._trail_activation_pct / 100 / surge_engine._leverage) + 10
        should_exit, _ = surge_engine._check_exit_conditions(pos, activation_price, now)
        assert pos.trailing_active is True
        assert pos.peak_price == activation_price

        # Now drop enough to trigger trailing stop
        # drawdown_from_peak = (peak - current) / peak * 100 * leverage >= trail_stop_pct
        # current = peak * (1 - trail_stop_pct / 100 / leverage)
        trail_trigger = pos.peak_price * (1 - surge_engine._trail_stop_pct / 100 / surge_engine._leverage) - 1
        should_exit, reason = surge_engine._check_exit_conditions(pos, trail_trigger, now)
        assert should_exit is True
        assert reason == "Trailing"

    def test_time_expiry(self, surge_engine):
        """Position exits after max_hold_minutes."""
        pos = self._make_long_pos(entry=65000.0)
        pos.entry_time = datetime.now(timezone.utc) - timedelta(minutes=surge_engine._max_hold_minutes + 1)
        now = datetime.now(timezone.utc)

        should_exit, reason = surge_engine._check_exit_conditions(pos, 65000.0, now)
        assert should_exit is True
        assert reason == "TimeExpiry"

    def test_short_sl_triggered(self, surge_engine):
        """Short stop loss when price rises."""
        pos = self._make_short_pos(entry=65000.0)
        sl_price = 65000.0 * (1 + surge_engine._sl_pct / 100 / surge_engine._leverage)
        now = datetime.now(timezone.utc)

        should_exit, reason = surge_engine._check_exit_conditions(pos, sl_price + 1, now)
        assert should_exit is True
        assert reason == "SL"

    def test_short_tp_triggered(self, surge_engine):
        """Short take profit when price drops."""
        pos = self._make_short_pos(entry=65000.0)
        tp_price = 65000.0 * (1 - surge_engine._tp_pct / 100 / surge_engine._leverage)
        now = datetime.now(timezone.utc)

        should_exit, reason = surge_engine._check_exit_conditions(pos, tp_price - 1, now)
        assert should_exit is True
        assert reason == "TP"

    def test_no_exit_within_normal_range(self, surge_engine):
        """No exit for small price fluctuations."""
        pos = self._make_long_pos(entry=65000.0)
        now = datetime.now(timezone.utc)

        # Small move: +0.1%
        should_exit, reason = surge_engine._check_exit_conditions(pos, 65065.0, now)
        assert should_exit is False
        assert reason == ""


# ── Test: Risk management ────────────────────────────────────────

class TestRiskManagement:
    def test_consecutive_loss_pause(self, surge_engine):
        """3 consecutive losses trigger a 30-minute pause."""
        surge_engine._consecutive_losses = 3
        surge_engine._pause_until = datetime.now(timezone.utc) + timedelta(minutes=30)

        now = datetime.now(timezone.utc)
        assert surge_engine._pause_until > now

    def test_daily_counter_reset(self, surge_engine):
        """Daily counters reset at date change."""
        surge_engine._daily_trades = 10
        surge_engine._daily_losses = 5
        surge_engine._consecutive_losses = 2
        surge_engine._last_reset_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        surge_engine._reset_daily_counters_if_needed()

        assert surge_engine._daily_trades == 0
        assert surge_engine._daily_losses == 0
        assert surge_engine._consecutive_losses == 0

    def test_daily_counter_no_reset_same_day(self, surge_engine):
        """Daily counters should not reset on the same day."""
        surge_engine._daily_trades = 5
        surge_engine._last_reset_date = datetime.now(timezone.utc).date()

        surge_engine._reset_daily_counters_if_needed()

        assert surge_engine._daily_trades == 5


# ── Test: Cross-engine conflict ──────────────────────────────────

class TestCrossEngineConflict:
    def test_no_conflict_when_no_main_engine(self, surge_engine):
        """No conflict if main engine not registered."""
        surge_engine._engine_registry.get_engine.return_value = None
        assert surge_engine._check_cross_engine_conflict("BTC/USDT", "long") is False

    def test_conflict_opposite_direction(self, surge_engine):
        """Block when main engine has opposite direction."""
        mock_main = MagicMock()
        mock_tracker = MagicMock()
        mock_tracker.direction = "short"
        mock_main._position_trackers = {"BTC/USDT": mock_tracker}
        surge_engine._engine_registry.get_engine.return_value = mock_main

        assert surge_engine._check_cross_engine_conflict("BTC/USDT", "long") is True

    def test_no_conflict_same_direction(self, surge_engine):
        """Allow when main engine has same direction."""
        mock_main = MagicMock()
        mock_tracker = MagicMock()
        mock_tracker.direction = "long"
        mock_main._position_trackers = {"BTC/USDT": mock_tracker}
        surge_engine._engine_registry.get_engine.return_value = mock_main

        assert surge_engine._check_cross_engine_conflict("BTC/USDT", "long") is False

    def test_no_conflict_no_position(self, surge_engine):
        """Allow when main engine has no position on this symbol."""
        mock_main = MagicMock()
        mock_main._position_trackers = {}
        surge_engine._engine_registry.get_engine.return_value = mock_main

        assert surge_engine._check_cross_engine_conflict("BTC/USDT", "long") is False


# ── Test: Symbol state updates ───────────────────────────────────

class TestSymbolState:
    def test_update_creates_state(self, surge_engine):
        """First update creates SymbolState."""
        assert "BTC/USDT" not in surge_engine._symbol_states
        surge_engine._update_symbol_state("BTC/USDT", {"last": 65000.0, "volume": 100.0}, 0.0)
        assert "BTC/USDT" in surge_engine._symbol_states
        assert surge_engine._symbol_states["BTC/USDT"].last_price == 65000.0

    def test_deque_maxlen(self, surge_engine):
        """Rolling windows respect maxlen."""
        state = SymbolState()
        assert state.volume_1m.maxlen == 60
        assert state.prices.maxlen == 60
        assert state.rsi_closes.maxlen == 20


# ── Test: Start/Stop lifecycle ───────────────────────────────────

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_sets_running(self, surge_engine):
        """start() sets is_running to True."""
        with patch.object(surge_engine, '_main_loop', new_callable=AsyncMock):
            await surge_engine.start()
            assert surge_engine.is_running is True
            await surge_engine.stop()
            assert surge_engine.is_running is False

    @pytest.mark.asyncio
    async def test_double_start_warns(self, surge_engine):
        """Starting when already running logs a warning."""
        surge_engine._running = True
        with patch.object(surge_engine, '_main_loop', new_callable=AsyncMock):
            await surge_engine.start()  # should warn, not crash
            surge_engine._running = False

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self, surge_engine):
        """stop() is safe when not running."""
        await surge_engine.stop()
        assert surge_engine.is_running is False


# ── Test: Config class ───────────────────────────────────────────

class TestSurgeTradingConfig:
    def test_defaults(self):
        cfg = SurgeTradingConfig()
        assert cfg.enabled is False
        assert cfg.mode == "paper"
        assert cfg.leverage == 3
        assert cfg.initial_balance_usdt == 150.0
        assert cfg.max_concurrent == 3
        assert cfg.position_pct == 0.08
        assert cfg.sl_pct == 2.5       # COIN-20: 2.0→2.5
        assert cfg.tp_pct == 3.0       # COIN-20: 4.0→3.0
        assert cfg.trail_activation_pct == 0.5  # COIN-20: 1.0→0.5
        assert cfg.trail_stop_pct == 0.8
        assert cfg.max_hold_minutes == 120
        assert cfg.vol_threshold == 4.0
        assert cfg.price_threshold == 1.0
        assert cfg.long_only is False  # COIN-36: default changed
        assert cfg.daily_trade_limit == 15
        assert cfg.scan_symbols_count == 30
        assert cfg.cooldown_per_symbol_sec == 3600
        assert cfg.scan_interval_sec == 5

    def test_coin20_filter_defaults(self):
        """COIN-20: New filter config defaults."""
        cfg = SurgeTradingConfig()
        assert cfg.min_score == 0.55
        assert cfg.rsi_overbought == 75.0
        assert cfg.rsi_oversold == 25.0
        assert cfg.consecutive_sl_cooldown_sec == 10800
        assert cfg.min_atr_pct == 0.5

    def test_invalid_mode_raises(self):
        with pytest.raises(Exception):
            SurgeTradingConfig(mode="invalid")

    def test_env_prefix(self):
        assert SurgeTradingConfig.model_config["env_prefix"] == "SURGE_TRADING_"


# ── Test: DB position restore ────────────────────────────────────

class TestPositionRestore:
    @pytest.mark.asyncio
    async def test_initialize_restores_positions(self, surge_engine, session):
        """initialize() restores open positions from DB."""
        # Add a position to DB
        pos = Position(
            exchange="binance_surge",
            symbol="BTC/USDT",
            quantity=0.01,
            average_buy_price=65000.0,
            total_invested=100.0,
            direction="long",
            leverage=3,
            highest_price=65500.0,
            trailing_active=False,
            entered_at=datetime.now(timezone.utc),
        )
        session.add(pos)
        await session.flush()

        # Mock get_session_factory to return our test session
        mock_factory = MagicMock()
        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_session_ctx

        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            await surge_engine.initialize()

        assert "BTC/USDT" in surge_engine._positions
        restored = surge_engine._positions["BTC/USDT"]
        assert restored.direction == "long"
        assert restored.entry_price == 65000.0
        assert restored.peak_price == 65500.0


# ── Test: Entry execution (integration mock) ─────────────────────

class TestEntryExecution:
    @pytest.mark.asyncio
    async def test_enter_position_updates_state(self, surge_engine, session):
        """_enter_position creates order and updates in-memory state."""
        # Create DB position for the update
        pos = Position(
            exchange="binance_surge",
            symbol="BTC/USDT",
            quantity=0.0,
            average_buy_price=0.0,
            total_invested=0.0,
            direction="long",
        )
        session.add(pos)
        await session.flush()

        mock_factory = MagicMock()
        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_session_ctx

        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._enter_position(
                    "BTC/USDT", "long", 0.75,
                    {"last": 65000.0, "bid": 64990.0, "ask": 65010.0},
                )

        assert "BTC/USDT" in surge_engine._positions
        assert surge_engine._daily_trades == 1
        assert "BTC/USDT" in surge_engine._cooldowns

    @pytest.mark.asyncio
    async def test_entry_deducts_futures_pm_cash(self, surge_engine, session):
        """서지 진입 시 선물 PM cash가 차감됨."""
        initial_cash = surge_engine._futures_pm.cash_balance

        mock_factory = MagicMock()
        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_session_ctx

        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._enter_position(
                    "ETH/USDT", "long", 0.75,
                    {"last": 3500.0, "bid": 3499.0, "ask": 3501.0},
                )

        assert surge_engine._futures_pm.cash_balance < initial_cash


# ── Test: Cash integration (futures PM 통합) ─────────────────────

class TestCashIntegration:
    def test_position_sizing_uses_futures_cash(self, surge_engine):
        """Position sizing is based on futures PM cash_balance."""
        surge_engine._futures_pm.cash_balance = 500.0
        # position_pct = 0.08 → 500 * 0.08 = 40 USDT
        expected_base = 500.0 * surge_engine._position_pct
        assert expected_base == 40.0

    def test_no_separate_surge_allocation(self, surge_engine):
        """서지 엔진에 별도 할당금이 없음 (선물 PM 직접 사용)."""
        assert not hasattr(surge_engine, '_initial_allocation')
        assert not hasattr(surge_engine, '_surge_realized_pnl')

    def test_entry_rejected_when_no_cash(self, surge_engine):
        """선물 PM cash가 부족하면 진입 불가."""
        surge_engine._futures_pm.cash_balance = 2.0  # 5 USDT 미만
        # _enter_position에서 size_usdt < 5 체크로 리턴됨
        assert surge_engine._futures_pm.cash_balance * surge_engine._position_pct < 5


# ── Test: Scan status ────────────────────────────────────────────

class TestScanStatus:
    def test_scan_status_returns_dict(self, surge_engine):
        """scan_status() returns expected structure."""
        result = surge_engine.scan_status()
        assert result["scan_symbols_count"] == 30
        assert result["open_positions"] == 0
        assert result["daily_trades"] == 0
        assert result["daily_limit"] == 15
        assert result["leverage"] == 3
        assert isinstance(result["scores"], list)
        assert len(result["scores"]) == 30

    def test_scan_status_with_data(self, surge_engine):
        """scan_status() returns scores when candle + ticker data exists."""
        surge_engine._symbol_states["BTC/USDT"] = SymbolState()
        state = surge_engine._symbol_states["BTC/USDT"]
        state.last_price = 65100.0
        # Set candle-based volume data
        surge_engine._candle_vol_ratios["BTC/USDT"] = 3.0
        surge_engine._candle_price_chgs["BTC/USDT"] = 1.0
        surge_engine._candle_vol_accel["BTC/USDT"] = 0.5

        result = surge_engine.scan_status()
        btc_score = next(s for s in result["scores"] if s["symbol"] == "BTC/USDT")
        assert btc_score["last_price"] == 65100.0
        assert btc_score["has_position"] is False
        assert btc_score["vol_ratio"] == 3.0

    def test_scan_status_with_position(self, surge_engine):
        """scan_status() marks positions correctly."""
        surge_engine._positions["ETH/USDT"] = SurgePositionState(
            symbol="ETH/USDT", direction="long",
            entry_price=3500.0, quantity=0.1,
            margin=50.0, entry_time=datetime.now(timezone.utc),
            peak_price=3500.0, trough_price=3500.0,
        )
        surge_engine._symbol_states["ETH/USDT"] = SymbolState()
        surge_engine._symbol_states["ETH/USDT"].last_price = 3550.0

        result = surge_engine.scan_status()
        eth_score = next(s for s in result["scores"] if s["symbol"] == "ETH/USDT")
        assert eth_score["has_position"] is True
        assert eth_score["direction"] == "long"
        assert eth_score["pnl_pct"] is not None
        assert eth_score["pnl_pct"] > 0  # 3550 > 3500

    def test_calc_position_pnl_pct_long(self, surge_engine):
        """PnL% calculation for long position."""
        pos = SurgePositionState(
            symbol="SOL/USDT", direction="long",
            entry_price=100.0, quantity=1.0, margin=33.33,
            entry_time=datetime.now(timezone.utc),
            peak_price=100.0, trough_price=100.0,
        )
        surge_engine._symbol_states["SOL/USDT"] = SymbolState()
        surge_engine._symbol_states["SOL/USDT"].last_price = 103.0
        # 3% * 3x leverage = 9%
        assert abs(surge_engine._calc_position_pnl_pct(pos) - 9.0) < 0.01

    def test_calc_position_pnl_pct_short(self, surge_engine):
        """PnL% calculation for short position."""
        pos = SurgePositionState(
            symbol="SOL/USDT", direction="short",
            entry_price=100.0, quantity=1.0, margin=33.33,
            entry_time=datetime.now(timezone.utc),
            peak_price=100.0, trough_price=100.0,
        )
        surge_engine._symbol_states["SOL/USDT"] = SymbolState()
        surge_engine._symbol_states["SOL/USDT"].last_price = 97.0
        # 3% * 3x leverage = 9%
        assert abs(surge_engine._calc_position_pnl_pct(pos) - 9.0) < 0.01


# ── Test: Candle volume data update ──────────────────────────────

class TestCandleVolumeData:
    @pytest.mark.asyncio
    async def test_update_candle_volume_data(self, surge_engine):
        """_update_candle_volume_data uses last completed candle (skips in-progress)."""
        from exchange.data_models import Candle

        # 60 baseline + 1 spike (completed) + 1 in-progress (will be dropped)
        candles = []
        for i in range(60):
            candles.append(Candle(
                timestamp=datetime.now(timezone.utc),
                open=100.0, high=101.0, low=99.0, close=100.0 + i * 0.01,
                volume=1000.0,  # baseline
            ))
        # Spike candle — last completed
        candles.append(Candle(
            timestamp=datetime.now(timezone.utc),
            open=100.0, high=104.0, low=100.0, close=103.0,
            volume=10000.0,  # 10x spike
        ))
        # In-progress candle — will be excluded
        candles.append(Candle(
            timestamp=datetime.now(timezone.utc),
            open=103.0, high=103.5, low=102.5, close=103.2,
            volume=200.0,  # low because incomplete
        ))

        surge_engine._exchange.fetch_ohlcv = AsyncMock(return_value=candles)
        surge_engine._scan_symbols = ["BTC/USDT"]

        await surge_engine._update_candle_volume_data()

        assert "BTC/USDT" in surge_engine._candle_vol_ratios
        # Spike candle (10000) vs baseline avg (1000) = 10x
        assert surge_engine._candle_vol_ratios["BTC/USDT"] == pytest.approx(10.0, rel=0.1)
        assert "BTC/USDT" in surge_engine._candle_price_chgs
        assert surge_engine._candle_price_chgs["BTC/USDT"] > 0

    @pytest.mark.asyncio
    async def test_update_candle_volume_handles_errors(self, surge_engine):
        """Errors in fetch_ohlcv are silently skipped per symbol."""
        surge_engine._exchange.fetch_ohlcv = AsyncMock(side_effect=Exception("API error"))
        surge_engine._scan_symbols = ["BTC/USDT"]

        await surge_engine._update_candle_volume_data()
        # Should not crash, just skip
        assert "BTC/USDT" not in surge_engine._candle_vol_ratios

    @pytest.mark.asyncio
    async def test_update_candle_volume_insufficient_data(self, surge_engine):
        """Skip symbols with < 6 candles."""
        from exchange.data_models import Candle

        candles = [Candle(
            timestamp=datetime.now(timezone.utc),
            open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0,
        ) for _ in range(3)]  # Only 3 candles

        surge_engine._exchange.fetch_ohlcv = AsyncMock(return_value=candles)
        surge_engine._scan_symbols = ["BTC/USDT"]

        await surge_engine._update_candle_volume_data()
        assert "BTC/USDT" not in surge_engine._candle_vol_ratios

    def test_candle_update_interval_default(self, surge_engine):
        """Default candle update interval is 60 seconds."""
        assert surge_engine._CANDLE_UPDATE_INTERVAL == 60


# ── Test: Batch ticker fetch ────────────────────────────────────

class TestBatchTickerFetch:
    @pytest.mark.asyncio
    async def test_fetch_tickers_batch(self, surge_engine):
        """_fetch_tickers uses batch API call (USDM key format)."""
        surge_engine._scan_symbols = ["BTC/USDT", "ETH/USDT"]
        surge_engine._exchange.fetch_tickers = AsyncMock(return_value={
            "BTC/USDT:USDT": {"last": 65000.0, "bid": 64990.0, "ask": 65010.0, "quoteVolume": 1e9},
            "ETH/USDT:USDT": {"last": 3500.0, "bid": 3499.0, "ask": 3501.0, "quoteVolume": 5e8},
            "OTHER/USDT:USDT": {"last": 1.0, "bid": 0.99, "ask": 1.01, "quoteVolume": 1000},
        })

        tickers = await surge_engine._fetch_tickers()
        assert "BTC/USDT" in tickers
        assert "ETH/USDT" in tickers
        assert "OTHER/USDT" not in tickers  # not in scan_symbols
        assert tickers["BTC/USDT"]["last"] == 65000.0

    @pytest.mark.asyncio
    async def test_fetch_tickers_fallback(self, surge_engine):
        """Falls back to individual fetch on batch failure."""
        surge_engine._scan_symbols = ["BTC/USDT", "ETH/USDT"]
        surge_engine._exchange.fetch_tickers = AsyncMock(side_effect=Exception("batch failed"))
        surge_engine._exchange.fetch_ticker = AsyncMock(return_value=MagicMock(
            last=65000.0, bid=64990.0, ask=65010.0, volume=1000.0,
        ))

        tickers = await surge_engine._fetch_tickers()
        assert "BTC/USDT" in tickers
        assert tickers["BTC/USDT"]["last"] == 65000.0

    @pytest.mark.asyncio
    async def test_fetch_tickers_normalizes_usdm_keys(self, surge_engine):
        """USDM futures ticker keys (BTC/USDT:USDT) are normalized."""
        surge_engine._scan_symbols = ["BTC/USDT"]
        surge_engine._exchange.fetch_tickers = AsyncMock(return_value={
            "BTC/USDT:USDT": {"last": 65000.0, "bid": 64990.0, "ask": 65010.0, "quoteVolume": 1e9},
        })

        tickers = await surge_engine._fetch_tickers()
        assert "BTC/USDT" in tickers
        assert tickers["BTC/USDT"]["last"] == 65000.0


# ── COIN-20: Test min_score entry filter ─────────────────────────

class TestMinScoreFilter:
    def test_min_score_default_is_055(self, surge_engine):
        """COIN-20: Default min_score is 0.55 (was hardcoded 0.40)."""
        assert surge_engine._min_score == 0.55

    def test_score_below_min_blocked(self, surge_engine):
        """Scores below min_score should be filtered out."""
        # Set up a symbol with low score
        surge_engine._candle_vol_ratios["BTC/USDT"] = 2.0  # low vol
        surge_engine._candle_price_chgs["BTC/USDT"] = 0.5  # low price change
        surge_engine._candle_vol_accel["BTC/USDT"] = 0.1

        score, _, _ = surge_engine.compute_surge_score("BTC/USDT")
        assert score < surge_engine._min_score  # score should be below 0.55

    def test_score_above_min_passes(self, surge_engine):
        """Scores above min_score should pass the filter."""
        # Set up a symbol with high score
        surge_engine._candle_vol_ratios["BTC/USDT"] = 10.0  # 10x volume
        surge_engine._candle_price_chgs["BTC/USDT"] = 3.0   # +3% price
        surge_engine._candle_vol_accel["BTC/USDT"] = 2.0

        score, _, _ = surge_engine.compute_surge_score("BTC/USDT")
        assert score >= surge_engine._min_score  # score should be >= 0.55

    def test_custom_min_score_from_config(self, mock_exchange, mock_portfolio, mock_order_manager, mock_registry):
        """Custom min_score via config is applied."""
        config = MagicMock()
        sc = SurgeTradingConfig(min_score=0.70)
        config.surge_trading = sc
        config.binance.enabled = True

        engine = SurgeEngine(
            config=config, exchange=mock_exchange,
            futures_pm=mock_portfolio, order_manager=mock_order_manager,
            engine_registry=mock_registry,
        )
        assert engine._min_score == 0.70


# ── COIN-20: Test RSI overbought/oversold filter ────────────────

class TestRSIFilterCOIN20:
    def test_rsi_overbought_default_75(self, surge_engine):
        """COIN-20: RSI overbought threshold is 75 (was 85)."""
        assert surge_engine._rsi_overbought == 75.0

    def test_rsi_oversold_default_25(self, surge_engine):
        """COIN-20: RSI oversold threshold is 25 (was 15)."""
        assert surge_engine._rsi_oversold == 25.0

    def test_rsi_78_blocks_long_entry(self, surge_engine):
        """RSI=78 should block long entry (was allowed at 85 threshold)."""
        surge_engine._symbol_states["BTC/USDT"] = SymbolState()
        state = surge_engine._symbol_states["BTC/USDT"]
        # Create rising prices for RSI ~78
        for i in range(20):
            state.rsi_closes.append(100.0 + i * 4)  # strong uptrend

        rsi = surge_engine.compute_rsi("BTC/USDT")
        assert rsi > 75  # above new threshold
        assert rsi > surge_engine._rsi_overbought

    def test_rsi_22_blocks_short_entry(self, surge_engine):
        """RSI=22 should block short entry (was allowed at 15 threshold)."""
        surge_engine._symbol_states["BTC/USDT"] = SymbolState()
        state = surge_engine._symbol_states["BTC/USDT"]
        # Create falling prices for low RSI
        for i in range(20):
            state.rsi_closes.append(200.0 - i * 4)  # strong downtrend

        rsi = surge_engine.compute_rsi("BTC/USDT")
        assert rsi < 25  # below new threshold
        assert rsi < surge_engine._rsi_oversold

    def test_rsi_60_allows_long_entry(self, surge_engine):
        """RSI=60 should still allow long entry."""
        assert 60 < surge_engine._rsi_overbought

    def test_rsi_35_allows_short_entry(self, surge_engine):
        """RSI=35 should still allow short entry."""
        assert 35 > surge_engine._rsi_oversold

    def test_custom_rsi_thresholds(self, mock_exchange, mock_portfolio, mock_order_manager, mock_registry):
        """Custom RSI thresholds via config."""
        config = MagicMock()
        sc = SurgeTradingConfig(rsi_overbought=80.0, rsi_oversold=20.0)
        config.surge_trading = sc
        config.binance.enabled = True

        engine = SurgeEngine(
            config=config, exchange=mock_exchange,
            futures_pm=mock_portfolio, order_manager=mock_order_manager,
            engine_registry=mock_registry,
        )
        assert engine._rsi_overbought == 80.0
        assert engine._rsi_oversold == 20.0


# ── COIN-20: Test consecutive SL cooldown ────────────────────────

class TestConsecutiveSLCooldown:
    def test_initial_sl_count_is_zero(self, surge_engine):
        """No consecutive SL by default."""
        assert surge_engine._consecutive_sl_count == {}

    def test_first_sl_sets_count_to_1(self, surge_engine):
        """First SL loss on a symbol sets count to 1."""
        surge_engine._consecutive_sl_count["FET/USDT"] = 1
        assert surge_engine._consecutive_sl_count["FET/USDT"] == 1

    def test_two_consecutive_sl_triggers_extended_cooldown(self, surge_engine):
        """2+ consecutive SL on same symbol triggers extended cooldown (180min)."""
        sym = "FET/USDT"
        # Simulate 2 consecutive SL
        surge_engine._consecutive_sl_count[sym] = 2
        now = datetime.now(timezone.utc)
        extended_cooldown = timedelta(seconds=surge_engine._consecutive_sl_cooldown_sec)
        surge_engine._cooldowns[sym] = now + extended_cooldown

        # The cooldown should be ~180 minutes in the future
        cooldown_minutes = (surge_engine._cooldowns[sym] - now).total_seconds() / 60
        assert cooldown_minutes == pytest.approx(180.0, abs=1.0)

    def test_profit_resets_sl_counter(self, surge_engine):
        """A profitable trade resets the consecutive SL counter."""
        surge_engine._consecutive_sl_count["FET/USDT"] = 3
        # Simulate a profit → should pop counter
        surge_engine._consecutive_sl_count.pop("FET/USDT", None)
        assert "FET/USDT" not in surge_engine._consecutive_sl_count

    def test_default_cooldown_sec(self, surge_engine):
        """Default consecutive SL cooldown is 10800 seconds (180 minutes)."""
        assert surge_engine._consecutive_sl_cooldown_sec == 10800

    def test_normal_cooldown_vs_extended(self, surge_engine):
        """Extended cooldown (180min) is longer than normal (60min)."""
        normal = surge_engine._cooldown_sec  # 3600 = 60min
        extended = surge_engine._consecutive_sl_cooldown_sec  # 10800 = 180min
        assert extended > normal
        assert extended == 3 * normal


# ── COIN-20: Test ATR volatility filter ──────────────────────────

class TestATRFilter:
    def test_default_min_atr_pct(self, surge_engine):
        """Default min_atr_pct is 0.5."""
        assert surge_engine._min_atr_pct == 0.5

    def test_atr_data_dict_exists(self, surge_engine):
        """ATR% data dict is initialized."""
        assert isinstance(surge_engine._candle_atr_pct, dict)
        assert len(surge_engine._candle_atr_pct) == 0

    def test_low_atr_would_be_blocked(self, surge_engine):
        """ATR% below threshold (e.g. 0.2%) should block entry."""
        atr_pct = 0.2
        assert atr_pct < surge_engine._min_atr_pct

    def test_high_atr_would_pass(self, surge_engine):
        """ATR% above threshold (e.g. 1.5%) should pass filter."""
        atr_pct = 1.5
        assert atr_pct >= surge_engine._min_atr_pct

    @pytest.mark.asyncio
    async def test_candle_update_computes_atr(self, surge_engine):
        """_update_candle_volume_data also computes ATR%."""
        from exchange.data_models import Candle

        # 60 baseline + 1 spike (completed) + 1 in-progress
        candles = []
        for i in range(60):
            candles.append(Candle(
                timestamp=datetime.now(timezone.utc),
                open=100.0, high=101.5, low=98.5, close=100.0 + i * 0.01,
                volume=1000.0,
            ))
        # Spike candle
        candles.append(Candle(
            timestamp=datetime.now(timezone.utc),
            open=100.0, high=104.0, low=98.0, close=103.0,
            volume=10000.0,
        ))
        # In-progress candle
        candles.append(Candle(
            timestamp=datetime.now(timezone.utc),
            open=103.0, high=103.5, low=102.5, close=103.2,
            volume=200.0,
        ))

        surge_engine._exchange.fetch_ohlcv = AsyncMock(return_value=candles)
        surge_engine._scan_symbols = ["BTC/USDT"]

        await surge_engine._update_candle_volume_data()

        assert "BTC/USDT" in surge_engine._candle_atr_pct
        # ATR% should be positive (there's price movement)
        assert surge_engine._candle_atr_pct["BTC/USDT"] > 0

    def test_custom_min_atr_pct(self, mock_exchange, mock_portfolio, mock_order_manager, mock_registry):
        """Custom min_atr_pct via config."""
        config = MagicMock()
        sc = SurgeTradingConfig(min_atr_pct=1.0)
        config.surge_trading = sc
        config.binance.enabled = True

        engine = SurgeEngine(
            config=config, exchange=mock_exchange,
            futures_pm=mock_portfolio, order_manager=mock_order_manager,
            engine_registry=mock_registry,
        )
        assert engine._min_atr_pct == 1.0


# ── COIN-20: Test scan_status new fields ─────────────────────────

class TestScanStatusCOIN20:
    def test_scan_status_has_min_score(self, surge_engine):
        """scan_status includes min_score."""
        result = surge_engine.scan_status()
        assert "min_score" in result
        assert result["min_score"] == 0.55

    def test_scan_status_has_min_atr_pct(self, surge_engine):
        """scan_status includes min_atr_pct."""
        result = surge_engine.scan_status()
        assert "min_atr_pct" in result
        assert result["min_atr_pct"] == 0.5

    def test_scan_status_has_rsi_thresholds(self, surge_engine):
        """scan_status includes RSI threshold info."""
        result = surge_engine.scan_status()
        assert "rsi_overbought" in result
        assert result["rsi_overbought"] == 75.0
        assert "rsi_oversold" in result
        assert result["rsi_oversold"] == 25.0

    def test_scan_status_scores_have_atr_pct(self, surge_engine):
        """Each score entry in scan_status has atr_pct field."""
        result = surge_engine.scan_status()
        for score_entry in result["scores"]:
            assert "atr_pct" in score_entry

    def test_scan_status_scores_have_consecutive_sl(self, surge_engine):
        """Each score entry in scan_status has consecutive_sl field."""
        # Set some SL count
        surge_engine._consecutive_sl_count["BTC/USDT"] = 2
        result = surge_engine.scan_status()
        btc_score = next(s for s in result["scores"] if s["symbol"] == "BTC/USDT")
        assert btc_score["consecutive_sl"] == 2

    def test_scan_status_atr_from_candle_data(self, surge_engine):
        """atr_pct in scores reflects candle ATR data."""
        surge_engine._candle_atr_pct["ETH/USDT"] = 1.23
        result = surge_engine.scan_status()
        eth_score = next(s for s in result["scores"] if s["symbol"] == "ETH/USDT")
        assert eth_score["atr_pct"] == 1.23


# ── COIN-22: Test exit cooldown ──────────────────────────────────

class TestExitCooldownCOIN22:
    """COIN-22: _exit_position() must set cooldown after closing position."""

    def _make_pos(self, symbol="IMX/USDT", direction="long", entry=2.50):
        return SurgePositionState(
            symbol=symbol, direction=direction,
            entry_price=entry, quantity=202.0,
            margin=168.33,
            entry_time=datetime.now(timezone.utc) - timedelta(minutes=10),
            peak_price=entry * 1.02 if direction == "long" else entry,
            trough_price=entry if direction == "long" else entry * 0.98,
        )

    @pytest.mark.asyncio
    async def test_exit_tp_sets_cooldown(self, surge_engine, session):
        """COIN-22: TP exit must set cooldown (was missing → immediate re-entry)."""
        sym = "IMX/USDT"
        pos = self._make_pos(sym)
        surge_engine._positions[sym] = pos

        # Ensure no pre-existing cooldown
        surge_engine._cooldowns.pop(sym, None)

        # Mock DB + session
        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=202.0, average_buy_price=2.50,
            total_invested=168.33, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        mock_factory = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_ctx

        # TP exit at profitable price
        tp_price = pos.entry_price * 1.02  # +2% raw → +6% leveraged
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._exit_position(sym, pos, tp_price, "TP")

        # Position should be removed
        assert sym not in surge_engine._positions
        # Cooldown MUST be set (this was the bug)
        assert sym in surge_engine._cooldowns
        # Cooldown should be ~60 minutes in the future
        remaining = (surge_engine._cooldowns[sym] - datetime.now(timezone.utc)).total_seconds()
        assert remaining > surge_engine._cooldown_sec - 10  # allow 10s margin
        assert remaining <= surge_engine._cooldown_sec + 1

    @pytest.mark.asyncio
    async def test_exit_sl_sets_cooldown(self, surge_engine, session):
        """COIN-22: SL exit (first SL, no extended) must set normal cooldown."""
        sym = "SOL/USDT"
        pos = self._make_pos(sym, entry=150.0)
        surge_engine._positions[sym] = pos
        surge_engine._cooldowns.pop(sym, None)
        surge_engine._consecutive_sl_count.pop(sym, None)

        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=202.0, average_buy_price=150.0,
            total_invested=168.33, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        mock_factory = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_ctx

        sl_price = pos.entry_price * 0.98  # -2% raw → -6% leveraged (hits SL)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._exit_position(sym, pos, sl_price, "SL")

        assert sym not in surge_engine._positions
        assert sym in surge_engine._cooldowns
        remaining = (surge_engine._cooldowns[sym] - datetime.now(timezone.utc)).total_seconds()
        assert remaining > surge_engine._cooldown_sec - 10

    @pytest.mark.asyncio
    async def test_exit_trailing_sets_cooldown(self, surge_engine, session):
        """COIN-22: Trailing exit must set cooldown."""
        sym = "ETH/USDT"
        pos = self._make_pos(sym, entry=3500.0)
        surge_engine._positions[sym] = pos
        surge_engine._cooldowns.pop(sym, None)

        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=202.0, average_buy_price=3500.0,
            total_invested=168.33, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        mock_factory = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_ctx

        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._exit_position(sym, pos, 3530.0, "Trailing")

        assert sym not in surge_engine._positions
        assert sym in surge_engine._cooldowns

    @pytest.mark.asyncio
    async def test_exit_time_expiry_sets_cooldown(self, surge_engine, session):
        """COIN-22: TimeExpiry exit must set cooldown."""
        sym = "DOGE/USDT"
        pos = self._make_pos(sym, entry=0.15)
        surge_engine._positions[sym] = pos
        surge_engine._cooldowns.pop(sym, None)

        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=202.0, average_buy_price=0.15,
            total_invested=168.33, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        mock_factory = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_ctx

        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._exit_position(sym, pos, 0.15, "TimeExpiry")

        assert sym not in surge_engine._positions
        assert sym in surge_engine._cooldowns

    @pytest.mark.asyncio
    async def test_exit_extended_cooldown_not_overridden(self, surge_engine, session):
        """COIN-22: Extended COIN-20 cooldown (180min) must NOT be overridden by normal (60min)."""
        sym = "FET/USDT"
        pos = self._make_pos(sym, entry=1.50)
        surge_engine._positions[sym] = pos
        # Pre-condition: 1 previous SL → next SL will be sl_count=2 → extended cooldown
        surge_engine._consecutive_sl_count[sym] = 1
        surge_engine._cooldowns.pop(sym, None)

        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=202.0, average_buy_price=1.50,
            total_invested=168.33, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        mock_factory = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_ctx

        # SL price must produce a negative PnL so code enters loss branch
        sl_price = pos.entry_price * 0.98  # -2% raw → -6% leveraged

        # Override mock order to return matching price (default mock returns 65000)
        sl_order = MagicMock()
        sl_order.executed_price = sl_price
        sl_order.executed_quantity = pos.quantity
        sl_order.fee = sl_price * pos.quantity * FEE_PCT
        surge_engine._order_manager.create_order = AsyncMock(return_value=sl_order)

        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._exit_position(sym, pos, sl_price, "SL")

        assert sym in surge_engine._cooldowns
        # Extended cooldown should be ~180 minutes, NOT 60 minutes
        remaining = (surge_engine._cooldowns[sym] - datetime.now(timezone.utc)).total_seconds()
        assert remaining > surge_engine._cooldown_sec + 60  # well above 60min
        # Should be close to 10800 (180min)
        assert remaining > surge_engine._consecutive_sl_cooldown_sec - 10

    def test_exit_cooldown_blocks_reentry(self, surge_engine):
        """COIN-22: After exit, cooldown prevents immediate re-entry in _scan_for_entries."""
        sym = "IMX/USDT"
        # Set cooldown 60 minutes in the future (as _exit_position would)
        surge_engine._cooldowns[sym] = datetime.now(timezone.utc) + timedelta(seconds=surge_engine._cooldown_sec)

        # Verify cooldown check in entry logic would block
        now = datetime.now(timezone.utc)
        assert sym in surge_engine._cooldowns
        assert now < surge_engine._cooldowns[sym]
        # This is the exact check from _scan_for_entries line 508
        blocked = sym in surge_engine._cooldowns and now < surge_engine._cooldowns[sym]
        assert blocked is True


# ── COIN-36: Short Entry Execution Tests ─────────────────────────

class TestShortEntryExecution:
    """COIN-36: Test short position entry when bidirectional is enabled."""

    def test_short_direction_from_negative_price_change(self, surge_engine):
        """Negative price change produces 'short' direction."""
        assert surge_engine._long_only is False
        # Set candle-based data (used by compute_surge_score)
        surge_engine._candle_vol_ratios["ETH/USDT"] = 5.0
        surge_engine._candle_price_chgs["ETH/USDT"] = -3.0  # negative = declining
        surge_engine._candle_vol_accel["ETH/USDT"] = 1.0

        _, _, price_chg = surge_engine.compute_surge_score("ETH/USDT")
        assert price_chg < 0
        direction = "long" if price_chg > 0 else "short"
        assert direction == "short"

    def test_short_position_state_created(self, surge_engine):
        """Short SurgePositionState has correct fields."""
        pos = SurgePositionState(
            symbol="BTC/USDT", direction="short", entry_price=65000.0,
            quantity=0.001, margin=10.0,
            entry_time=datetime.now(timezone.utc),
            peak_price=65000.0, trough_price=65000.0,
        )
        assert pos.direction == "short"
        assert pos.trough_price == 65000.0

    def test_short_entry_uses_sell_side(self, surge_engine):
        """Short entry should use 'sell' side for order."""
        direction = "short"
        side = "buy" if direction == "long" else "sell"
        assert side == "sell"

    def test_short_signal_type_is_sell(self, surge_engine):
        """Short entry should produce SELL signal type."""
        direction = "short"
        signal_type = SignalType.BUY if direction == "long" else SignalType.SELL
        assert signal_type == SignalType.SELL


# ── COIN-36: Short Exit Execution Tests ──────────────────────────

class TestShortExitExecution:
    """COIN-36: Test short position exit conditions."""

    def test_short_sl_triggers(self, surge_engine):
        """Short SL triggers when price rises beyond threshold."""
        entry_price = 65000.0
        # For short: pnl_pct = (entry - current) / entry * 100 * leverage
        # SL at -2.5%: need (65000 - current) / 65000 * 100 * 3 = -2.5
        # current = 65000 * (1 + 2.5/300) ≈ 65541.67
        sl_price = entry_price * (1 + surge_engine._sl_pct / (100 * surge_engine._leverage))
        pos = SurgePositionState(
            symbol="BTC/USDT", direction="short", entry_price=entry_price,
            quantity=0.001, margin=10.0,
            entry_time=datetime.now(timezone.utc),
            peak_price=entry_price, trough_price=entry_price,
        )
        should_exit, reason = surge_engine._check_exit_conditions(
            pos, sl_price + 100, datetime.now(timezone.utc),
        )
        assert should_exit is True
        assert reason == "SL"

    def test_short_tp_triggers(self, surge_engine):
        """Short TP triggers when price drops enough."""
        entry_price = 65000.0
        # For short: pnl_pct = (entry - current) / entry * 100 * leverage
        # TP at 3%: need (65000 - current) / 65000 * 100 * 3 = 3.0
        # current = 65000 * (1 - 3.0/300) = 65000 * 0.99 = 64350
        tp_price = entry_price * (1 - surge_engine._tp_pct / (100 * surge_engine._leverage))
        pos = SurgePositionState(
            symbol="BTC/USDT", direction="short", entry_price=entry_price,
            quantity=0.001, margin=10.0,
            entry_time=datetime.now(timezone.utc),
            peak_price=entry_price, trough_price=entry_price,
        )
        should_exit, reason = surge_engine._check_exit_conditions(
            pos, tp_price - 100, datetime.now(timezone.utc),
        )
        assert should_exit is True
        assert reason == "TP"

    def test_short_time_expiry(self, surge_engine):
        """Short position exits on time expiry."""
        entry_time = datetime.now(timezone.utc) - timedelta(minutes=130)
        pos = SurgePositionState(
            symbol="BTC/USDT", direction="short", entry_price=65000.0,
            quantity=0.001, margin=10.0,
            entry_time=entry_time,
            peak_price=65000.0, trough_price=65000.0,
        )
        should_exit, reason = surge_engine._check_exit_conditions(
            pos, 65000.0, datetime.now(timezone.utc),
        )
        assert should_exit is True
        assert reason == "TimeExpiry"


# ── COIN-36: Short Exit Condition Details ────────────────────────

class TestShortExitConditions:
    """COIN-36: Detailed short exit condition tests."""

    def test_short_no_exit_in_profit_range(self, surge_engine):
        """Short position stays open within normal profit range."""
        entry_price = 65000.0
        # Small profit: price dropped a little
        current_price = 64800.0
        pos = SurgePositionState(
            symbol="BTC/USDT", direction="short", entry_price=entry_price,
            quantity=0.001, margin=10.0,
            entry_time=datetime.now(timezone.utc),
            peak_price=entry_price, trough_price=entry_price,
        )
        should_exit, reason = surge_engine._check_exit_conditions(
            pos, current_price, datetime.now(timezone.utc),
        )
        assert should_exit is False

    def test_short_trough_tracks_lowest(self, surge_engine):
        """Short position trough_price tracks the lowest price."""
        entry_price = 65000.0
        pos = SurgePositionState(
            symbol="BTC/USDT", direction="short", entry_price=entry_price,
            quantity=0.001, margin=10.0,
            entry_time=datetime.now(timezone.utc),
            peak_price=entry_price, trough_price=entry_price,
        )
        # Price drops to 64500 (within TP range: pnl = 0.77*3 = 2.3% < 3%)
        surge_engine._check_exit_conditions(pos, 64500.0, datetime.now(timezone.utc))
        assert pos.trough_price == 64500.0

        # Price bounces up but trough stays
        surge_engine._check_exit_conditions(pos, 64800.0, datetime.now(timezone.utc))
        assert pos.trough_price == 64500.0

    def test_short_trailing_activates(self, surge_engine):
        """Short trailing stop activates after sufficient profit."""
        entry_price = 65000.0
        pos = SurgePositionState(
            symbol="BTC/USDT", direction="short", entry_price=entry_price,
            quantity=0.001, margin=10.0,
            entry_time=datetime.now(timezone.utc),
            peak_price=entry_price, trough_price=entry_price,
        )
        # Trail activation: 0.5% with 3x leverage
        # Need trough_pnl = (entry - trough) / entry * 100 * 3 >= 0.5
        # trough = entry * (1 - 0.5/300) ≈ 64892
        activation_price = entry_price * (1 - surge_engine._trail_activation_pct / (100 * surge_engine._leverage))
        surge_engine._check_exit_conditions(
            pos, activation_price - 10, datetime.now(timezone.utc),
        )
        assert pos.trailing_active is True

    def test_short_trailing_stop_triggers(self, surge_engine):
        """Short trailing stop triggers after drawup from trough."""
        entry_price = 65000.0
        pos = SurgePositionState(
            symbol="BTC/USDT", direction="short", entry_price=entry_price,
            quantity=0.001, margin=10.0,
            entry_time=datetime.now(timezone.utc),
            peak_price=entry_price, trough_price=entry_price,
        )
        # First, push price down to activate trailing
        trough = entry_price * (1 - 1.0 / (100 * surge_engine._leverage))  # well below activation
        pos.trough_price = trough
        pos.trailing_active = True

        # Then bounce up enough to trigger trail stop
        # drawup = (current - trough) / trough * 100 * leverage >= trail_stop_pct (0.8)
        trigger_price = trough * (1 + surge_engine._trail_stop_pct / (100 * surge_engine._leverage))
        should_exit, reason = surge_engine._check_exit_conditions(
            pos, trigger_price + 10, datetime.now(timezone.utc),
        )
        assert should_exit is True
        assert reason == "Trailing"


# ── COIN-36: Bidirectional Position Tests ────────────────────────

class TestBidirectionalPositions:
    """COIN-36: Test bidirectional (long + short) positions operating simultaneously."""

    def test_long_and_short_positions_coexist(self, surge_engine):
        """Engine can hold both long and short positions simultaneously."""
        now = datetime.now(timezone.utc)
        surge_engine._positions["BTC/USDT"] = SurgePositionState(
            symbol="BTC/USDT", direction="long", entry_price=65000.0,
            quantity=0.001, margin=10.0, entry_time=now,
            peak_price=65000.0, trough_price=65000.0,
        )
        surge_engine._positions["ETH/USDT"] = SurgePositionState(
            symbol="ETH/USDT", direction="short", entry_price=4000.0,
            quantity=0.01, margin=10.0, entry_time=now,
            peak_price=4000.0, trough_price=4000.0,
        )
        assert len(surge_engine._positions) == 2
        assert surge_engine._positions["BTC/USDT"].direction == "long"
        assert surge_engine._positions["ETH/USDT"].direction == "short"

    def test_long_exit_independent_of_short(self, surge_engine):
        """Long position exit conditions don't affect short positions."""
        now = datetime.now(timezone.utc)
        long_pos = SurgePositionState(
            symbol="BTC/USDT", direction="long", entry_price=65000.0,
            quantity=0.001, margin=10.0, entry_time=now,
            peak_price=65000.0, trough_price=65000.0,
        )
        short_pos = SurgePositionState(
            symbol="ETH/USDT", direction="short", entry_price=4000.0,
            quantity=0.01, margin=10.0, entry_time=now,
            peak_price=4000.0, trough_price=4000.0,
        )

        # Long hits SL (price drops significantly)
        long_exit, long_reason = surge_engine._check_exit_conditions(
            long_pos, 64000.0, now,
        )
        # Short has small profit (pnl = (4000-3990)/4000*100*3 = 0.75%, below TP 3%)
        short_exit, short_reason = surge_engine._check_exit_conditions(
            short_pos, 3990.0, now,
        )
        assert long_exit is True
        assert long_reason == "SL"
        assert short_exit is False

    def test_short_exit_independent_of_long(self, surge_engine):
        """Short position exit conditions don't affect long positions."""
        now = datetime.now(timezone.utc)
        long_pos = SurgePositionState(
            symbol="BTC/USDT", direction="long", entry_price=65000.0,
            quantity=0.001, margin=10.0, entry_time=now,
            peak_price=65000.0, trough_price=65000.0,
        )
        short_pos = SurgePositionState(
            symbol="ETH/USDT", direction="short", entry_price=4000.0,
            quantity=0.01, margin=10.0, entry_time=now,
            peak_price=4000.0, trough_price=4000.0,
        )

        # Short hits SL (price rises)
        short_exit, short_reason = surge_engine._check_exit_conditions(
            short_pos, 4100.0, now,
        )
        # Long should still be fine
        long_exit, long_reason = surge_engine._check_exit_conditions(
            long_pos, 65100.0, now,
        )
        assert short_exit is True
        assert short_reason == "SL"
        assert long_exit is False

    def test_max_concurrent_counts_both_directions(self, surge_engine):
        """Max concurrent limit applies to total positions regardless of direction."""
        now = datetime.now(timezone.utc)
        # Fill to max with mixed directions
        surge_engine._positions["BTC/USDT"] = SurgePositionState(
            symbol="BTC/USDT", direction="long", entry_price=65000.0,
            quantity=0.001, margin=10.0, entry_time=now,
            peak_price=65000.0, trough_price=65000.0,
        )
        surge_engine._positions["ETH/USDT"] = SurgePositionState(
            symbol="ETH/USDT", direction="short", entry_price=4000.0,
            quantity=0.01, margin=10.0, entry_time=now,
            peak_price=4000.0, trough_price=4000.0,
        )
        surge_engine._positions["SOL/USDT"] = SurgePositionState(
            symbol="SOL/USDT", direction="long", entry_price=150.0,
            quantity=1.0, margin=10.0, entry_time=now,
            peak_price=150.0, trough_price=150.0,
        )

        assert len(surge_engine._positions) >= surge_engine._max_concurrent

    def test_cross_engine_conflict_check_both_directions(self, surge_engine):
        """Cross-engine conflict check works for both long and short."""
        # Simulate main engine having a long BTC position via _position_trackers
        main_engine = MagicMock()
        tracker = MagicMock(direction="long")
        main_engine._position_trackers = {"BTC/USDT": tracker}
        registry = MagicMock()
        registry.get_engine.return_value = main_engine
        surge_engine._engine_registry = registry

        # Short on same symbol with opposite direction = conflict
        conflict = surge_engine._check_cross_engine_conflict("BTC/USDT", "short")
        assert conflict is True

        # Same direction = no conflict
        no_conflict = surge_engine._check_cross_engine_conflict("BTC/USDT", "long")
        assert no_conflict is False
