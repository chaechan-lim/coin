"""COIN-98: FuturesEngineV2 WS mark price loop integration tests."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.futures_engine_v2 import FuturesEngineV2
from exchange.data_models import Balance, LongShortRatio, MarkPriceInfo, OpenInterest
from services.derivatives_data import DerivativesDataService
from config import AppConfig


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def mock_exchange():
    exchange = AsyncMock()
    exchange.set_leverage = AsyncMock()
    exchange.fetch_balance = AsyncMock(
        return_value={
            "USDT": Balance(currency="USDT", free=500.0, used=0.0, total=500.0),
        }
    )
    exchange.close_ws = AsyncMock()
    exchange.watch_mark_prices = AsyncMock(return_value={})
    exchange.fetch_open_interest = AsyncMock()
    exchange.fetch_long_short_ratio = AsyncMock()
    return exchange


@pytest.fixture
def mock_market_data():
    md = AsyncMock()
    md.get_current_price = AsyncMock(return_value=80000.0)
    md.get_ohlcv_df = AsyncMock(return_value=None)
    return md


@pytest.fixture
def mock_pm(mock_market_data):
    pm = MagicMock()
    pm.cash_balance = 500.0
    pm._is_paper = False
    pm._exchange_name = "binance_futures"
    pm.apply_income = AsyncMock()
    return pm


@pytest.fixture
def mock_om(mock_exchange):
    return MagicMock()


@pytest.fixture
def app_config():
    return AppConfig()


@pytest.fixture
def derivatives_data():
    return DerivativesDataService()


@pytest.fixture
def engine_without_derivatives(app_config, mock_exchange, mock_market_data, mock_om, mock_pm):
    """Engine created without derivatives_data (backward-compat path)."""
    return FuturesEngineV2(
        config=app_config,
        exchange=mock_exchange,
        market_data=mock_market_data,
        order_manager=mock_om,
        portfolio_manager=mock_pm,
    )


@pytest.fixture
def engine_with_derivatives(
    app_config, mock_exchange, mock_market_data, mock_om, mock_pm, derivatives_data
):
    """Engine created with derivatives_data."""
    return FuturesEngineV2(
        config=app_config,
        exchange=mock_exchange,
        market_data=mock_market_data,
        order_manager=mock_om,
        portfolio_manager=mock_pm,
        derivatives_data=derivatives_data,
    )


def _mark_price_response(symbol: str, mark: float = 65000.0) -> dict:
    """Build a minimal watch_mark_prices response dict for *symbol*."""
    return {
        symbol: {
            "markPrice": str(mark),
            "indexPrice": str(mark * 0.999),
            "fundingRate": "0.0001",
            "nextFundingTime": "1700000000000",
        }
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Tests: Initialisation ──────────────────────────────────────────────


class TestInitWithDerivatives:
    def test_no_derivatives_data_none(self, engine_without_derivatives):
        """Backward compatibility: no derivatives_data → attribute is None."""
        assert engine_without_derivatives._derivatives_data is None

    def test_with_derivatives_data_stored(self, engine_with_derivatives, derivatives_data):
        """derivatives_data parameter is stored on the instance."""
        assert engine_with_derivatives._derivatives_data is derivatives_data

    def test_ws_mark_price_task_initially_none(self, engine_without_derivatives):
        """_ws_mark_price_task starts as None before start()."""
        assert engine_without_derivatives._ws_mark_price_task is None

    def test_ws_mark_price_task_initially_none_with_derivatives(self, engine_with_derivatives):
        assert engine_with_derivatives._ws_mark_price_task is None


# ── Tests: _parse_mark_price_data ─────────────────────────────────────


class TestParseMarkPriceData:
    def test_valid_data_returns_mark_price_info(self):
        data = {
            "markPrice": "65000.0",
            "indexPrice": "64900.0",
            "fundingRate": "0.0001",
            "nextFundingTime": "1700000000000",
        }
        result = FuturesEngineV2._parse_mark_price_data("BTC/USDT", data)
        assert result is not None
        assert isinstance(result, MarkPriceInfo)
        assert result.symbol == "BTC/USDT"
        assert result.mark_price == pytest.approx(65000.0)
        assert result.index_price == pytest.approx(64900.0)
        assert result.last_funding_rate == pytest.approx(0.0001)

    def test_ccxt_native_next_funding_timestamp(self):
        """ccxt native format uses nextFundingTimestamp (not nextFundingTime)."""
        data = {
            "markPrice": "50000.0",
            "indexPrice": "49950.0",
            "fundingRate": "0.0002",
            "nextFundingTimestamp": 1700000000000,
        }
        result = FuturesEngineV2._parse_mark_price_data("ETH/USDT", data)
        assert result is not None
        assert result.mark_price == pytest.approx(50000.0)

    def test_zero_mark_price_returns_none(self):
        """mark_price == 0 → return None (invalid data)."""
        data = {"markPrice": "0", "indexPrice": "0", "fundingRate": "0"}
        result = FuturesEngineV2._parse_mark_price_data("BTC/USDT", data)
        assert result is None

    def test_missing_mark_price_returns_none(self):
        """Missing markPrice → return None."""
        result = FuturesEngineV2._parse_mark_price_data("BTC/USDT", {})
        assert result is None

    def test_premium_pct_computed(self):
        """premium_pct is auto-computed by MarkPriceInfo.__post_init__."""
        data = {
            "markPrice": "65100.0",
            "indexPrice": "65000.0",
            "fundingRate": "0.0001",
            "nextFundingTime": "1700000000000",
        }
        result = FuturesEngineV2._parse_mark_price_data("BTC/USDT", data)
        assert result is not None
        assert result.premium_pct == pytest.approx(100.0 / 65000.0 * 100, rel=1e-4)


# ── Tests: _ws_mark_price_loop ─────────────────────────────────────────


class TestWsMarkPriceLoop:
    @pytest.mark.asyncio
    async def test_loop_updates_cache_on_tick(
        self, engine_with_derivatives, mock_exchange, derivatives_data
    ):
        """Successful tick → MarkPriceInfo written to derivatives_data cache."""
        # CancelledError is caught internally by the loop (break); no re-raise
        mock_exchange.watch_mark_prices = AsyncMock(
            side_effect=[
                _mark_price_response("BTC/USDT", 65000.0),
                asyncio.CancelledError(),
            ]
        )
        engine_with_derivatives._is_running = True

        await engine_with_derivatives._ws_mark_price_loop()

        cached = derivatives_data.get_mark_price("BTC/USDT")
        assert cached is not None
        assert cached.mark_price == pytest.approx(65000.0)

    @pytest.mark.asyncio
    async def test_loop_multiple_symbols(
        self, engine_with_derivatives, mock_exchange, derivatives_data
    ):
        """Multiple symbols in one tick → all written to cache."""
        tick = {
            "BTC/USDT": {
                "markPrice": "65000.0",
                "indexPrice": "64900.0",
                "fundingRate": "0.0001",
                "nextFundingTime": "1700000000000",
            },
            "ETH/USDT": {
                "markPrice": "3000.0",
                "indexPrice": "2998.0",
                "fundingRate": "0.0002",
                "nextFundingTime": "1700000000000",
            },
        }
        mock_exchange.watch_mark_prices = AsyncMock(
            side_effect=[tick, asyncio.CancelledError()]
        )
        engine_with_derivatives._is_running = True

        await engine_with_derivatives._ws_mark_price_loop()

        assert derivatives_data.get_mark_price("BTC/USDT") is not None
        assert derivatives_data.get_mark_price("ETH/USDT") is not None

    @pytest.mark.asyncio
    async def test_timeout_error_continues(
        self, engine_with_derivatives, mock_exchange, derivatives_data
    ):
        """asyncio.TimeoutError → continue without crashing."""
        mock_exchange.watch_mark_prices = AsyncMock(
            side_effect=[
                asyncio.TimeoutError(),
                _mark_price_response("BTC/USDT", 60000.0),
                asyncio.CancelledError(),
            ]
        )
        engine_with_derivatives._is_running = True

        await engine_with_derivatives._ws_mark_price_loop()

        # Cache should be populated from the successful tick after timeout
        assert derivatives_data.get_mark_price("BTC/USDT") is not None

    @pytest.mark.asyncio
    async def test_three_consecutive_errors_warns_no_fallback(
        self, engine_with_derivatives, mock_exchange
    ):
        """3 consecutive errors → NO fallback task created (mark price is supplementary)."""
        error = RuntimeError("ws broken")
        mock_exchange.watch_mark_prices = AsyncMock(
            side_effect=[error, error, error, asyncio.CancelledError()]
        )
        # Mock _ws_reconnect to avoid actually sleeping
        engine_with_derivatives._ws_reconnect = AsyncMock(
            return_value=engine_with_derivatives._WS_RECONNECT_MIN
        )
        engine_with_derivatives._is_running = True

        await engine_with_derivatives._ws_mark_price_loop()

        # No fallback task should be created (mark price is supplementary)
        assert engine_with_derivatives._fast_sl_task is None

    @pytest.mark.asyncio
    async def test_three_consecutive_errors_calls_reconnect(
        self, engine_with_derivatives, mock_exchange
    ):
        """3 consecutive errors → _ws_reconnect is called."""
        error = RuntimeError("ws broken")
        mock_exchange.watch_mark_prices = AsyncMock(
            side_effect=[error, error, error, asyncio.CancelledError()]
        )
        engine_with_derivatives._ws_reconnect = AsyncMock(
            return_value=engine_with_derivatives._WS_RECONNECT_MIN
        )
        engine_with_derivatives._is_running = True

        await engine_with_derivatives._ws_mark_price_loop()

        engine_with_derivatives._ws_reconnect.assert_called_once()


# ── Tests: Lifecycle (start/stop) ─────────────────────────────────────


class TestLifecycleWithDerivatives:
    @pytest.mark.asyncio
    async def test_start_creates_mark_price_task_when_ws_started(
        self, engine_with_derivatives, mock_exchange
    ):
        """start() with ws_started=True + derivatives_data → mark price task created."""
        mock_exchange.create_ws_exchange = AsyncMock()

        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine_with_derivatives.start()

        assert engine_with_derivatives._ws_mark_price_task is not None
        assert not engine_with_derivatives._ws_mark_price_task.done()
        assert engine_with_derivatives._ws_mark_price_task.get_name() == "v2_ws_mark_price"

        await engine_with_derivatives.stop()

    @pytest.mark.asyncio
    async def test_start_no_mark_price_task_without_derivatives(
        self, engine_without_derivatives, mock_exchange
    ):
        """start() without derivatives_data → no mark price task."""
        mock_exchange.create_ws_exchange = AsyncMock()

        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine_without_derivatives.start()

        assert engine_without_derivatives._ws_mark_price_task is None

        await engine_without_derivatives.stop()

    @pytest.mark.asyncio
    async def test_stop_resets_mark_price_task(
        self, engine_with_derivatives, mock_exchange
    ):
        """stop() resets _ws_mark_price_task to None."""
        mock_exchange.create_ws_exchange = AsyncMock()

        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine_with_derivatives.start()
            assert engine_with_derivatives._ws_mark_price_task is not None
            await engine_with_derivatives.stop()

        assert engine_with_derivatives._ws_mark_price_task is None
        assert engine_with_derivatives.is_running is False

    @pytest.mark.asyncio
    async def test_mark_price_task_in_tasks_list(
        self, engine_with_derivatives, mock_exchange
    ):
        """start() adds mark price task to self._tasks."""
        mock_exchange.create_ws_exchange = AsyncMock()

        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine_with_derivatives.start()

        task_names = [t.get_name() for t in engine_with_derivatives._tasks]
        assert "v2_ws_mark_price" in task_names

        await engine_with_derivatives.stop()

    @pytest.mark.asyncio
    async def test_start_no_mark_price_task_when_ws_fails(
        self, app_config, mock_market_data, mock_om, mock_pm, derivatives_data
    ):
        """start() with WS init failure → no mark price task."""
        exchange = AsyncMock()
        exchange.set_leverage = AsyncMock()
        exchange.fetch_balance = AsyncMock(
            return_value={
                "USDT": Balance(currency="USDT", free=500.0, used=0.0, total=500.0),
            }
        )
        exchange.close_ws = AsyncMock()
        exchange.create_ws_exchange = AsyncMock(side_effect=RuntimeError("WS failed"))

        engine = FuturesEngineV2(
            config=app_config,
            exchange=exchange,
            market_data=mock_market_data,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
            derivatives_data=derivatives_data,
        )

        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine.start()

        # WS failed → no mark price task should be created
        assert engine._ws_mark_price_task is None

        await engine.stop()


# ── Tests: get_status ─────────────────────────────────────────────────


class TestGetStatusMarkPrice:
    def test_get_status_includes_ws_mark_price_field(self, engine_with_derivatives):
        """get_status() always returns ws_mark_price key."""
        status = engine_with_derivatives.get_status()
        assert "ws_mark_price" in status

    def test_ws_mark_price_false_when_not_started(self, engine_with_derivatives):
        """ws_mark_price is False before start()."""
        status = engine_with_derivatives.get_status()
        assert status["ws_mark_price"] is False

    def test_ws_mark_price_false_without_derivatives(self, engine_without_derivatives):
        """ws_mark_price is False when no derivatives_data provided."""
        status = engine_without_derivatives.get_status()
        assert status["ws_mark_price"] is False


# ── Tests: REST fallback loop ─────────────────────────────────────────


class TestDerivativesRestLoop:
    @pytest.mark.asyncio
    async def test_rest_loop_fetches_oi_and_ls(
        self, engine_with_derivatives, mock_exchange, derivatives_data, app_config
    ):
        """_derivatives_rest_loop fetches OI and LS ratio per tracked coin."""
        tracked = list(app_config.futures_v2.tier1_coins)
        symbol = tracked[0]

        oi = OpenInterest(
            symbol=symbol,
            open_interest_value=1000.0,
            timestamp=_now(),
        )
        ls = LongShortRatio(
            symbol=symbol,
            long_account_ratio=0.55,
            short_account_ratio=0.45,
            long_position_ratio=0.60,
            short_position_ratio=0.40,
            timestamp=_now(),
        )
        mock_exchange.fetch_open_interest = AsyncMock(return_value=oi)
        mock_exchange.fetch_long_short_ratio = AsyncMock(return_value=ls)

        engine_with_derivatives._is_running = True

        # Patch asyncio.sleep so the loop doesn't run indefinitely
        sleep_call_count = 0

        async def mock_sleep(seconds):
            nonlocal sleep_call_count
            sleep_call_count += 1
            if sleep_call_count >= 1:
                engine_with_derivatives._is_running = False

        with patch("asyncio.sleep", new=mock_sleep):
            await engine_with_derivatives._derivatives_rest_loop()

        assert mock_exchange.fetch_open_interest.called
        assert mock_exchange.fetch_long_short_ratio.called
        assert derivatives_data.get_open_interest(symbol) is not None
        assert derivatives_data.get_long_short_ratio(symbol) is not None

    @pytest.mark.asyncio
    async def test_rest_loop_oi_error_continues(
        self, engine_with_derivatives, mock_exchange
    ):
        """OI fetch error → log warning, continue without crashing."""
        mock_exchange.fetch_open_interest = AsyncMock(
            side_effect=RuntimeError("network error")
        )
        mock_exchange.fetch_long_short_ratio = AsyncMock(
            side_effect=RuntimeError("network error")
        )

        engine_with_derivatives._is_running = True
        call_count = 0

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                engine_with_derivatives._is_running = False

        with patch("asyncio.sleep", new=mock_sleep):
            # Should not raise
            await engine_with_derivatives._derivatives_rest_loop()

    @pytest.mark.asyncio
    async def test_derivatives_rest_task_in_start(
        self, engine_with_derivatives, mock_exchange
    ):
        """start() adds v2_derivatives_rest task when derivatives_data is set."""
        mock_exchange.create_ws_exchange = AsyncMock()

        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine_with_derivatives.start()

        task_names = [t.get_name() for t in engine_with_derivatives._tasks]
        assert "v2_derivatives_rest" in task_names

        await engine_with_derivatives.stop()

    @pytest.mark.asyncio
    async def test_no_derivatives_rest_task_without_derivatives(
        self, engine_without_derivatives, mock_exchange
    ):
        """start() without derivatives_data → no v2_derivatives_rest task."""
        mock_exchange.create_ws_exchange = AsyncMock()

        with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
            await engine_without_derivatives.start()

        task_names = [t.get_name() for t in engine_without_derivatives._tasks]
        assert "v2_derivatives_rest" not in task_names

        await engine_without_derivatives.stop()
