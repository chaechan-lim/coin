"""
Tests for new R&D engines: BreakoutPullback, VolumeMomentum, BTCNeutralAltMR.

Each engine has:
1. test_get_status_initial — 초기 상태 확인
2. test_evaluate_no_crash — evaluate_now() 호출 시 에러 없음
3. test_loss_limit_pause — 누적 손실 -10% 도달 시 _paused=True
4. test_daily_loss_pause — 일일 손실 -5% 도달 시 _daily_paused=True
5. test_record_order — DB에 주문 기록 확인
6. 엔진별 전략 고유 테스트
"""
import os
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("DB_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("EXCHANGE_API_KEY", "test")
os.environ.setdefault("EXCHANGE_API_SECRET", "test")
os.environ.setdefault("TRADING_MODE", "paper")

from core.models import Base, Order
from config import AppConfig


# ── Fixtures ──────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine):
    return async_sessionmaker(
        bind=db_engine, class_=AsyncSession, expire_on_commit=False
    )


@dataclass
class FakeOrder:
    executed_price: float = 0.0
    executed_quantity: float = 0.0
    status: str = "filled"
    filled: float = 0.0
    price: float = 0.0
    average: float = 0.0

    def __post_init__(self):
        if self.filled == 0 and self.executed_quantity > 0:
            self.filled = self.executed_quantity
        if self.price == 0 and self.executed_price > 0:
            self.price = self.executed_price
            self.average = self.executed_price


def _make_ohlcv_df(n: int = 50, base_price: float = 100.0, timeframe: str = "1h",
                    vol_base: float = 1000.0, trend: float = 0.0) -> pd.DataFrame:
    """Create a fake OHLCV DataFrame for testing."""
    dates = pd.date_range("2026-01-01", periods=n, freq="1h")
    prices = np.linspace(base_price, base_price + trend * n, n)
    noise = np.random.default_rng(42).normal(0, base_price * 0.005, n)
    closes = prices + noise
    highs = closes * 1.01
    lows = closes * 0.99
    opens = closes * 1.001
    volumes = np.full(n, vol_base) + np.random.default_rng(42).normal(0, vol_base * 0.1, n)

    return pd.DataFrame({
        "timestamp": dates,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": np.abs(volumes),
    })


def _make_daily_df_with_breakout(n: int = 30, base_price: float = 100.0,
                                  breakout_high: bool = False,
                                  breakout_low: bool = False) -> pd.DataFrame:
    """Create daily OHLCV with optional breakout on last candle."""
    dates = pd.date_range("2026-01-01", periods=n, freq="1D")
    prices = np.full(n, base_price)
    noise = np.random.default_rng(42).normal(0, base_price * 0.01, n)
    closes = prices + noise
    highs = closes * 1.02
    lows = closes * 0.98
    opens = closes * 1.001
    volumes = np.full(n, 1000.0)

    if breakout_high:
        # Last candle breaks above all previous highs
        closes[-1] = float(highs[:-1].max()) + base_price * 0.03
        highs[-1] = closes[-1] * 1.01
    if breakout_low:
        closes[-1] = float(lows[:-1].min()) - base_price * 0.03
        lows[-1] = closes[-1] * 0.99

    return pd.DataFrame({
        "timestamp": dates,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def _mock_exchange():
    exchange = AsyncMock()
    exchange.create_market_buy = AsyncMock(return_value=FakeOrder(executed_price=100.0, executed_quantity=1.0))
    exchange.create_market_sell = AsyncMock(return_value=FakeOrder(executed_price=100.0, executed_quantity=1.0))
    return exchange


def _mock_market_data(df_map=None):
    md = AsyncMock()
    if df_map:
        async def get_ohlcv(symbol, tf, limit=None):
            key = f"{symbol}_{tf}"
            if key in df_map:
                d = df_map[key]
                if limit and len(d) > limit:
                    return d.iloc[-limit:]
                return d
            # fallback: generic
            return _make_ohlcv_df(n=limit or 50)
        md.get_ohlcv_df = AsyncMock(side_effect=get_ohlcv)
    else:
        md.get_ohlcv_df = AsyncMock(return_value=_make_ohlcv_df())
    return md


# ═══════════════════════════════════════════════════════════════
# BreakoutPullbackEngine Tests
# ═══════════════════════════════════════════════════════════════

class TestBreakoutPullbackEngine:

    def _make_engine(self, exchange=None, market_data=None, capital=150.0):
        from engine.breakout_pullback_engine import BreakoutPullbackEngine
        return BreakoutPullbackEngine(
            config=AppConfig(),
            futures_exchange=exchange or _mock_exchange(),
            market_data=market_data or _mock_market_data(),
            initial_capital_usdt=capital,
            leverage=2,
        )

    @pytest.mark.asyncio
    async def test_get_status_initial(self):
        engine = self._make_engine()
        status = engine.get_status()
        assert status["exchange"] == "binance_breakout_pb"
        assert status["is_running"] is False
        assert status["leverage"] == 2
        assert status["capital_usdt"] == 150.0
        assert status["cumulative_pnl"] == 0.0
        assert status["daily_pnl"] == 0.0
        assert status["paused"] is False
        assert len(status["positions"]) == 0
        assert len(status["pending_signals"]) == 0

    @pytest.mark.asyncio
    async def test_evaluate_no_crash(self, session_factory):
        md = _mock_market_data()
        md.get_ohlcv_df = AsyncMock(return_value=_make_ohlcv_df(n=30, timeframe="1d"))
        engine = self._make_engine(market_data=md)

        with patch("engine.breakout_pullback_engine.get_session_factory", return_value=session_factory):
            with patch("engine.breakout_pullback_engine.emit_event", new_callable=AsyncMock):
                await engine.evaluate_now()

        # Should not crash
        assert engine._paused is False

    @pytest.mark.asyncio
    async def test_loss_limit_pause(self):
        engine = self._make_engine(capital=100.0)
        engine._cumulative_pnl = -9.0  # Not yet at limit
        engine._initial_capital = 100.0

        with patch("engine.breakout_pullback_engine.emit_event", new_callable=AsyncMock):
            await engine._check_loss_limits()
        assert engine._paused is False

        engine._cumulative_pnl = -10.0  # At limit (-10%)
        with patch("engine.breakout_pullback_engine.emit_event", new_callable=AsyncMock):
            await engine._check_loss_limits()
        assert engine._paused is True

    @pytest.mark.asyncio
    async def test_daily_loss_pause(self):
        engine = self._make_engine(capital=100.0)
        engine._daily_pnl = -4.0  # Not yet at limit
        engine._initial_capital = 100.0

        with patch("engine.breakout_pullback_engine.emit_event", new_callable=AsyncMock):
            await engine._check_loss_limits()
        assert engine._daily_paused is False

        engine._daily_pnl = -5.0  # At limit (-5%)
        with patch("engine.breakout_pullback_engine.emit_event", new_callable=AsyncMock):
            await engine._check_loss_limits()
        assert engine._daily_paused is True

    @pytest.mark.asyncio
    async def test_record_order(self, session_factory):
        engine = self._make_engine()

        with patch("engine.breakout_pullback_engine.get_session_factory", return_value=session_factory):
            await engine._record_order("BTC/USDT", "buy", 50000.0, 0.01,
                                        pnl=0.0, reason="breakout_pb_long_entry")

        async with session_factory() as session:
            result = await session.execute(select(Order))
            orders = result.scalars().all()
            assert len(orders) == 1
            assert orders[0].exchange == "binance_breakout_pb"
            assert orders[0].symbol == "BTC/USDT"
            assert orders[0].side == "buy"
            assert orders[0].strategy_name == "breakout_pullback"
            assert orders[0].signal_reason == "breakout_pb_long_entry"

    @pytest.mark.asyncio
    async def test_pending_signal_management(self):
        """풀백 대기 상태 관리 테스트."""
        from engine.breakout_pullback_engine import BreakoutPullbackEngine, BreakoutSignal

        # Breakout high → pending signal created
        df_breakout = _make_daily_df_with_breakout(n=30, base_price=100.0, breakout_high=True)
        md = _mock_market_data({"BTC/USDT_1d": df_breakout})
        exchange = _mock_exchange()
        engine = self._make_engine(exchange=exchange, market_data=md)

        with patch("engine.breakout_pullback_engine.get_session_factory"):
            with patch("engine.breakout_pullback_engine.emit_event", new_callable=AsyncMock):
                await engine._evaluate_symbol("BTC/USDT")

        # Should have a pending signal
        assert "BTC/USDT" in engine._pending_signals
        assert engine._pending_signals["BTC/USDT"].side == "long"

    @pytest.mark.asyncio
    async def test_pending_signal_expiry(self):
        """3일 초과 시 시그널 취소."""
        from engine.breakout_pullback_engine import BreakoutSignal

        engine = self._make_engine()
        # Create an expired signal
        engine._pending_signals["BTC/USDT"] = BreakoutSignal(
            symbol="BTC/USDT", side="long", breakout_price=105.0,
            detected_at=datetime.now(timezone.utc) - timedelta(days=4),
        )

        with patch("engine.breakout_pullback_engine.get_session_factory"):
            with patch("engine.breakout_pullback_engine.emit_event", new_callable=AsyncMock):
                await engine._check_pullback_entry("BTC/USDT",
                                                    engine._pending_signals["BTC/USDT"],
                                                    100.0)

        assert "BTC/USDT" not in engine._pending_signals

    @pytest.mark.asyncio
    async def test_tracked_coins(self):
        engine = self._make_engine()
        assert "BTC/USDT" in engine.tracked_coins
        assert len(engine.tracked_coins) >= 5

    @pytest.mark.asyncio
    async def test_set_methods_no_op(self):
        engine = self._make_engine()
        engine.set_engine_registry(None)
        engine.set_broadcast_callback(None)
        engine.set_agent_coordinator(None)
        engine.set_futures_rnd_coordinator(MagicMock())
        assert engine._coordinator is not None


# ═══════════════════════════════════════════════════════════════
# VolumeMomentumEngine Tests
# ═══════════════════════════════════════════════════════════════

class TestVolumeMomentumEngine:

    def _make_engine(self, exchange=None, market_data=None, capital=100.0):
        from engine.volume_momentum_engine import VolumeMomentumEngine
        return VolumeMomentumEngine(
            config=AppConfig(),
            futures_exchange=exchange or _mock_exchange(),
            market_data=market_data or _mock_market_data(),
            initial_capital_usdt=capital,
            leverage=2,
        )

    @pytest.mark.asyncio
    async def test_get_status_initial(self):
        engine = self._make_engine()
        status = engine.get_status()
        assert status["exchange"] == "binance_vol_mom"
        assert status["is_running"] is False
        assert status["leverage"] == 2
        assert status["capital_usdt"] == 100.0
        assert status["cumulative_pnl"] == 0.0
        assert status["daily_pnl"] == 0.0
        assert status["paused"] is False
        assert status["vol_mult"] == 2.0
        assert len(status["positions"]) == 0

    @pytest.mark.asyncio
    async def test_evaluate_no_crash(self, session_factory):
        md = _mock_market_data()
        engine = self._make_engine(market_data=md)

        with patch("engine.volume_momentum_engine.get_session_factory", return_value=session_factory):
            with patch("engine.volume_momentum_engine.emit_event", new_callable=AsyncMock):
                await engine.evaluate_now()

        assert engine._paused is False

    @pytest.mark.asyncio
    async def test_loss_limit_pause(self):
        engine = self._make_engine(capital=100.0)
        engine._cumulative_pnl = -10.0

        with patch("engine.volume_momentum_engine.emit_event", new_callable=AsyncMock):
            await engine._check_loss_limits()
        assert engine._paused is True

    @pytest.mark.asyncio
    async def test_daily_loss_pause(self):
        engine = self._make_engine(capital=100.0)
        engine._daily_pnl = -5.0

        with patch("engine.volume_momentum_engine.emit_event", new_callable=AsyncMock):
            await engine._check_loss_limits()
        assert engine._daily_paused is True

    @pytest.mark.asyncio
    async def test_record_order(self, session_factory):
        engine = self._make_engine()

        with patch("engine.volume_momentum_engine.get_session_factory", return_value=session_factory):
            await engine._record_order("ETH/USDT", "sell", 3000.0, 0.1,
                                        pnl=0.0, reason="vol_mom_short_entry")

        async with session_factory() as session:
            result = await session.execute(select(Order))
            orders = result.scalars().all()
            assert len(orders) == 1
            assert orders[0].exchange == "binance_vol_mom"
            assert orders[0].strategy_name == "volume_momentum"
            assert orders[0].signal_reason == "vol_mom_short_entry"

    @pytest.mark.asyncio
    async def test_volume_spike_detection(self):
        """거래량 급증 감지 로직 테스트."""
        from engine.volume_momentum_engine import VolumeMomentumEngine

        # Normal volume: all 1000
        df_normal = _make_ohlcv_df(n=30, vol_base=1000.0)
        ratio_normal = VolumeMomentumEngine._compute_vol_ratio(df_normal)
        # Should be approximately 1.0 (within noise)
        assert 0.5 < ratio_normal < 2.0

        # Spike: last bar has 5x volume
        df_spike = df_normal.copy()
        df_spike.loc[df_spike.index[-1], "volume"] = 5000.0
        ratio_spike = VolumeMomentumEngine._compute_vol_ratio(df_spike)
        assert ratio_spike > 2.0  # Should detect the spike

    @pytest.mark.asyncio
    async def test_rsi_computation(self):
        """RSI 계산 테스트."""
        from engine.volume_momentum_engine import VolumeMomentumEngine

        # Uptrend: closes go up monotonically
        df_up = _make_ohlcv_df(n=30, trend=1.0)
        rsi_up = VolumeMomentumEngine._compute_rsi(df_up)
        assert rsi_up is not None
        assert rsi_up > 50  # Uptrend → RSI > 50

        # Downtrend: closes go down monotonically
        df_down = _make_ohlcv_df(n=30, trend=-1.0)
        rsi_down = VolumeMomentumEngine._compute_rsi(df_down)
        assert rsi_down is not None
        assert rsi_down < 50  # Downtrend → RSI < 50

    @pytest.mark.asyncio
    async def test_atr_computation(self):
        """ATR 계산 테스트."""
        from engine.volume_momentum_engine import VolumeMomentumEngine

        df = _make_ohlcv_df(n=30)
        atr = VolumeMomentumEngine._compute_atr(df)
        assert atr is not None
        assert atr > 0

    @pytest.mark.asyncio
    async def test_sl_tp_check_long(self, session_factory):
        """SL/TP intra-candle 체크 (long)."""
        from engine.volume_momentum_engine import VMPosition

        exchange = _mock_exchange()
        md = _mock_market_data()
        engine = self._make_engine(exchange=exchange, market_data=md)

        # Create a position
        engine._positions["BTC/USDT"] = VMPosition(
            symbol="BTC/USDT", side="long", quantity=1.0,
            entry_price=100.0, sl_price=95.0, tp_price=110.0,
        )

        # TP hit (high >= 110)
        df = _make_ohlcv_df(n=5)
        df.loc[df.index[-1], "high"] = 112.0
        df.loc[df.index[-1], "low"] = 99.0
        df.loc[df.index[-1], "close"] = 111.0

        with patch("engine.volume_momentum_engine.get_session_factory", return_value=session_factory):
            with patch("engine.volume_momentum_engine.emit_event", new_callable=AsyncMock):
                await engine._check_sl_tp("BTC/USDT", df)

        assert "BTC/USDT" not in engine._positions  # closed

    @pytest.mark.asyncio
    async def test_tracked_coins(self):
        engine = self._make_engine()
        assert "BTC/USDT" in engine.tracked_coins
        assert len(engine.tracked_coins) >= 3


# ═══════════════════════════════════════════════════════════════
# BTCNeutralAltMREngine Tests
# ═══════════════════════════════════════════════════════════════

class TestBTCNeutralAltMREngine:

    def _make_engine(self, exchange=None, market_data=None, capital=100.0):
        from engine.btc_neutral_alt_mr_engine import BTCNeutralAltMREngine
        return BTCNeutralAltMREngine(
            config=AppConfig(),
            futures_exchange=exchange or _mock_exchange(),
            market_data=market_data or _mock_market_data(),
            initial_capital_usdt=capital,
            leverage=2,
        )

    @pytest.mark.asyncio
    async def test_get_status_initial(self):
        engine = self._make_engine()
        status = engine.get_status()
        assert status["exchange"] == "binance_btc_neutral"
        assert status["is_running"] is False
        assert status["leverage"] == 2
        assert status["capital_usdt"] == 100.0
        assert status["cumulative_pnl"] == 0.0
        assert status["daily_pnl"] == 0.0
        assert status["paused"] is False
        assert status["z_entry"] == 2.0
        assert status["z_exit"] == 0.3
        assert status["max_hold_days"] == 7
        assert status["max_concurrent"] == 3
        assert len(status["positions"]) == 0

    @pytest.mark.asyncio
    async def test_evaluate_no_crash(self, session_factory):
        md = _mock_market_data()
        engine = self._make_engine(market_data=md)

        with patch("engine.btc_neutral_alt_mr_engine.get_session_factory", return_value=session_factory):
            with patch("engine.btc_neutral_alt_mr_engine.emit_event", new_callable=AsyncMock):
                await engine.evaluate_now()

        assert engine._paused is False

    @pytest.mark.asyncio
    async def test_loss_limit_pause(self):
        engine = self._make_engine(capital=100.0)
        engine._cumulative_pnl = -10.0

        with patch("engine.btc_neutral_alt_mr_engine.emit_event", new_callable=AsyncMock):
            await engine._check_loss_limits()
        assert engine._paused is True

    @pytest.mark.asyncio
    async def test_daily_loss_pause(self):
        engine = self._make_engine(capital=100.0)
        engine._daily_pnl = -5.0

        with patch("engine.btc_neutral_alt_mr_engine.emit_event", new_callable=AsyncMock):
            await engine._check_loss_limits()
        assert engine._daily_paused is True

    @pytest.mark.asyncio
    async def test_record_order(self, session_factory):
        engine = self._make_engine()

        with patch("engine.btc_neutral_alt_mr_engine.get_session_factory", return_value=session_factory):
            await engine._record_order("ETH/USDT", "buy", 3000.0, 0.05,
                                        pnl=0.0, reason="btcneutral_long_alt_entry")

        async with session_factory() as session:
            result = await session.execute(select(Order))
            orders = result.scalars().all()
            assert len(orders) == 1
            assert orders[0].exchange == "binance_btc_neutral"
            assert orders[0].strategy_name == "btc_neutral_mr"
            assert orders[0].signal_reason == "btcneutral_long_alt_entry"

    @pytest.mark.asyncio
    async def test_z_score_computation(self):
        """z-score 계산 + 양방향 포지션 테스트."""
        from engine.btc_neutral_alt_mr_engine import BTCNeutralAltMREngine

        # BTC stable at 50000, ALT at 3000 with deviation at the end
        n_hours = 7 * 24 + 10  # 7 days + 10
        btc_closes = np.full(n_hours, 50000.0)
        alt_closes = np.full(n_hours, 3000.0)

        # Make the last point deviate significantly
        alt_closes[-1] = 3000.0 * 1.10  # 10% up vs BTC (should give positive z)

        # Build dataframes
        dates = pd.date_range("2026-01-01", periods=n_hours, freq="1h")
        btc_df = pd.DataFrame({
            "timestamp": dates, "open": btc_closes, "high": btc_closes * 1.01,
            "low": btc_closes * 0.99, "close": btc_closes, "volume": np.full(n_hours, 100),
        })
        alt_df = pd.DataFrame({
            "timestamp": dates, "open": alt_closes, "high": alt_closes * 1.01,
            "low": alt_closes * 0.99, "close": alt_closes, "volume": np.full(n_hours, 100),
        })

        md = _mock_market_data({
            "ETH/USDT_1h": alt_df,
            "BTC/USDT_1h": btc_df,
        })
        engine = BTCNeutralAltMREngine(
            config=AppConfig(),
            futures_exchange=_mock_exchange(),
            market_data=md,
            initial_capital_usdt=100.0,
            lookback_days=7,
            z_entry=2.0,
        )

        z = await engine._compute_z_score("ETH/USDT", btc_closes=btc_df["close"].values)
        assert z is not None
        # With a 10% deviation at the end of a flat series, z should be large positive
        assert z > 0

    @pytest.mark.asyncio
    async def test_max_hold_exit(self, session_factory):
        """max_hold_days 초과 시 청산."""
        from engine.btc_neutral_alt_mr_engine import NeutralPosition

        exchange = _mock_exchange()
        md = _mock_market_data()
        engine = self._make_engine(exchange=exchange, market_data=md, capital=100.0)
        engine._max_hold_days = 7

        # Create an old position (8 days ago)
        old_time = datetime.now(timezone.utc) - timedelta(days=8)
        engine._positions["ETH/USDT"] = NeutralPosition(
            alt_symbol="ETH/USDT", alt_side="long",
            alt_qty=0.05, alt_entry=3000.0,
            btc_side="short", btc_qty=0.001, btc_entry=50000.0,
            entered_at=old_time,
        )

        with patch("engine.btc_neutral_alt_mr_engine.get_session_factory", return_value=session_factory):
            with patch("engine.btc_neutral_alt_mr_engine.emit_event", new_callable=AsyncMock):
                await engine._check_exit("ETH/USDT", datetime.now(timezone.utc))

        # Should have been closed
        assert "ETH/USDT" not in engine._positions

    @pytest.mark.asyncio
    async def test_max_concurrent_limit(self, session_factory):
        """최대 동시 포지션 수 제한."""
        from engine.btc_neutral_alt_mr_engine import NeutralPosition

        engine = self._make_engine(capital=100.0)
        engine._max_concurrent = 2

        # Fill up to max
        for sym in ["ETH/USDT", "SOL/USDT"]:
            engine._positions[sym] = NeutralPosition(
                alt_symbol=sym, alt_side="long",
                alt_qty=0.05, alt_entry=100.0,
                btc_side="short", btc_qty=0.001, btc_entry=50000.0,
            )

        # _scan_entries should not add more
        with patch("engine.btc_neutral_alt_mr_engine.get_session_factory", return_value=session_factory):
            with patch("engine.btc_neutral_alt_mr_engine.emit_event", new_callable=AsyncMock):
                await engine._scan_entries()

        assert len(engine._positions) == 2

    @pytest.mark.asyncio
    async def test_tracked_coins_includes_btc(self):
        engine = self._make_engine()
        assert "BTC/USDT" in engine.tracked_coins
        assert "ETH/USDT" in engine.tracked_coins
        assert len(engine.tracked_coins) >= 5  # 4 alts + BTC

    @pytest.mark.asyncio
    async def test_set_methods_no_op(self):
        engine = self._make_engine()
        engine.set_engine_registry(None)
        engine.set_broadcast_callback(None)
        engine.set_agent_coordinator(None)
        engine.set_futures_rnd_coordinator(MagicMock())
        assert engine._coordinator is not None

    @pytest.mark.asyncio
    async def test_z_score_returns_none_insufficient_data(self):
        """데이터 부족 시 z-score None 반환."""
        md = AsyncMock()
        md.get_ohlcv_df = AsyncMock(return_value=None)
        engine = self._make_engine(market_data=md)

        z = await engine._compute_z_score("ETH/USDT")
        assert z is None


# ═══════════════════════════════════════════════════════════════
# Cross-engine integration: start/stop lifecycle
# ═══════════════════════════════════════════════════════════════

class TestEngineLifecycle:

    @pytest.mark.asyncio
    async def test_breakout_start_stop(self, session_factory):
        from engine.breakout_pullback_engine import BreakoutPullbackEngine
        engine = BreakoutPullbackEngine(
            config=AppConfig(),
            futures_exchange=_mock_exchange(),
            market_data=_mock_market_data(),
            initial_capital_usdt=150.0,
        )
        with patch("engine.breakout_pullback_engine.get_session_factory", return_value=session_factory):
            with patch("engine.breakout_pullback_engine.emit_event", new_callable=AsyncMock):
                await engine.start()
                assert engine.is_running is True
                await engine.stop()
                assert engine.is_running is False

    @pytest.mark.asyncio
    async def test_vol_mom_start_stop(self, session_factory):
        from engine.volume_momentum_engine import VolumeMomentumEngine
        engine = VolumeMomentumEngine(
            config=AppConfig(),
            futures_exchange=_mock_exchange(),
            market_data=_mock_market_data(),
            initial_capital_usdt=100.0,
        )
        with patch("engine.volume_momentum_engine.get_session_factory", return_value=session_factory):
            with patch("engine.volume_momentum_engine.emit_event", new_callable=AsyncMock):
                await engine.start()
                assert engine.is_running is True
                await engine.stop()
                assert engine.is_running is False

    @pytest.mark.asyncio
    async def test_btc_neutral_start_stop(self, session_factory):
        from engine.btc_neutral_alt_mr_engine import BTCNeutralAltMREngine
        engine = BTCNeutralAltMREngine(
            config=AppConfig(),
            futures_exchange=_mock_exchange(),
            market_data=_mock_market_data(),
            initial_capital_usdt=100.0,
        )
        with patch("engine.btc_neutral_alt_mr_engine.get_session_factory", return_value=session_factory):
            with patch("engine.btc_neutral_alt_mr_engine.emit_event", new_callable=AsyncMock):
                await engine.start()
                assert engine.is_running is True
                await engine.stop()
                assert engine.is_running is False


# ═══════════════════════════════════════════════════════════════
# Fill check tests — position must NOT be updated on unfilled orders
# ═══════════════════════════════════════════════════════════════

@dataclass
class UnfilledOrder:
    """Simulates an order that was not filled (e.g. rejected, pending)."""
    executed_price: float = 0.0
    executed_quantity: float = 0.0
    status: str = "open"  # not filled


class TestFillCheckBreakoutPullback:

    def _make_engine(self, exchange=None, market_data=None, capital=150.0):
        from engine.breakout_pullback_engine import BreakoutPullbackEngine
        return BreakoutPullbackEngine(
            config=AppConfig(),
            futures_exchange=exchange or _mock_exchange(),
            market_data=market_data or _mock_market_data(),
            initial_capital_usdt=capital,
            leverage=2,
        )

    @pytest.mark.asyncio
    async def test_open_position_not_registered_on_unfilled(self, session_factory):
        """주문 미체결 시 포지션 미등록."""
        exchange = AsyncMock()
        exchange.create_market_buy = AsyncMock(return_value=UnfilledOrder())
        engine = self._make_engine(exchange=exchange, capital=150.0)

        with patch("engine.breakout_pullback_engine.get_session_factory", return_value=session_factory):
            with patch("engine.breakout_pullback_engine.emit_event", new_callable=AsyncMock):
                await engine._open_position("BTC/USDT", "long", 50000.0)

        assert "BTC/USDT" not in engine._positions

    @pytest.mark.asyncio
    async def test_close_position_kept_on_unfilled(self, session_factory):
        """청산 주문 미체결 시 포지션 유지."""
        from engine.breakout_pullback_engine import BPPosition
        exchange = AsyncMock()
        exchange.create_market_sell = AsyncMock(return_value=UnfilledOrder())
        engine = self._make_engine(exchange=exchange, capital=150.0)
        engine._positions["BTC/USDT"] = BPPosition(
            symbol="BTC/USDT", side="long", quantity=0.01,
            entry_price=50000.0, sl_price=47500.0, tp_price=54000.0,
            highest_since_entry=50000.0, lowest_since_entry=50000.0,
        )

        with patch("engine.breakout_pullback_engine.get_session_factory", return_value=session_factory):
            with patch("engine.breakout_pullback_engine.emit_event", new_callable=AsyncMock):
                await engine._close_position("BTC/USDT", 49000.0, "sl_hit")

        # Position should still be there (not deleted)
        assert "BTC/USDT" in engine._positions


class TestFillCheckVolumeMomentum:

    def _make_engine(self, exchange=None, market_data=None, capital=100.0):
        from engine.volume_momentum_engine import VolumeMomentumEngine
        return VolumeMomentumEngine(
            config=AppConfig(),
            futures_exchange=exchange or _mock_exchange(),
            market_data=market_data or _mock_market_data(),
            initial_capital_usdt=capital,
            leverage=2,
        )

    @pytest.mark.asyncio
    async def test_open_position_not_registered_on_unfilled(self, session_factory):
        """주문 미체결 시 포지션 미등록."""
        exchange = AsyncMock()
        exchange.create_market_buy = AsyncMock(return_value=UnfilledOrder())
        engine = self._make_engine(exchange=exchange)

        with patch("engine.volume_momentum_engine.get_session_factory", return_value=session_factory):
            with patch("engine.volume_momentum_engine.emit_event", new_callable=AsyncMock):
                await engine._open_position("BTC/USDT", "long", 50000.0, 47500.0, 55000.0, "detail")

        assert "BTC/USDT" not in engine._positions

    @pytest.mark.asyncio
    async def test_close_position_kept_on_unfilled(self, session_factory):
        """청산 주문 미체결 시 포지션 유지."""
        from engine.volume_momentum_engine import VMPosition
        exchange = AsyncMock()
        exchange.create_market_sell = AsyncMock(return_value=UnfilledOrder())
        engine = self._make_engine(exchange=exchange)
        engine._positions["BTC/USDT"] = VMPosition(
            symbol="BTC/USDT", side="long", quantity=0.01,
            entry_price=50000.0, sl_price=47500.0, tp_price=55000.0,
        )

        with patch("engine.volume_momentum_engine.get_session_factory", return_value=session_factory):
            with patch("engine.volume_momentum_engine.emit_event", new_callable=AsyncMock):
                await engine._close_position("BTC/USDT", 47000.0, "sl_hit")

        assert "BTC/USDT" in engine._positions


class TestFillCheckBTCNeutral:

    def _make_engine(self, exchange=None, market_data=None, capital=100.0):
        from engine.btc_neutral_alt_mr_engine import BTCNeutralAltMREngine
        return BTCNeutralAltMREngine(
            config=AppConfig(),
            futures_exchange=exchange or _mock_exchange(),
            market_data=market_data or _mock_market_data(),
            initial_capital_usdt=capital,
            leverage=2,
        )

    @pytest.mark.asyncio
    async def test_open_pair_not_registered_if_alt_unfilled(self, session_factory):
        """Alt 주문 미체결 시 포지션 미등록."""
        exchange = AsyncMock()
        exchange.create_market_buy = AsyncMock(return_value=UnfilledOrder())
        exchange.create_market_sell = AsyncMock(return_value=UnfilledOrder())

        n = 200
        dates = pd.date_range("2026-01-01", periods=n, freq="1h")
        prices = np.full(n, 100.0)
        df = pd.DataFrame({"open": prices, "high": prices*1.01, "low": prices*0.99,
                            "close": prices, "volume": np.ones(n)*1000})
        md = AsyncMock()
        md.get_ohlcv_df = AsyncMock(return_value=df)

        engine = self._make_engine(exchange=exchange, market_data=md)

        with patch("engine.btc_neutral_alt_mr_engine.get_session_factory", return_value=session_factory):
            with patch("engine.btc_neutral_alt_mr_engine.emit_event", new_callable=AsyncMock):
                await engine._open_pair("ETH/USDT", "long", z=3.0)

        assert "ETH/USDT" not in engine._positions

    @pytest.mark.asyncio
    async def test_close_pair_kept_if_alt_unfilled(self, session_factory):
        """Alt 청산 주문 미체결 시 포지션 유지."""
        from engine.btc_neutral_alt_mr_engine import NeutralPosition
        exchange = AsyncMock()
        exchange.create_market_sell = AsyncMock(return_value=UnfilledOrder())
        exchange.create_market_buy = AsyncMock(return_value=UnfilledOrder())
        engine = self._make_engine(exchange=exchange)
        engine._positions["ETH/USDT"] = NeutralPosition(
            alt_symbol="ETH/USDT", alt_side="long",
            alt_qty=0.05, alt_entry=3000.0,
            btc_side="short", btc_qty=0.001, btc_entry=50000.0,
        )

        with patch("engine.btc_neutral_alt_mr_engine.get_session_factory", return_value=session_factory):
            with patch("engine.btc_neutral_alt_mr_engine.emit_event", new_callable=AsyncMock):
                await engine._close_pair("ETH/USDT", reason="test")

        assert "ETH/USDT" in engine._positions
