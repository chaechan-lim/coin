import pandas as pd
import pandas_ta as ta
from exchange.data_models import Ticker
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from core.enums import SignalType


@StrategyRegistry.register
class LarryWilliamsStrategy(BaseStrategy):
    """
    래리 윌리엄스 변동성 돌파 + Williams %R.

    시가 + k × 전일변동폭 돌파 + Williams %R 확인.
    - BUY: close > open + k*(prev_high - prev_low) AND %R 과매도 탈출 AND close > SMA20
    - SELL: close < open - k*(prev_high - prev_low) AND %R 과매수 진입
    """

    name = "larry_williams"
    display_name = "래리 윌리엄스 (변동성 돌파)"
    applicable_market_types = ["trending"]
    default_coins = ["BTC/KRW", "ETH/KRW", "XRP/KRW", "SOL/KRW", "ADA/KRW"]
    required_timeframe = "4h"
    min_candles_required = 20

    def __init__(
        self,
        k: float = 0.5,
        willr_period: int = 14,
        willr_oversold: float = -80.0,
        willr_overbought: float = -20.0,
        sma_period: int = 20,
    ):
        self._k = k
        self._willr_period = willr_period
        self._willr_oversold = willr_oversold
        self._willr_overbought = willr_overbought
        self._sma_period = sma_period

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        if len(df) < self.min_candles_required:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="데이터 부족",
            )

        # 현재 캔들 & 전일 캔들
        current = df.iloc[-1]
        prev = df.iloc[-2]

        current_close = current["close"]
        current_open = current["open"]
        prev_high = prev["high"]
        prev_low = prev["low"]
        prev_range = prev_high - prev_low

        if prev_range <= 0:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="전일 변동폭 없음",
            )

        # 돌파 기준선
        breakout_up = current_open + self._k * prev_range
        breakout_down = current_open - self._k * prev_range

        # Williams %R 계산
        willr_col = f"WILLR_{self._willr_period}"
        if willr_col not in df.columns:
            willr_lower = f"willr_{self._willr_period}"
            if willr_lower in df.columns:
                willr_col = willr_lower
            else:
                highest = df["high"].rolling(self._willr_period).max()
                lowest = df["low"].rolling(self._willr_period).min()
                denom = highest - lowest
                df[willr_col] = ((highest - df["close"]) / denom.replace(0, float("nan"))) * -100

        current_willr = df[willr_col].iloc[-1]
        prev_willr = df[willr_col].iloc[-2] if len(df) > 2 else None

        if pd.isna(current_willr):
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="Williams %R 값 없음",
            )

        # SMA 트렌드 필터
        sma_col = f"sma_{self._sma_period}"
        if sma_col not in df.columns:
            sma_upper = f"SMA_{self._sma_period}"
            if sma_upper in df.columns:
                sma_col = sma_upper
            else:
                df[sma_col] = ta.sma(df["close"], length=self._sma_period)
        current_sma = df[sma_col].iloc[-1]
        above_sma = current_close > current_sma if not pd.isna(current_sma) else True

        indicators = {
            "breakout_up": round(float(breakout_up), 2),
            "breakout_down": round(float(breakout_down), 2),
            "williams_r": round(float(current_willr), 2),
            "prev_range": round(float(prev_range), 2),
            "above_sma": above_sma,
            "current_price": ticker.last,
        }

        # ── BUY: 상향 돌파 + %R 과매도 탈출 + SMA 위 ──
        willr_oversold_exit = current_willr > self._willr_oversold
        if prev_willr is not None and not pd.isna(prev_willr):
            willr_oversold_exit = prev_willr <= self._willr_oversold and current_willr > self._willr_oversold

        if current_close > breakout_up and willr_oversold_exit and above_sma:
            # 돌파 강도
            breakout_strength = (current_close - breakout_up) / prev_range
            confidence = 0.55 + min(breakout_strength * 0.3, 0.30)

            # %R이 깊은 과매도에서 탈출할수록 강함
            if current_willr < -60:
                confidence += 0.05

            return Signal(
                signal_type=SignalType.BUY,
                confidence=round(min(confidence, 0.95), 2),
                strategy_name=self.name,
                reason=f"변동성 돌파 매수: 돌파선={breakout_up:.0f}, %R={current_willr:.0f}",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # ── SELL: 하향 돌파 + %R 과매수 진입 ──
        if current_close < breakout_down and current_willr > self._willr_overbought:
            breakout_strength = (breakout_down - current_close) / prev_range
            confidence = 0.55 + min(breakout_strength * 0.3, 0.30)

            return Signal(
                signal_type=SignalType.SELL,
                confidence=round(min(confidence, 0.95), 2),
                strategy_name=self.name,
                reason=f"변동성 돌파 매도: 돌파선={breakout_down:.0f}, %R={current_willr:.0f}",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # ── HOLD ──
        return Signal(
            signal_type=SignalType.HOLD, confidence=0.25,
            strategy_name=self.name,
            reason=f"돌파 없음: 가격={current_close:.0f}, %R={current_willr:.0f}",
            indicators=indicators,
        )

    def get_params(self) -> dict:
        return {
            "k": self._k,
            "willr_period": self._willr_period,
            "willr_oversold": self._willr_oversold,
            "willr_overbought": self._willr_overbought,
            "sma_period": self._sma_period,
        }

    def set_params(self, params: dict) -> None:
        for key in ["k", "willr_period", "willr_oversold", "willr_overbought", "sma_period"]:
            if key in params:
                setattr(self, f"_{key}", params[key])
