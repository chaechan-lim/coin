import time
import structlog
import pandas as pd
import pandas_ta as ta
from typing import Optional
from exchange.base import ExchangeAdapter
from exchange.data_models import Candle, Ticker

logger = structlog.get_logger(__name__)


class MarketDataService:
    """Centralized market data provider with caching and indicator computation."""

    def __init__(self, exchange: ExchangeAdapter, cache_ttl_sec: int = 60):
        self._exchange = exchange
        self._cache_ttl = cache_ttl_sec
        self._ohlcv_cache: dict[str, tuple[float, pd.DataFrame]] = {}
        self._ticker_cache: dict[str, tuple[float, Ticker]] = {}

    def _cache_key(self, symbol: str, timeframe: str) -> str:
        return f"{symbol}:{timeframe}"

    async def get_candles(
        self, symbol: str, timeframe: str = "1h", limit: int = 200
    ) -> pd.DataFrame:
        """Fetch OHLCV as DataFrame with technical indicators pre-computed."""
        key = self._cache_key(symbol, timeframe)
        now = time.time()

        if key in self._ohlcv_cache:
            cached_time, cached_df = self._ohlcv_cache[key]
            if now - cached_time < self._cache_ttl:
                return cached_df

        candles = await self._exchange.fetch_ohlcv(symbol, timeframe, limit)
        df = self._candles_to_dataframe(candles)
        df = self._compute_indicators(df)

        self._ohlcv_cache[key] = (now, df)
        return df

    async def get_ticker(self, symbol: str) -> Ticker:
        """Fetch current ticker with caching."""
        now = time.time()
        if symbol in self._ticker_cache:
            cached_time, cached_ticker = self._ticker_cache[symbol]
            if now - cached_time < 10:  # 10-second TTL for tickers
                return cached_ticker

        ticker = await self._exchange.fetch_ticker(symbol)
        self._ticker_cache[symbol] = (now, ticker)
        return ticker

    async def get_current_price(self, symbol: str) -> float:
        ticker = await self.get_ticker(symbol)
        return ticker.last

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

    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Pre-compute commonly used technical indicators."""
        if len(df) < 2:
            return df

        # Simple Moving Averages
        df["sma_9"] = ta.sma(df["close"], length=9)
        df["sma_20"] = ta.sma(df["close"], length=20)
        df["sma_50"] = ta.sma(df["close"], length=50)
        df["sma_200"] = ta.sma(df["close"], length=200)

        # Exponential Moving Averages
        df["ema_12"] = ta.ema(df["close"], length=12)
        df["ema_26"] = ta.ema(df["close"], length=26)

        # RSI
        df["rsi_14"] = ta.rsi(df["close"], length=14)

        # MACD
        macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
        if macd is not None:
            df = pd.concat([df, macd], axis=1)

        # Bollinger Bands
        bbands = ta.bbands(df["close"], length=20, std=2.0)
        if bbands is not None:
            df = pd.concat([df, bbands], axis=1)

        # ATR (Average True Range)
        df["atr_14"] = ta.atr(df["high"], df["low"], df["close"], length=14)

        # ADX (Average Directional Index) — 시장 상태 감지용
        adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
        if adx_df is not None:
            df = pd.concat([df, adx_df], axis=1)

        # SMA 60 — 시장 추세 판단용
        df["sma_60"] = ta.sma(df["close"], length=60)

        # Volume SMA
        df["volume_sma_20"] = ta.sma(df["volume"], length=20)

        return df

    def clear_cache(self) -> None:
        self._ohlcv_cache.clear()
        self._ticker_cache.clear()
