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
    required_timeframe = "4h"
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

        # Freefall guard: 최근 20캔들 고점 대비 30%+ 하락 시 매수 차단
        lookback = min(20, len(df))
        recent_high = df["high"].iloc[-lookback:].max()
        if recent_high > 0:
            drop_from_high = (ticker.last - recent_high) / recent_high
            if drop_from_high < -0.30 and current_rsi <= self._oversold:
                return Signal(
                    signal_type=SignalType.HOLD,
                    confidence=0.2,
                    strategy_name=self.name,
                    reason=f"급락 방어: 고점 대비 {drop_from_high*100:.1f}% 하락 (RSI={current_rsi:.1f})",
                    indicators=indicators,
                )

        # Trend check: SMA20 vs SMA50 — 하락 추세 여부
        import pandas_ta as ta
        sma20_col = next((c for c in df.columns if c.lower() in ("sma_20", "sma20")), None)
        sma50_col = next((c for c in df.columns if c.lower() in ("sma_50", "sma50")), None)
        if sma20_col is None:
            df["_sma20"] = ta.sma(df["close"], length=20)
            sma20_col = "_sma20"
        if sma50_col is None:
            df["_sma50"] = ta.sma(df["close"], length=50)
            sma50_col = "_sma50"
        sma20_val = df[sma20_col].iloc[-1] if sma20_col in df.columns else None
        sma50_val = df[sma50_col].iloc[-1] if sma50_col in df.columns else None
        _in_downtrend = (
            sma20_val is not None and sma50_val is not None
            and not pd.isna(sma20_val) and not pd.isna(sma50_val)
            and sma20_val < sma50_val
        )

        # Extreme oversold
        if current_rsi <= self._extreme_oversold:
            confidence = 0.85
            if _in_downtrend and sma50_val > 0:
                sma_gap = (sma50_val - sma20_val) / sma50_val
                if sma_gap > 0.03:  # 갭 3% 이상이면 강한 하락 추세
                    confidence *= 0.5
            return Signal(
                signal_type=SignalType.BUY,
                confidence=round(confidence, 2),
                strategy_name=self.name,
                reason=f"극심한 과매도: RSI={current_rsi:.1f} (임계값: {self._extreme_oversold}). "
                f"반등 가능성 높음{' [역추세 할인]' if _in_downtrend else ''}",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # Oversold
        if current_rsi <= self._oversold:
            confidence = 0.5 + (self._oversold - current_rsi) / self._oversold * 0.3
            if rsi_rising:
                confidence += 0.1  # RSI turning up from oversold is stronger signal
            if _in_downtrend and sma50_val > 0:
                sma_gap = (sma50_val - sma20_val) / sma50_val
                if sma_gap > 0.03:  # 갭 3% 이상이면 강한 하락 추세
                    confidence *= 0.5
            return Signal(
                signal_type=SignalType.BUY,
                confidence=round(min(confidence, 0.9), 2),
                strategy_name=self.name,
                reason=f"과매도 구간: RSI={current_rsi:.1f} < {self._oversold}. "
                f"{'RSI 반등 시작' if rsi_rising else 'RSI 계속 하락 중'}"
                f"{' [역추세 할인]' if _in_downtrend else ''}",
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
