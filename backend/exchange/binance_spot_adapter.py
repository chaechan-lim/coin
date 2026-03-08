"""
바이낸스 현물(Spot) 어댑터
==========================
ccxt `binance` 사용. 선물 메서드 없음 — 순수 현물 매매만 지원.
"""

import asyncio
import structlog
from datetime import datetime, timezone
from typing import Optional

import ccxt.async_support as ccxt

from exchange.base import ExchangeAdapter
from exchange.data_models import (
    Candle, Ticker, OrderResult, Balance, OrderBook,
)
from core.exceptions import (
    ExchangeConnectionError,
    ExchangeRateLimitError,
    InsufficientBalanceError,
    ExchangeError,
    OrderNotFoundError,
)

logger = structlog.get_logger(__name__)


class BinanceSpotAdapter(ExchangeAdapter):
    """Binance spot adapter using ccxt."""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = True,
        rate_limit: int = 10,
    ):
        self._api_key = api_key
        self._api_secret = api_secret
        self._testnet = testnet
        self._rate_limit = rate_limit
        self._exchange: Optional[ccxt.binance] = None
        self._semaphore = asyncio.Semaphore(rate_limit)

    async def initialize(self) -> None:
        config = {
            "enableRateLimit": True,
            "rateLimit": int(1000 / self._rate_limit),
        }
        if self._api_key and self._api_secret:
            config["apiKey"] = self._api_key
            config["secret"] = self._api_secret

        self._exchange = ccxt.binance(config)

        if self._testnet:
            self._exchange.set_sandbox_mode(True)

        try:
            await self._exchange.load_markets()
            # 메모리 최적화: /USDT 마켓만 유지 (4000+ → ~200개, ~70MB 절감)
            full_count = len(self._exchange.markets)
            usdt_markets = {k: v for k, v in self._exchange.markets.items() if "/USDT" in k}
            self._exchange.markets = usdt_markets
            self._exchange.symbols = list(usdt_markets.keys())
            logger.info(
                "binance_spot_connected",
                markets_total=full_count,
                markets_loaded=len(usdt_markets),
                testnet=self._testnet,
            )
        except Exception as e:
            raise ExchangeConnectionError(f"Failed to connect to Binance Spot: {e}")

    async def close(self) -> None:
        if self._exchange:
            await self._exchange.close()
            logger.info("binance_spot_disconnected")

    # Circuit breaker settings
    _CB_THRESHOLD = 5       # consecutive failures to trip
    _CB_RESET_SEC = 60      # seconds before retry after trip
    _API_TIMEOUT = 30       # seconds per API call

    def __init_cb(self):
        """Lazy init circuit breaker state (called in _call)."""
        if not hasattr(self, '_cb_failures'):
            self._cb_failures = 0
            self._cb_open_until = 0.0

    async def _call(self, method, *args, **kwargs):
        """Rate-limited API call with timeout and circuit breaker."""
        self.__init_cb()
        import time as _time
        now = _time.monotonic()
        if self._cb_failures >= self._CB_THRESHOLD:
            if now < self._cb_open_until:
                raise ExchangeConnectionError(
                    f"Circuit breaker open ({self._cb_failures} consecutive failures)"
                )
            # Reset after cooldown
            self._cb_failures = 0

        async with self._semaphore:
            try:
                result = await asyncio.wait_for(
                    method(*args, **kwargs), timeout=self._API_TIMEOUT
                )
                self._cb_failures = 0
                return result
            except asyncio.TimeoutError:
                self._cb_failures += 1
                self._cb_open_until = _time.monotonic() + self._CB_RESET_SEC
                raise ExchangeConnectionError(
                    f"API call timed out after {self._API_TIMEOUT}s"
                )
            except ccxt.RateLimitExceeded as e:
                raise ExchangeRateLimitError(str(e))
            except ccxt.InsufficientFunds as e:
                raise InsufficientBalanceError(str(e))
            except ccxt.OrderNotFound as e:
                raise OrderNotFoundError(str(e))
            except ccxt.NetworkError as e:
                self._cb_failures += 1
                self._cb_open_until = _time.monotonic() + self._CB_RESET_SEC
                raise ExchangeConnectionError(str(e))
            except ccxt.ExchangeError as e:
                raise ExchangeError(str(e))

    # -- Market data --

    async def fetch_tickers(self) -> dict:
        """Fetch all tickers."""
        return await self._call(self._exchange.fetch_tickers)

    async def fetch_ticker(self, symbol: str) -> Ticker:
        data = await self._call(self._exchange.fetch_ticker, symbol)
        return Ticker(
            symbol=symbol,
            last=float(data["last"] or 0),
            bid=float(data["bid"] or 0),
            ask=float(data["ask"] or 0),
            high=float(data["high"] or 0),
            low=float(data["low"] or 0),
            volume=float(data["baseVolume"] or 0),
            timestamp=datetime.fromtimestamp(
                data["timestamp"] / 1000, tz=timezone.utc
            ),
        )

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1h", limit: int = 100,
        since: int | None = None,
    ) -> list[Candle]:
        data = await self._call(
            self._exchange.fetch_ohlcv, symbol, timeframe,
            since=since, limit=limit,
        )
        return [
            Candle(
                timestamp=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in data
        ]

    async def fetch_orderbook(self, symbol: str, limit: int = 20) -> OrderBook:
        data = await self._call(self._exchange.fetch_order_book, symbol, limit)
        return OrderBook(
            symbol=symbol,
            bids=[(float(b[0]), float(b[1])) for b in data["bids"]],
            asks=[(float(a[0]), float(a[1])) for a in data["asks"]],
            timestamp=datetime.now(timezone.utc),
        )

    async def fetch_balance(self) -> dict[str, Balance]:
        data = await self._call(self._exchange.fetch_balance)
        balances = {}
        for currency, info in data.items():
            if isinstance(info, dict) and "free" in info:
                balances[currency] = Balance(
                    currency=currency,
                    free=float(info.get("free", 0) or 0),
                    used=float(info.get("used", 0) or 0),
                    total=float(info.get("total", 0) or 0),
                )
        return balances

    # -- Orders --

    def _parse_order(self, data: dict) -> OrderResult:
        return OrderResult(
            order_id=str(data["id"]),
            symbol=data["symbol"],
            side=data["side"],
            order_type=data["type"],
            status=data["status"],
            price=float(data["price"] or 0),
            amount=float(data["amount"] or 0),
            filled=float(data["filled"] or 0),
            remaining=float(data["remaining"] or 0),
            cost=float(data["cost"] or 0),
            fee=float((data.get("fee") or {}).get("cost", 0) or 0),
            fee_currency=(data.get("fee") or {}).get("currency", "USDT"),
            timestamp=datetime.fromtimestamp(
                data["timestamp"] / 1000, tz=timezone.utc
            ),
            info=data.get("info", {}),
        )

    async def create_limit_buy(
        self, symbol: str, amount: float, price: float
    ) -> OrderResult:
        data = await self._call(
            self._exchange.create_limit_buy_order, symbol, amount, price
        )
        logger.info("spot_limit_buy", symbol=symbol, amount=amount, price=price)
        return self._parse_order(data)

    async def create_limit_sell(
        self, symbol: str, amount: float, price: float
    ) -> OrderResult:
        data = await self._call(
            self._exchange.create_limit_sell_order, symbol, amount, price
        )
        logger.info("spot_limit_sell", symbol=symbol, amount=amount, price=price)
        return self._parse_order(data)

    async def create_market_buy(self, symbol: str, amount: float) -> OrderResult:
        data = await self._call(
            self._exchange.create_market_buy_order, symbol, amount
        )
        logger.info("spot_market_buy", symbol=symbol, amount=amount)
        return self._parse_order(data)

    async def create_market_sell(self, symbol: str, amount: float) -> OrderResult:
        data = await self._call(
            self._exchange.create_market_sell_order, symbol, amount
        )
        logger.info("spot_market_sell", symbol=symbol, amount=amount)
        return self._parse_order(data)

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        await self._call(self._exchange.cancel_order, order_id, symbol)
        logger.info("spot_order_cancelled", order_id=order_id, symbol=symbol)
        return True

    async def fetch_order(self, order_id: str, symbol: str) -> OrderResult:
        data = await self._call(self._exchange.fetch_order, order_id, symbol)
        return self._parse_order(data)
