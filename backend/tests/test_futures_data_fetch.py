"""Tests for futures data fetch methods in BinanceUSDMAdapter.

Tests: fetch_open_interest, fetch_open_interest_history,
       fetch_mark_price, fetch_long_short_ratio.
Also tests base adapter NotImplementedError defaults.
"""

import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from exchange.binance_usdm_adapter import BinanceUSDMAdapter
from exchange.base import ExchangeAdapter
from exchange.data_models import OpenInterest, MarkPriceInfo, LongShortRatio
from core.exceptions import ExchangeConnectionError, ExchangeError


# ── Helpers ──────────────────────────────────────────────────────


def _make_adapter() -> BinanceUSDMAdapter:
    """Create adapter with a mocked ccxt exchange."""
    adapter = BinanceUSDMAdapter.__new__(BinanceUSDMAdapter)
    adapter._exchange = AsyncMock()
    adapter._semaphore = asyncio.Semaphore(10)
    adapter._rate_limit = 10
    adapter._cb_failures = 0
    adapter._cb_open_until = 0.0
    return adapter


# ── Base Adapter Stubs (NotImplementedError) ──────────────────────


class TestBaseAdapterStubs:
    """Base adapter optional methods should raise NotImplementedError."""

    @pytest.mark.asyncio
    async def test_fetch_open_interest_not_implemented(self):
        adapter = AsyncMock(spec=ExchangeAdapter)
        adapter.fetch_open_interest = ExchangeAdapter.fetch_open_interest
        with pytest.raises(NotImplementedError, match="Futures not supported"):
            await adapter.fetch_open_interest(adapter, "BTC/USDT")

    @pytest.mark.asyncio
    async def test_fetch_open_interest_history_not_implemented(self):
        adapter = AsyncMock(spec=ExchangeAdapter)
        adapter.fetch_open_interest_history = (
            ExchangeAdapter.fetch_open_interest_history
        )
        with pytest.raises(NotImplementedError, match="Futures not supported"):
            await adapter.fetch_open_interest_history(adapter, "BTC/USDT")

    @pytest.mark.asyncio
    async def test_fetch_mark_price_not_implemented(self):
        adapter = AsyncMock(spec=ExchangeAdapter)
        adapter.fetch_mark_price = ExchangeAdapter.fetch_mark_price
        with pytest.raises(NotImplementedError, match="Futures not supported"):
            await adapter.fetch_mark_price(adapter, "BTC/USDT")

    @pytest.mark.asyncio
    async def test_fetch_long_short_ratio_not_implemented(self):
        adapter = AsyncMock(spec=ExchangeAdapter)
        adapter.fetch_long_short_ratio = ExchangeAdapter.fetch_long_short_ratio
        with pytest.raises(NotImplementedError, match="Futures not supported"):
            await adapter.fetch_long_short_ratio(adapter, "BTC/USDT")


# ── fetch_open_interest ──────────────────────────────────────────


class TestFetchOpenInterest:
    @pytest.mark.asyncio
    async def test_success(self):
        adapter = _make_adapter()
        adapter._exchange.fetch_open_interest = AsyncMock(
            return_value={
                "openInterestValue": 1_500_000_000.0,
                "timestamp": 1704067200000,  # 2024-01-01 00:00:00 UTC
            }
        )

        result = await adapter.fetch_open_interest("BTC/USDT")

        assert isinstance(result, OpenInterest)
        assert result.symbol == "BTC/USDT"
        assert result.open_interest_value == 1_500_000_000.0
        assert result.timestamp.year == 2024

    @pytest.mark.asyncio
    async def test_zero_oi(self):
        adapter = _make_adapter()
        adapter._exchange.fetch_open_interest = AsyncMock(
            return_value={
                "openInterestValue": 0,
                "timestamp": 1704067200000,
            }
        )

        result = await adapter.fetch_open_interest("TEST/USDT")
        assert result.open_interest_value == 0.0

    @pytest.mark.asyncio
    async def test_missing_timestamp_uses_now(self):
        adapter = _make_adapter()
        adapter._exchange.fetch_open_interest = AsyncMock(
            return_value={
                "openInterestValue": 100_000.0,
                "timestamp": None,
            }
        )

        result = await adapter.fetch_open_interest("ETH/USDT")
        assert result.timestamp is not None
        # Should be close to now
        delta = abs((datetime.now(timezone.utc) - result.timestamp).total_seconds())
        assert delta < 5

    @pytest.mark.asyncio
    async def test_none_oi_value_defaults_zero(self):
        adapter = _make_adapter()
        adapter._exchange.fetch_open_interest = AsyncMock(
            return_value={
                "openInterestValue": None,
                "timestamp": 1704067200000,
            }
        )

        result = await adapter.fetch_open_interest("BTC/USDT")
        assert result.open_interest_value == 0.0


# ── fetch_open_interest_history ──────────────────────────────────


class TestFetchOpenInterestHistory:
    @pytest.mark.asyncio
    async def test_success(self):
        adapter = _make_adapter()
        adapter._exchange.fapiPublicGetOpenInterestHist = AsyncMock(
            return_value=[
                {
                    "symbol": "BTCUSDT",
                    "sumOpenInterestValue": "1500000000.00",
                    "timestamp": "1704067200000",
                },
                {
                    "symbol": "BTCUSDT",
                    "sumOpenInterestValue": "1510000000.00",
                    "timestamp": "1704070800000",
                },
            ]
        )

        result = await adapter.fetch_open_interest_history("BTC/USDT", "1h", 2)

        assert len(result) == 2
        assert all(isinstance(r, OpenInterest) for r in result)
        assert result[0].open_interest_value == 1_500_000_000.0
        assert result[1].open_interest_value == 1_510_000_000.0
        assert result[0].symbol == "BTC/USDT"

    @pytest.mark.asyncio
    async def test_symbol_normalization(self):
        """BTC/USDT should be sent as BTCUSDT to the raw API."""
        adapter = _make_adapter()
        adapter._exchange.fapiPublicGetOpenInterestHist = AsyncMock(return_value=[])

        await adapter.fetch_open_interest_history("BTC/USDT", "5m", 10)

        call_args = adapter._exchange.fapiPublicGetOpenInterestHist.call_args
        assert call_args[0][0]["symbol"] == "BTCUSDT"
        assert call_args[0][0]["period"] == "5m"
        assert call_args[0][0]["limit"] == 10

    @pytest.mark.asyncio
    async def test_empty_response(self):
        adapter = _make_adapter()
        adapter._exchange.fapiPublicGetOpenInterestHist = AsyncMock(return_value=[])

        result = await adapter.fetch_open_interest_history("BTC/USDT")
        assert result == []

    @pytest.mark.asyncio
    async def test_non_list_response(self):
        """If API returns non-list, return empty list."""
        adapter = _make_adapter()
        adapter._exchange.fapiPublicGetOpenInterestHist = AsyncMock(return_value={})

        result = await adapter.fetch_open_interest_history("BTC/USDT")
        assert result == []

    @pytest.mark.asyncio
    async def test_missing_timestamp(self):
        adapter = _make_adapter()
        adapter._exchange.fapiPublicGetOpenInterestHist = AsyncMock(
            return_value=[
                {"sumOpenInterestValue": "100000", "timestamp": None},
            ]
        )

        result = await adapter.fetch_open_interest_history("BTC/USDT")
        assert len(result) == 1
        assert result[0].timestamp is not None


# ── fetch_mark_price ─────────────────────────────────────────────


class TestFetchMarkPrice:
    @pytest.mark.asyncio
    async def test_success(self):
        adapter = _make_adapter()
        adapter._exchange.fapiPublicGetPremiumIndex = AsyncMock(
            return_value={
                "symbol": "BTCUSDT",
                "markPrice": "65100.50",
                "indexPrice": "65000.00",
                "lastFundingRate": "0.00010000",
                "nextFundingTime": "1704096000000",
            }
        )

        result = await adapter.fetch_mark_price("BTC/USDT")

        assert isinstance(result, MarkPriceInfo)
        assert result.symbol == "BTC/USDT"
        assert result.mark_price == 65100.50
        assert result.index_price == 65000.00
        assert result.last_funding_rate == pytest.approx(0.0001)
        # premium_pct = (65100.5 - 65000) / 65000 * 100
        expected_premium = (65100.50 - 65000.00) / 65000.00 * 100
        assert result.premium_pct == pytest.approx(expected_premium, rel=1e-5)

    @pytest.mark.asyncio
    async def test_symbol_normalization(self):
        adapter = _make_adapter()
        adapter._exchange.fapiPublicGetPremiumIndex = AsyncMock(
            return_value={
                "markPrice": "3000",
                "indexPrice": "3000",
                "lastFundingRate": "0",
                "nextFundingTime": "0",
            }
        )

        await adapter.fetch_mark_price("ETH/USDT")

        call_args = adapter._exchange.fapiPublicGetPremiumIndex.call_args
        assert call_args[0][0]["symbol"] == "ETHUSDT"

    @pytest.mark.asyncio
    async def test_negative_premium(self):
        """Mark < index → negative premium."""
        adapter = _make_adapter()
        adapter._exchange.fapiPublicGetPremiumIndex = AsyncMock(
            return_value={
                "markPrice": "64900.00",
                "indexPrice": "65000.00",
                "lastFundingRate": "-0.0002",
                "nextFundingTime": "1704096000000",
            }
        )

        result = await adapter.fetch_mark_price("BTC/USDT")

        assert result.premium_pct < 0
        expected = (64900.0 - 65000.0) / 65000.0 * 100
        assert result.premium_pct == pytest.approx(expected, rel=1e-5)

    @pytest.mark.asyncio
    async def test_zero_index_price(self):
        """Zero index_price should not cause division by zero."""
        adapter = _make_adapter()
        adapter._exchange.fapiPublicGetPremiumIndex = AsyncMock(
            return_value={
                "markPrice": "65000",
                "indexPrice": "0",
                "lastFundingRate": "0",
                "nextFundingTime": "0",
            }
        )

        result = await adapter.fetch_mark_price("BTC/USDT")
        assert result.premium_pct == 0.0

    @pytest.mark.asyncio
    async def test_list_response(self):
        """API might return a list; adapter should handle it."""
        adapter = _make_adapter()
        adapter._exchange.fapiPublicGetPremiumIndex = AsyncMock(
            return_value=[
                {
                    "symbol": "BTCUSDT",
                    "markPrice": "65000",
                    "indexPrice": "65000",
                    "lastFundingRate": "0.0001",
                    "nextFundingTime": "1704096000000",
                }
            ]
        )

        result = await adapter.fetch_mark_price("BTC/USDT")
        assert result.mark_price == 65000.0

    @pytest.mark.asyncio
    async def test_empty_list_response(self):
        """Empty list from API → zero values."""
        adapter = _make_adapter()
        adapter._exchange.fapiPublicGetPremiumIndex = AsyncMock(return_value=[])

        result = await adapter.fetch_mark_price("BTC/USDT")
        assert result.mark_price == 0.0
        assert result.index_price == 0.0
        assert result.premium_pct == 0.0

    @pytest.mark.asyncio
    async def test_missing_next_funding_time(self):
        adapter = _make_adapter()
        adapter._exchange.fapiPublicGetPremiumIndex = AsyncMock(
            return_value={
                "markPrice": "65000",
                "indexPrice": "65000",
                "lastFundingRate": "0.0001",
                "nextFundingTime": None,
            }
        )

        result = await adapter.fetch_mark_price("BTC/USDT")
        assert result.next_funding_time is not None


# ── fetch_long_short_ratio ───────────────────────────────────────


class TestFetchLongShortRatio:
    @pytest.mark.asyncio
    async def test_success(self):
        adapter = _make_adapter()
        adapter._exchange.fapiDataGetTopLongShortAccountRatio = AsyncMock(
            return_value=[
                {
                    "symbol": "BTCUSDT",
                    "longAccount": "0.5500",
                    "shortAccount": "0.4500",
                    "timestamp": "1704067200000",
                }
            ]
        )
        adapter._exchange.fapiDataGetTopLongShortPositionRatio = AsyncMock(
            return_value=[
                {
                    "symbol": "BTCUSDT",
                    "longPosition": "0.6000",
                    "shortPosition": "0.4000",
                    "timestamp": "1704067200000",
                }
            ]
        )

        result = await adapter.fetch_long_short_ratio("BTC/USDT", "1h")

        assert isinstance(result, LongShortRatio)
        assert result.symbol == "BTC/USDT"
        assert result.long_account_ratio == 0.55
        assert result.short_account_ratio == 0.45
        assert result.long_position_ratio == 0.60
        assert result.short_position_ratio == 0.40
        assert result.timestamp.year == 2024

    @pytest.mark.asyncio
    async def test_symbol_normalization(self):
        adapter = _make_adapter()
        adapter._exchange.fapiDataGetTopLongShortAccountRatio = AsyncMock(
            return_value=[]
        )
        adapter._exchange.fapiDataGetTopLongShortPositionRatio = AsyncMock(
            return_value=[]
        )

        await adapter.fetch_long_short_ratio("ETH/USDT", "5m")

        acct_args = adapter._exchange.fapiDataGetTopLongShortAccountRatio.call_args
        pos_args = adapter._exchange.fapiDataGetTopLongShortPositionRatio.call_args
        assert acct_args[0][0]["symbol"] == "ETHUSDT"
        assert pos_args[0][0]["symbol"] == "ETHUSDT"
        assert acct_args[0][0]["period"] == "5m"

    @pytest.mark.asyncio
    async def test_empty_account_data(self):
        """Empty account ratio response → zero ratios."""
        adapter = _make_adapter()
        adapter._exchange.fapiDataGetTopLongShortAccountRatio = AsyncMock(
            return_value=[]
        )
        adapter._exchange.fapiDataGetTopLongShortPositionRatio = AsyncMock(
            return_value=[
                {
                    "longPosition": "0.60",
                    "shortPosition": "0.40",
                    "timestamp": "1704067200000",
                }
            ]
        )

        result = await adapter.fetch_long_short_ratio("BTC/USDT")
        assert result.long_account_ratio == 0.0
        assert result.short_account_ratio == 0.0
        assert result.long_position_ratio == 0.60

    @pytest.mark.asyncio
    async def test_empty_position_data(self):
        """Empty position ratio response → zero ratios."""
        adapter = _make_adapter()
        adapter._exchange.fapiDataGetTopLongShortAccountRatio = AsyncMock(
            return_value=[
                {
                    "longAccount": "0.55",
                    "shortAccount": "0.45",
                    "timestamp": "1704067200000",
                }
            ]
        )
        adapter._exchange.fapiDataGetTopLongShortPositionRatio = AsyncMock(
            return_value=[]
        )

        result = await adapter.fetch_long_short_ratio("BTC/USDT")
        assert result.long_account_ratio == 0.55
        assert result.long_position_ratio == 0.0
        assert result.short_position_ratio == 0.0

    @pytest.mark.asyncio
    async def test_both_empty(self):
        """Both APIs empty → all zero ratios."""
        adapter = _make_adapter()
        adapter._exchange.fapiDataGetTopLongShortAccountRatio = AsyncMock(
            return_value=[]
        )
        adapter._exchange.fapiDataGetTopLongShortPositionRatio = AsyncMock(
            return_value=[]
        )

        result = await adapter.fetch_long_short_ratio("BTC/USDT")
        assert result.long_account_ratio == 0.0
        assert result.short_account_ratio == 0.0
        assert result.long_position_ratio == 0.0
        assert result.short_position_ratio == 0.0

    @pytest.mark.asyncio
    async def test_non_list_response(self):
        """Non-list from API → zero ratios."""
        adapter = _make_adapter()
        adapter._exchange.fapiDataGetTopLongShortAccountRatio = AsyncMock(
            return_value={}
        )
        adapter._exchange.fapiDataGetTopLongShortPositionRatio = AsyncMock(
            return_value={}
        )

        result = await adapter.fetch_long_short_ratio("BTC/USDT")
        assert result.long_account_ratio == 0.0
        assert result.long_position_ratio == 0.0


# ── Error Handling (Circuit Breaker / Timeout) ───────────────────


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_circuit_breaker_trips_on_consecutive_timeouts(self):
        """After _CB_THRESHOLD timeouts, circuit breaker should open."""
        adapter = _make_adapter()
        adapter._exchange.fetch_open_interest = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )

        for _ in range(BinanceUSDMAdapter._CB_THRESHOLD):
            with pytest.raises(ExchangeConnectionError, match="timed out"):
                await adapter.fetch_open_interest("BTC/USDT")

        # Next call should be blocked by circuit breaker
        with pytest.raises(ExchangeConnectionError, match="Circuit breaker"):
            await adapter.fetch_open_interest("BTC/USDT")

    @pytest.mark.asyncio
    async def test_exchange_error_propagated(self):
        """CCXT ExchangeError should be translated."""
        import ccxt.async_support as ccxt_async

        adapter = _make_adapter()
        adapter._exchange.fapiPublicGetPremiumIndex = AsyncMock(
            side_effect=ccxt_async.ExchangeError("Invalid symbol")
        )

        with pytest.raises(ExchangeError, match="Invalid symbol"):
            await adapter.fetch_mark_price("INVALID/USDT")

    @pytest.mark.asyncio
    async def test_network_error_increments_cb(self):
        """Network errors should increment circuit breaker counter."""
        import ccxt.async_support as ccxt_async

        adapter = _make_adapter()
        adapter._exchange.fetch_open_interest = AsyncMock(
            side_effect=ccxt_async.NetworkError("Connection reset")
        )

        with pytest.raises(ExchangeConnectionError, match="Connection reset"):
            await adapter.fetch_open_interest("BTC/USDT")

        assert adapter._cb_failures == 1

    @pytest.mark.asyncio
    async def test_successful_call_resets_cb(self):
        """A successful call should reset the circuit breaker counter."""
        adapter = _make_adapter()
        adapter._cb_failures = 3  # partially failed

        adapter._exchange.fetch_open_interest = AsyncMock(
            return_value={
                "openInterestValue": 1000.0,
                "timestamp": 1704067200000,
            }
        )

        result = await adapter.fetch_open_interest("BTC/USDT")
        assert result.open_interest_value == 1000.0
        assert adapter._cb_failures == 0
