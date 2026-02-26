"""
바이낸스 USDM 선물 어댑터
========================
ccxt `binanceusdm` 사용. 선물 전용 메서드 (레버리지, 포지션, 펀딩비) 구현.
"""

import asyncio
import structlog
from datetime import datetime, timezone
from typing import Optional

import ccxt.async_support as ccxt

from exchange.base import ExchangeAdapter
from exchange.data_models import (
    Candle, Ticker, OrderResult, Balance, OrderBook, FuturesPosition,
)
from core.exceptions import (
    ExchangeConnectionError,
    ExchangeRateLimitError,
    InsufficientBalanceError,
    ExchangeError,
    OrderNotFoundError,
)

logger = structlog.get_logger(__name__)


class BinanceUSDMAdapter(ExchangeAdapter):
    """Binance USDM perpetual futures adapter using ccxt."""

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
        self._exchange: Optional[ccxt.binanceusdm] = None
        self._semaphore = asyncio.Semaphore(rate_limit)

    async def initialize(self) -> None:
        config = {
            "enableRateLimit": True,
            "rateLimit": int(1000 / self._rate_limit),
            "options": {
                "defaultType": "future",
            },
        }
        if self._api_key and self._api_secret:
            config["apiKey"] = self._api_key
            config["secret"] = self._api_secret

        self._exchange = ccxt.binanceusdm(config)

        if self._testnet:
            self._exchange.set_sandbox_mode(True)

        try:
            await self._exchange.load_markets()
            logger.info(
                "binance_usdm_connected",
                markets=len(self._exchange.markets),
                testnet=self._testnet,
            )
        except Exception as e:
            raise ExchangeConnectionError(f"Failed to connect to Binance USDM: {e}")

    async def close(self) -> None:
        if self._exchange:
            await self._exchange.close()
            logger.info("binance_usdm_disconnected")

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

    # ── 시세 조회 (공개 API) ──────────────────────────────────────

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

    # ── 주문 ─────────────────────────────────────────────────────

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
        logger.info("futures_limit_buy", symbol=symbol, amount=amount, price=price)
        return self._parse_order(data)

    async def create_limit_sell(
        self, symbol: str, amount: float, price: float
    ) -> OrderResult:
        data = await self._call(
            self._exchange.create_limit_sell_order, symbol, amount, price
        )
        logger.info("futures_limit_sell", symbol=symbol, amount=amount, price=price)
        return self._parse_order(data)

    async def create_market_buy(self, symbol: str, amount: float) -> OrderResult:
        data = await self._call(
            self._exchange.create_market_buy_order, symbol, amount
        )
        logger.info("futures_market_buy", symbol=symbol, amount=amount)
        return self._parse_order(data)

    async def create_market_sell(self, symbol: str, amount: float) -> OrderResult:
        data = await self._call(
            self._exchange.create_market_sell_order, symbol, amount
        )
        logger.info("futures_market_sell", symbol=symbol, amount=amount)
        return self._parse_order(data)

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        await self._call(self._exchange.cancel_order, order_id, symbol)
        logger.info("futures_order_cancelled", order_id=order_id, symbol=symbol)
        return True

    async def fetch_order(self, order_id: str, symbol: str) -> OrderResult:
        data = await self._call(self._exchange.fetch_order, order_id, symbol)
        return self._parse_order(data)

    # ── 선물 전용 ────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        """심볼의 레버리지를 설정한다."""
        result = await self._call(
            self._exchange.set_leverage, leverage, symbol
        )
        logger.info("leverage_set", symbol=symbol, leverage=leverage)
        return result

    async def fetch_futures_position(self, symbol: str) -> FuturesPosition | None:
        """현재 선물 포지션 조회."""
        positions = await self._call(
            self._exchange.fetch_positions, [symbol]
        )
        for pos in positions:
            contracts = float(pos.get("contracts", 0) or 0)
            if contracts == 0:
                continue
            side = pos.get("side", "long")
            return FuturesPosition(
                symbol=symbol,
                side=side,
                amount=contracts,
                entry_price=float(pos.get("entryPrice", 0) or 0),
                leverage=int(pos.get("leverage", 1) or 1),
                liquidation_price=float(pos.get("liquidationPrice", 0) or 0),
                unrealized_pnl=float(pos.get("unrealizedPnl", 0) or 0),
                margin=float(pos.get("initialMargin", 0) or 0),
            )
        return None

    async def fetch_funding_rate(self, symbol: str) -> float:
        """현재 펀딩비율 조회."""
        data = await self._call(
            self._exchange.fetch_funding_rate, symbol
        )
        return float(data.get("fundingRate", 0) or 0)
