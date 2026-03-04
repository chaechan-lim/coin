import pandas as pd
import pandas_ta as ta
from exchange.data_models import Ticker
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from core.enums import SignalType


@StrategyRegistry.register
class BNFDeviationStrategy(BaseStrategy):
    """
    BNF 이격도 전략 — 평균 회귀.

    가격이 SMA25에서 크게 이탈하면 복귀를 베팅한다.
    - BUY: deviation < -10% (과매도 이탈)
    - SELL: deviation > +5% (과매수) 또는 SMA 복귀 완료
    - HOLD: -5% ~ +5% 사이 (중립 구간)
    """

    name = "bnf_deviation"
    display_name = "BNF 이격도 (평균 회귀)"
    applicable_market_types = ["all"]
    default_coins = ["BTC/KRW", "ETH/KRW", "XRP/KRW", "SOL/KRW", "ADA/KRW"]
    required_timeframe = "4h"
    min_candles_required = 30

    def __init__(
        self,
        sma_period: int = 25,
        buy_deviation: float = -10.0,
        sell_deviation: float = 5.0,
        rsi_period: int = 14,
        rsi_boost_threshold: float = 40.0,
    ):
        self._sma_period = sma_period
        self._buy_deviation = buy_deviation
        self._sell_deviation = sell_deviation
        self._rsi_period = rsi_period
        self._rsi_boost_threshold = rsi_boost_threshold

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        if len(df) < self.min_candles_required:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="데이터 부족",
            )

        # SMA 계산
        sma_col = f"sma_{self._sma_period}"
        if sma_col not in df.columns:
            sma_upper = f"SMA_{self._sma_period}"
            if sma_upper in df.columns:
                sma_col = sma_upper
            else:
                df[sma_col] = ta.sma(df["close"], length=self._sma_period)

        current_price = df["close"].iloc[-1]
        current_sma = df[sma_col].iloc[-1]

        if pd.isna(current_sma) or current_sma == 0:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="SMA 값 없음",
            )

        deviation = (current_price - current_sma) / current_sma * 100

        # RSI (보조 확인)
        rsi_col = f"rsi_{self._rsi_period}"
        if rsi_col not in df.columns:
            rsi_upper = f"RSI_{self._rsi_period}"
            if rsi_upper in df.columns:
                rsi_col = rsi_upper
            else:
                df[rsi_col] = ta.rsi(df["close"], length=self._rsi_period)
        current_rsi = df[rsi_col].iloc[-1] if rsi_col in df.columns else None

        indicators = {
            "deviation_pct": round(deviation, 2),
            "sma": round(float(current_sma), 2),
            "rsi": round(float(current_rsi), 2) if current_rsi is not None and not pd.isna(current_rsi) else None,
            "current_price": ticker.last,
        }

        # ── BUY: 이격도 < -10% (과매도 이탈) ──
        if deviation <= self._buy_deviation:
            # 이격도 깊이에 따른 confidence 스케일링
            if deviation <= -20:
                confidence = 0.85
            elif deviation <= -15:
                confidence = 0.70
            else:
                confidence = 0.50

            # RSI 과매도 보너스
            if current_rsi is not None and not pd.isna(current_rsi) and current_rsi < self._rsi_boost_threshold:
                confidence += 0.10

            return Signal(
                signal_type=SignalType.BUY,
                confidence=round(min(confidence, 0.95), 2),
                strategy_name=self.name,
                reason=f"이격도 과매도: SMA{self._sma_period} 대비 {deviation:+.1f}%"
                f"{f', RSI={current_rsi:.0f}' if current_rsi is not None and not pd.isna(current_rsi) else ''}",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # ── SELL: 이격도 > +5% (과매수) ──
        if deviation >= self._sell_deviation:
            if deviation >= 15:
                confidence = 0.80
            elif deviation >= 10:
                confidence = 0.65
            else:
                confidence = 0.50

            return Signal(
                signal_type=SignalType.SELL,
                confidence=round(min(confidence, 0.95), 2),
                strategy_name=self.name,
                reason=f"이격도 과매수: SMA{self._sma_period} 대비 {deviation:+.1f}%",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # ── HOLD: 중립 구간 ──
        return Signal(
            signal_type=SignalType.HOLD, confidence=0.25,
            strategy_name=self.name,
            reason=f"이격도 중립: SMA{self._sma_period} 대비 {deviation:+.1f}%",
            indicators=indicators,
        )

    def get_params(self) -> dict:
        return {
            "sma_period": self._sma_period,
            "buy_deviation": self._buy_deviation,
            "sell_deviation": self._sell_deviation,
            "rsi_period": self._rsi_period,
            "rsi_boost_threshold": self._rsi_boost_threshold,
        }

    def set_params(self, params: dict) -> None:
        for key in ["sma_period", "buy_deviation", "sell_deviation", "rsi_period", "rsi_boost_threshold"]:
            if key in params:
                setattr(self, f"_{key}", params[key])
