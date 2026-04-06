"""
TrendFollower — 추세 순응 전략 (EMA 크로스 + RSI + ADX).

TRENDING_UP: 풀백 매수 (RSI 30-50) + 모멘텀 지속 (RSI 50-65)
TRENDING_DOWN: 랠리 매도 (RSI 50-70) + 모멘텀 지속 (RSI 35-50)
SAR: 추세 이탈 시 방향 전환 (ADX 확인).
"""
import pandas as pd

from core.enums import Direction, Regime
from engine.regime_detector import RegimeState
from strategies.regime_base import RegimeStrategy, StrategyDecision


class TrendFollowerStrategy(RegimeStrategy):

    @property
    def name(self) -> str:
        return "trend_follower"

    @property
    def target_regimes(self) -> list[Regime]:
        return [Regime.TRENDING_UP, Regime.TRENDING_DOWN]

    async def evaluate(
        self,
        df_5m: pd.DataFrame,
        df_1h: pd.DataFrame,
        regime: RegimeState,
        current_position: Direction | None,
    ) -> StrategyDecision:
        if len(df_5m) < 21:
            return self._hold(current_position, "insufficient_data")

        ema_fast = self._col(df_5m, "ema_9")
        ema_slow = self._col(df_5m, "ema_21")
        rsi = self._col(df_5m, "rsi_14")
        atr = self._col(df_5m, "atr_14")
        close = self._col(df_5m, "close")
        adx = regime.adx  # 레짐 감지 시점의 ADX

        if close <= 0 or ema_slow <= 0:
            return self._hold(current_position, "invalid_price")

        if regime.regime == Regime.TRENDING_UP:
            return self._evaluate_uptrend(
                ema_fast, ema_slow, rsi, atr, close, adx, current_position,
            )
        elif regime.regime == Regime.TRENDING_DOWN:
            return self._evaluate_downtrend(
                ema_fast, ema_slow, rsi, atr, close, adx, current_position,
            )

        return self._hold(current_position, "regime_mismatch")

    def _evaluate_uptrend(
        self, ema_fast, ema_slow, rsi, atr, close, adx, current_position,
    ) -> StrategyDecision:
        spread_pct = (ema_fast - ema_slow) / ema_slow * 100 if ema_slow > 0 else 0

        if ema_fast > ema_slow and adx >= 25:
            # 풀백 매수: RSI 35-48 (눌림목, 타이트 범위)
            if 35 <= rsi <= 48 and spread_pct > 0.15:
                conf = min(1.0, spread_pct / 0.5)
                conf = max(0.3, conf)
                if adx > 35:
                    conf = min(1.0, conf + 0.1)
                return StrategyDecision(
                    direction=Direction.LONG,
                    confidence=conf,
                    sizing_factor=self._calc_sizing(conf, atr, close),
                    stop_loss_atr=1.5,
                    take_profit_atr=3.0,
                    reason=f"Trend pullback buy: spread={spread_pct:.2f}%, RSI={rsi:.0f}, ADX={adx:.0f}",
                    strategy_name=self.name,
                    indicators={"ema_fast": ema_fast, "ema_slow": ema_slow, "rsi": rsi, "adx": adx},
                )

            # 모멘텀 지속 매수: RSI 50-60 + 강한 스프레드 + 강한 ADX
            if 50 < rsi <= 60 and spread_pct > 0.4 and adx > 30:
                conf = min(1.0, spread_pct / 0.8)
                conf = max(0.3, conf)
                return StrategyDecision(
                    direction=Direction.LONG,
                    confidence=conf * 0.85,
                    sizing_factor=self._calc_sizing(conf * 0.85, atr, close),
                    stop_loss_atr=1.5,
                    take_profit_atr=2.5,
                    reason=f"Trend momentum buy: spread={spread_pct:.2f}%, RSI={rsi:.0f}",
                    strategy_name=self.name,
                    indicators={"ema_fast": ema_fast, "ema_slow": ema_slow, "rsi": rsi, "adx": adx},
                )

        # SAR: 추세 이탈 + 현재 롱 → 숏 전환 (ADX 확인)
        if ema_fast < ema_slow and current_position == Direction.LONG:
            sar_conf = 0.6 if adx > 25 else 0.5
            return StrategyDecision(
                direction=Direction.SHORT,
                confidence=sar_conf,
                sizing_factor=0.5,
                stop_loss_atr=2.0,
                take_profit_atr=2.5,
                reason=f"SAR: EMA cross down, ADX={adx:.0f}",
                strategy_name=self.name,
                indicators={"ema_fast": ema_fast, "ema_slow": ema_slow, "adx": adx},
            )

        return self._hold(current_position, "no_signal_uptrend")

    def _evaluate_downtrend(
        self, ema_fast, ema_slow, rsi, atr, close, adx, current_position,
    ) -> StrategyDecision:
        spread_pct = (ema_slow - ema_fast) / ema_slow * 100 if ema_slow > 0 else 0

        if ema_fast < ema_slow and adx >= 25:
            # 랠리 매도: RSI 52-65 (반등 후 재하락, 타이트 범위)
            if 52 <= rsi <= 65 and spread_pct > 0.15:
                conf = min(1.0, spread_pct / 0.5)
                conf = max(0.3, conf)
                if adx > 35:
                    conf = min(1.0, conf + 0.1)
                return StrategyDecision(
                    direction=Direction.SHORT,
                    confidence=conf,
                    sizing_factor=self._calc_sizing(conf, atr, close),
                    stop_loss_atr=1.5,
                    take_profit_atr=3.0,
                    reason=f"Trend rally sell: spread={spread_pct:.2f}%, RSI={rsi:.0f}, ADX={adx:.0f}",
                    strategy_name=self.name,
                    indicators={"ema_fast": ema_fast, "ema_slow": ema_slow, "rsi": rsi, "adx": adx},
                )

            # 모멘텀 지속 매도: RSI 40-50 + 강한 스프레드 + 강한 ADX
            if 40 <= rsi < 50 and spread_pct > 0.4 and adx > 30:
                conf = min(1.0, spread_pct / 0.8)
                conf = max(0.3, conf)
                return StrategyDecision(
                    direction=Direction.SHORT,
                    confidence=conf * 0.85,
                    sizing_factor=self._calc_sizing(conf * 0.85, atr, close),
                    stop_loss_atr=1.5,
                    take_profit_atr=2.5,
                    reason=f"Trend momentum sell: spread={spread_pct:.2f}%, RSI={rsi:.0f}",
                    strategy_name=self.name,
                    indicators={"ema_fast": ema_fast, "ema_slow": ema_slow, "rsi": rsi, "adx": adx},
                )

        # SAR: 추세 이탈 + 현재 숏 → 롱 전환
        if ema_fast > ema_slow and current_position == Direction.SHORT:
            sar_conf = 0.6 if adx > 25 else 0.5
            return StrategyDecision(
                direction=Direction.LONG,
                confidence=sar_conf,
                sizing_factor=0.5,
                stop_loss_atr=2.0,
                take_profit_atr=2.5,
                reason=f"SAR: EMA cross up, ADX={adx:.0f}",
                strategy_name=self.name,
                indicators={"ema_fast": ema_fast, "ema_slow": ema_slow, "adx": adx},
            )

        return self._hold(current_position, "no_signal_downtrend")

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
