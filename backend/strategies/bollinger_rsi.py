import pandas as pd
from exchange.data_models import Ticker
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from core.enums import SignalType


@StrategyRegistry.register
class BollingerRSIStrategy(BaseStrategy):
    """
    Strategy 5: Bollinger Bands + RSI
    Buy when price touches lower band AND RSI < 30.
    Sell when price touches upper band AND RSI > 70.
    Dual confirmation reduces false signals.
    """

    name = "bollinger_rsi"
    display_name = "볼린저 밴드 + RSI"
    applicable_market_types = ["sideways", "trending"]
    default_coins = ["ETH/KRW", "XRP/KRW", "ADA/KRW"]
    required_timeframe = "4h"
    min_candles_required = 25

    def __init__(
        self,
        bb_period: int = 20,
        bb_std: float = 2.0,
        rsi_period: int = 14,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
    ):
        self._bb_period = bb_period
        self._bb_std = bb_std
        self._rsi_period = rsi_period
        self._rsi_oversold = rsi_oversold
        self._rsi_overbought = rsi_overbought

    def _find_bb_columns(self, df: pd.DataFrame):
        """
        Find Bollinger Band column names in dataframe.
        pandas-ta naming varies by version: BBL_20_2.0 or BBL_20_2.0_2.0
        Use prefix matching to handle both formats robustly.
        """
        prefix = f"BBL_{self._bb_period}_"
        bbl_cols = [c for c in df.columns if c.startswith(prefix)]
        if not bbl_cols:
            return None, None, None
        # Derive BBM and BBU from the found BBL column suffix
        suffix = bbl_cols[0][len("BBL"):]  # e.g. "_20_2.0" or "_20_2.0_2.0"
        return f"BBL{suffix}", f"BBM{suffix}", f"BBU{suffix}"

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        if len(df) < self.min_candles_required:
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.0,
                strategy_name=self.name,
                reason="Insufficient data for Bollinger+RSI analysis",
            )

        import pandas_ta as ta

        rsi_col = f"rsi_{self._rsi_period}"

        # Compute Bollinger Bands if not already present
        bbl_col, bbm_col, bbu_col = self._find_bb_columns(df)
        if bbl_col is None:
            bbands = ta.bbands(df["close"], length=self._bb_period, std=self._bb_std)
            if bbands is not None:
                df = pd.concat([df, bbands], axis=1)
                bbl_col, bbm_col, bbu_col = self._find_bb_columns(df)

        if rsi_col not in df.columns:
            df[rsi_col] = ta.rsi(df["close"], length=self._rsi_period)

        # Check columns exist
        if bbl_col is None or bbl_col not in df.columns:
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.0,
                strategy_name=self.name,
                reason="Unable to compute Bollinger Bands",
            )

        current_price = ticker.last
        bb_lower = df[bbl_col].iloc[-1]
        bb_middle = df[bbm_col].iloc[-1]
        bb_upper = df[bbu_col].iloc[-1]
        current_rsi = df[rsi_col].iloc[-1]

        if pd.isna(bb_lower) or pd.isna(current_rsi):
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.0,
                strategy_name=self.name,
                reason="Indicator values not available",
            )

        # Band width (volatility measure)
        band_width = (bb_upper - bb_lower) / bb_middle if bb_middle > 0 else 0

        indicators = {
            "bb_lower": round(bb_lower, 0),
            "bb_middle": round(bb_middle, 0),
            "bb_upper": round(bb_upper, 0),
            "band_width_pct": round(band_width * 100, 2),
            "rsi": round(current_rsi, 2),
            "current_price": current_price,
            "price_position": round((current_price - bb_lower) / (bb_upper - bb_lower) * 100, 1) if bb_upper != bb_lower else 50,
        }

        # Freefall guard: 밴드폭이 넓으면 변동성 과다 — 매수 차단
        if band_width > 0.25:  # 25% 이상이면 고변동성 (기존 50% → 25% 강화)
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.2,
                strategy_name=self.name,
                reason=f"변동성 필터: 밴드폭 {band_width*100:.1f}% > 25% (신호 불안정)",
                indicators=indicators,
            )

        # Trend check: SMA20 vs SMA50 — 하락 추세에서 역추세 매수 신뢰도 할인
        sma20_col = next((c for c in df.columns if c.lower() in ("sma_20", "sma20")), None)
        sma50_col = next((c for c in df.columns if c.lower() in ("sma_50", "sma50")), None)
        if sma20_col is None:
            df["_sma20"] = ta.sma(df["close"], length=20)
            sma20_col = "_sma20"
        if sma50_col is None:
            df["_sma50"] = ta.sma(df["close"], length=50)
            sma50_col = "_sma50"
        sma20_val = df[sma20_col].iloc[-1] if sma20_col in df.columns else None
        sma50_val = df[sma50_col].iloc[-1] if sma50_col in df.columns else None
        _in_downtrend = (
            sma20_val is not None and sma50_val is not None
            and not pd.isna(sma20_val) and not pd.isna(sma50_val)
            and sma20_val < sma50_val
        )

        # RSI 방향 체크 (반등 확인)
        prev_rsi = df[rsi_col].iloc[-2] if len(df) >= 2 else current_rsi
        rsi_rising = current_rsi > prev_rsi if not pd.isna(prev_rsi) else False

        # BUY: price at or below lower band AND RSI oversold
        price_near_lower = current_price <= bb_lower * 1.005  # within 0.5% of lower band
        rsi_oversold = current_rsi < self._rsi_oversold

        if price_near_lower and rsi_oversold:
            # RSI가 계속 하락 중이면 매수 보류 (나이프캐치 방지)
            if not rsi_rising and current_rsi < 25:
                return Signal(
                    signal_type=SignalType.HOLD,
                    confidence=0.35,
                    strategy_name=self.name,
                    reason=f"볼린저 하단 + RSI↓: 반등 미확인 (RSI={current_rsi:.1f}↓, 진입 보류)",
                    indicators=indicators,
                )
            # Double confirmation - high confidence
            confidence = 0.75
            if rsi_rising:
                confidence += 0.05  # RSI 반등 보너스
            # Even stronger when RSI is very low
            if current_rsi < 25:
                confidence += 0.1
            # 하락 추세에서 역추세 매수: 신뢰도 할인 (SMA 갭 비례)
            if _in_downtrend and sma50_val > 0:
                sma_gap = (sma50_val - sma20_val) / sma50_val
                if sma_gap > 0.03:  # 갭 3% 이상이면 강한 하락 추세
                    confidence *= 0.5
            return Signal(
                signal_type=SignalType.BUY,
                confidence=round(min(confidence, 0.95), 2),
                strategy_name=self.name,
                reason=f"이중 확인 매수: 가격({current_price:,.0f}) ≤ 볼린저 하단({bb_lower:,.0f}) "
                f"AND RSI({current_rsi:.1f}) < {self._rsi_oversold}. "
                f"밴드폭: {band_width*100:.1f}%"
                f"{' [역추세 할인]' if _in_downtrend else ''}",
                suggested_price=current_price,
                indicators=indicators,
            )

        # SELL: price at or above upper band AND RSI overbought
        price_near_upper = current_price >= bb_upper * 0.995
        rsi_overbought = current_rsi > self._rsi_overbought

        if price_near_upper and rsi_overbought:
            confidence = 0.75
            if current_rsi > 80:
                confidence += 0.1
            # 하락추세에서 숏 부스트
            if _in_downtrend:
                confidence = min(confidence * 1.15, 0.95)
            return Signal(
                signal_type=SignalType.SELL,
                confidence=round(min(confidence, 0.95), 2),
                strategy_name=self.name,
                reason=f"이중 확인 매도: 가격({current_price:,.0f}) ≥ 볼린저 상단({bb_upper:,.0f}) "
                f"AND RSI({current_rsi:.1f}) > {self._rsi_overbought}. "
                f"밴드폭: {band_width*100:.1f}%{' [추세부스트]' if _in_downtrend else ''}",
                suggested_price=current_price,
                indicators=indicators,
            )

        # Single confirmation signals (lower confidence)
        if price_near_lower:
            return Signal(
                signal_type=SignalType.BUY,
                confidence=0.35,
                strategy_name=self.name,
                reason=f"볼린저 하단 접근: 가격({current_price:,.0f}) ≤ 하단({bb_lower:,.0f}), "
                f"그러나 RSI({current_rsi:.1f})는 과매도 아님",
                indicators=indicators,
            )

        if price_near_upper:
            return Signal(
                signal_type=SignalType.SELL,
                confidence=0.35,
                strategy_name=self.name,
                reason=f"볼린저 상단 접근: 가격({current_price:,.0f}) ≥ 상단({bb_upper:,.0f}), "
                f"그러나 RSI({current_rsi:.1f})는 과매수 아님",
                indicators=indicators,
            )

        return Signal(
            signal_type=SignalType.HOLD,
            confidence=0.3,
            strategy_name=self.name,
            reason=f"밴드 내 중립: 가격 {current_price:,.0f}, "
            f"밴드[{bb_lower:,.0f} ~ {bb_upper:,.0f}], RSI={current_rsi:.1f}",
            indicators=indicators,
        )

    def get_params(self) -> dict:
        return {
            "bb_period": self._bb_period,
            "bb_std": self._bb_std,
            "rsi_period": self._rsi_period,
            "rsi_oversold": self._rsi_oversold,
            "rsi_overbought": self._rsi_overbought,
        }

    def set_params(self, params: dict) -> None:
        for key in ["bb_period", "bb_std", "rsi_period", "rsi_oversold", "rsi_overbought"]:
            if key in params:
                setattr(self, f"_{key}", params[key])
