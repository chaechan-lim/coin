import asyncio
import random
import structlog
from datetime import datetime, timezone
from typing import Optional

from exchange.base import ExchangeAdapter
from exchange.bithumb_adapter import BithumbAdapter
from exchange.data_models import Candle, Ticker, OrderResult, Balance, OrderBook
from core.exceptions import InsufficientBalanceError, OrderNotFoundError

logger = structlog.get_logger(__name__)


class PaperAdapter(ExchangeAdapter):
    """Paper trading adapter: real market data, simulated order execution."""

    def __init__(
        self,
        real_adapter: BithumbAdapter,
        initial_balance_krw: float = 500_000,
        slippage_pct: float = 0.001,
        taker_fee_pct: float = 0.0025,
        maker_fee_pct: float = 0.0004,
    ):
        self._real = real_adapter
        self._slippage_pct = slippage_pct
        self._taker_fee_pct = taker_fee_pct
        self._maker_fee_pct = maker_fee_pct

        # Simulated state
        self._krw_balance = initial_balance_krw
        self._holdings: dict[str, float] = {}  # currency -> amount
        self._orders: dict[str, dict] = {}  # order_id -> order_data
        self._order_counter = 0
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self._real.initialize()
        logger.info(
            "paper_adapter_initialized",
            balance_krw=self._krw_balance,
        )

    async def close(self) -> None:
        await self._real.close()

    # -- Market data: delegate to real adapter --

    async def fetch_ticker(self, symbol: str) -> Ticker:
        return await self._real.fetch_ticker(symbol)

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1h", limit: int = 100,
        since: int | None = None,
    ) -> list[Candle]:
        return await self._real.fetch_ohlcv(symbol, timeframe, limit, since=since)

    async def fetch_orderbook(self, symbol: str, limit: int = 20) -> OrderBook:
        return await self._real.fetch_orderbook(symbol, limit)

    # -- Balance: simulated --

    async def fetch_balance(self) -> dict[str, Balance]:
        async with self._lock:
            balances = {
                "KRW": Balance(
                    currency="KRW",
                    free=self._krw_balance,
                    used=0.0,
                    total=self._krw_balance,
                ),
            }
            for currency, amount in self._holdings.items():
                if amount > 0:
                    balances[currency] = Balance(
                        currency=currency,
                        free=amount,
                        used=0.0,
                        total=amount,
                    )
            return balances

    def _next_order_id(self) -> str:
        self._order_counter += 1
        return f"paper_{self._order_counter}"

    def _apply_slippage(self, price: float, side: str) -> float:
        slip = price * self._slippage_pct * random.uniform(0, 1)
        if side == "buy":
            return price + slip  # buy slightly higher
        return price - slip  # sell slightly lower

    def _extract_currency(self, symbol: str) -> str:
        """Extract base currency from symbol like 'BTC/KRW'."""
        return symbol.split("/")[0]

    async def create_limit_buy(
        self, symbol: str, amount: float, price: float
    ) -> OrderResult:
        async with self._lock:
            fill_price = self._apply_slippage(price, "buy")
            cost = fill_price * amount
            fee = cost * self._taker_fee_pct
            total_cost = cost + fee

            if total_cost > self._krw_balance:
                raise InsufficientBalanceError(
                    f"Need {total_cost:.0f} KRW but have {self._krw_balance:.0f}"
                )

            self._krw_balance -= total_cost
            currency = self._extract_currency(symbol)
            self._holdings[currency] = self._holdings.get(currency, 0) + amount

            order_id = self._next_order_id()
            now = datetime.now(timezone.utc)

            result = OrderResult(
                order_id=order_id,
                symbol=symbol,
                side="buy",
                order_type="limit",
                status="closed",
                price=fill_price,
                amount=amount,
                filled=amount,
                remaining=0.0,
                cost=cost,
                fee=fee,
                fee_currency="KRW",
                timestamp=now,
            )
            self._orders[order_id] = {
                "result": result,
                "created_at": now,
            }

            logger.info(
                "paper_buy_executed",
                symbol=symbol,
                amount=amount,
                price=fill_price,
                cost=total_cost,
                balance_after=self._krw_balance,
            )
            return result

    async def create_limit_sell(
        self, symbol: str, amount: float, price: float
    ) -> OrderResult:
        async with self._lock:
            currency = self._extract_currency(symbol)
            held = self._holdings.get(currency, 0)
            if amount > held:
                raise InsufficientBalanceError(
                    f"Need {amount} {currency} but hold {held}"
                )

            fill_price = self._apply_slippage(price, "sell")
            cost = fill_price * amount
            fee = cost * self._taker_fee_pct
            proceeds = cost - fee

            self._holdings[currency] = held - amount
            if self._holdings[currency] <= 0:
                del self._holdings[currency]
            self._krw_balance += proceeds

            order_id = self._next_order_id()
            now = datetime.now(timezone.utc)

            result = OrderResult(
                order_id=order_id,
                symbol=symbol,
                side="sell",
                order_type="limit",
                status="closed",
                price=fill_price,
                amount=amount,
                filled=amount,
                remaining=0.0,
                cost=cost,
                fee=fee,
                fee_currency="KRW",
                timestamp=now,
            )
            self._orders[order_id] = {
                "result": result,
                "created_at": now,
            }

            logger.info(
                "paper_sell_executed",
                symbol=symbol,
                amount=amount,
                price=fill_price,
                proceeds=proceeds,
                balance_after=self._krw_balance,
            )
            return result

    async def create_market_buy(self, symbol: str, amount: float) -> OrderResult:
        ticker = await self.fetch_ticker(symbol)
        return await self.create_limit_buy(symbol, amount, ticker.ask)

    async def create_market_sell(self, symbol: str, amount: float) -> OrderResult:
        ticker = await self.fetch_ticker(symbol)
        return await self.create_limit_sell(symbol, amount, ticker.bid)

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        async with self._lock:
            if order_id not in self._orders:
                raise OrderNotFoundError(f"Paper order {order_id} not found")
            del self._orders[order_id]
            return True

    async def fetch_order(self, order_id: str, symbol: str) -> OrderResult:
        async with self._lock:
            if order_id not in self._orders:
                raise OrderNotFoundError(f"Paper order {order_id} not found")
            return self._orders[order_id]["result"]
