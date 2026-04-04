from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


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
    """미결제약정(OI) 데이터."""

    symbol: str
    open_interest: float  # OI 수량 (contracts)
    open_interest_value: float  # OI 금액 (USDT)
    timestamp: datetime


@dataclass
class MarkPriceInfo:
    """마크 프라이스 + 프리미엄 지수 데이터."""

    symbol: str
    mark_price: float
    index_price: float
    last_funding_rate: float
    next_funding_time: Optional[datetime]
    premium_pct: float  # (markPrice - indexPrice) / indexPrice * 100
    timestamp: datetime


@dataclass
class LongShortRatio:
    """Top Trader 롱/숏 비율 데이터."""

    symbol: str
    long_account: float  # 롱 계좌 비율
    short_account: float  # 숏 계좌 비율
    long_short_ratio: float  # 롱/숏 비율
    timestamp: datetime
