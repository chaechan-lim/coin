"""
MeanReversion — 평균 회귀 전략 (BB + RSI + 1h 확인).

RANGING 레짐 전용:
- 하단 근접 + RSI<40 + 1h RSI 상승 반전 → 롱
- 상단 근접 + RSI>60 + 1h RSI 하락 반전 → 숏
- BB 중앙 도달 또는 반대 극단 도달 → 청산
"""
import pandas as pd

from core.enums import Direction, Regime
from engine.regime_detector import RegimeState
from strategies.regime_base import RegimeStrategy, StrategyDecision


class MeanReversionStrategy(RegimeStrategy):

    # 진입 파라미터
    BB_ENTRY_LOW: float = 0.15      # BB 위치 하단 진입 (기존 0.1)
    BB_ENTRY_HIGH: float = 0.85     # BB 위치 상단 진입 (기존 0.9)
    RSI_OVERSOLD: float = 40        # RSI 과매도 (기존 35)
    RSI_OVERBOUGHT: float = 60      # RSI 과매수 (기존 65)

    # 탈출 파라미터
    BB_EXIT_LONG: float = 0.55      # 롱 탈출 (기존 0.5)
    BB_EXIT_SHORT: float = 0.45     # 숏 탈출 (기존 0.5)

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
        rsi = self._col(df_5m, "rsi_14")
        atr = self._col(df_5m, "atr_14")

        if close <= 0 or (bb_upper - bb_lower) <= 0:
            return self._hold(current_position, "invalid_data")

        bb_pos = (close - bb_lower) / (bb_upper - bb_lower)

        # 1h RSI 방향 확인 (2-bar 변화)
        rsi_1h = self._col(df_1h, "rsi_14")
        rsi_1h_prev = self._col_offset(df_1h, "rsi_14", 2) if len(df_1h) >= 3 else rsi_1h
        rsi_1h_rising = rsi_1h > rsi_1h_prev
        rsi_1h_falling = rsi_1h < rsi_1h_prev

        # ── 롱 진입: 하단 근접 + RSI 과매도 + 1h RSI 반등 필수 ──
        if bb_pos < self.BB_ENTRY_LOW and rsi < self.RSI_OVERSOLD and rsi_1h_rising:
            conf = min(1.0, (self.RSI_OVERSOLD - rsi) / 20 + 0.15)
            conf = max(0.35, conf)
            return StrategyDecision(
                direction=Direction.LONG,
                confidence=conf,
                sizing_factor=self._calc_sizing(conf, atr, close),
                stop_loss_atr=1.5,
                take_profit_atr=2.0,
                reason=f"BB lower: pos={bb_pos:.2f}, RSI={rsi:.0f}, 1h_rising=True",
                strategy_name=self.name,
                indicators={"bb_pos": bb_pos, "rsi": rsi, "rsi_1h": rsi_1h, "close": close},
            )

        # ── 숏 진입: 상단 근접 + RSI 과매수 + 1h RSI 하락 필수 ──
        if bb_pos > self.BB_ENTRY_HIGH and rsi > self.RSI_OVERBOUGHT and rsi_1h_falling:
            conf = min(1.0, (rsi - self.RSI_OVERBOUGHT) / 20 + 0.15)
            conf = max(0.35, conf)
            return StrategyDecision(
                direction=Direction.SHORT,
                confidence=conf,
                sizing_factor=self._calc_sizing(conf, atr, close),
                stop_loss_atr=1.5,
                take_profit_atr=2.0,
                reason=f"BB upper: pos={bb_pos:.2f}, RSI={rsi:.0f}, 1h_falling=True",
                strategy_name=self.name,
                indicators={"bb_pos": bb_pos, "rsi": rsi, "rsi_1h": rsi_1h, "close": close},
            )

        # ── 청산: BB 중앙 도달 ──
        if current_position == Direction.LONG and bb_pos > self.BB_EXIT_LONG:
            return StrategyDecision(
                direction=Direction.FLAT,
                confidence=0.6,
                sizing_factor=0.0,
                stop_loss_atr=0,
                take_profit_atr=0,
                reason=f"Mean reversion target: BB pos={bb_pos:.2f} (long exit)",
                strategy_name=self.name,
                indicators={"bb_pos": bb_pos},
            )

        if current_position == Direction.SHORT and bb_pos < self.BB_EXIT_SHORT:
            return StrategyDecision(
                direction=Direction.FLAT,
                confidence=0.6,
                sizing_factor=0.0,
                stop_loss_atr=0,
                take_profit_atr=0,
                reason=f"Mean reversion target: BB pos={bb_pos:.2f} (short exit)",
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

    @staticmethod
    def _col_offset(df: pd.DataFrame, name: str, offset: int) -> float:
        if name not in df.columns or len(df) < offset:
            return 0.0
        val = df[name].iloc[-offset]
        return float(val) if pd.notna(val) else 0.0
