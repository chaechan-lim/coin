import pandas as pd
import pandas_ta as ta
from exchange.data_models import Ticker
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from core.enums import SignalType


@StrategyRegistry.register
class VolatilityRegimeStrategy(BaseStrategy):
    """
    변동성 레짐 스위칭 전략.

    ATR 백분위(50캔들)로 현재 변동성 레짐을 판별하고,
    레짐에 따라 다른 진입 로직을 적용:

    - 저변동(ATR < 25th): 스퀴즈 브레이크아웃 — 볼린저 밴드가 좁아진 후
      가격이 상/하단을 돌파하면 방향 매매 (거래량 확인)
    - 고변동(ATR > 75th): 평균회귀 — 과매도 매수, 과매수 매도
    - 중간: HOLD (다른 전략에 위임)

    기존 전략들과 차별점: 시장 "상태"가 아닌 "변동성 사이클"에 적응.
    """

    name = "volatility_regime"
    display_name = "변동성 레짐 스위칭"
    applicable_market_types = ["all"]
    default_coins = ["BTC/USDT", "ETH/USDT"]
    required_timeframe = "4h"
    min_candles_required = 55  # ATR(14) + 50 lookback

    def __init__(
        self,
        atr_period: int = 14,
        atr_lookback: int = 50,
        low_pct: float = 25.0,      # 저변동 백분위 임계값
        high_pct: float = 75.0,     # 고변동 백분위 임계값
        bb_period: int = 20,
        bb_std: float = 2.0,
        rsi_period: int = 14,
        vol_confirm_mult: float = 1.3,  # 거래량 확인 배수 (SMA 대비)
    ):
        self._atr_period = atr_period
        self._atr_lookback = atr_lookback
        self._low_pct = low_pct
        self._high_pct = high_pct
        self._bb_period = bb_period
        self._bb_std = bb_std
        self._rsi_period = rsi_period
        self._vol_confirm_mult = vol_confirm_mult

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        if len(df) < self.min_candles_required:
            return self._hold("데이터 부족")

        # ── 지표 준비 ──
        atr_col = f"ATRr_{self._atr_period}"
        if atr_col not in df.columns:
            df.ta.atr(length=self._atr_period, append=True)
        if atr_col not in df.columns:
            return self._hold("ATR 계산 실패")

        rsi_col = f"rsi_{self._rsi_period}"
        if rsi_col not in df.columns:
            df[rsi_col] = ta.rsi(df["close"], length=self._rsi_period)

        # 볼린저 밴드
        bb_prefix = f"BBL_{self._bb_period}_"
        bbl_cols = [c for c in df.columns if c.startswith(bb_prefix)]
        if not bbl_cols:
            bbands = ta.bbands(df["close"], length=self._bb_period, std=self._bb_std)
            if bbands is not None:
                df = pd.concat([df, bbands], axis=1)
                bbl_cols = [c for c in df.columns if c.startswith(bb_prefix)]

        if not bbl_cols:
            return self._hold("볼린저 밴드 계산 실패")

        suffix = bbl_cols[0][len("BBL"):]
        bbl_col = f"BBL{suffix}"
        bbm_col = f"BBM{suffix}"
        bbu_col = f"BBU{suffix}"

        # 거래량 SMA
        if "Volume_SMA_20" not in df.columns:
            df["Volume_SMA_20"] = df["volume"].rolling(window=20).mean()

        # ── 현재값 추출 ──
        row = df.iloc[-1]
        price = ticker.last
        atr_val = row.get(atr_col)
        rsi = row.get(rsi_col)
        bb_lower = row.get(bbl_col)
        bb_upper = row.get(bbu_col)
        bb_middle = row.get(bbm_col)
        volume = row.get("volume", 0)
        vol_sma = row.get("Volume_SMA_20", 0)

        if any(pd.isna(v) for v in [atr_val, rsi, bb_lower, bb_upper]):
            return self._hold("지표값 NaN")

        # ── ATR 백분위 계산 ──
        atr_series = df[atr_col].iloc[-self._atr_lookback:]
        atr_series = atr_series.dropna()
        if len(atr_series) < 20:
            return self._hold("ATR 히스토리 부족")

        current_atr = float(atr_val)
        atr_percentile = float((atr_series < current_atr).sum() / len(atr_series) * 100)

        # 밴드폭 (변동성 보조지표)
        band_width = (float(bb_upper) - float(bb_lower)) / float(bb_middle) if float(bb_middle) > 0 else 0

        indicators = {
            "atr": round(current_atr, 2),
            "atr_pct": round(current_atr / price * 100, 2) if price > 0 else 0,
            "atr_percentile": round(atr_percentile, 1),
            "regime": "low" if atr_percentile < self._low_pct else ("high" if atr_percentile > self._high_pct else "normal"),
            "rsi": round(float(rsi), 2),
            "band_width_pct": round(band_width * 100, 2),
            "volume_ratio": round(float(volume) / float(vol_sma), 2) if vol_sma and float(vol_sma) > 0 else 0,
        }

        # ── 레짐별 로직 ──

        # 1. 저변동 레짐 (스퀴즈 → 브레이크아웃 대기)
        if atr_percentile < self._low_pct:
            vol_confirmed = float(vol_sma) > 0 and float(volume) > float(vol_sma) * self._vol_confirm_mult

            # 스퀴즈 브레이크아웃: 밴드 상단 돌파 + 거래량 확인
            if price > float(bb_upper) * 0.998 and vol_confirmed:
                conf = 0.70
                if float(rsi) > 55:
                    conf += 0.05
                # 강한 브레이크아웃 (밴드 외부)
                if price > float(bb_upper):
                    conf += 0.05
                return Signal(
                    signal_type=SignalType.BUY,
                    confidence=round(min(conf, 0.90), 2),
                    strategy_name=self.name,
                    reason=f"저변동 브레이크아웃↑: ATR P{atr_percentile:.0f} "
                    f"+ 가격>{bb_upper:.0f} + 거래량 {indicators['volume_ratio']:.1f}x",
                    suggested_price=price,
                    indicators=indicators,
                )

            # 스퀴즈 하향 돌파: 밴드 하단 이탈 + 거래량 확인 → 숏
            if price < float(bb_lower) * 1.002 and vol_confirmed:
                conf = 0.70
                if float(rsi) < 45:
                    conf += 0.05
                if price < float(bb_lower):
                    conf += 0.05
                return Signal(
                    signal_type=SignalType.SELL,
                    confidence=round(min(conf, 0.90), 2),
                    strategy_name=self.name,
                    reason=f"저변동 브레이크아웃↓: ATR P{atr_percentile:.0f} "
                    f"+ 가격<{bb_lower:.0f} + 거래량 {indicators['volume_ratio']:.1f}x",
                    suggested_price=price,
                    indicators=indicators,
                )

            return self._hold(
                f"저변동 대기: ATR P{atr_percentile:.0f}, 밴드폭 {band_width*100:.1f}%",
                indicators=indicators,
            )

        # 2. 고변동 레짐 (평균회귀)
        if atr_percentile > self._high_pct:
            # 과매도 매수: RSI < 30 + 볼린저 하단 근접
            if float(rsi) < 30 and price <= float(bb_lower) * 1.01:
                # RSI 반등 확인
                prev_rsi = df[rsi_col].iloc[-2] if len(df) >= 2 else rsi
                rsi_rising = float(rsi) > float(prev_rsi) if not pd.isna(prev_rsi) else False

                if rsi_rising:
                    conf = 0.75
                    if float(rsi) < 25:
                        conf += 0.05
                    return Signal(
                        signal_type=SignalType.BUY,
                        confidence=round(min(conf, 0.90), 2),
                        strategy_name=self.name,
                        reason=f"고변동 평균회귀 매수: ATR P{atr_percentile:.0f} "
                        f"+ RSI {rsi:.1f}↑ + 볼린저 하단",
                        suggested_price=price,
                        indicators=indicators,
                    )

            # 과매수 매도: RSI > 70 + 볼린저 상단 근접
            if float(rsi) > 70 and price >= float(bb_upper) * 0.99:
                prev_rsi = df[rsi_col].iloc[-2] if len(df) >= 2 else rsi
                rsi_falling = float(rsi) < float(prev_rsi) if not pd.isna(prev_rsi) else False

                if rsi_falling:
                    conf = 0.75
                    if float(rsi) > 80:
                        conf += 0.05
                    return Signal(
                        signal_type=SignalType.SELL,
                        confidence=round(min(conf, 0.90), 2),
                        strategy_name=self.name,
                        reason=f"고변동 평균회귀 매도: ATR P{atr_percentile:.0f} "
                        f"+ RSI {rsi:.1f}↓ + 볼린저 상단",
                        suggested_price=price,
                        indicators=indicators,
                    )

            return self._hold(
                f"고변동 대기: ATR P{atr_percentile:.0f}, RSI {rsi:.1f}",
                indicators=indicators,
            )

        # 3. 중간 변동성: HOLD (다른 전략에 위임)
        return self._hold(
            f"중간 변동성: ATR P{atr_percentile:.0f} (다른 전략 위임)",
            indicators=indicators,
        )

    def _hold(self, reason: str, indicators: dict | None = None) -> Signal:
        return Signal(
            signal_type=SignalType.HOLD,
            confidence=0.0,
            strategy_name=self.name,
            reason=reason,
            indicators=indicators or {},
        )

    def get_params(self) -> dict:
        return {
            "atr_period": self._atr_period,
            "atr_lookback": self._atr_lookback,
            "low_pct": self._low_pct,
            "high_pct": self._high_pct,
            "bb_period": self._bb_period,
            "bb_std": self._bb_std,
            "rsi_period": self._rsi_period,
            "vol_confirm_mult": self._vol_confirm_mult,
        }

    def set_params(self, params: dict) -> None:
        for key in self.get_params():
            if key in params:
                setattr(self, f"_{key}", params[key])
