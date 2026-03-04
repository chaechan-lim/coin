import pandas as pd
from exchange.data_models import Ticker
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from core.enums import SignalType


@StrategyRegistry.register
class CISMomentumStrategy(BaseStrategy):
    """
    CIS 모멘텀 전략 — 순수 모멘텀.

    "오르는 걸 사고, 내리는 걸 판다" — ROC(Rate of Change) 기반.
    - BUY: ROC5 > 2% AND ROC10 > 3% AND 거래량 비율 > 1.2
    - SELL: ROC5 < -2% AND ROC10 < -3% (모멘텀 반전)
    - HOLD: 모멘텀 불명확
    """

    name = "cis_momentum"
    display_name = "CIS 모멘텀 (추세 추종)"
    applicable_market_types = ["trending"]
    default_coins = ["BTC/KRW", "ETH/KRW", "XRP/KRW", "SOL/KRW", "ADA/KRW"]
    required_timeframe = "4h"
    min_candles_required = 25

    def __init__(
        self,
        roc_short: int = 5,
        roc_long: int = 10,
        roc_short_buy: float = 2.0,
        roc_long_buy: float = 3.0,
        roc_short_sell: float = -2.0,
        roc_long_sell: float = -3.0,
        volume_ratio_threshold: float = 1.2,
        volume_ma_period: int = 20,
    ):
        self._roc_short = roc_short
        self._roc_long = roc_long
        self._roc_short_buy = roc_short_buy
        self._roc_long_buy = roc_long_buy
        self._roc_short_sell = roc_short_sell
        self._roc_long_sell = roc_long_sell
        self._volume_ratio_threshold = volume_ratio_threshold
        self._volume_ma_period = volume_ma_period

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        if len(df) < self.min_candles_required:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="데이터 부족",
            )

        close = df["close"]

        # ROC 계산 (Rate of Change %)
        roc5 = (close.iloc[-1] - close.iloc[-1 - self._roc_short]) / close.iloc[-1 - self._roc_short] * 100
        roc10 = (close.iloc[-1] - close.iloc[-1 - self._roc_long]) / close.iloc[-1 - self._roc_long] * 100

        if pd.isna(roc5) or pd.isna(roc10):
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="ROC 값 없음",
            )

        # 거래량 비율 계산
        vol = df["volume"]
        vol_ma = vol.rolling(self._volume_ma_period).mean().iloc[-1]
        current_vol = vol.iloc[-1]
        volume_ratio = current_vol / vol_ma if vol_ma > 0 and not pd.isna(vol_ma) else 1.0

        indicators = {
            "roc5": round(roc5, 2),
            "roc10": round(roc10, 2),
            "volume_ratio": round(volume_ratio, 2),
            "current_price": ticker.last,
        }

        # ── BUY: 강한 상승 모멘텀 + 거래량 확인 ──
        if roc5 > self._roc_short_buy and roc10 > self._roc_long_buy and volume_ratio > self._volume_ratio_threshold:
            # 모멘텀 강도에 따른 confidence
            momentum_strength = min((roc5 + roc10) / 10.0, 1.0)
            confidence = 0.55 + momentum_strength * 0.25

            # 거래량 보너스
            if volume_ratio > 2.0:
                confidence += 0.10

            return Signal(
                signal_type=SignalType.BUY,
                confidence=round(min(confidence, 0.95), 2),
                strategy_name=self.name,
                reason=f"모멘텀 상승: ROC5={roc5:+.1f}%, ROC10={roc10:+.1f}%, Vol={volume_ratio:.1f}x",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # ── SELL: 모멘텀 반전 ──
        if roc5 < self._roc_short_sell and roc10 < self._roc_long_sell:
            momentum_strength = min((abs(roc5) + abs(roc10)) / 10.0, 1.0)
            confidence = 0.55 + momentum_strength * 0.25

            return Signal(
                signal_type=SignalType.SELL,
                confidence=round(min(confidence, 0.95), 2),
                strategy_name=self.name,
                reason=f"모멘텀 반전: ROC5={roc5:+.1f}%, ROC10={roc10:+.1f}%",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # ── HOLD: 모멘텀 불명확 ──
        return Signal(
            signal_type=SignalType.HOLD, confidence=0.25,
            strategy_name=self.name,
            reason=f"모멘텀 불명확: ROC5={roc5:+.1f}%, ROC10={roc10:+.1f}%",
            indicators=indicators,
        )

    def get_params(self) -> dict:
        return {
            "roc_short": self._roc_short,
            "roc_long": self._roc_long,
            "roc_short_buy": self._roc_short_buy,
            "roc_long_buy": self._roc_long_buy,
            "roc_short_sell": self._roc_short_sell,
            "roc_long_sell": self._roc_long_sell,
            "volume_ratio_threshold": self._volume_ratio_threshold,
            "volume_ma_period": self._volume_ma_period,
        }

    def set_params(self, params: dict) -> None:
        for key in [
            "roc_short", "roc_long", "roc_short_buy", "roc_long_buy",
            "roc_short_sell", "roc_long_sell", "volume_ratio_threshold", "volume_ma_period",
        ]:
            if key in params:
                setattr(self, f"_{key}", params[key])
