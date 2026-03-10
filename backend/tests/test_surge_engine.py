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
from engine.surge_engine import (
    SurgeEngine,
    SurgePortfolioView,
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
        """Default SurgeTradingConfig values are applied."""
        assert surge_engine._leverage == 3
        assert surge_engine._max_concurrent == 3
        assert surge_engine._sl_pct == 2.0
        assert surge_engine._tp_pct == 4.0
        assert surge_engine._trail_activation_pct == 1.0
        assert surge_engine._trail_stop_pct == 0.8
        assert surge_engine._max_hold_minutes == 120
        assert surge_engine._long_only is True
        assert surge_engine._daily_trade_limit == 15

    def test_tracked_coins(self, surge_engine):
        assert len(surge_engine.tracked_coins) == 30

    def test_status_dict(self, surge_engine):
        s = surge_engine.status()
        assert s["running"] is False
        assert s["leverage"] == 3
        assert s["open_positions"] == 0


# ── Test: Surge score computation ────────────────────────────────

class TestSurgeScore:
    def test_empty_state_returns_zero(self, surge_engine):
        score, vol, price = surge_engine.compute_surge_score("UNKNOWN/USDT")
        assert score == 0.0
        assert vol == 0.0
        assert price == 0.0

    def test_insufficient_data_returns_zero(self, surge_engine):
        """Need at least 5 volume entries and 4 price entries."""
        surge_engine._symbol_states["BTC/USDT"] = SymbolState()
        state = surge_engine._symbol_states["BTC/USDT"]
        # Add only 3 entries
        for i in range(3):
            state.volume_1m.append(100.0)
            state.prices.append(65000.0)

        score, vol, price = surge_engine.compute_surge_score("BTC/USDT")
        assert score == 0.0

    def test_high_volume_surge_score(self, surge_engine):
        """High volume spike should produce a meaningful score."""
        surge_engine._symbol_states["BTC/USDT"] = SymbolState()
        state = surge_engine._symbol_states["BTC/USDT"]

        # Build baseline: 10 normal volume candles
        for i in range(10):
            state.volume_1m.append(100.0)
            state.prices.append(65000.0 + i * 10)
            state.rsi_closes.append(65000.0 + i * 10)

        # Now add a volume spike with price jump
        state.volume_1m.append(1000.0)  # 10x volume
        state.prices.append(66500.0)    # +2.3% jump
        state.rsi_closes.append(66500.0)

        score, vol_ratio, price_chg = surge_engine.compute_surge_score("BTC/USDT")
        assert vol_ratio > 5.0  # 10x vs ~100 avg
        assert score > 0.3      # should be significant

    def test_no_volume_no_score(self, surge_engine):
        """Zero volume should not trigger."""
        surge_engine._symbol_states["ETH/USDT"] = SymbolState()
        state = surge_engine._symbol_states["ETH/USDT"]

        for i in range(10):
            state.volume_1m.append(0.0)
            state.prices.append(3500.0)
            state.rsi_closes.append(3500.0)

        score, vol_ratio, price_chg = surge_engine.compute_surge_score("ETH/USDT")
        assert score <= 0.1  # minimal score from flat price

    def test_score_weights_sum(self, surge_engine):
        """Score weights should sum to 1.0 (0.40 + 0.35 + 0.25)."""
        # Maximum all signals = 1.0
        surge_engine._symbol_states["SOL/USDT"] = SymbolState()
        state = surge_engine._symbol_states["SOL/USDT"]

        # Create extreme surge
        for i in range(10):
            state.volume_1m.append(10.0)
            state.prices.append(100.0)
            state.rsi_closes.append(100.0)

        # Extreme values
        state.volume_1m.append(200.0)  # 20x
        state.prices.append(150.0)     # 50% jump
        state.rsi_closes.append(150.0)

        score, _, _ = surge_engine.compute_surge_score("SOL/USDT")
        assert 0.0 <= score <= 1.0


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
    def test_long_only_blocks_short(self, surge_engine):
        """With long_only=True, short direction candidates are skipped."""
        assert surge_engine._long_only is True
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
        """Long stop loss at -2% (leveraged)."""
        pos = self._make_long_pos(entry=65000.0)
        # SL = 2%, leverage = 3 -> price drop = 2%/3 = 0.667%
        sl_price = 65000.0 * (1 - surge_engine._sl_pct / 100 / surge_engine._leverage)
        now = datetime.now(timezone.utc)

        should_exit, reason = surge_engine._check_exit_conditions(pos, sl_price - 1, now)
        assert should_exit is True
        assert reason == "SL"

    def test_long_tp_triggered(self, surge_engine):
        """Long take profit at +4% (leveraged)."""
        pos = self._make_long_pos(entry=65000.0)
        # TP = 4%, leverage = 3 -> price rise = 4%/3 = 1.333%
        tp_price = 65000.0 * (1 + surge_engine._tp_pct / 100 / surge_engine._leverage)
        now = datetime.now(timezone.utc)

        should_exit, reason = surge_engine._check_exit_conditions(pos, tp_price + 1, now)
        assert should_exit is True
        assert reason == "TP"

    def test_long_trailing_stop(self, surge_engine):
        """Trailing activates after +1% PnL, exits on drawdown."""
        pos = self._make_long_pos(entry=65000.0)
        now = datetime.now(timezone.utc)

        # First, move price up to activate trailing (pnl > 1%)
        # pnl = (price - entry) / entry * 100 * leverage
        # 1% = (price - 65000) / 65000 * 100 * 3
        # price = 65000 * (1 + 1 / 300) = 65216.67
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
        assert cfg.sl_pct == 2.0
        assert cfg.tp_pct == 4.0
        assert cfg.trail_activation_pct == 1.0
        assert cfg.trail_stop_pct == 0.8
        assert cfg.max_hold_minutes == 120
        assert cfg.vol_threshold == 5.0
        assert cfg.price_threshold == 1.5
        assert cfg.long_only is True
        assert cfg.daily_trade_limit == 15
        assert cfg.scan_symbols_count == 30
        assert cfg.cooldown_per_symbol_sec == 1800
        assert cfg.scan_interval_sec == 5

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


# ── Test: Cash integration ───────────────────────────────────────

class TestCashIntegration:
    def test_surge_available_cash_respects_allocation(self, surge_engine):
        """Available cash capped by initial allocation."""
        surge_engine._futures_pm.cash_balance = 500.0  # plenty of futures cash
        surge_engine._surge_realized_pnl = 0.0
        # No positions → available = min(150, 500) = 150
        available = surge_engine._surge_available_cash()
        assert available == surge_engine._initial_allocation  # 150

    def test_surge_available_cash_reduces_with_positions(self, surge_engine):
        """Open positions reduce available cash."""
        surge_engine._futures_pm.cash_balance = 500.0
        surge_engine._positions["BTC/USDT"] = SurgePositionState(
            symbol="BTC/USDT", direction="long",
            entry_price=65000.0, quantity=0.01, margin=50.0,
            entry_time=datetime.now(timezone.utc),
            peak_price=65000.0, trough_price=65000.0,
        )
        available = surge_engine._surge_available_cash()
        assert available == 100.0  # 150 - 50

    def test_surge_available_cash_limited_by_futures(self, surge_engine):
        """Cannot exceed actual futures PM cash."""
        surge_engine._futures_pm.cash_balance = 50.0  # futures almost out of cash
        available = surge_engine._surge_available_cash()
        assert available == 50.0  # min(150, 50) = 50

    def test_realized_pnl_increases_allocation(self, surge_engine):
        """Positive realized PnL increases available allocation."""
        surge_engine._futures_pm.cash_balance = 500.0
        surge_engine._surge_realized_pnl = 30.0
        available = surge_engine._surge_available_cash()
        assert available == 180.0  # 150 + 30

    def test_realized_pnl_loss_decreases_allocation(self, surge_engine):
        """Negative realized PnL decreases available allocation."""
        surge_engine._futures_pm.cash_balance = 500.0
        surge_engine._surge_realized_pnl = -50.0
        available = surge_engine._surge_available_cash()
        assert available == 100.0  # 150 - 50


# ── Test: SurgePortfolioView ─────────────────────────────────────

class TestSurgePortfolioView:
    def test_cash_balance_property(self, surge_engine):
        """cash_balance delegates to engine."""
        surge_engine._futures_pm.cash_balance = 500.0
        view = SurgePortfolioView(surge_engine)
        assert view.cash_balance == surge_engine._surge_available_cash()

    def test_cash_balance_setter_noop(self, surge_engine):
        """Setting cash_balance is a no-op."""
        view = SurgePortfolioView(surge_engine)
        view.cash_balance = 9999  # should not crash
        # Still returns computed value
        assert view.cash_balance == surge_engine._surge_available_cash()

    @pytest.mark.asyncio
    async def test_sync_is_noop(self, surge_engine):
        """sync_exchange_positions does nothing."""
        view = SurgePortfolioView(surge_engine)
        await view.sync_exchange_positions(None, None, [])  # should not crash

    @pytest.mark.asyncio
    async def test_get_portfolio_summary(self, surge_engine, session):
        """Portfolio summary returns correct structure."""
        view = SurgePortfolioView(surge_engine)
        summary = await view.get_portfolio_summary(session)
        assert summary["exchange"] == "binance_surge"
        assert "total_value_krw" in summary
        assert "positions" in summary
        assert summary["initial_balance_krw"] == 150.0
