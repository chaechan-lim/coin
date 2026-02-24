import pandas as pd
import pandas_ta as ta
from exchange.data_models import Ticker
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from core.enums import SignalType


@StrategyRegistry.register
class SupertrendStrategy(BaseStrategy):
    """
    Supertrend: ATR 기반 추세 추종 지표.

    - Supertrend 방향 전환 시 강한 시그널
    - 추세 지속 + 가격이 Supertrend에 가까울 때 소프트 시그널
    - 추세 장에서 높은 성과, 횡보장에서는 HOLD 위주
    """

    name = "supertrend"
    display_name = "슈퍼트렌드"
    applicable_market_types = ["trending"]
    default_coins = ["BTC/KRW", "ETH/KRW", "XRP/KRW", "SOL/KRW", "ADA/KRW"]
    required_timeframe = "1h"
    min_candles_required = 20

    def __init__(self, length: int = 10, multiplier: float = 3.0):
        self._length = length
        self._multiplier = multiplier

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        if len(df) < self.min_candles_required:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="데이터 부족",
            )

        # Supertrend 계산
        st = ta.supertrend(
            df["high"], df["low"], df["close"],
            length=self._length, multiplier=self._multiplier,
        )
        if st is None or st.empty:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="Supertrend 계산 불가",
            )

        # pandas_ta supertrend 컬럼: SUPERT_{length}_{mult}, SUPERTd_{length}_{mult}, ...
        # SUPERTd = direction: 1 = uptrend, -1 = downtrend
        dir_col = [c for c in st.columns if c.startswith("SUPERTd_")]
        val_col = [c for c in st.columns if c.startswith("SUPERT_") and "SUPERTd" not in c and "SUPERTl" not in c and "SUPERTs" not in c]

        if not dir_col or not val_col:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="Supertrend 컬럼 없음",
            )

        dir_col = dir_col[0]
        val_col = val_col[0]

        current_dir = st[dir_col].iloc[-1]
        prev_dir = st[dir_col].iloc[-2]
        current_st_val = st[val_col].iloc[-1]
        current_price = df["close"].iloc[-1]

        if pd.isna(current_dir) or pd.isna(prev_dir):
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="Supertrend 값 없음",
            )

        current_dir = int(current_dir)
        prev_dir = int(prev_dir)

        # 가격과 Supertrend 라인의 거리 (%)
        distance_pct = abs(current_price - current_st_val) / current_price * 100

        indicators = {
            "supertrend_dir": current_dir,
            "supertrend_val": round(float(current_st_val), 0),
            "distance_pct": round(distance_pct, 2),
            "current_price": ticker.last,
        }

        # ── 방향 전환: 가장 강한 시그널 ──

        # 하락 → 상승 전환
        if prev_dir == -1 and current_dir == 1:
            confidence = 0.75
            if distance_pct < 2.0:
                confidence += 0.1  # 가격이 ST 라인에 가까울수록 강함
            return Signal(
                signal_type=SignalType.BUY,
                confidence=round(min(confidence, 0.9), 2),
                strategy_name=self.name,
                reason=f"Supertrend 상승 전환: 가격 {current_price:.0f} > ST {current_st_val:.0f}",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # 상승 → 하락 전환
        if prev_dir == 1 and current_dir == -1:
            confidence = 0.75
            if distance_pct < 2.0:
                confidence += 0.1
            return Signal(
                signal_type=SignalType.SELL,
                confidence=round(min(confidence, 0.9), 2),
                strategy_name=self.name,
                reason=f"Supertrend 하락 전환: 가격 {current_price:.0f} < ST {current_st_val:.0f}",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # ── 추세 지속 소프트 시그널 ──

        # 상승 추세 지속 + 가격이 ST 라인 근접 (바운스 가능)
        if current_dir == 1 and distance_pct < 1.5:
            return Signal(
                signal_type=SignalType.BUY,
                confidence=0.45,
                strategy_name=self.name,
                reason=f"Supertrend 상승 지속, ST 라인 근접 ({distance_pct:.1f}%): 바운스 가능",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # 하락 추세 지속 + 가격이 ST 라인 근접
        if current_dir == -1 and distance_pct < 1.5:
            return Signal(
                signal_type=SignalType.SELL,
                confidence=0.45,
                strategy_name=self.name,
                reason=f"Supertrend 하락 지속, ST 라인 근접 ({distance_pct:.1f}%): 저항 가능",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # 추세 지속이지만 라인에서 먼 경우
        if current_dir == 1:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.35,
                strategy_name=self.name,
                reason=f"Supertrend 상승 추세 유지 (거리 {distance_pct:.1f}%)",
                indicators=indicators,
            )

        return Signal(
            signal_type=SignalType.HOLD, confidence=0.35,
            strategy_name=self.name,
            reason=f"Supertrend 하락 추세 유지 (거리 {distance_pct:.1f}%)",
            indicators=indicators,
        )

    def get_params(self) -> dict:
        return {"length": self._length, "multiplier": self._multiplier}

    def set_params(self, params: dict) -> None:
        for key in ["length", "multiplier"]:
            if key in params:
                setattr(self, f"_{key}", params[key])
