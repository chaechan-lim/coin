"""
MeanReversion — 평균 회귀 전략 (BB + RSI 극단값).

RANGING 레짐 전용:
- 하단 터치 + RSI<35 → 롱
- 상단 터치 + RSI>65 → 숏
- 중앙 도달 → 청산
"""
import pandas as pd

from core.enums import Direction, Regime
from engine.regime_detector import RegimeState
from strategies.regime_base import RegimeStrategy, StrategyDecision


class MeanReversionStrategy(RegimeStrategy):

    @property
    def name(self) -> str:
        return "mean_reversion"

    @property
    def target_regimes(self) -> list[Regime]:
        return [Regime.RANGING]

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
        bb_upper = self._col(df_5m, "bb_upper_20")
        bb_lower = self._col(df_5m, "bb_lower_20")
        bb_mid = self._col(df_5m, "bb_mid_20")
        rsi = self._col(df_5m, "rsi_14")
        atr = self._col(df_5m, "atr_14")

        if close <= 0 or (bb_upper - bb_lower) <= 0:
            return self._hold(current_position, "invalid_data")

        bb_pos = (close - bb_lower) / (bb_upper - bb_lower)

        # 하단 터치 + RSI 과매도 → 롱
        if bb_pos < 0.1 and rsi < 35:
            conf = min(1.0, (35 - rsi) / 20)
            conf = max(0.3, conf)
            return StrategyDecision(
                direction=Direction.LONG,
                confidence=conf,
                sizing_factor=self._calc_sizing(conf, atr, close),
                stop_loss_atr=1.0,
                take_profit_atr=1.5,
                reason=f"BB lower touch: pos={bb_pos:.2f}, RSI={rsi:.0f}",
                strategy_name=self.name,
                indicators={"bb_pos": bb_pos, "rsi": rsi, "close": close},
            )

        # 상단 터치 + RSI 과매수 → 숏
        if bb_pos > 0.9 and rsi > 65:
            conf = min(1.0, (rsi - 65) / 20)
            conf = max(0.3, conf)
            return StrategyDecision(
                direction=Direction.SHORT,
                confidence=conf,
                sizing_factor=self._calc_sizing(conf, atr, close),
                stop_loss_atr=1.0,
                take_profit_atr=1.5,
                reason=f"BB upper touch: pos={bb_pos:.2f}, RSI={rsi:.0f}",
                strategy_name=self.name,
                indicators={"bb_pos": bb_pos, "rsi": rsi, "close": close},
            )

        # 포지션 있고 중앙 도달 → 청산
        if current_position == Direction.LONG and bb_pos > 0.5:
            return StrategyDecision(
                direction=Direction.FLAT,
                confidence=0.6,
                sizing_factor=0.0,
                stop_loss_atr=0,
                take_profit_atr=0,
                reason="Mean reversion target: BB mid reached (long exit)",
                strategy_name=self.name,
                indicators={"bb_pos": bb_pos},
            )

        if current_position == Direction.SHORT and bb_pos < 0.5:
            return StrategyDecision(
                direction=Direction.FLAT,
                confidence=0.6,
                sizing_factor=0.0,
                stop_loss_atr=0,
                take_profit_atr=0,
                reason="Mean reversion target: BB mid reached (short exit)",
                strategy_name=self.name,
                indicators={"bb_pos": bb_pos},
            )

        return self._hold(current_position, "no_signal_ranging")

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
