import pandas as pd
from exchange.data_models import Ticker
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from core.enums import SignalType


@StrategyRegistry.register
class MACDCrossoverStrategy(BaseStrategy):
    """
    Strategy 4: MACD Crossover
    Buy when MACD line crosses above signal line with positive histogram.
    """

    name = "macd_crossover"
    display_name = "MACD 크로스오버"
    applicable_market_types = ["trending"]
    default_coins = ["BTC/KRW", "ETH/KRW", "SOL/KRW"]
    required_timeframe = "1h"
    min_candles_required = 35

    def __init__(self, fast: int = 12, slow: int = 26, signal_period: int = 9):
        self._fast = fast
        self._slow = slow
        self._signal_period = signal_period

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        if len(df) < self.min_candles_required:
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.0,
                strategy_name=self.name,
                reason="Insufficient data for MACD analysis",
            )

        # Look for MACD columns (pandas_ta naming convention)
        macd_col = f"MACD_{self._fast}_{self._slow}_{self._signal_period}"
        signal_col = f"MACDs_{self._fast}_{self._slow}_{self._signal_period}"
        hist_col = f"MACDh_{self._fast}_{self._slow}_{self._signal_period}"

        # Fallback computation if columns missing
        if macd_col not in df.columns:
            import pandas_ta as ta
            macd_result = ta.macd(df["close"], fast=self._fast, slow=self._slow, signal=self._signal_period)
            if macd_result is not None:
                df = pd.concat([df, macd_result], axis=1)

        if macd_col not in df.columns:
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.0,
                strategy_name=self.name,
                reason="Unable to compute MACD indicators",
            )

        current_macd = df[macd_col].iloc[-1]
        current_signal = df[signal_col].iloc[-1]
        current_hist = df[hist_col].iloc[-1]
        prev_macd = df[macd_col].iloc[-2]
        prev_signal = df[signal_col].iloc[-2]
        prev_hist = df[hist_col].iloc[-2]

        if pd.isna(current_macd) or pd.isna(current_signal):
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.0,
                strategy_name=self.name,
                reason="MACD values not available",
            )

        indicators = {
            "macd": round(current_macd, 2),
            "signal": round(current_signal, 2),
            "histogram": round(current_hist, 2),
            "prev_histogram": round(prev_hist, 2) if not pd.isna(prev_hist) else None,
            "current_price": ticker.last,
        }

        # Bullish crossover: MACD crosses above signal
        bullish_cross = prev_macd <= prev_signal and current_macd > current_signal
        # Bearish crossover: MACD crosses below signal
        bearish_cross = prev_macd >= prev_signal and current_macd < current_signal

        # Histogram momentum
        hist_increasing = current_hist > prev_hist if not pd.isna(prev_hist) else False

        if bullish_cross and current_hist > 0:
            confidence = 0.65
            if hist_increasing:
                confidence += 0.15
            return Signal(
                signal_type=SignalType.BUY,
                confidence=round(min(confidence, 0.95), 2),
                strategy_name=self.name,
                reason=f"MACD 골든크로스: MACD({current_macd:.2f}) > 시그널({current_signal:.2f}), "
                f"히스토그램 양수({current_hist:.2f}), "
                f"{'모멘텀 증가' if hist_increasing else '모멘텀 유지'}",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        if bearish_cross and current_hist < 0:
            confidence = 0.65
            if not hist_increasing:
                confidence += 0.15
            return Signal(
                signal_type=SignalType.SELL,
                confidence=round(min(confidence, 0.95), 2),
                strategy_name=self.name,
                reason=f"MACD 데드크로스: MACD({current_macd:.2f}) < 시그널({current_signal:.2f}), "
                f"히스토그램 음수({current_hist:.2f})",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # 추세 지속 소프트 신호: 히스토그램 방향 기반
        hist_decreasing = current_hist < prev_hist if not pd.isna(prev_hist) else False

        if current_macd > current_signal and hist_increasing:
            # MACD > Signal + 히스토그램 증가: 소프트 BUY
            return Signal(
                signal_type=SignalType.BUY,
                confidence=0.4,
                strategy_name=self.name,
                reason=f"상승 모멘텀 지속: MACD > 시그널, "
                f"히스토그램 증가 ({prev_hist:.2f} → {current_hist:.2f})",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        if current_macd < current_signal and hist_decreasing:
            # MACD < Signal + 히스토그램 감소: 소프트 SELL
            return Signal(
                signal_type=SignalType.SELL,
                confidence=0.4,
                strategy_name=self.name,
                reason=f"하락 모멘텀 지속: MACD < 시그널, "
                f"히스토그램 감소 ({prev_hist:.2f} → {current_hist:.2f})",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        return Signal(
            signal_type=SignalType.HOLD,
            confidence=0.3,
            strategy_name=self.name,
            reason=f"MACD 방향 불명확: MACD={current_macd:.2f}, 시그널={current_signal:.2f}",
            indicators=indicators,
        )

    def get_params(self) -> dict:
        return {
            "fast": self._fast,
            "slow": self._slow,
            "signal_period": self._signal_period,
        }

    def set_params(self, params: dict) -> None:
        for key in ["fast", "slow", "signal_period"]:
            if key in params:
                setattr(self, f"_{key}", params[key])
