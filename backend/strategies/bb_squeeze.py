import pandas as pd
import pandas_ta as ta
import numpy as np
from exchange.data_models import Ticker
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from core.enums import SignalType


@StrategyRegistry.register
class BBSqueezeStrategy(BaseStrategy):
    """
    볼린저 스퀴즈 브레이크아웃 — 횡보 끝 방향 포착.

    스퀴즈 = BB가 켈트너 채널 안에 수축 (극저변동성).
    스퀴즈 해제 시 모멘텀 방향으로 진입.
    - BUY: 스퀴즈 해제 + 모멘텀 양(+) + 가격 SMA 위
    - SELL: 스퀴즈 해제 + 모멘텀 음(-) + 가격 SMA 아래
    횡보 자체를 트레이딩하지 않고, 횡보→추세 전환점을 잡음.
    """

    name = "bb_squeeze"
    display_name = "볼린저 스퀴즈 (브레이크아웃)"
    applicable_market_types = ["sideways", "trending"]
    default_coins = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"]
    required_timeframe = "4h"
    min_candles_required = 30

    def __init__(
        self,
        bb_period: int = 20,
        bb_std: float = 2.0,
        kc_period: int = 20,
        kc_mult: float = 1.5,
        mom_period: int = 12,
        sma_period: int = 20,
        squeeze_min_bars: int = 3,  # 최소 스퀴즈 지속 캔들
    ):
        self._bb_period = bb_period
        self._bb_std = bb_std
        self._kc_period = kc_period
        self._kc_mult = kc_mult
        self._mom_period = mom_period
        self._sma_period = sma_period
        self._squeeze_min_bars = squeeze_min_bars

    def _find_bb_columns(self, df: pd.DataFrame):
        prefix = f"BBL_{self._bb_period}_"
        bbl_cols = [c for c in df.columns if c.startswith(prefix)]
        if not bbl_cols:
            return None, None, None
        suffix = bbl_cols[0][len("BBL"):]
        return f"BBL{suffix}", f"BBM{suffix}", f"BBU{suffix}"

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        if len(df) < self.min_candles_required:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="데이터 부족",
            )

        # ── 볼린저 밴드 ──
        bbl_col, bbm_col, bbu_col = self._find_bb_columns(df)
        if bbl_col is None:
            bbands = ta.bbands(df["close"], length=self._bb_period, std=self._bb_std)
            if bbands is not None:
                df = pd.concat([df, bbands], axis=1)
                bbl_col, bbm_col, bbu_col = self._find_bb_columns(df)

        if bbl_col is None or bbl_col not in df.columns:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="볼린저 밴드 계산 불가",
            )

        # ── 켈트너 채널 (ATR 기반) ──
        atr_col = f"atr_{self._kc_period}"
        if atr_col not in df.columns:
            atr_upper = f"ATR_{self._kc_period}"
            atr_r = f"ATRr_{self._kc_period}"
            if atr_upper in df.columns:
                atr_col = atr_upper
            elif atr_r in df.columns:
                atr_col = atr_r
            else:
                atr_result = ta.atr(df["high"], df["low"], df["close"], length=self._kc_period)
                if atr_result is not None:
                    df[atr_col] = atr_result

        sma_col = f"sma_{self._sma_period}"
        if sma_col not in df.columns:
            sma_upper = f"SMA_{self._sma_period}"
            if sma_upper in df.columns:
                sma_col = sma_upper
            else:
                df[sma_col] = ta.sma(df["close"], length=self._sma_period)

        if atr_col not in df.columns or sma_col not in df.columns:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0,
                strategy_name=self.name, reason="ATR/SMA 계산 불가",
            )

        # 켈트너 채널 상/하한
        kc_mid = df[sma_col]
        kc_upper = kc_mid + self._kc_mult * df[atr_col]
        kc_lower = kc_mid - self._kc_mult * df[atr_col]

        # ── 스퀴즈 감지: BB가 KC 안에 있으면 스퀴즈 ──
        bb_lower = df[bbl_col]
        bb_upper = df[bbu_col]
        squeeze = (bb_lower > kc_lower) & (bb_upper < kc_upper)

        # 현재 스퀴즈 상태
        current_squeeze = bool(squeeze.iloc[-1]) if not pd.isna(squeeze.iloc[-1]) else False
        prev_squeeze = bool(squeeze.iloc[-2]) if len(df) > 1 and not pd.isna(squeeze.iloc[-2]) else False

        # 스퀴즈 지속 기간 계산
        squeeze_duration = 0
        for j in range(len(df) - 2, max(0, len(df) - 30), -1):
            if not pd.isna(squeeze.iloc[j]) and squeeze.iloc[j]:
                squeeze_duration += 1
            else:
                break

        # ── 모멘텀 (선형 회귀 기울기 또는 close - SMA) ──
        mom = df["close"] - df[sma_col]
        current_mom = float(mom.iloc[-1]) if not pd.isna(mom.iloc[-1]) else 0
        prev_mom = float(mom.iloc[-2]) if len(df) > 1 and not pd.isna(mom.iloc[-2]) else 0
        mom_rising = current_mom > prev_mom

        current_price = ticker.last
        current_sma = float(df[sma_col].iloc[-1])
        bb_width = float((bb_upper.iloc[-1] - bb_lower.iloc[-1]) / df[bbm_col].iloc[-1]) if float(df[bbm_col].iloc[-1]) > 0 else 0

        indicators = {
            "squeeze": current_squeeze,
            "squeeze_duration": squeeze_duration,
            "squeeze_released": prev_squeeze and not current_squeeze,
            "momentum": round(current_mom, 4),
            "mom_rising": mom_rising,
            "bb_width_pct": round(bb_width * 100, 2),
            "price_vs_sma": round((current_price - current_sma) / current_sma * 100, 2) if current_sma > 0 else 0,
            "current_price": current_price,
        }

        # ── 스퀴즈 해제 감지 ──
        # 직전까지 스퀴즈 → 현재 해제 = 브레이크아웃 시작
        squeeze_released = prev_squeeze and not current_squeeze

        # 또는 최근 N봉 내에 스퀴즈 해제 (1-2봉 전)
        if not squeeze_released and len(df) > 2:
            two_ago = bool(squeeze.iloc[-3]) if not pd.isna(squeeze.iloc[-3]) else False
            if two_ago and prev_squeeze and not current_squeeze:
                squeeze_released = True
            elif two_ago and not prev_squeeze and not current_squeeze:
                # 2봉 전 스퀴즈 → 1봉 전 해제 → 현재 확인
                squeeze_released = True

        if not squeeze_released:
            if current_squeeze:
                return Signal(
                    signal_type=SignalType.HOLD, confidence=0.3,
                    strategy_name=self.name,
                    reason=f"스퀴즈 진행 중: {squeeze_duration}봉 지속, 해제 대기",
                    indicators=indicators,
                )
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.2,
                strategy_name=self.name,
                reason=f"스퀴즈 아님: 일반 변동성 구간",
                indicators=indicators,
            )

        # 최소 스퀴즈 지속 기간 확인
        if squeeze_duration < self._squeeze_min_bars:
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.25,
                strategy_name=self.name,
                reason=f"스퀴즈 짧음: {squeeze_duration}봉 < {self._squeeze_min_bars}봉 (신뢰도 부족)",
                indicators=indicators,
            )

        # ── 방향 결정: 모멘텀 + 가격 위치 ──
        confidence = 0.60

        # 스퀴즈 길수록 브레이크아웃 강도 ↑
        duration_bonus = min(squeeze_duration * 0.02, 0.15)
        confidence += duration_bonus

        # BUY: 모멘텀 양 + 가격 SMA 위
        if current_mom > 0 and current_price > current_sma:
            if mom_rising:
                confidence += 0.05

            return Signal(
                signal_type=SignalType.BUY,
                confidence=round(min(confidence, 0.90), 2),
                strategy_name=self.name,
                reason=f"스퀴즈 상향 돌파: {squeeze_duration}봉 수축 후 해제, "
                       f"모멘텀={current_mom:+.2f}, 가격>SMA",
                suggested_price=current_price,
                indicators=indicators,
            )

        # SELL: 모멘텀 음 + 가격 SMA 아래
        if current_mom < 0 and current_price < current_sma:
            if not mom_rising:
                confidence += 0.05

            return Signal(
                signal_type=SignalType.SELL,
                confidence=round(min(confidence, 0.90), 2),
                strategy_name=self.name,
                reason=f"스퀴즈 하향 돌파: {squeeze_duration}봉 수축 후 해제, "
                       f"모멘텀={current_mom:+.2f}, 가격<SMA",
                suggested_price=current_price,
                indicators=indicators,
            )

        # 방향 불명확
        return Signal(
            signal_type=SignalType.HOLD, confidence=0.3,
            strategy_name=self.name,
            reason=f"스퀴즈 해제 but 방향 불명확: 모멘텀={current_mom:+.2f}",
            indicators=indicators,
        )

    def get_params(self) -> dict:
        return {
            "bb_period": self._bb_period,
            "bb_std": self._bb_std,
            "kc_period": self._kc_period,
            "kc_mult": self._kc_mult,
            "mom_period": self._mom_period,
            "sma_period": self._sma_period,
            "squeeze_min_bars": self._squeeze_min_bars,
        }

    def set_params(self, params: dict) -> None:
        for key in self.get_params():
            if key in params:
                setattr(self, f"_{key}", params[key])
