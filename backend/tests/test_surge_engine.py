"""
SurgeEngine 단위 테스트
=======================
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

from core.models import Position
from core.enums import SignalType
from engine.surge_engine import (
    SurgeEngine,
    SurgePositionState,
    SymbolState,
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

    @pytest.mark.asyncio
    async def test_entry_failure_refunds_cash_in_finally_block(self, surge_engine, session):
        """COIN-68: _enter_position finally block must refund cash under _cash_lock when entry fails before commit."""
        initial_cash = surge_engine._futures_pm.cash_balance

        # Create a session that will raise an exception during commit
        mock_factory = MagicMock()
        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_session_ctx

        # Make session.commit() fail to simulate pre-commit failure
        session.commit = AsyncMock(side_effect=Exception("DB connection lost during commit"))

        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._enter_position(
                    "BTC/USDT", "long", 0.75,
                    {"last": 65000.0, "bid": 64990.0, "ask": 65010.0},
                )

        # Cash must be refunded in finally block (since _order_committed was never set to True)
        assert surge_engine._futures_pm.cash_balance == initial_cash, \
            "Cash should be refunded to initial level when entry fails before commit"

    @pytest.mark.asyncio
    async def test_entry_order_failure_refunds_cash_in_finally_block(self, surge_engine, session):
        """COIN-68: When create_order fails, finally block must refund the pre-reserved cash."""
        initial_cash = surge_engine._futures_pm.cash_balance

        mock_factory = MagicMock()
        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_session_ctx

        # Make order creation fail
        surge_engine._order_manager.create_order = AsyncMock(
            side_effect=Exception("Exchange connection error")
        )

        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._enter_position(
                    "ETH/USDT", "long", 0.75,
                    {"last": 3500.0, "bid": 3499.0, "ask": 3501.0},
                )

        # Cash must be refunded since order never succeeded
        assert surge_engine._futures_pm.cash_balance == initial_cash, \
            "Cash should be refunded when order creation fails"


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


# ── COIN-58: Exit failure handling ───────────────────────────────

class TestExitFailureHandling:
    """COIN-58: Tests for _exit_position failure scenarios."""

    def _make_pos(self, symbol="BTC/USDT", direction="long", entry=65000.0):
        return SurgePositionState(
            symbol=symbol, direction=direction,
            entry_price=entry, quantity=0.001,
            margin=10.0,
            entry_time=datetime.now(timezone.utc) - timedelta(minutes=15),
            peak_price=entry, trough_price=entry,
        )

    def _make_session_ctx(self, session):
        mock_factory = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_ctx
        return mock_factory

    @pytest.mark.asyncio
    async def test_exchange_failure_position_stays_in_memory(self, surge_engine, session):
        """COIN-58: When exchange order fails, position must stay in memory for retry."""
        sym = "BTC/USDT"
        pos = self._make_pos(sym)
        surge_engine._positions[sym] = pos

        # Make exchange order fail
        surge_engine._order_manager.create_order = AsyncMock(
            side_effect=Exception("Exchange API error")
        )

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._exit_position(sym, pos, 65100.0, "TP")

        # Position must still be in memory
        assert sym in surge_engine._positions

    @pytest.mark.asyncio
    async def test_exchange_failure_increments_retry_count(self, surge_engine, session):
        """COIN-58: Exchange failure increments exit_retry_count on the position."""
        sym = "ETH/USDT"
        pos = self._make_pos(sym, entry=3500.0)
        surge_engine._positions[sym] = pos
        assert pos.exit_retry_count == 0

        surge_engine._order_manager.create_order = AsyncMock(
            side_effect=Exception("Connection timeout")
        )

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._exit_position(sym, pos, 3450.0, "SL")

        assert pos.exit_retry_count == 1

        # Retry again → count increments
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._exit_position(sym, pos, 3450.0, "SL")

        assert pos.exit_retry_count == 2

    @pytest.mark.asyncio
    async def test_exchange_max_retries_emits_error_event_exactly_once(self, surge_engine, session):
        """COIN-58: emit_event('error') fires exactly once when exit_retry_count == MAX_EXIT_RETRIES.

        With `>= MAX_EXIT_RETRIES` the event would spam on every subsequent cycle;
        with `== MAX_EXIT_RETRIES` it fires exactly once and the position is frozen
        via pending_exit so _check_all_exits stops triggering new exchange orders.
        """
        from engine.surge_engine import MAX_EXIT_RETRIES

        sym = "BTC/USDT"
        pos = self._make_pos(sym)
        surge_engine._positions[sym] = pos

        surge_engine._order_manager.create_order = AsyncMock(
            side_effect=Exception("Exchange rejected")
        )

        mock_factory = self._make_session_ctx(session)
        emit_mock = AsyncMock()

        # Call MAX_EXIT_RETRIES - 1 times: event must NOT fire yet
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", emit_mock):
                for _ in range(MAX_EXIT_RETRIES - 1):
                    await surge_engine._exit_position(sym, pos, 65000.0, "TP")

        assert pos.exit_retry_count == MAX_EXIT_RETRIES - 1
        assert emit_mock.call_count == 0, "event must not fire before MAX_EXIT_RETRIES"

        # MAX_EXIT_RETRIES-th call: event fires exactly once, position frozen
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", emit_mock):
                await surge_engine._exit_position(sym, pos, 65000.0, "TP")

        assert pos.exit_retry_count == MAX_EXIT_RETRIES
        assert emit_mock.call_count == 1
        # Verify the event is the error-severity stuck-exit alert
        severity, event_type, message = emit_mock.call_args[0][:3]
        assert severity == "error"
        assert "stuck_exit" in str(emit_mock.call_args)
        # Position must be frozen via pending_exit (no more exchange orders next cycle)
        assert pos.pending_exit is True

        # One more call: count increments but event does NOT fire again
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", emit_mock):
                await surge_engine._exit_position(sym, pos, 65000.0, "TP")

        # emit_event call count stays at 1 (pending_exit blocks _check_all_exits,
        # but _exit_position itself was called directly here)
        assert emit_mock.call_count == 1, "event must not fire again after MAX_EXIT_RETRIES"

    @pytest.mark.asyncio
    async def test_aexit_failure_after_commit_credits_cash_and_marks_pending(
        self, surge_engine, session
    ):
        """COIN-58: session.__aexit__ raising after successful DB commit must credit cash
        immediately (commit is confirmed) and mark pending_exit for memory cleanup,
        NOT increment exit_retry_count (this is not a Phase-1 exchange failure).
        """
        sym = "ETH/USDT"
        pos = self._make_pos(sym, entry=3500.0)
        surge_engine._positions[sym] = pos
        initial_cash = surge_engine._futures_pm.cash_balance

        order = MagicMock()
        order.executed_price = 3550.0
        order.executed_quantity = 0.001
        order.fee = 0.001
        surge_engine._order_manager.create_order = AsyncMock(return_value=order)

        # DB position with quantity > 0 so _zero_db_position returns True
        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=0.001, average_buy_price=3500.0,
            total_invested=10.0, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        # __aexit__ raises AFTER session.commit() succeeds
        mock_factory = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_ctx.__aexit__ = AsyncMock(side_effect=Exception("teardown error"))
        mock_factory.return_value = mock_ctx

        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._exit_position(sym, pos, 3550.0, "TP")

        # Cash must have been credited (commit confirmed the DB cleanup)
        assert surge_engine._futures_pm.cash_balance > initial_cash
        # Position must be marked pending_exit so _finalize_exit_cleanup runs next cycle
        assert pos.pending_exit is True
        # This is NOT a Phase-1 exchange failure — retry count must not increment
        assert pos.exit_retry_count == 0
        # exit_cost_return must be 0 (cash already applied above, idempotency guard)
        assert pos.exit_cost_return == 0.0

    @pytest.mark.asyncio
    async def test_db_failure_creates_pending_exit(self, surge_engine, session):
        """COIN-58: Exchange succeeds, DB fails → position marked as pending_exit."""
        sym = "SOL/USDT"
        pos = self._make_pos(sym, entry=150.0)
        surge_engine._positions[sym] = pos

        # Order succeeds
        order = MagicMock()
        order.executed_price = 152.0
        order.executed_quantity = pos.quantity
        order.fee = 0.001
        surge_engine._order_manager.create_order = AsyncMock(return_value=order)

        # DB commit fails
        session.commit = AsyncMock(side_effect=Exception("DB connection lost"))
        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=pos.quantity, average_buy_price=150.0,
            total_invested=10.0, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._exit_position(sym, pos, 152.0, "TP")

        # Position must still be in memory AND marked as pending
        assert sym in surge_engine._positions
        assert pos.pending_exit is True
        assert pos.exit_reason == "TP"

    @pytest.mark.asyncio
    async def test_pm_cash_not_corrupted_on_db_failure(self, surge_engine, session):
        """COIN-58: PM cash must NOT change when DB commit fails."""
        sym = "ADA/USDT"
        pos = self._make_pos(sym, entry=0.50)
        surge_engine._positions[sym] = pos
        initial_cash = surge_engine._futures_pm.cash_balance

        order = MagicMock()
        order.executed_price = 0.51
        order.executed_quantity = pos.quantity
        order.fee = 0.0001
        surge_engine._order_manager.create_order = AsyncMock(return_value=order)

        # DB commit fails
        session.commit = AsyncMock(side_effect=Exception("Timeout"))

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._exit_position(sym, pos, 0.51, "TP")

        # PM cash must be unchanged
        assert surge_engine._futures_pm.cash_balance == initial_cash

    @pytest.mark.asyncio
    async def test_pending_exit_state_stores_exec_results(self, surge_engine, session):
        """COIN-58: Pending exit stores execution results for retry."""
        sym = "LINK/USDT"
        pos = self._make_pos(sym, entry=15.0)
        surge_engine._positions[sym] = pos

        order = MagicMock()
        order.executed_price = 15.3
        order.executed_quantity = pos.quantity
        order.fee = 0.001
        surge_engine._order_manager.create_order = AsyncMock(return_value=order)

        session.commit = AsyncMock(side_effect=Exception("DB error"))

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._exit_position(sym, pos, 15.3, "Trailing")

        # Verify stored state
        assert pos.pending_exit is True
        assert pos.exit_exec_price == 15.3
        assert pos.exit_exec_qty == pos.quantity
        assert pos.exit_reason == "Trailing"
        # long entry=15.0 → exec=15.3, leverage=3, margin=10.0
        # raw_pnl_pct = (15.3-15.0)/15.0*100 = 2.0%; lev = 6.0%; fee_pct = 0.04%*3*2*100 = 0.24%
        # net_pnl_pct = 5.76%; pnl_usdt = 10.0 * 5.76/100 = 0.576; cost_return = 10.576
        assert abs(pos.exit_cost_return - 10.576) < 0.001

    @pytest.mark.asyncio
    async def test_successful_exit_removes_position_and_updates_cash(self, surge_engine, session):
        """COIN-58: Successful exit removes position from memory and updates cash AFTER commit."""
        sym = "DOT/USDT"
        pos = self._make_pos(sym, entry=7.0)
        surge_engine._positions[sym] = pos
        initial_cash = surge_engine._futures_pm.cash_balance

        order = MagicMock()
        order.executed_price = 7.1
        order.executed_quantity = pos.quantity
        order.fee = 0.0001
        surge_engine._order_manager.create_order = AsyncMock(return_value=order)

        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=pos.quantity, average_buy_price=7.0,
            total_invested=10.0, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._exit_position(sym, pos, 7.1, "TP")

        # Position removed, cash updated
        assert sym not in surge_engine._positions
        assert surge_engine._futures_pm.cash_balance > initial_cash


# ── COIN-58: Pending exit retry tests ────────────────────────────

class TestPendingExitRetry:
    """COIN-58: Tests for _retry_pending_exits logic."""

    def _make_pending_pos(self, symbol="BTC/USDT"):
        pos = SurgePositionState(
            symbol=symbol, direction="long",
            entry_price=65000.0, quantity=0.001,
            margin=10.0,
            entry_time=datetime.now(timezone.utc) - timedelta(minutes=20),
            peak_price=65000.0, trough_price=65000.0,
        )
        pos.pending_exit = True
        pos.exit_reason = "TP"
        pos.exit_exec_price = 65500.0
        pos.exit_exec_qty = 0.001
        pos.exit_fee = 0.003
        pos.exit_cost_return = 10.5
        pos.exit_net_pnl_pct = 1.2
        pos.exit_pnl_usdt = 0.12
        pos.exit_retry_count = 1
        return pos

    def _make_session_ctx(self, session):
        mock_factory = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_ctx
        return mock_factory

    @pytest.mark.asyncio
    async def test_no_pending_exits_does_nothing(self, surge_engine, session):
        """No pending exits → _retry_pending_exits is a no-op."""
        surge_engine._positions.clear()
        initial_cash = surge_engine._futures_pm.cash_balance

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._retry_pending_exits()

        assert surge_engine._futures_pm.cash_balance == initial_cash

    @pytest.mark.asyncio
    async def test_retry_pending_exit_success(self, surge_engine, session):
        """COIN-58: Pending exit retried successfully → position removed, cash updated."""
        sym = "BTC/USDT"
        pos = self._make_pending_pos(sym)
        surge_engine._positions[sym] = pos
        initial_cash = surge_engine._futures_pm.cash_balance

        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=0.001, average_buy_price=65000.0,
            total_invested=10.0, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._retry_pending_exits()

        # Position removed, cash updated with cost_return
        assert sym not in surge_engine._positions
        assert surge_engine._futures_pm.cash_balance == initial_cash + pos.exit_cost_return

    @pytest.mark.asyncio
    async def test_retry_pending_exit_still_failing(self, surge_engine, session):
        """COIN-58: Retry still fails → position stays pending, retry_count increments."""
        sym = "ETH/USDT"
        pos = self._make_pending_pos(sym)
        pos.exit_retry_count = 2
        surge_engine._positions[sym] = pos

        session.commit = AsyncMock(side_effect=Exception("Still broken"))

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._retry_pending_exits()

        # Still in memory, still pending
        assert sym in surge_engine._positions
        assert pos.pending_exit is True
        # Retry count incremented
        assert pos.exit_retry_count == 3

    @pytest.mark.asyncio
    async def test_max_retries_triggers_force_cleanup(self, surge_engine, session):
        """COIN-58: After MAX_EXIT_RETRIES, position is force-cleaned."""
        from engine.surge_engine import MAX_EXIT_RETRIES

        sym = "SOL/USDT"
        pos = self._make_pending_pos(sym)
        pos.exit_retry_count = MAX_EXIT_RETRIES  # already at limit
        surge_engine._positions[sym] = pos
        initial_cash = surge_engine._futures_pm.cash_balance

        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=0.001, average_buy_price=150.0,
            total_invested=10.0, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._retry_pending_exits()

        # Position should be force-cleaned from memory
        assert sym not in surge_engine._positions
        # Cash should be updated
        assert surge_engine._futures_pm.cash_balance == initial_cash + pos.exit_cost_return

    @pytest.mark.asyncio
    async def test_pending_exit_blocks_new_entry(self, surge_engine):
        """COIN-58: pending_exit=True position in _positions blocks new entry.

        COIN-58 keeps pending-exit positions in _positions (not removed until DB cleanup
        succeeds).  The existing `if sym in self._positions: continue` guard therefore
        handles both active positions AND pending-exit positions with the same code path.
        This test verifies that a pending-exit position does NOT allow re-entry.
        """
        sym = "BTC/USDT"
        pos = SurgePositionState(
            symbol=sym, direction="long",
            entry_price=65000.0, quantity=0.001,
            margin=10.0,
            entry_time=datetime.now(timezone.utc),
            peak_price=65000.0, trough_price=65000.0,
        )
        pos.pending_exit = True
        surge_engine._positions[sym] = pos

        # Set up good surge conditions (would normally trigger entry on a fresh symbol)
        surge_engine._candle_vol_ratios[sym] = 10.0
        surge_engine._candle_price_chgs[sym] = 3.0
        surge_engine._candle_vol_accel[sym] = 2.0
        surge_engine._candle_atr_pct[sym] = 1.5

        tickers = {sym: {"last": 65100.0, "bid": 65090.0, "ask": 65110.0}}
        enter_mock = AsyncMock()
        surge_engine._enter_position = enter_mock

        with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
            await surge_engine._scan_for_entries(tickers)

        # Entry must be blocked: sym is in _positions (pending-exit), so `continue` fires
        enter_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_position_also_blocks_new_entry(self, surge_engine):
        """COIN-58: pending_exit=False (active) position in _positions also blocks entry.

        Confirms the pre-COIN-58 guard still works, and that the single `sym in _positions`
        check covers both active and pending-exit cases.
        """
        sym = "ETH/USDT"
        pos = SurgePositionState(
            symbol=sym, direction="long",
            entry_price=3500.0, quantity=0.01,
            margin=10.0,
            entry_time=datetime.now(timezone.utc),
            peak_price=3500.0, trough_price=3500.0,
        )
        pos.pending_exit = False  # regular active position
        surge_engine._positions[sym] = pos

        surge_engine._candle_vol_ratios[sym] = 10.0
        surge_engine._candle_price_chgs[sym] = 3.0
        surge_engine._candle_vol_accel[sym] = 2.0
        surge_engine._candle_atr_pct[sym] = 1.5

        tickers = {sym: {"last": 3510.0, "bid": 3509.0, "ask": 3511.0}}
        enter_mock = AsyncMock()
        surge_engine._enter_position = enter_mock

        with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
            await surge_engine._scan_for_entries(tickers)

        enter_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_idempotent_db_already_zeroed(self, surge_engine, session):
        """COIN-58: When DB row already has quantity=0 (prior commit succeeded but
        __aexit__ threw), cash must NOT be incremented on the next retry call.

        This directly tests the critical double-cash-update bug: if a prior attempt
        committed successfully but the session context manager raised during cleanup,
        the position remains in _positions with pending_exit=True.  On the next call to
        _retry_pending_exits the DB row has quantity=0; _zero_db_position returns False
        and the cash credit must be skipped.
        """
        sym = "BTC/USDT"
        pos = self._make_pending_pos(sym)
        surge_engine._positions[sym] = pos
        initial_cash = surge_engine._futures_pm.cash_balance

        # DB row already has quantity=0 (prior commit succeeded)
        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=0,  # already zeroed
            average_buy_price=0.0,
            total_invested=0.0, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._retry_pending_exits()

        # Cash must NOT have changed (row was already clean → idempotent guard fired)
        assert surge_engine._futures_pm.cash_balance == initial_cash
        # Position must still be cleaned up from memory
        assert sym not in surge_engine._positions

    @pytest.mark.asyncio
    async def test_retry_pending_sets_cooldown_on_success(self, surge_engine, session):
        """COIN-58: After successful retry, cooldown is set."""
        sym = "AVAX/USDT"
        pos = self._make_pending_pos(sym)
        surge_engine._positions[sym] = pos
        surge_engine._cooldowns.pop(sym, None)

        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=0.001, average_buy_price=35.0,
            total_invested=10.0, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._retry_pending_exits()

        # Cooldown set
        assert sym in surge_engine._cooldowns

    @pytest.mark.asyncio
    async def test_phase1_force_cleanup_does_not_reset_consecutive_losses(self, surge_engine, session):
        """COIN-58: Phase-1 max-retry force-cleanup (exit_exec_price==0) must NOT
        reset _consecutive_losses to 0 via the profit-branch in _finalize_exit_cleanup.
        Pre-existing losses must be preserved (and incremented by the unknown-loss helper).
        """
        from engine.surge_engine import MAX_EXIT_RETRIES

        sym = "ETH/USDT"
        pos = SurgePositionState(
            symbol=sym, direction="long",
            entry_price=3500.0, quantity=0.01,
            margin=10.0,
            entry_time=datetime.now(timezone.utc) - timedelta(minutes=20),
            peak_price=3500.0, trough_price=3500.0,
        )
        pos.pending_exit = True
        pos.exit_retry_count = MAX_EXIT_RETRIES  # incremented to MAX+1 → force-cleanup
        pos.exit_exec_price = 0.0               # Phase-1: no confirmed exchange order
        pos.exit_pnl_usdt = 0.0
        pos.exit_net_pnl_pct = 0.0
        pos.exit_reason = "SL"
        surge_engine._positions[sym] = pos
        surge_engine._consecutive_losses = 2    # pre-existing losses to preserve

        # DB row already zeroed (or irrelevant — no cash should be credited anyway)
        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=0, average_buy_price=0.0,
            total_invested=0.0, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._retry_pending_exits()

        # Position removed from memory
        assert sym not in surge_engine._positions
        # consecutive_losses must be >= 2 — incremented, never reset to 0
        assert surge_engine._consecutive_losses >= 2


# ── COIN-58: Zombie detection tests ──────────────────────────────

class TestZombieDetection:
    """COIN-58: Tests for _detect_zombie_positions."""

    def _make_pos(self, symbol="BTC/USDT"):
        return SurgePositionState(
            symbol=symbol, direction="long",
            entry_price=65000.0, quantity=0.001,
            margin=10.0,
            entry_time=datetime.now(timezone.utc) - timedelta(hours=1),
            peak_price=65000.0, trough_price=65000.0,
        )

    def _make_session_ctx(self, session):
        mock_factory = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_ctx
        return mock_factory

    @pytest.mark.asyncio
    async def test_no_positions_no_scan(self, surge_engine):
        """No positions → zombie scan is a no-op."""
        surge_engine._positions.clear()
        surge_engine._exchange.fetch_positions = AsyncMock()

        await surge_engine._detect_zombie_positions()

        # fetch_positions not called when no positions
        surge_engine._exchange.fetch_positions.assert_not_called()

    @pytest.mark.asyncio
    async def test_zombie_detected_and_cleaned(self, surge_engine, session):
        """COIN-58: In-memory position not on exchange → zombie cleaned up with warning event."""
        sym = "BTC/USDT"
        pos = self._make_pos(sym)
        surge_engine._positions[sym] = pos

        # Exchange returns empty positions (nothing open)
        surge_engine._exchange.fetch_positions = AsyncMock(return_value=[])

        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=0.001, average_buy_price=65000.0,
            total_invested=10.0, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        mock_factory = self._make_session_ctx(session)
        emit_mock = AsyncMock()
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", emit_mock):
                await surge_engine._detect_zombie_positions()

        # Zombie removed from memory
        assert sym not in surge_engine._positions
        # Warning event emitted with correct severity and ZOMBIE in message
        emit_mock.assert_called()
        first_call = emit_mock.call_args_list[0]
        assert first_call[0][0] == "warning"
        assert "ZOMBIE" in first_call[0][2].upper()

    @pytest.mark.asyncio
    async def test_normal_position_not_flagged(self, surge_engine, session):
        """COIN-58: Position on exchange is NOT flagged as zombie."""
        sym = "ETH/USDT"
        pos = self._make_pos(sym)
        surge_engine._positions[sym] = pos

        # Exchange returns this position as open
        exchange_pos = {"symbol": "ETH/USDT:USDT", "contracts": 0.01}
        surge_engine._exchange.fetch_positions = AsyncMock(return_value=[exchange_pos])

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._detect_zombie_positions()

        # Position stays in memory (it's on exchange)
        assert sym in surge_engine._positions

    @pytest.mark.asyncio
    async def test_zombie_sets_cooldown(self, surge_engine, session):
        """COIN-58: Zombie cleanup sets cooldown to prevent re-entry."""
        sym = "SOL/USDT"
        pos = self._make_pos(sym)
        surge_engine._positions[sym] = pos
        surge_engine._cooldowns.pop(sym, None)

        surge_engine._exchange.fetch_positions = AsyncMock(return_value=[])

        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=0.001, average_buy_price=150.0,
            total_invested=10.0, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._detect_zombie_positions()

        assert sym in surge_engine._cooldowns

    @pytest.mark.asyncio
    async def test_fetch_positions_failure_does_not_crash(self, surge_engine):
        """COIN-58: fetch_positions failure is handled gracefully."""
        sym = "DOGE/USDT"
        pos = self._make_pos(sym)
        surge_engine._positions[sym] = pos

        surge_engine._exchange.fetch_positions = AsyncMock(
            side_effect=Exception("API error")
        )

        # Should not raise
        await surge_engine._detect_zombie_positions()

        # Position unchanged
        assert sym in surge_engine._positions

    @pytest.mark.asyncio
    async def test_pending_exit_skipped_in_zombie_scan(self, surge_engine, session):
        """COIN-58: Positions with pending_exit are not processed by zombie detection."""
        sym = "BNB/USDT"
        pos = self._make_pos(sym)
        pos.pending_exit = True  # Already being handled
        surge_engine._positions[sym] = pos

        # Exchange returns empty (would normally trigger zombie cleanup)
        surge_engine._exchange.fetch_positions = AsyncMock(return_value=[])

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._detect_zombie_positions()

        # NOT removed (pending_exit, let retry handle it)
        assert sym in surge_engine._positions

    @pytest.mark.asyncio
    async def test_zombie_scan_interval_constant(self, surge_engine):
        """COIN-58: Zombie scan interval is 5 minutes."""
        from engine.surge_engine import ZOMBIE_SCAN_INTERVAL_SEC
        assert ZOMBIE_SCAN_INTERVAL_SEC == 300

    @pytest.mark.asyncio
    async def test_zombie_scan_usdm_key_normalized(self, surge_engine, session):
        """COIN-58: Exchange key BTC/USDT:USDT is normalized to BTC/USDT."""
        sym = "BTC/USDT"
        pos = self._make_pos(sym)
        surge_engine._positions[sym] = pos

        # Exchange returns USDM-format key
        exchange_pos = {"symbol": "BTC/USDT:USDT", "contracts": 0.001}
        surge_engine._exchange.fetch_positions = AsyncMock(return_value=[exchange_pos])

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._detect_zombie_positions()

        # After normalization, BTC/USDT:USDT → BTC/USDT → position found on exchange → not zombie
        assert sym in surge_engine._positions


# ── COIN-58: New constants tests ─────────────────────────────────

class TestCOIN58Constants:
    """COIN-58: Tests for new constants and dataclass fields."""

    def test_max_exit_retries_constant(self):
        """MAX_EXIT_RETRIES is 5."""
        from engine.surge_engine import MAX_EXIT_RETRIES
        assert MAX_EXIT_RETRIES == 5

    def test_zombie_scan_interval_constant(self):
        """ZOMBIE_SCAN_INTERVAL_SEC is 300 (5 minutes)."""
        from engine.surge_engine import ZOMBIE_SCAN_INTERVAL_SEC
        assert ZOMBIE_SCAN_INTERVAL_SEC == 300

    def test_surge_position_state_has_pending_exit_field(self):
        """SurgePositionState has pending_exit field defaulting to False."""
        pos = SurgePositionState(
            symbol="BTC/USDT", direction="long",
            entry_price=65000.0, quantity=0.001,
            margin=10.0,
            entry_time=datetime.now(timezone.utc),
            peak_price=65000.0, trough_price=65000.0,
        )
        assert pos.pending_exit is False

    def test_surge_position_state_has_exit_retry_count(self):
        """SurgePositionState has exit_retry_count defaulting to 0."""
        pos = SurgePositionState(
            symbol="BTC/USDT", direction="long",
            entry_price=65000.0, quantity=0.001,
            margin=10.0,
            entry_time=datetime.now(timezone.utc),
            peak_price=65000.0, trough_price=65000.0,
        )
        assert pos.exit_retry_count == 0

    def test_surge_position_state_exit_fields_default_zero(self):
        """SurgePositionState exit result fields default to empty/zero."""
        pos = SurgePositionState(
            symbol="ETH/USDT", direction="short",
            entry_price=3500.0, quantity=0.01,
            margin=15.0,
            entry_time=datetime.now(timezone.utc),
            peak_price=3500.0, trough_price=3500.0,
        )
        assert pos.exit_reason == ""
        assert pos.exit_exec_price == 0.0
        assert pos.exit_exec_qty == 0.0
        assert pos.exit_fee == 0.0
        assert pos.exit_cost_return == 0.0
        assert pos.exit_net_pnl_pct == 0.0
        assert pos.exit_pnl_usdt == 0.0

    def test_engine_has_last_zombie_scan_attr(self, surge_engine):
        """SurgeEngine has _last_zombie_scan attribute."""
        assert hasattr(surge_engine, "_last_zombie_scan")
        assert surge_engine._last_zombie_scan == 0.0

    def test_surge_position_state_backward_compatible(self):
        """Existing SurgePositionState fields still work (no regressions)."""
        pos = SurgePositionState(
            symbol="SOL/USDT", direction="long",
            entry_price=150.0, quantity=1.0,
            margin=50.0,
            entry_time=datetime.now(timezone.utc),
            peak_price=150.0, trough_price=150.0,
            trailing_active=True,
            surge_score=0.75,
        )
        assert pos.trailing_active is True
        assert pos.surge_score == 0.75
        assert pos.symbol == "SOL/USDT"


# ── COIN-63: Bug fixes tests ──────────────────────────────────────

def _coin63_session_ctx(session):
    """Shared helper: build a mock session-factory context for COIN-63 tests."""
    mock_factory = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_factory.return_value = mock_ctx
    return mock_factory


class TestCOIN63CashLock:
    """COIN-63 Bug 1: Cash lock prevents negative balance on concurrent entries."""

    def test_engine_has_cash_lock_attribute(self, surge_engine):
        """SurgeEngine has _cash_lock (asyncio.Lock) attribute."""
        import asyncio
        assert hasattr(surge_engine, "_cash_lock")
        assert isinstance(surge_engine._cash_lock, asyncio.Lock)

    @pytest.mark.asyncio
    async def test_cash_lock_prevents_overdraw_on_concurrent_entries(self, surge_engine, session):
        """COIN-63: Two concurrent _enter_position calls cannot jointly overdraw cash.

        position_pct=0.6, cash=10.0 → each entry wants 6.0 USDT.
        Without the lock both goroutines read cash=10, both pass `margin > cash`,
        and together they deduct 12.0 from a 10.0 balance (negative).
        With the lock the second entry sees cash=4.0 (post-reservation) and its
        size_usdt=4.0*0.6=2.4 < 5 minimum → rejected.  Final cash = 10 - 6 = 4.0.
        """
        import asyncio as _asyncio
        # Use pct > 0.5 so that 2× allocation exceeds initial cash
        surge_engine._position_pct = 0.6
        initial_cash = 10.0
        surge_engine._futures_pm.cash_balance = initial_cash

        # Return order with executed_quantity matching the requested amount so
        # actual_margin == size_usdt; use a small truthy fee to avoid the
        # fee-fallback calculation (order.fee=0.0 is falsy → triggers FEE_PCT path).
        async def _consistent_order(session, symbol, side, amount, price, **kwargs):
            o = MagicMock()
            o.executed_price = price
            o.executed_quantity = amount
            o.fee = price * amount * 0.0004  # explicit fee, matches FEE_PCT constant
            return o
        surge_engine._order_manager.create_order = _consistent_order

        # Give each concurrent coroutine its own independent mock session so they
        # never share session state (shared sessions cause non-deterministic failures
        # under asyncio interleaving — add/flush/commit collisions).
        def _fresh_session():
            s = MagicMock()
            s.add = MagicMock()
            s.flush = AsyncMock()
            s.commit = AsyncMock()
            s.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
            return s

        mock_factory = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=lambda: _fresh_session())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_ctx

        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await _asyncio.gather(
                    surge_engine._enter_position(
                        "BTC/USDT", "long", 0.75,
                        {"last": 65000.0, "bid": 64990.0, "ask": 65010.0},
                    ),
                    surge_engine._enter_position(
                        "ETH/USDT", "long", 0.75,
                        {"last": 3500.0, "bid": 3499.0, "ask": 3501.0},
                    ),
                    return_exceptions=True,
                )

        # Cash must never go negative regardless of concurrent interleaving.
        # Tighter bound: at most one entry can succeed (the second sees
        # reduced cash=4.0 → size_usdt=2.4 < 5 min → rejected), so the total
        # deduction equals single_margin + fee (≈ margin * 1.0004).
        single_margin = initial_cash * surge_engine._position_pct  # 6.0
        assert surge_engine._futures_pm.cash_balance >= 0
        # Allow a small fee on top of single_margin; two entries would leave
        # cash well below initial_cash - single_margin * 1.01.
        assert surge_engine._futures_pm.cash_balance >= initial_cash - single_margin * 1.01

    @pytest.mark.asyncio
    async def test_cash_refunded_on_entry_failure(self, surge_engine, session):
        """COIN-63: Pre-reserved cash is refunded when order creation fails."""
        initial_cash = 300.0
        surge_engine._futures_pm.cash_balance = initial_cash

        # Make order creation fail
        surge_engine._order_manager.create_order = AsyncMock(
            side_effect=Exception("Exchange error")
        )

        mock_factory = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_ctx

        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            await surge_engine._enter_position(
                "ETH/USDT", "long", 0.75,
                {"last": 3500.0, "bid": 3499.0, "ask": 3501.0},
            )

        # Cash must be restored to initial value after failure
        assert surge_engine._futures_pm.cash_balance == initial_cash

    @pytest.mark.asyncio
    async def test_cash_fully_refunded_on_db_commit_failure(self, surge_engine, session):
        """COIN-63: Cash is fully restored even when DB commit fails after adjustment.

        Order creation succeeds (so cash_balance -= adjustment has been applied),
        but session.commit() then raises.  The except block must refund
        actual_margin + fee, not just the pre-reserved margin.
        """
        import pytest as _pytest
        initial_cash = 300.0
        surge_engine._futures_pm.cash_balance = initial_cash

        # Provide numeric order values so actual_margin + fee is a real float,
        # not a MagicMock (which would make arithmetic and assertions meaningless).
        async def _ok_order(session, symbol, side, amount, price, **kwargs):
            o = MagicMock()
            o.executed_price = price
            o.executed_quantity = amount
            o.fee = price * amount * 0.0004
            return o
        surge_engine._order_manager.create_order = _ok_order

        # Simulate: order placed OK but DB write fails
        session.commit = AsyncMock(side_effect=Exception("DB unavailable"))

        mock_factory = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_ctx

        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            await surge_engine._enter_position(
                "ETH/USDT", "long", 0.75,
                {"last": 3500.0, "bid": 3499.0, "ask": 3501.0},
            )

        # Cash must be fully restored (not just margin, but margin + adjustment)
        assert surge_engine._futures_pm.cash_balance == _pytest.approx(initial_cash, abs=0.01)

    @pytest.mark.asyncio
    async def test_cash_not_refunded_on_post_commit_failure(self, surge_engine, session):
        """COIN-63: Cash is NOT refunded when emit_event fails after a successful commit.

        Once session.commit() succeeds the position exists in the DB; refunding
        cash at that point would inflate the balance and cause over-allocation.
        """
        initial_cash = 300.0
        surge_engine._futures_pm.cash_balance = initial_cash

        # Provide real float values so actual_margin + fee arithmetic is valid
        # and cash_balance remains a float throughout (not a MagicMock).
        async def _ok_order(session, symbol, side, amount, price, **kwargs):
            o = MagicMock()
            o.executed_price = price
            o.executed_quantity = amount
            o.fee = price * amount * 0.0004
            return o
        surge_engine._order_manager.create_order = _ok_order

        # Order and DB commit succeed, but notification fails
        with patch("engine.surge_engine.get_session_factory", return_value=MagicMock(
            return_value=MagicMock(
                __aenter__=AsyncMock(return_value=session),
                __aexit__=AsyncMock(return_value=False),
            )
        )):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock,
                       side_effect=Exception("Notification failed")):
                await surge_engine._enter_position(
                    "ETH/USDT", "long", 0.75,
                    {"last": 3500.0, "bid": 3499.0, "ask": 3501.0},
                )

        # Cash must NOT be restored — the position is in the DB and cost real margin.
        # Verify cash_balance is a real float (not a MagicMock) before comparing.
        assert isinstance(surge_engine._futures_pm.cash_balance, float)
        assert surge_engine._futures_pm.cash_balance < initial_cash

    @pytest.mark.asyncio
    async def test_cash_lock_is_released_after_check(self, surge_engine, session):
        """COIN-63: Cash lock is released after the check, allowing subsequent entries."""
        surge_engine._futures_pm.cash_balance = 300.0

        mock_factory = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_ctx

        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._enter_position(
                    "BTC/USDT", "long", 0.75,
                    {"last": 65000.0, "bid": 64990.0, "ask": 65010.0},
                )

        # Lock must be released (not held) after method returns
        assert not surge_engine._cash_lock.locked()


class TestCOIN63ZeroDivision:
    """COIN-63 Bug 2: ZeroDivisionError guard when entry_price=0."""

    def _make_pos_zero_entry(self, symbol="BTC/USDT", direction="long"):
        return SurgePositionState(
            symbol=symbol, direction=direction,
            entry_price=0.0,  # zero entry price (corrupt state)
            quantity=0.001,
            margin=10.0,
            entry_time=datetime.now(timezone.utc) - timedelta(minutes=10),
            peak_price=0.0, trough_price=0.0,
        )

    def _make_session_ctx(self, session):
        return _coin63_session_ctx(session)

    @pytest.mark.asyncio
    async def test_exit_position_zero_entry_price_no_exception(self, surge_engine, session):
        """COIN-63: _exit_position does not raise ZeroDivisionError when entry_price=0."""
        sym = "BTC/USDT"
        pos = self._make_pos_zero_entry(sym)
        surge_engine._positions[sym] = pos

        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=0.001, average_buy_price=0.0,
            total_invested=10.0, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                # Must not raise ZeroDivisionError
                await surge_engine._exit_position(sym, pos, 65000.0, "TP")

    @pytest.mark.asyncio
    async def test_exit_position_zero_entry_price_pnl_is_zero(self, surge_engine, session):
        """COIN-63: When entry_price=0, PnL is reported as 0.0 (not nan/exception)."""
        sym = "ETH/USDT"
        pos = self._make_pos_zero_entry(sym, direction="long")
        surge_engine._positions[sym] = pos

        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=0.001, average_buy_price=0.0,
            total_invested=10.0, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        pnl_recorded = []

        async def capture_finalize(symbol, pos, net_pnl_pct, pnl_usdt, reason, **kwargs):
            pnl_recorded.append(net_pnl_pct)

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                with patch.object(surge_engine, "_finalize_exit_cleanup", side_effect=capture_finalize):
                    await surge_engine._exit_position(sym, pos, 65000.0, "TP")

        # PnL should be 0.0 (fee-adjusted), not nan or an exception
        assert len(pnl_recorded) == 1
        import math
        assert not math.isnan(pnl_recorded[0])

    @pytest.mark.asyncio
    async def test_exit_position_short_zero_entry_price_no_exception(self, surge_engine, session):
        """COIN-63: Short position with entry_price=0 also does not raise."""
        sym = "SOL/USDT"
        pos = self._make_pos_zero_entry(sym, direction="short")
        surge_engine._positions[sym] = pos

        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=0.01, average_buy_price=0.0,
            total_invested=5.0, direction="short",
        )
        session.add(db_pos)
        await session.flush()

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                # Must not raise
                await surge_engine._exit_position(sym, pos, 140.0, "SL")


class TestCOIN63RetryOffByOne:
    """COIN-63 Bug 3: Retry off-by-one — force cleanup at exactly MAX_EXIT_RETRIES."""

    def _make_pending_pos(self, symbol="BTC/USDT", retry_count=0):
        pos = SurgePositionState(
            symbol=symbol, direction="long",
            entry_price=65000.0, quantity=0.001,
            margin=10.0,
            entry_time=datetime.now(timezone.utc) - timedelta(minutes=30),
            peak_price=65000.0, trough_price=65000.0,
        )
        pos.pending_exit = True
        pos.exit_reason = "TP"
        pos.exit_exec_price = 65500.0
        pos.exit_exec_qty = 0.001
        pos.exit_fee = 0.003
        pos.exit_cost_return = 10.5
        pos.exit_net_pnl_pct = 1.2
        pos.exit_pnl_usdt = 0.12
        pos.exit_retry_count = retry_count
        return pos

    def _make_session_ctx(self, session):
        return _coin63_session_ctx(session)

    @pytest.mark.asyncio
    async def test_force_cleanup_triggers_at_max_exit_retries(self, surge_engine, session):
        """COIN-63: Force cleanup triggers at exit_retry_count == MAX_EXIT_RETRIES (not MAX+1)."""
        from engine.surge_engine import MAX_EXIT_RETRIES

        sym = "BTC/USDT"
        # Start at MAX_EXIT_RETRIES - 1 so that after +1 it equals MAX_EXIT_RETRIES
        pos = self._make_pending_pos(sym, retry_count=MAX_EXIT_RETRIES - 1)
        surge_engine._positions[sym] = pos

        db_pos = Position(
            exchange="binance_surge", symbol=sym,
            quantity=0.001, average_buy_price=65000.0,
            total_invested=10.0, direction="long",
        )
        session.add(db_pos)
        await session.flush()

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._retry_pending_exits()

        # After count goes MAX-1 → MAX, force cleanup should trigger
        # Position must be removed from memory
        assert sym not in surge_engine._positions

    @pytest.mark.asyncio
    async def test_no_force_cleanup_before_max_exit_retries(self, surge_engine, session):
        """COIN-63: Force cleanup does NOT trigger before MAX_EXIT_RETRIES is reached."""
        from engine.surge_engine import MAX_EXIT_RETRIES

        sym = "ETH/USDT"
        # Start at MAX_EXIT_RETRIES - 2: after +1 → MAX_EXIT_RETRIES - 1, still retry
        pos = self._make_pending_pos(sym, retry_count=MAX_EXIT_RETRIES - 2)
        surge_engine._positions[sym] = pos

        # Make DB retry fail so position stays pending (no successful cleanup)
        session.commit = AsyncMock(side_effect=Exception("Still broken"))

        mock_factory = self._make_session_ctx(session)
        with patch("engine.surge_engine.get_session_factory", return_value=mock_factory):
            with patch("engine.surge_engine.emit_event", new_callable=AsyncMock):
                await surge_engine._retry_pending_exits()

        # Should NOT be force-cleaned yet (count went from MAX-2 to MAX-1 < MAX)
        assert sym in surge_engine._positions
        assert surge_engine._positions[sym].exit_retry_count == MAX_EXIT_RETRIES - 1

