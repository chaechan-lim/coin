import pandas as pd
import pandas_ta as ta
from exchange.data_models import Ticker
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from core.enums import SignalType


@StrategyRegistry.register
class StochasticRSIStrategy(BaseStrategy):
    """
    Stochastic RSI: RSI에 Stochastic 공식을 적용한 모멘텀 오실레이터.
    일반 RSI보다 민감하여 초기 반전 포착에 유리.

    - K선이 D선을 상향 돌파 + 과매도 구간: BUY
    - K선이 D선을 하향 돌파 + 과매수 구간: SELL
    """

    name = "stochastic_rsi"
    display_name = "Stochastic RSI"
    applicable_market_types = ["all"]
    default_coins = ["BTC/KRW", "ETH/KRW", "XRP/KRW", "SOL/KRW", "ADA/KRW"]
    required_timeframe = "4h"
    min_candles_required = 30

    def __init__(
        self,
        rsi_length: int = 14,
        stoch_length: int = 14,
        k_smooth: int = 3,
        d_smooth: int = 3,
        oversold: float = 20.0,
        overbought: float = 80.0,
    ):
        self._rsi_length = rsi_length
        self._stoch_length = stoch_length
        self._k_smooth = k_smooth
        self._d_smooth = d_smooth
        self._oversold = oversold
        self._overbought = overbought

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        if len(df) < self.min_candles_required:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="데이터 부족",
            )

        # Stochastic RSI 계산
        stochrsi = ta.stochrsi(
            df["close"],
            length=self._stoch_length,
            rsi_length=self._rsi_length,
            k=self._k_smooth,
            d=self._d_smooth,
        )
        if stochrsi is None or stochrsi.empty:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="StochRSI 계산 불가",
            )

        # pandas_ta StochRSI 컬럼명: STOCHRSIk_14_14_3_3, STOCHRSId_14_14_3_3
        k_col = stochrsi.columns[0]  # K line
        d_col = stochrsi.columns[1]  # D line

        k_now = stochrsi[k_col].iloc[-1]
        d_now = stochrsi[d_col].iloc[-1]
        k_prev = stochrsi[k_col].iloc[-2]
        d_prev = stochrsi[d_col].iloc[-2]

        if any(pd.isna(v) for v in [k_now, d_now, k_prev, d_prev]):
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="StochRSI 값 없음",
            )

        indicators = {
            "stochrsi_k": round(k_now, 2),
            "stochrsi_d": round(d_now, 2),
            "prev_k": round(k_prev, 2),
            "prev_d": round(d_prev, 2),
            "current_price": ticker.last,
        }

        # 추세 확인: SMA50 기반 — 하락추세에서 매수 신뢰도 할인
        sma50_col = next((c for c in df.columns if c.lower() in ("sma_50", "sma50")), None)
        if sma50_col is None:
            df["_sma50"] = ta.sma(df["close"], length=50)
            sma50_col = "_sma50"
        sma50_val = df[sma50_col].iloc[-1] if sma50_col in df.columns else None
        _price_below_sma50 = (
            sma50_val is not None and not pd.isna(sma50_val)
            and ticker.last < sma50_val
        )

        # 골든크로스: K가 D를 상향 돌파
        bullish_cross = k_prev <= d_prev and k_now > d_now
        # 데드크로스: K가 D를 하향 돌파
        bearish_cross = k_prev >= d_prev and k_now < d_now

        # 과매도 구간에서 골든크로스 → 강한 BUY
        if bullish_cross and d_now < self._oversold:
            confidence = 0.75 + (self._oversold - d_now) / self._oversold * 0.15
            # 가격이 SMA50 아래면 하락추세 → 신뢰도 할인
            if _price_below_sma50:
                confidence *= 0.6
            return Signal(
                signal_type=SignalType.BUY,
                confidence=round(min(confidence, 0.9), 2),
                strategy_name=self.name,
                reason=f"StochRSI 골든크로스 (과매도): K={k_now:.1f} > D={d_now:.1f}, 구간 < {self._oversold}"
                f"{' [추세할인]' if _price_below_sma50 else ''}",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # K가 과매도 구간에서 상승 중 — HOLD (크로스 없이는 노이즈)
        if k_now < self._oversold and k_now > k_prev:
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.35,
                strategy_name=self.name,
                reason=f"StochRSI 과매도 반등 대기: K={k_now:.1f} ↑, D={d_now:.1f} (크로스 필요)",
                indicators=indicators,
            )

        # 과매수 구간에서 데드크로스 → 강한 SELL
        if bearish_cross and d_now > self._overbought:
            confidence = 0.75 + (d_now - self._overbought) / (100 - self._overbought) * 0.15
            # 하락추세에서 숏 부스트
            if _price_below_sma50:
                confidence *= 1.15
            return Signal(
                signal_type=SignalType.SELL,
                confidence=round(min(confidence, 0.95), 2),
                strategy_name=self.name,
                reason=f"StochRSI 데드크로스 (과매수): K={k_now:.1f} < D={d_now:.1f}, 구간 > {self._overbought}"
                f"{' [추세부스트]' if _price_below_sma50 else ''}",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # K가 과매수 구간에서 하락 중 — HOLD (크로스 없이는 노이즈)
        if k_now > self._overbought and k_now < k_prev:
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.35,
                strategy_name=self.name,
                reason=f"StochRSI 과매수 하락 대기: K={k_now:.1f} ↓, D={d_now:.1f} (크로스 필요)",
                indicators=indicators,
            )

        return Signal(
            signal_type=SignalType.HOLD, confidence=0.3,
            strategy_name=self.name,
            reason=f"StochRSI 중립: K={k_now:.1f}, D={d_now:.1f}",
            indicators=indicators,
        )

    def get_params(self) -> dict:
        return {
            "rsi_length": self._rsi_length,
            "stoch_length": self._stoch_length,
            "k_smooth": self._k_smooth,
            "d_smooth": self._d_smooth,
            "oversold": self._oversold,
            "overbought": self._overbought,
        }

    def set_params(self, params: dict) -> None:
        for key in ["rsi_length", "stoch_length", "k_smooth", "d_smooth", "oversold", "overbought"]:
            if key in params:
                setattr(self, f"_{key}", params[key])
