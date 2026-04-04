from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Ticker:
    symbol: str
    last: float
    bid: float
    ask: float
    high: float
    low: float
    volume: float
    timestamp: datetime


@dataclass
class Balance:
    currency: str
    free: float  # available
    used: float  # in orders
    total: float


@dataclass
class OrderResult:
    order_id: str
    symbol: str
    side: str  # "buy" / "sell"
    order_type: str  # "limit" / "market"
    status: str  # "open" / "closed" / "canceled"
    price: float
    amount: float
    filled: float
    remaining: float
    cost: float
    fee: float
    fee_currency: str
    timestamp: datetime
    info: dict = field(default_factory=dict)


@dataclass
class OrderBook:
    symbol: str
    bids: list[tuple[float, float]]  # [(price, amount), ...]
    asks: list[tuple[float, float]]
    timestamp: datetime


@dataclass
class FuturesPosition:
    symbol: str
    side: str  # "long" / "short"
    amount: float  # 포지션 수량
    entry_price: float
    leverage: int
    liquidation_price: float
    unrealized_pnl: float
    margin: float


@dataclass
class OpenInterest:
    """미결제약정 (Open Interest) 데이터."""

    symbol: str
    open_interest_value: float  # OI value in quote currency (USDT)
    timestamp: datetime


@dataclass
class MarkPriceInfo:
    """마크 프라이스 + 프리미엄 정보."""

    symbol: str
    mark_price: float
    index_price: float
    last_funding_rate: float
    next_funding_time: datetime
    premium_pct: float  # (mark - index) / index * 100
    timestamp: datetime


@dataclass
class LongShortRatio:
    """롱숏 비율 데이터."""

    symbol: str
    long_account_ratio: float
    short_account_ratio: float
    long_position_ratio: float
    short_position_ratio: float
    timestamp: datetime
