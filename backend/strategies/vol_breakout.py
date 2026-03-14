"""
VolBreakout — 변동성 돌파 전략 (Keltner Channel + 모멘텀).

VOLATILE 레짐 전용:
- KC 돌파 + 거래량 확인 + RSI 모멘텀 → 진입
- 트레일링 스탑 (1.5 ATR) 또는 모멘텀 소실 → 청산
"""
import pandas as pd

from core.enums import Direction, Regime
from engine.regime_detector import RegimeState
from strategies.regime_base import RegimeStrategy, StrategyDecision


class VolBreakoutStrategy(RegimeStrategy):

    KC_MULT: float = 1.8       # Keltner Channel ATR 배수 (기존 2.0→완화)
    VOL_MIN: float = 1.2       # 최소 거래량 비율 (기존 1.5→완화)

    @property
    def name(self) -> str:
        return "vol_breakout"

    @property
    def target_regimes(self) -> list[Regime]:
        return [Regime.VOLATILE]

    async def evaluate(
        self,
        df_5m: pd.DataFrame,
        df_1h: pd.DataFrame,
        regime: RegimeState,
        current_position: Direction | None,
    ) -> StrategyDecision:
        if len(df_5m) < 20:
            return self._hold(current_position, "insufficient_data")

        close = self._col(df_5m, "close")
        ema20 = self._col(df_5m, "ema_20")
        atr = self._col(df_5m, "atr_14")
        rsi = self._col(df_5m, "rsi_14")
        volume = self._col(df_5m, "volume")

        if close <= 0 or ema20 <= 0 or atr <= 0:
            return self._hold(current_position, "invalid_data")

        # Volume ratio
        vol_sma = df_5m["volume"].rolling(20).mean().iloc[-1] if len(df_5m) >= 20 else volume
        vol_ratio = volume / vol_sma if vol_sma > 0 else 1.0

        # Keltner Channel
        kc_upper = ema20 + self.KC_MULT * atr
        kc_lower = ema20 - self.KC_MULT * atr

        # 1h RSI 방향 (모멘텀 확인)
        rsi_1h = self._col(df_1h, "rsi_14")

        # ── 상단 돌파 + 거래량 + RSI 모멘텀 ──
        if close > kc_upper and vol_ratio > self.VOL_MIN:
            # RSI > 50이면 모멘텀 정렬 (부스트)
            momentum_aligned = rsi > 50 and rsi_1h > 50
            conf = min(1.0, vol_ratio / 3.0 * 0.4 + (close - kc_upper) / atr * 0.3)
            conf = max(0.3, conf)
            if momentum_aligned:
                conf = min(1.0, conf + 0.1)
            return StrategyDecision(
                direction=Direction.LONG,
                confidence=conf,
                sizing_factor=min(0.8, conf * 0.7),
                stop_loss_atr=1.8,
                take_profit_atr=3.5,
                reason=f"KC upper breakout: vol={vol_ratio:.1f}x, RSI={rsi:.0f}, momentum={momentum_aligned}",
                strategy_name=self.name,
                indicators={"kc_upper": kc_upper, "vol_ratio": vol_ratio, "rsi": rsi, "close": close},
            )

        # ── 하단 돌파 + 거래량 + RSI 모멘텀 ──
        if close < kc_lower and vol_ratio > self.VOL_MIN:
            momentum_aligned = rsi < 50 and rsi_1h < 50
            conf = min(1.0, vol_ratio / 3.0 * 0.4 + (kc_lower - close) / atr * 0.3)
            conf = max(0.3, conf)
            if momentum_aligned:
                conf = min(1.0, conf + 0.1)
            return StrategyDecision(
                direction=Direction.SHORT,
                confidence=conf,
                sizing_factor=min(0.8, conf * 0.7),
                stop_loss_atr=1.8,
                take_profit_atr=3.5,
                reason=f"KC lower breakout: vol={vol_ratio:.1f}x, RSI={rsi:.0f}, momentum={momentum_aligned}",
                strategy_name=self.name,
                indicators={"kc_lower": kc_lower, "vol_ratio": vol_ratio, "rsi": rsi, "close": close},
            )

        # ── 청산: 모멘텀 소실 (EMA20 복귀 + RSI 중립) ──
        if current_position == Direction.LONG:
            # EMA20 아래로 복귀 + RSI 약화
            if close < ema20 and rsi < 50:
                return StrategyDecision(
                    direction=Direction.FLAT,
                    confidence=0.7,
                    sizing_factor=0.0,
                    stop_loss_atr=0,
                    take_profit_atr=0,
                    reason=f"Breakout failure: close < EMA20, RSI={rsi:.0f}",
                    strategy_name=self.name,
                    indicators={"close": close, "ema20": ema20, "rsi": rsi},
                )

        if current_position == Direction.SHORT:
            if close > ema20 and rsi > 50:
                return StrategyDecision(
                    direction=Direction.FLAT,
                    confidence=0.7,
                    sizing_factor=0.0,
                    stop_loss_atr=0,
                    take_profit_atr=0,
                    reason=f"Breakout failure: close > EMA20, RSI={rsi:.0f}",
                    strategy_name=self.name,
                    indicators={"close": close, "ema20": ema20, "rsi": rsi},
                )

        return self._hold(current_position, "no_breakout")

    def _hold(self, current_position: Direction | None, reason: str) -> StrategyDecision:
        return StrategyDecision(
            direction=current_position or Direction.FLAT,
            confidence=0.5,
            sizing_factor=0.0,
            stop_loss_atr=0,
            take_profit_atr=0,
            reason=reason,
            strategy_name=self.name,
            indicators={},
        )

    @staticmethod
    def _col(df: pd.DataFrame, name: str) -> float:
        if name not in df.columns:
            return 0.0
        val = df[name].iloc[-1]
        return float(val) if pd.notna(val) else 0.0
