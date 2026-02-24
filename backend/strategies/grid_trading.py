import structlog
import pandas as pd
from dataclasses import dataclass, field
from exchange.data_models import Ticker
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from core.enums import SignalType

logger = structlog.get_logger(__name__)


@dataclass
class GridLevel:
    price: float
    side: str  # "buy" or "sell"
    is_filled: bool = False
    order_id: str | None = None


@StrategyRegistry.register
class GridTradingStrategy(BaseStrategy):
    """
    Strategy 6: Grid Trading
    Places buy/sell orders at fixed intervals above and below current price.
    Self-managing: bypasses the signal combiner.
    """

    name = "grid_trading"
    display_name = "그리드 트레이딩"
    applicable_market_types = ["sideways"]
    default_coins = ["BTC/KRW", "ETH/KRW"]
    required_timeframe = "30m"
    min_candles_required = 10

    def __init__(
        self,
        grid_spacing_pct: float = 0.01,  # 1% between grids
        num_grids: int = 5,  # 5 above + 5 below
        order_size_krw: float = 50_000,
    ):
        self._grid_spacing_pct = grid_spacing_pct
        self._num_grids = num_grids
        self._order_size_krw = order_size_krw
        self._grid_levels: dict[str, list[GridLevel]] = {}  # symbol -> levels
        self._initialized: dict[str, bool] = {}

    def _initialize_grid(self, symbol: str, center_price: float) -> list[GridLevel]:
        """Create grid levels centered around current price."""
        levels = []
        for i in range(1, self._num_grids + 1):
            # Buy levels below
            buy_price = center_price * (1 - self._grid_spacing_pct * i)
            levels.append(GridLevel(price=round(buy_price, 0), side="buy"))
            # Sell levels above
            sell_price = center_price * (1 + self._grid_spacing_pct * i)
            levels.append(GridLevel(price=round(sell_price, 0), side="sell"))

        levels.sort(key=lambda x: x.price)
        self._grid_levels[symbol] = levels
        self._initialized[symbol] = True
        logger.info(
            "grid_initialized",
            symbol=symbol,
            center_price=center_price,
            num_levels=len(levels),
            range_low=levels[0].price,
            range_high=levels[-1].price,
        )
        return levels

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        symbol = ticker.symbol
        current_price = ticker.last

        if symbol not in self._initialized:
            self._initialize_grid(symbol, current_price)

        levels = self._grid_levels[symbol]

        # Find the nearest unfilled buy level that price has crossed below
        nearest_buy = None
        for level in levels:
            if level.side == "buy" and not level.is_filled and current_price <= level.price:
                nearest_buy = level
                break

        # Find the nearest unfilled sell level that price has crossed above
        nearest_sell = None
        for level in reversed(levels):
            if level.side == "sell" and not level.is_filled and current_price >= level.price:
                nearest_sell = level
                break

        grid_range_low = levels[0].price
        grid_range_high = levels[-1].price
        active_buys = sum(1 for l in levels if l.side == "buy" and not l.is_filled)
        active_sells = sum(1 for l in levels if l.side == "sell" and not l.is_filled)

        indicators = {
            "grid_range": f"{grid_range_low:,.0f} ~ {grid_range_high:,.0f}",
            "active_buy_levels": active_buys,
            "active_sell_levels": active_sells,
            "grid_spacing_pct": self._grid_spacing_pct * 100,
            "current_price": current_price,
        }

        if nearest_buy:
            amount = self._order_size_krw / current_price
            return Signal(
                signal_type=SignalType.BUY,
                confidence=0.7,
                strategy_name=self.name,
                reason=f"그리드 매수: 가격({current_price:,.0f})이 "
                f"매수 그리드({nearest_buy.price:,.0f})에 도달. "
                f"주문 크기: {self._order_size_krw:,.0f}원",
                suggested_price=current_price,
                suggested_amount=amount,
                indicators=indicators,
            )

        if nearest_sell:
            amount = self._order_size_krw / current_price
            return Signal(
                signal_type=SignalType.SELL,
                confidence=0.7,
                strategy_name=self.name,
                reason=f"그리드 매도: 가격({current_price:,.0f})이 "
                f"매도 그리드({nearest_sell.price:,.0f})에 도달. "
                f"주문 크기: {self._order_size_krw:,.0f}원",
                suggested_price=current_price,
                suggested_amount=amount,
                indicators=indicators,
            )

        # Check if price is outside grid range - need to re-center
        if current_price < grid_range_low * 0.95 or current_price > grid_range_high * 1.05:
            self._initialize_grid(symbol, current_price)
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.3,
                strategy_name=self.name,
                reason=f"그리드 범위 이탈로 재설정: 새 중심가 {current_price:,.0f}",
                indicators=indicators,
            )

        return Signal(
            signal_type=SignalType.HOLD,
            confidence=0.3,
            strategy_name=self.name,
            reason=f"그리드 대기: 다음 매수 레벨 또는 매도 레벨에 미도달",
            indicators=indicators,
        )

    def mark_level_filled(self, symbol: str, price: float, side: str) -> None:
        """Mark a grid level as filled after order execution."""
        if symbol in self._grid_levels:
            for level in self._grid_levels[symbol]:
                if level.side == side and abs(level.price - price) / price < 0.005:
                    level.is_filled = True
                    break

    def get_params(self) -> dict:
        return {
            "grid_spacing_pct": self._grid_spacing_pct,
            "num_grids": self._num_grids,
            "order_size_krw": self._order_size_krw,
        }

    def set_params(self, params: dict) -> None:
        if "grid_spacing_pct" in params:
            self._grid_spacing_pct = params["grid_spacing_pct"]
        if "num_grids" in params:
            self._num_grids = params["num_grids"]
        if "order_size_krw" in params:
            self._order_size_krw = params["order_size_krw"]
        # Reset grids on param change
        self._initialized.clear()
        self._grid_levels.clear()
