import asyncio
import time
import structlog
import pandas as pd
from collections import OrderedDict
from exchange.base import ExchangeAdapter
from exchange.data_models import Candle, Ticker

logger = structlog.get_logger(__name__)

_MAX_RETRIES = 3
_RETRY_BASE_SEC = 1.0


class _LRUCache:
    """Simple LRU cache with TTL and max size."""

    def __init__(self, max_size: int, ttl_sec: float):
        self._max_size = max_size
        self._ttl = ttl_sec
        self._data: OrderedDict[str, tuple[float, object]] = OrderedDict()

    def get(self, key: str):
        if key in self._data:
            ts, val = self._data[key]
            if time.time() - ts < self._ttl:
                self._data.move_to_end(key)
                return val
            del self._data[key]
        return None

    def put(self, key: str, value):
        self._data[key] = (time.time(), value)
        self._data.move_to_end(key)
        while len(self._data) > self._max_size:
            self._data.popitem(last=False)

    def clear(self):
        self._data.clear()


class MarketDataService:
    """Centralized market data provider with LRU caching, retry, and indicator computation."""

    def __init__(self, exchange: ExchangeAdapter, cache_ttl_sec: int = 60):
        self._exchange = exchange
        self._cache_ttl = cache_ttl_sec
        self._ohlcv_cache = _LRUCache(max_size=100, ttl_sec=cache_ttl_sec)
        self._ticker_cache = _LRUCache(max_size=50, ttl_sec=10)

    def _cache_key(self, symbol: str, timeframe: str) -> str:
        return f"{symbol}:{timeframe}"

    async def _fetch_with_retry(self, coro_func, *args, **kwargs):
        """Retry with exponential backoff on transient failures."""
        last_err = None
        for attempt in range(_MAX_RETRIES):
            try:
                return await coro_func(*args, **kwargs)
            except Exception as e:
                last_err = e
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BASE_SEC * (2**attempt)
                    err_str = str(e)
                    # 삭제된 심볼은 재시도 없이 즉시 실패
                    if "does not have market symbol" in err_str:
                        raise e
                    logger.warning(
                        "market_data_retry",
                        attempt=attempt + 1,
                        wait=wait,
                        error=err_str,
                    )
                    await asyncio.sleep(wait)
        raise last_err

    async def get_candles(
        self, symbol: str, timeframe: str = "1h", limit: int = 200
    ) -> pd.DataFrame:
        """Fetch OHLCV as DataFrame with technical indicators pre-computed."""
        key = self._cache_key(symbol, timeframe)

        cached = self._ohlcv_cache.get(key)
        if cached is not None:
            return cached

        candles = await self._fetch_with_retry(
            self._exchange.fetch_ohlcv, symbol, timeframe, limit
        )
        df = self._candles_to_dataframe(candles)
        df = self._compute_indicators(df)

        self._ohlcv_cache.put(key, df)
        return df

    async def get_ohlcv_df(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 200,
    ) -> pd.DataFrame:
        """Alias for get_candles (v2 엔진 호환)."""
        return await self.get_candles(symbol, timeframe, limit)

    async def get_ticker(self, symbol: str) -> Ticker:
        """Fetch current ticker with caching."""
        cached = self._ticker_cache.get(symbol)
        if cached is not None:
            return cached

        ticker = await self._fetch_with_retry(self._exchange.fetch_ticker, symbol)
        self._ticker_cache.put(symbol, ticker)
        return ticker

    async def get_current_price(self, symbol: str) -> float:
        ticker = await self.get_ticker(symbol)
        if ticker.last > 0:
            return ticker.last
        # fallback: ticker.last=0이면 오더북 mid-price 시도
        try:
            ob = await self._exchange.fetch_orderbook(symbol, limit=5)
            if ob.bids and ob.asks:
                mid = (ob.bids[0][0] + ob.asks[0][0]) / 2
                logger.warning("price_fallback_orderbook", symbol=symbol, mid=mid)
                return mid
        except Exception as e:
            logger.warning("orderbook_fallback_failed", symbol=symbol, error=str(e))
        return 0.0

    def _candles_to_dataframe(self, candles: list[Candle]) -> pd.DataFrame:
        data = {
            "timestamp": [c.timestamp for c in candles],
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume for c in candles],
        }
        df = pd.DataFrame(data)
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)
        return df

    # COIN-52: _INDICATOR_RENAME → services.indicators._RENAME_MAP으로 통합 이관.
    # 기존 테스트 호환을 위해 참조 유지.
    _INDICATOR_RENAME: dict[str, str] = {
        "ADX_14": "adx_14",
        "DMP_14": "dmp_14",
        "DMN_14": "dmn_14",
        "MACD_12_26_9": "macd_line",
        "MACDs_12_26_9": "macd_signal",
        "MACDh_12_26_9": "macd_hist",
    }

    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Pre-compute commonly used technical indicators.

        COIN-52: services.indicators.compute_indicators()로 위임.
        백테스트/라이브 동일 파이프라인 사용.
        """
        from services.indicators import compute_indicators

        return compute_indicators(df)

    def clear_cache(self) -> None:
        self._ohlcv_cache.clear()
        self._ticker_cache.clear()

    @property
    def cache_stats(self) -> dict:
        return {
            "ohlcv_entries": len(self._ohlcv_cache._data),
            "ticker_entries": len(self._ticker_cache._data),
        }
