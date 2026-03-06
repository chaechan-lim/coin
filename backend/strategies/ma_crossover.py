import pandas as pd
from exchange.data_models import Ticker
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from core.enums import SignalType


@StrategyRegistry.register
class MACrossoverStrategy(BaseStrategy):
    """
    Strategy 2: Moving Average Crossover (Golden Cross / Death Cross)
    Buy when short MA crosses above long MA, sell on opposite.
    """

    name = "ma_crossover"
    display_name = "이동평균 크로스오버"
    applicable_market_types = ["trending"]
    default_coins = ["BTC/KRW", "ETH/KRW", "XRP/KRW"]
    required_timeframe = "4h"
    min_candles_required = 55  # Need at least long_period + buffer

    def __init__(self, short_period: int = 20, long_period: int = 50):
        self._short_period = short_period
        self._long_period = long_period

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        if len(df) < self.min_candles_required:
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.0,
                strategy_name=self.name,
                reason="Insufficient data for MA crossover analysis",
            )

        short_col = f"sma_{self._short_period}"
        long_col = f"sma_{self._long_period}"

        # Use pre-computed or compute on the fly
        if short_col not in df.columns:
            df[short_col] = df["close"].rolling(self._short_period).mean()
        if long_col not in df.columns:
            df[long_col] = df["close"].rolling(self._long_period).mean()

        current_short = df[short_col].iloc[-1]
        current_long = df[long_col].iloc[-1]
        prev_short = df[short_col].iloc[-2]
        prev_long = df[long_col].iloc[-2]

        if pd.isna(current_short) or pd.isna(current_long):
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.0,
                strategy_name=self.name,
                reason="MA values not yet available (insufficient history)",
            )

        indicators = {
            f"sma_{self._short_period}": round(current_short, 0),
            f"sma_{self._long_period}": round(current_long, 0),
            "ma_diff_pct": round((current_short / current_long - 1) * 100, 2),
            "current_price": ticker.last,
        }

        # Golden Cross: short crosses above long
        golden_cross = prev_short <= prev_long and current_short > current_long
        # Death Cross: short crosses below long
        death_cross = prev_short >= prev_long and current_short < current_long

        # MA slope for confidence
        short_slope = (current_short - prev_short) / prev_short if prev_short > 0 else 0
        ma_gap_pct = abs(current_short - current_long) / current_long if current_long > 0 else 0

        if golden_cross:
            confidence = min(0.6 + ma_gap_pct * 10, 0.95)
            return Signal(
                signal_type=SignalType.BUY,
                confidence=round(confidence, 2),
                strategy_name=self.name,
                reason=f"골든크로스 발생: SMA{self._short_period}({current_short:,.0f}) > "
                f"SMA{self._long_period}({current_long:,.0f}), "
                f"갭 {ma_gap_pct*100:.2f}%",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        if death_cross:
            confidence = min(0.6 + ma_gap_pct * 10, 0.95)
            return Signal(
                signal_type=SignalType.SELL,
                confidence=round(confidence, 2),
                strategy_name=self.name,
                reason=f"데드크로스 발생: SMA{self._short_period}({current_short:,.0f}) < "
                f"SMA{self._long_period}({current_long:,.0f}), "
                f"갭 {ma_gap_pct*100:.2f}%",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # 추세 지속 — HOLD (크로스 이벤트만 시그널 발생)
        direction = "상승" if current_short > current_long else "하락"
        return Signal(
            signal_type=SignalType.HOLD,
            confidence=0.3,
            strategy_name=self.name,
            reason=f"{direction} 추세 지속: SMA{self._short_period}({current_short:,.0f}) "
            f"{'>' if current_short > current_long else '<'} "
            f"SMA{self._long_period}({current_long:,.0f}), 갭 {ma_gap_pct*100:.2f}%",
            indicators=indicators,
        )

    def get_params(self) -> dict:
        return {
            "short_period": self._short_period,
            "long_period": self._long_period,
        }

    def set_params(self, params: dict) -> None:
        if "short_period" in params:
            self._short_period = params["short_period"]
        if "long_period" in params:
            self._long_period = params["long_period"]
