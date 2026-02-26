from abc import ABC, abstractmethod
from exchange.data_models import Candle, Ticker, OrderResult, Balance, OrderBook, FuturesPosition


class ExchangeAdapter(ABC):
    """Abstract interface for exchange operations."""

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the exchange connection."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close the exchange connection."""
        ...

    @abstractmethod
    async def fetch_ticker(self, symbol: str) -> Ticker:
        """Get current ticker for a symbol."""
        ...

    @abstractmethod
    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1h", limit: int = 100,
        since: int | None = None,
    ) -> list[Candle]:
        """Fetch OHLCV candlestick data.

        Args:
            since: Start timestamp in milliseconds (epoch). If provided,
                   fetches candles starting from this time.
        """
        ...

    @abstractmethod
    async def fetch_orderbook(self, symbol: str, limit: int = 20) -> OrderBook:
        """Fetch order book."""
        ...

    @abstractmethod
    async def fetch_balance(self) -> dict[str, Balance]:
        """Get account balances."""
        ...

    @abstractmethod
    async def create_limit_buy(
        self, symbol: str, amount: float, price: float
    ) -> OrderResult:
        """Place a limit buy order."""
        ...

    @abstractmethod
    async def create_limit_sell(
        self, symbol: str, amount: float, price: float
    ) -> OrderResult:
        """Place a limit sell order."""
        ...

    @abstractmethod
    async def create_market_buy(
        self, symbol: str, amount: float
    ) -> OrderResult:
        """Place a market buy order."""
        ...

    @abstractmethod
    async def create_market_sell(
        self, symbol: str, amount: float
    ) -> OrderResult:
        """Place a market sell order."""
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel an open order."""
        ...

    @abstractmethod
    async def fetch_order(self, order_id: str, symbol: str) -> OrderResult:
        """Fetch order status."""
        ...

    # ── 선물 전용 (Optional — 현물 어댑터는 NotImplementedError) ──

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        """Set leverage for a futures symbol."""
        raise NotImplementedError("Futures not supported")

    async def fetch_futures_position(self, symbol: str) -> FuturesPosition | None:
        """Fetch current futures position for a symbol."""
        raise NotImplementedError("Futures not supported")

    async def fetch_funding_rate(self, symbol: str) -> float:
        """Fetch current funding rate for a symbol."""
        raise NotImplementedError("Futures not supported")
