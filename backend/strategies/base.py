from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd

from core.enums import SignalType
from exchange.data_models import Ticker


@dataclass
class Signal:
    """Trading signal produced by a strategy."""

    signal_type: SignalType
    confidence: float  # 0.0 to 1.0
    strategy_name: str
    reason: str  # Human-readable reason for logging/retrospective
    suggested_price: Optional[float] = None
    suggested_amount: Optional[float] = None
    indicators: dict = field(default_factory=dict)  # Snapshot of indicators used


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy identifier."""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable strategy name."""
        ...

    @property
    @abstractmethod
    def applicable_market_types(self) -> list[str]:
        """Market types where this strategy works well: 'trending', 'sideways', 'all'."""
        ...

    @property
    @abstractmethod
    def default_coins(self) -> list[str]:
        """Default coins this strategy should be applied to."""
        ...

    @property
    @abstractmethod
    def required_timeframe(self) -> str:
        """OHLCV timeframe: '1m', '5m', '30m', '1h', '4h', '1d'."""
        ...

    @property
    @abstractmethod
    def min_candles_required(self) -> int:
        """Minimum candles needed to produce a signal."""
        ...

    @abstractmethod
    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        """
        Analyze market data and produce a trading signal.

        Args:
            df: DataFrame with OHLCV + pre-computed indicators
            ticker: Current real-time ticker
        Returns:
            Signal with type, confidence, reason, and indicator snapshot
        """
        ...

    @abstractmethod
    def get_params(self) -> dict:
        """Return current tunable parameters."""
        ...

    @abstractmethod
    def set_params(self, params: dict) -> None:
        """Update tunable parameters at runtime."""
        ...
