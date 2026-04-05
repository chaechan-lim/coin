"""Tests for BinanceUSDMAdapter.watch_mark_prices() WS method (COIN-97).

Covers:
  - ExchangeConnectionError when WS not initialized
  - Empty symbols list returns {} immediately
  - Successful return from ccxt.pro watch_mark_prices
  - asyncio.TimeoutError propagation
  - Fallback to watch_tickers when watch_mark_prices is unavailable:
      * markPrice prefers info.markPrice over last-trade
      * indexPrice comes from info.indexPrice (not ccxt unified "index")
      * missing info fields return None gracefully
  - Base adapter raises NotImplementedError
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from exchange.base import ExchangeAdapter
from exchange.binance_usdm_adapter import BinanceUSDMAdapter
from core.exceptions import ExchangeConnectionError


# ── Helpers ──────────────────────────────────────────────────────────


def _make_adapter() -> BinanceUSDMAdapter:
    """Create adapter with mocked ccxt exchange, no WS by default."""
    adapter = BinanceUSDMAdapter.__new__(BinanceUSDMAdapter)
    adapter._exchange = AsyncMock()
    adapter._ws_exchange = None
    adapter._semaphore = asyncio.Semaphore(10)
    adapter._rate_limit = 10
    adapter._cb_failures = 0
    adapter._cb_open_until = 0.0
    return adapter


_MARK_PRICE_PAYLOAD = {
    "BTC/USDT": {
        "markPrice": 65000.0,
        "indexPrice": 64950.0,
        "fundingRate": 0.0001,
        "nextFundingTime": 1700000000000,
    }
}


# ── watch_mark_prices tests ───────────────────────────────────────────


class TestWatchMarkPrices:
    @pytest.mark.asyncio
    async def test_not_initialized_raises_connection_error(self):
        """Raises ExchangeConnectionError when _ws_exchange is None."""
        adapter = _make_adapter()
        with pytest.raises(ExchangeConnectionError, match="WebSocket exchange not initialized"):
            await adapter.watch_mark_prices(["BTC/USDT"])

    @pytest.mark.asyncio
    async def test_empty_symbols_returns_empty_dict(self):
        """Returns {} immediately for an empty symbols list (no WS call made)."""
        adapter = _make_adapter()
        mock_ws = AsyncMock()
        adapter._ws_exchange = mock_ws

        result = await adapter.watch_mark_prices([])

        assert result == {}
        mock_ws.watch_mark_prices.assert_not_called()
        mock_ws.watch_tickers.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_returns_dict_from_ws(self):
        """Returns the dict produced by ccxt.pro watch_mark_prices."""
        adapter = _make_adapter()
        mock_ws = AsyncMock()
        mock_ws.watch_mark_prices = AsyncMock(return_value=_MARK_PRICE_PAYLOAD)
        adapter._ws_exchange = mock_ws

        result = await adapter.watch_mark_prices(["BTC/USDT"])

        assert result == _MARK_PRICE_PAYLOAD
        mock_ws.watch_mark_prices.assert_called_once_with(["BTC/USDT"])

    @pytest.mark.asyncio
    async def test_timeout_raises_timeout_error(self):
        """asyncio.TimeoutError propagates when WS times out."""
        adapter = _make_adapter()
        adapter._ws_exchange = AsyncMock()  # has watch_mark_prices

        with patch(
            "exchange.binance_usdm_adapter.asyncio.wait_for",
            side_effect=asyncio.TimeoutError,
        ):
            with pytest.raises(asyncio.TimeoutError):
                await adapter.watch_mark_prices(["BTC/USDT"])

    @pytest.mark.asyncio
    async def test_fallback_prefers_info_mark_price_over_last(self):
        """Fallback uses info.markPrice (not last-trade) when available."""
        adapter = _make_adapter()
        mock_ws = AsyncMock()
        mock_ws.watch_mark_prices = None  # triggers fallback path
        mock_ws.watch_tickers = AsyncMock(
            return_value={
                "BTC/USDT": {
                    "last": 65100.0,  # last-trade price — should NOT be used
                    "info": {
                        "markPrice": "65000.0",  # smoothed index-derived mark price
                        "indexPrice": "64950.0",
                        "fundingRate": "0.0001",
                        "nextFundingTime": "1700000000000",
                    },
                }
            }
        )
        adapter._ws_exchange = mock_ws

        result = await adapter.watch_mark_prices(["BTC/USDT"])

        assert "BTC/USDT" in result
        # Must prefer info.markPrice, not the last-trade price
        assert result["BTC/USDT"]["markPrice"] == "65000.0"
        assert result["BTC/USDT"]["indexPrice"] == "64950.0"
        mock_ws.watch_tickers.assert_called_once_with(["BTC/USDT"])

    @pytest.mark.asyncio
    async def test_fallback_uses_last_trade_when_info_mark_price_absent(self):
        """Fallback falls back to last-trade only when info.markPrice is absent."""
        adapter = _make_adapter()
        mock_ws = AsyncMock()
        mock_ws.watch_mark_prices = None
        mock_ws.watch_tickers = AsyncMock(
            return_value={
                "BTC/USDT": {
                    "last": 65000.0,
                    "info": {
                        # no markPrice in info
                        "fundingRate": "0.0001",
                    },
                }
            }
        )
        adapter._ws_exchange = mock_ws

        result = await adapter.watch_mark_prices(["BTC/USDT"])

        assert result["BTC/USDT"]["markPrice"] == 65000.0  # falls back to last

    @pytest.mark.asyncio
    async def test_fallback_returns_none_for_missing_info_fields(self):
        """Fallback returns None for all info-derived fields when info is absent."""
        adapter = _make_adapter()
        mock_ws = AsyncMock()
        mock_ws.watch_mark_prices = None  # use fallback
        mock_ws.watch_tickers = AsyncMock(
            return_value={
                "ETH/USDT": {
                    "last": 3500.0,
                    # No "info" key at all
                },
            }
        )
        adapter._ws_exchange = mock_ws

        result = await adapter.watch_mark_prices(["ETH/USDT"])

        assert "ETH/USDT" in result
        # No info dict → info-derived fields are None; markPrice falls back to last
        assert result["ETH/USDT"]["markPrice"] == 3500.0
        assert result["ETH/USDT"]["indexPrice"] is None
        assert result["ETH/USDT"]["fundingRate"] is None
        assert result["ETH/USDT"]["nextFundingTime"] is None

    @pytest.mark.asyncio
    async def test_multiple_symbols(self):
        """watch_mark_prices handles multiple symbols correctly."""
        adapter = _make_adapter()
        payload = {
            "BTC/USDT": {"markPrice": 65000.0, "indexPrice": 64950.0,
                         "fundingRate": 0.0001, "nextFundingTime": 1700000000000},
            "ETH/USDT": {"markPrice": 3500.0, "indexPrice": 3495.0,
                         "fundingRate": 0.0002, "nextFundingTime": 1700000000000},
        }
        mock_ws = AsyncMock()
        mock_ws.watch_mark_prices = AsyncMock(return_value=payload)
        adapter._ws_exchange = mock_ws

        result = await adapter.watch_mark_prices(["BTC/USDT", "ETH/USDT"])

        assert len(result) == 2
        assert "BTC/USDT" in result
        assert "ETH/USDT" in result


# ── Base adapter stub ─────────────────────────────────────────────────


class TestBaseAdapterWatchMarkPrices:
    @pytest.mark.asyncio
    async def test_watch_mark_prices_not_implemented_on_base(self):
        """Base ExchangeAdapter raises NotImplementedError."""
        with pytest.raises(NotImplementedError, match="Futures WS not supported"):
            await ExchangeAdapter.watch_mark_prices(None, ["BTC/USDT"])  # type: ignore[arg-type]
