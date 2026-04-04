from abc import ABC, abstractmethod
from exchange.data_models import (
    Candle,
    Ticker,
    OrderResult,
    Balance,
    OrderBook,
    FuturesPosition,
    OpenInterest,
    MarkPriceInfo,
    LongShortRatio,
)


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
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 100,
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
    async def create_market_buy(self, symbol: str, amount: float) -> OrderResult:
        """Place a market buy order."""
        ...

    @abstractmethod
    async def create_market_sell(self, symbol: str, amount: float) -> OrderResult:
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

    # ── 정밀도 / 마켓 정보 (ccxt 위임 — SafeOrderPipeline 등에서 사용) ──

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        """Apply exchange-specific amount precision (truncation).

        Default delegates to the underlying ccxt exchange object.
        Subclasses may override if the internal attribute differs.
        """
        raw = getattr(self, "_exchange", None)
        if raw is not None:
            return raw.amount_to_precision(symbol, amount)
        raise NotImplementedError("amount_to_precision not available")

    def market(self, symbol: str) -> dict:
        """Return market info dict for a symbol.

        Default delegates to the underlying ccxt exchange object.
        Subclasses may override if needed.
        """
        raw = getattr(self, "_exchange", None)
        if raw is not None:
            return raw.market(symbol)
        raise NotImplementedError("market info not available")

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

    async def fetch_leverage_brackets(self, symbol: str) -> list[dict]:
        """Fetch leverage brackets (notional tiers + maintMarginRatio) for a symbol."""
        raise NotImplementedError("Futures not supported")

    async def fetch_position_risk(self, symbol: str | None = None) -> list[dict]:
        """Fetch position risk data (markPrice, marginRatio, etc.)."""
        raise NotImplementedError("Futures not supported")

    async def fetch_open_interest(self, symbol: str) -> OpenInterest:
        """Fetch current open interest for a symbol."""
        raise NotImplementedError("Futures not supported")

    async def fetch_open_interest_history(
        self,
        symbol: str,
        period: str = "1h",
        limit: int = 30,
    ) -> list[OpenInterest]:
        """Fetch open interest history for a symbol.

        Args:
            period: Kline interval — 5m/15m/30m/1h/2h/4h/6h/12h/1d.
            limit: Number of records (max 500).
        """
        raise NotImplementedError("Futures not supported")

    async def fetch_mark_price(self, symbol: str) -> MarkPriceInfo:
        """Fetch mark price, index price, and funding rate for a symbol."""
        raise NotImplementedError("Futures not supported")

    async def fetch_long_short_ratio(
        self,
        symbol: str,
        period: str = "1h",
    ) -> LongShortRatio:
        """Fetch top trader long/short ratio (account + position).

        Args:
            period: Kline interval — 5m/15m/30m/1h/2h/4h/6h/12h/1d.
        """
        raise NotImplementedError("Futures not supported")
