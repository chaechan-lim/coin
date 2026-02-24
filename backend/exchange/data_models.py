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
