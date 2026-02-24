import pandas as pd
from exchange.data_models import Ticker
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from core.enums import SignalType


@StrategyRegistry.register
class VolatilityBreakoutStrategy(BaseStrategy):
    """
    Strategy 1: Volatility Breakout
    Buy when price breaks above: today_open + (yesterday_high - yesterday_low) * K
    """

    name = "volatility_breakout"
    display_name = "변동성 돌파"
    applicable_market_types = ["trending"]
    default_coins = ["BTC/KRW", "ETH/KRW"]
    required_timeframe = "1d"
    min_candles_required = 2

    def __init__(
        self,
        k_factor: float = 0.5,
        stop_loss_pct: float = 0.03,
        take_profit_pct: float = 0.05,
        volume_confirm_ratio: float = 1.5,
    ):
        self._k_factor = k_factor
        self._stop_loss_pct = stop_loss_pct
        self._take_profit_pct = take_profit_pct
        self._volume_confirm_ratio = volume_confirm_ratio

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        if len(df) < self.min_candles_required:
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.0,
                strategy_name=self.name,
                reason="Insufficient data for volatility breakout analysis",
            )

        # Previous day's range
        prev_day = df.iloc[-2]
        today = df.iloc[-1]
        prev_range = prev_day["high"] - prev_day["low"]

        # Breakout target
        target_price = today["open"] + prev_range * self._k_factor
        current_price = ticker.last

        # Volume confirmation
        avg_volume = df["volume"].rolling(20).mean().iloc[-1] if len(df) >= 20 else df["volume"].mean()
        volume_ratio = today["volume"] / avg_volume if avg_volume > 0 else 1.0
        volume_confirmed = volume_ratio >= self._volume_confirm_ratio

        indicators = {
            "prev_range": prev_range,
            "target_price": target_price,
            "current_price": current_price,
            "today_open": today["open"],
            "k_factor": self._k_factor,
            "volume_ratio": round(volume_ratio, 2),
        }

        if current_price > target_price:
            # Breakout detected
            confidence = min(0.5 + (0.3 if volume_confirmed else 0.0), 1.0)
            # Higher confidence when breakout is strong
            breakout_strength = (current_price - target_price) / prev_range if prev_range > 0 else 0
            confidence = min(confidence + breakout_strength * 0.2, 1.0)

            # 추세 확인: SMA20 < SMA60이면 하락 추세 → BUY를 HOLD로 강등
            sma20 = df["close"].rolling(20).mean().iloc[-1] if len(df) >= 20 else None
            sma60 = df["close"].rolling(60).mean().iloc[-1] if len(df) >= 60 else None
            if sma20 is not None and sma60 is not None and not pd.isna(sma20) and not pd.isna(sma60):
                if sma20 < sma60:
                    indicators["trend_blocked"] = True
                    return Signal(
                        signal_type=SignalType.HOLD,
                        confidence=round(confidence * 0.5, 2),
                        strategy_name=self.name,
                        reason=f"변동성 돌파 감지되었으나 하락 추세(SMA20 < SMA60)로 매수 차단. "
                        f"현재가 {current_price:,.0f} > 목표가 {target_price:,.0f}",
                        indicators=indicators,
                    )

            return Signal(
                signal_type=SignalType.BUY,
                confidence=round(confidence, 2),
                strategy_name=self.name,
                reason=f"변동성 돌파: 현재가 {current_price:,.0f} > 목표가 {target_price:,.0f} "
                f"(시가 {today['open']:,.0f} + 전일변동폭 {prev_range:,.0f} × K={self._k_factor}). "
                f"거래량 비율: {volume_ratio:.1f}x",
                suggested_price=current_price,
                indicators=indicators,
            )

        # Check if already in position and need to exit
        if current_price < today["open"] * (1 - self._stop_loss_pct):
            return Signal(
                signal_type=SignalType.SELL,
                confidence=0.8,
                strategy_name=self.name,
                reason=f"손절 조건: 현재가 {current_price:,.0f}이 시가 대비 "
                f"{((current_price / today['open'] - 1) * 100):.1f}% 하락",
                suggested_price=current_price,
                indicators=indicators,
            )

        return Signal(
            signal_type=SignalType.HOLD,
            confidence=0.5,
            strategy_name=self.name,
            reason=f"돌파 미발생: 현재가 {current_price:,.0f} < 목표가 {target_price:,.0f}",
            indicators=indicators,
        )

    def get_params(self) -> dict:
        return {
            "k_factor": self._k_factor,
            "stop_loss_pct": self._stop_loss_pct,
            "take_profit_pct": self._take_profit_pct,
            "volume_confirm_ratio": self._volume_confirm_ratio,
        }

    def set_params(self, params: dict) -> None:
        if "k_factor" in params:
            self._k_factor = params["k_factor"]
        if "stop_loss_pct" in params:
            self._stop_loss_pct = params["stop_loss_pct"]
        if "take_profit_pct" in params:
            self._take_profit_pct = params["take_profit_pct"]
        if "volume_confirm_ratio" in params:
            self._volume_confirm_ratio = params["volume_confirm_ratio"]
