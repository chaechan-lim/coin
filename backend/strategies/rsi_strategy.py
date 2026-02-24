import pandas as pd
from exchange.data_models import Ticker
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from core.enums import SignalType


@StrategyRegistry.register
class RSIStrategy(BaseStrategy):
    """
    Strategy 3: RSI Overbought/Oversold
    Buy when RSI < oversold threshold, sell when RSI > overbought threshold.
    Designed primarily as a filter/confirmation for other strategies.
    """

    name = "rsi"
    display_name = "RSI 과매수/과매도"
    applicable_market_types = ["all"]
    default_coins = ["BTC/KRW", "ETH/KRW", "XRP/KRW", "SOL/KRW", "ADA/KRW"]
    required_timeframe = "1h"
    min_candles_required = 20

    def __init__(
        self,
        period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
        extreme_oversold: float = 20.0,
        extreme_overbought: float = 80.0,
    ):
        self._period = period
        self._oversold = oversold
        self._overbought = overbought
        self._extreme_oversold = extreme_oversold
        self._extreme_overbought = extreme_overbought

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        if len(df) < self.min_candles_required:
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.0,
                strategy_name=self.name,
                reason="Insufficient data for RSI analysis",
            )

        rsi_col = f"rsi_{self._period}"
        if rsi_col not in df.columns:
            import pandas_ta as ta
            df[rsi_col] = ta.rsi(df["close"], length=self._period)

        current_rsi = df[rsi_col].iloc[-1]
        prev_rsi = df[rsi_col].iloc[-2]

        if pd.isna(current_rsi):
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.0,
                strategy_name=self.name,
                reason="RSI value not available",
            )

        indicators = {
            "rsi": round(current_rsi, 2),
            "prev_rsi": round(prev_rsi, 2) if not pd.isna(prev_rsi) else None,
            "oversold": self._oversold,
            "overbought": self._overbought,
            "current_price": ticker.last,
        }

        # RSI divergence detection
        rsi_rising = current_rsi > prev_rsi if not pd.isna(prev_rsi) else False

        # Extreme oversold
        if current_rsi <= self._extreme_oversold:
            return Signal(
                signal_type=SignalType.BUY,
                confidence=0.85,
                strategy_name=self.name,
                reason=f"극심한 과매도: RSI={current_rsi:.1f} (임계값: {self._extreme_oversold}). "
                f"반등 가능성 높음",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # Oversold
        if current_rsi <= self._oversold:
            confidence = 0.5 + (self._oversold - current_rsi) / self._oversold * 0.3
            if rsi_rising:
                confidence += 0.1  # RSI turning up from oversold is stronger signal
            return Signal(
                signal_type=SignalType.BUY,
                confidence=round(min(confidence, 0.9), 2),
                strategy_name=self.name,
                reason=f"과매도 구간: RSI={current_rsi:.1f} < {self._oversold}. "
                f"{'RSI 반등 시작' if rsi_rising else 'RSI 계속 하락 중'}",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # Extreme overbought
        if current_rsi >= self._extreme_overbought:
            return Signal(
                signal_type=SignalType.SELL,
                confidence=0.85,
                strategy_name=self.name,
                reason=f"극심한 과매수: RSI={current_rsi:.1f} (임계값: {self._extreme_overbought}). "
                f"조정 가능성 높음",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # Overbought
        if current_rsi >= self._overbought:
            confidence = 0.5 + (current_rsi - self._overbought) / (100 - self._overbought) * 0.3
            if not rsi_rising:
                confidence += 0.1  # RSI turning down from overbought
            return Signal(
                signal_type=SignalType.SELL,
                confidence=round(min(confidence, 0.9), 2),
                strategy_name=self.name,
                reason=f"과매수 구간: RSI={current_rsi:.1f} > {self._overbought}. "
                f"{'RSI 하락 시작' if not rsi_rising else 'RSI 계속 상승 중'}",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # Neutral zone
        return Signal(
            signal_type=SignalType.HOLD,
            confidence=0.3,
            strategy_name=self.name,
            reason=f"RSI 중립 구간: RSI={current_rsi:.1f} "
            f"(과매도={self._oversold}, 과매수={self._overbought})",
            indicators=indicators,
        )

    def get_params(self) -> dict:
        return {
            "period": self._period,
            "oversold": self._oversold,
            "overbought": self._overbought,
            "extreme_oversold": self._extreme_oversold,
            "extreme_overbought": self._extreme_overbought,
        }

    def set_params(self, params: dict) -> None:
        for key in ["period", "oversold", "overbought", "extreme_oversold", "extreme_overbought"]:
            if key in params:
                setattr(self, f"_{key}", params[key])
