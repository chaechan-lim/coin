import asyncio
import structlog
from datetime import datetime, timezone
from typing import Optional

import ccxt.async_support as ccxt

from exchange.base import ExchangeAdapter
from exchange.data_models import Candle, Ticker, OrderResult, Balance, OrderBook
from core.exceptions import (
    ExchangeConnectionError,
    ExchangeRateLimitError,
    InsufficientBalanceError,
    ExchangeError,
    OrderNotFoundError,
)

logger = structlog.get_logger(__name__)


class BithumbAdapter(ExchangeAdapter):
    """Bithumb exchange adapter using ccxt."""

    def __init__(self, api_key: str = "", api_secret: str = "", rate_limit: int = 8):
        self._api_key = api_key
        self._api_secret = api_secret
        self._rate_limit = rate_limit
        self._exchange: Optional[ccxt.bithumb] = None
        self._semaphore = asyncio.Semaphore(rate_limit)

    async def initialize(self) -> None:
        config = {
            "enableRateLimit": True,
            "rateLimit": int(1000 / self._rate_limit),
        }
        if self._api_key and self._api_secret:
            config["apiKey"] = self._api_key
            config["secret"] = self._api_secret

        self._exchange = ccxt.bithumb(config)
        try:
            await self._exchange.load_markets()
            logger.info("bithumb_connected", markets=len(self._exchange.markets))
        except Exception as e:
            raise ExchangeConnectionError(f"Failed to connect to Bithumb: {e}")

    async def close(self) -> None:
        if self._exchange:
            await self._exchange.close()
            logger.info("bithumb_disconnected")

    async def _call(self, method, *args, **kwargs):
        """Rate-limited API call with error handling."""
        async with self._semaphore:
            try:
                return await method(*args, **kwargs)
            except ccxt.RateLimitExceeded as e:
                raise ExchangeRateLimitError(str(e))
            except ccxt.InsufficientFunds as e:
                raise InsufficientBalanceError(str(e))
            except ccxt.OrderNotFound as e:
                raise OrderNotFoundError(str(e))
            except ccxt.NetworkError as e:
                raise ExchangeConnectionError(str(e))
            except ccxt.ExchangeError as e:
                raise ExchangeError(str(e))

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
            fee_currency=(data.get("fee") or {}).get("currency", "KRW"),
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
        logger.info(
            "limit_buy_created",
            symbol=symbol,
            amount=amount,
            price=price,
            order_id=data["id"],
        )
        return self._parse_order(data)

    async def create_limit_sell(
        self, symbol: str, amount: float, price: float
    ) -> OrderResult:
        data = await self._call(
            self._exchange.create_limit_sell_order, symbol, amount, price
        )
        logger.info(
            "limit_sell_created",
            symbol=symbol,
            amount=amount,
            price=price,
            order_id=data["id"],
        )
        return self._parse_order(data)

    async def create_market_buy(self, symbol: str, amount: float) -> OrderResult:
        data = await self._call(
            self._exchange.create_market_buy_order, symbol, amount
        )
        logger.info("market_buy_created", symbol=symbol, amount=amount)
        return self._parse_order(data)

    async def create_market_sell(self, symbol: str, amount: float) -> OrderResult:
        data = await self._call(
            self._exchange.create_market_sell_order, symbol, amount
        )
        logger.info("market_sell_created", symbol=symbol, amount=amount)
        return self._parse_order(data)

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        await self._call(self._exchange.cancel_order, order_id, symbol)
        logger.info("order_cancelled", order_id=order_id, symbol=symbol)
        return True

    async def fetch_order(self, order_id: str, symbol: str) -> OrderResult:
        data = await self._call(self._exchange.fetch_order, order_id, symbol)
        return self._parse_order(data)
