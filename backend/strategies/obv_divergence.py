import pandas as pd
import pandas_ta as ta
from exchange.data_models import Ticker
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from core.enums import SignalType


@StrategyRegistry.register
class OBVDivergenceStrategy(BaseStrategy):
    """
    OBV(On Balance Volume) Divergence: 거래량-가격 괴리 감지.

    - 가격 하락 + OBV 상승 (강세 다이버전스) → BUY
    - 가격 상승 + OBV 하락 (약세 다이버전스) → SELL
    - OBV 추세 + 가격 추세 일치 시 추세 확인 시그널
    """

    name = "obv_divergence"
    display_name = "OBV 다이버전스"
    applicable_market_types = ["all"]
    default_coins = ["BTC/KRW", "ETH/KRW", "XRP/KRW", "SOL/KRW", "ADA/KRW"]
    required_timeframe = "4h"
    min_candles_required = 30

    def __init__(self, lookback: int = 10, sma_length: int = 20):
        self._lookback = lookback
        self._sma_length = sma_length

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        if len(df) < max(self.min_candles_required, self._sma_length + self._lookback):
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="데이터 부족",
            )

        # OBV 계산
        obv = ta.obv(df["close"], df["volume"])
        if obv is None or obv.empty:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="OBV 계산 불가",
            )

        obv_sma = ta.sma(obv, length=self._sma_length)

        current_obv = obv.iloc[-1]
        prev_obv = obv.iloc[-self._lookback]
        current_obv_sma = obv_sma.iloc[-1] if obv_sma is not None and not obv_sma.empty else None

        if pd.isna(current_obv) or pd.isna(prev_obv):
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="OBV 값 없음",
            )

        # 가격 변화
        current_price = df["close"].iloc[-1]
        prev_price = df["close"].iloc[-self._lookback]
        if pd.isna(prev_price) or pd.isna(current_price) or prev_price <= 0:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="가격 데이터 없음",
            )
        price_change_pct = (current_price - prev_price) / prev_price * 100

        # OBV 변화 방향
        obv_change = current_obv - prev_obv
        obv_rising = obv_change > 0
        obv_above_sma = (current_obv > current_obv_sma) if current_obv_sma is not None and not pd.isna(current_obv_sma) else None

        indicators = {
            "obv": round(float(current_obv), 0),
            "obv_sma": round(float(current_obv_sma), 0) if current_obv_sma is not None and not pd.isna(current_obv_sma) else None,
            "obv_rising": obv_rising,
            "price_change_pct": round(price_change_pct, 2),
            "current_price": ticker.last,
        }

        # ── 강세 다이버전스: 가격 하락 + OBV 상승 ──
        if price_change_pct < -1.0 and obv_rising:
            strength = min(abs(price_change_pct) / 5.0, 1.0)  # 괴리 폭에 비례
            confidence = 0.55 + strength * 0.25
            if obv_above_sma:
                confidence += 0.05
            return Signal(
                signal_type=SignalType.BUY,
                confidence=round(min(confidence, 0.85), 2),
                strategy_name=self.name,
                reason=f"강세 다이버전스: 가격 {price_change_pct:+.1f}% but OBV 상승"
                f"{' (SMA 위)' if obv_above_sma else ''}",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # 추세 확인: SMA50 기반
        sma50_col = next((c for c in df.columns if c.lower() in ("sma_50", "sma50")), None)
        if sma50_col is None:
            df["_sma50"] = ta.sma(df["close"], length=50)
            sma50_col = "_sma50"
        sma50_val = df[sma50_col].iloc[-1] if sma50_col in df.columns else None
        _in_downtrend = (
            sma50_val is not None and not pd.isna(sma50_val)
            and ticker.last < sma50_val
        )

        # ── 약세 다이버전스: 가격 상승 + OBV 하락 ──
        if price_change_pct > 1.0 and not obv_rising:
            strength = min(price_change_pct / 5.0, 1.0)
            confidence = 0.55 + strength * 0.25
            if obv_above_sma is False:
                confidence += 0.05
            # 하락추세에서 숏 부스트
            if _in_downtrend:
                confidence *= 1.15
            return Signal(
                signal_type=SignalType.SELL,
                confidence=round(min(confidence, 0.90), 2),
                strategy_name=self.name,
                reason=f"약세 다이버전스: 가격 {price_change_pct:+.1f}% but OBV 하락"
                f"{' (SMA 아래)' if obv_above_sma is False else ''}"
                f"{' [추세부스트]' if _in_downtrend else ''}",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # ── 추세 확인: 가격 & OBV 방향 일치 + SMA 확인 ──
        if price_change_pct > 0.5 and obv_rising and obv_above_sma:
            return Signal(
                signal_type=SignalType.BUY,
                confidence=0.4,
                strategy_name=self.name,
                reason=f"OBV 추세 확인 (상승): 가격 {price_change_pct:+.1f}%, OBV↑ SMA 위",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        if price_change_pct < -0.5 and not obv_rising and obv_above_sma is False:
            confidence = 0.4
            # 하락추세에서 숏 부스트
            if _in_downtrend:
                confidence = min(confidence * 1.15, 0.55)
            return Signal(
                signal_type=SignalType.SELL,
                confidence=round(confidence, 2),
                strategy_name=self.name,
                reason=f"OBV 추세 확인 (하락): 가격 {price_change_pct:+.1f}%, OBV↓ SMA 아래"
                f"{' [추세부스트]' if _in_downtrend else ''}",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        return Signal(
            signal_type=SignalType.HOLD, confidence=0.25,
            strategy_name=self.name,
            reason=f"OBV 시그널 없음: 가격 {price_change_pct:+.1f}%, OBV {'↑' if obv_rising else '↓'}",
            indicators=indicators,
        )

    def get_params(self) -> dict:
        return {"lookback": self._lookback, "sma_length": self._sma_length}

    def set_params(self, params: dict) -> None:
        for key in ["lookback", "sma_length"]:
            if key in params:
                setattr(self, f"_{key}", params[key])
