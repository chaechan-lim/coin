import pandas as pd
import pandas_ta as ta
from exchange.data_models import Ticker
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from core.enums import SignalType


@StrategyRegistry.register
class DonchianChannelStrategy(BaseStrategy):
    """
    돈치안 채널 전략 — 터틀 트레이딩.

    20봉 최고/최저 돌파 → 추세 추종.
    - BUY: close > 20봉 최고가 (거래량·ADX 보너스)
    - SELL: close < 20봉 최저가 또는 close < 10봉 최저가 (터틀 청산)
    """

    name = "donchian_channel"
    display_name = "돈치안 채널 (터틀)"
    applicable_market_types = ["trending"]
    default_coins = ["BTC/KRW", "ETH/KRW", "XRP/KRW", "SOL/KRW", "ADA/KRW"]
    required_timeframe = "4h"
    min_candles_required = 25

    def __init__(
        self,
        entry_period: int = 20,
        exit_period: int = 10,
        adx_period: int = 14,
        adx_threshold: float = 25.0,
        volume_ma_period: int = 20,
        volume_multiplier: float = 1.5,
    ):
        self._entry_period = entry_period
        self._exit_period = exit_period
        self._adx_period = adx_period
        self._adx_threshold = adx_threshold
        self._volume_ma_period = volume_ma_period
        self._volume_multiplier = volume_multiplier

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        if len(df) < self.min_candles_required:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="데이터 부족",
            )

        current_close = df["close"].iloc[-1]

        # 돈치안 채널 (현재 캔들 제외 → look-ahead 방지)
        highs_prev = df["high"].iloc[-(self._entry_period + 1):-1]
        lows_prev = df["low"].iloc[-(self._entry_period + 1):-1]
        upper_channel = highs_prev.max()
        lower_channel = lows_prev.min()

        # 청산용 10봉 최저 (현재 캔들 제외)
        exit_lows = df["low"].iloc[-(self._exit_period + 1):-1]
        exit_lower = exit_lows.min()

        if pd.isna(upper_channel) or pd.isna(lower_channel):
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="채널 계산 불가",
            )

        channel_width = (upper_channel - lower_channel) / lower_channel * 100 if lower_channel > 0 else 0

        # 거래량 비율
        vol = df["volume"]
        vol_ma = vol.rolling(self._volume_ma_period).mean().iloc[-1]
        current_vol = vol.iloc[-1]
        volume_ratio = current_vol / vol_ma if vol_ma > 0 and not pd.isna(vol_ma) else 1.0

        # ADX (추세 강도)
        adx_col = f"ADX_{self._adx_period}"
        adx_val = None
        if adx_col in df.columns:
            adx_val = df[adx_col].iloc[-1]
        else:
            adx_lower = f"adx_{self._adx_period}"
            if adx_lower in df.columns:
                adx_val = df[adx_lower].iloc[-1]
            else:
                adx_result = ta.adx(df["high"], df["low"], df["close"], length=self._adx_period)
                if adx_result is not None and not adx_result.empty:
                    adx_col_name = f"ADX_{self._adx_period}"
                    if adx_col_name in adx_result.columns:
                        adx_val = adx_result[adx_col_name].iloc[-1]

        indicators = {
            "upper_channel": round(float(upper_channel), 2),
            "lower_channel": round(float(lower_channel), 2),
            "exit_lower": round(float(exit_lower), 2) if not pd.isna(exit_lower) else None,
            "channel_width_pct": round(channel_width, 2),
            "volume_ratio": round(volume_ratio, 2),
            "adx": round(float(adx_val), 2) if adx_val is not None and not pd.isna(adx_val) else None,
            "current_price": ticker.last,
        }

        # ── BUY: 상단 돌파 ──
        if current_close > upper_channel:
            # 채널 폭이 넓을수록 강한 시그널
            width_bonus = min(channel_width / 20.0, 0.15)
            confidence = 0.55 + width_bonus

            # 거래량 보너스
            if volume_ratio > self._volume_multiplier:
                confidence += 0.15

            # ADX 보너스
            if adx_val is not None and not pd.isna(adx_val) and adx_val > self._adx_threshold:
                confidence += 0.10

            return Signal(
                signal_type=SignalType.BUY,
                confidence=round(min(confidence, 0.95), 2),
                strategy_name=self.name,
                reason=f"돈치안 상단 돌파: {upper_channel:.0f} → {current_close:.0f}"
                f" (폭={channel_width:.1f}%"
                f"{f', ADX={adx_val:.0f}' if adx_val is not None and not pd.isna(adx_val) else ''})",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # ── SELL: 하단 돌파 또는 터틀 청산 (10봉 최저 이탈) ──
        if current_close < lower_channel:
            width_bonus = min(channel_width / 20.0, 0.15)
            confidence = 0.60 + width_bonus

            return Signal(
                signal_type=SignalType.SELL,
                confidence=round(min(confidence, 0.95), 2),
                strategy_name=self.name,
                reason=f"돈치안 하단 돌파: {lower_channel:.0f} → {current_close:.0f}",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        if not pd.isna(exit_lower) and current_close < exit_lower:
            return Signal(
                signal_type=SignalType.SELL,
                confidence=0.50,
                strategy_name=self.name,
                reason=f"터틀 청산: 10봉 최저 {exit_lower:.0f} 이탈",
                suggested_price=ticker.last,
                indicators=indicators,
            )

        # ── HOLD ──
        # 채널 내 위치 표시
        if upper_channel > lower_channel:
            position_pct = (current_close - lower_channel) / (upper_channel - lower_channel) * 100
        else:
            position_pct = 50.0

        return Signal(
            signal_type=SignalType.HOLD, confidence=0.25,
            strategy_name=self.name,
            reason=f"채널 내: 위치 {position_pct:.0f}% (상={upper_channel:.0f}, 하={lower_channel:.0f})",
            indicators=indicators,
        )

    def get_params(self) -> dict:
        return {
            "entry_period": self._entry_period,
            "exit_period": self._exit_period,
            "adx_period": self._adx_period,
            "adx_threshold": self._adx_threshold,
            "volume_ma_period": self._volume_ma_period,
            "volume_multiplier": self._volume_multiplier,
        }

    def set_params(self, params: dict) -> None:
        for key in ["entry_period", "exit_period", "adx_period", "adx_threshold", "volume_ma_period", "volume_multiplier"]:
            if key in params:
                setattr(self, f"_{key}", params[key])
