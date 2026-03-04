import structlog
import pandas as pd
from dataclasses import dataclass
from core.enums import MarketState
from services.market_data import MarketDataService

logger = structlog.get_logger(__name__)


@dataclass
class MarketAnalysis:
    state: MarketState
    confidence: float
    volatility_level: str  # "low", "medium", "high", "extreme"
    recommended_weights: dict[str, float]
    reasoning: str
    indicators: dict


# 선물용 가중치 프로필 (기존 6전략)
FUTURES_WEIGHT_PROFILES: dict[MarketState, dict[str, float]] = {
    MarketState.STRONG_UPTREND: {
        "ma_crossover": 0.12, "rsi": 0.18, "macd_crossover": 0.18,
        "bollinger_rsi": 0.22, "stochastic_rsi": 0.15, "obv_divergence": 0.15,
    },
    MarketState.UPTREND: {
        "ma_crossover": 0.10, "rsi": 0.22, "macd_crossover": 0.13,
        "bollinger_rsi": 0.25, "stochastic_rsi": 0.15, "obv_divergence": 0.15,
    },
    MarketState.SIDEWAYS: {
        "ma_crossover": 0.05, "rsi": 0.27, "macd_crossover": 0.10,
        "bollinger_rsi": 0.30, "stochastic_rsi": 0.15, "obv_divergence": 0.13,
    },
    MarketState.DOWNTREND: {
        "ma_crossover": 0.06, "rsi": 0.27, "macd_crossover": 0.10,
        "bollinger_rsi": 0.30, "stochastic_rsi": 0.15, "obv_divergence": 0.12,
    },
}

# 현물용 가중치 프로필 (신규 4전략 — 추세추종 + BNF 평균회귀)
SPOT_WEIGHT_PROFILES: dict[MarketState, dict[str, float]] = {
    MarketState.STRONG_UPTREND: {
        "bnf_deviation": 0.05, "cis_momentum": 0.35, "larry_williams": 0.35, "donchian_channel": 0.25,
    },
    MarketState.UPTREND: {
        "bnf_deviation": 0.08, "cis_momentum": 0.33, "larry_williams": 0.33, "donchian_channel": 0.26,
    },
    MarketState.SIDEWAYS: {
        "bnf_deviation": 0.15, "cis_momentum": 0.30, "larry_williams": 0.30, "donchian_channel": 0.25,
    },
    MarketState.DOWNTREND: {
        "bnf_deviation": 0.20, "cis_momentum": 0.28, "larry_williams": 0.28, "donchian_channel": 0.24,
    },
}

# 하위호환 기본값
WEIGHT_PROFILES = FUTURES_WEIGHT_PROFILES


class MarketAnalysisAgent:
    """
    Analyzes overall market state and recommends strategy weight adjustments.
    Runs every 15 minutes.
    """

    def __init__(self, market_data: MarketDataService, market_symbol: str = "BTC/KRW", exchange_name: str = "bithumb"):
        self._market_data = market_data
        self._market_symbol = market_symbol
        self._exchange_name = exchange_name
        self._weight_profiles = FUTURES_WEIGHT_PROFILES if "futures" in exchange_name else SPOT_WEIGHT_PROFILES
        self._last_analysis: MarketAnalysis | None = None

    async def analyze(self) -> MarketAnalysis:
        """Analyze BTC as market proxy to determine overall state."""
        try:
            # Multi-timeframe analysis on BTC
            df_1h = await self._market_data.get_candles(self._market_symbol, "1h", 200)
            df_1d = await self._market_data.get_candles(self._market_symbol, "1d", 100)
            ticker = await self._market_data.get_ticker(self._market_symbol)

            state, confidence, reasoning, indicators = self._classify_market(
                df_1h, df_1d, ticker.last
            )

            volatility = self._assess_volatility(df_1h)
            weights = self._weight_profiles.get(state, self._weight_profiles.get(MarketState.SIDEWAYS, {}))

            analysis = MarketAnalysis(
                state=state,
                confidence=confidence,
                volatility_level=volatility,
                recommended_weights=weights,
                reasoning=reasoning,
                indicators=indicators,
            )

            self._last_analysis = analysis
            logger.info(
                "market_analyzed",
                state=state.value,
                confidence=confidence,
                volatility=volatility,
            )
            return analysis

        except Exception as e:
            logger.error("market_analysis_failed", error=str(e))
            # Return last known analysis or default
            if self._last_analysis:
                return self._last_analysis
            return MarketAnalysis(
                state=MarketState.SIDEWAYS,
                confidence=0.3,
                volatility_level="medium",
                recommended_weights=self._weight_profiles.get(MarketState.SIDEWAYS, {}),
                reasoning=f"Analysis failed ({e}), defaulting to sideways",
                indicators={},
            )

    def _classify_market(
        self, df_1h: pd.DataFrame, df_1d: pd.DataFrame, current_price: float
    ) -> tuple[MarketState, float, str, dict]:
        """Classify market state using multiple indicators."""
        reasons = []
        scores = {state: 0.0 for state in MarketState}

        # 1. Price vs moving averages (1h)
        sma_20 = df_1h["sma_20"].iloc[-1] if "sma_20" in df_1h.columns else None
        sma_50 = df_1h["sma_50"].iloc[-1] if "sma_50" in df_1h.columns else None

        if sma_20 is not None and not pd.isna(sma_20):
            if current_price > sma_20 * 1.05:
                scores[MarketState.STRONG_UPTREND] += 2
                reasons.append(f"가격이 SMA20 대비 5% 이상 상회")
            elif current_price > sma_20:
                scores[MarketState.UPTREND] += 1.5
                reasons.append(f"가격이 SMA20 상회")
            elif current_price < sma_20 * 0.95:
                scores[MarketState.DOWNTREND] += 1.5
                reasons.append(f"가격이 SMA20 대비 5% 이상 하회")
            elif current_price < sma_20:
                scores[MarketState.DOWNTREND] += 1.5
                reasons.append(f"가격이 SMA20 하회")

        # 2. MA alignment
        if sma_20 is not None and sma_50 is not None:
            if not pd.isna(sma_20) and not pd.isna(sma_50):
                if sma_20 > sma_50:
                    scores[MarketState.UPTREND] += 1
                    scores[MarketState.STRONG_UPTREND] += 0.5
                else:
                    scores[MarketState.DOWNTREND] += 1

        # 3. RSI
        rsi = df_1h["rsi_14"].iloc[-1] if "rsi_14" in df_1h.columns else None
        if rsi is not None and not pd.isna(rsi):
            if rsi > 70:
                scores[MarketState.STRONG_UPTREND] += 1
                reasons.append(f"RSI={rsi:.0f} (과매수)")
            elif rsi > 55:
                scores[MarketState.UPTREND] += 1
            elif rsi < 30:
                scores[MarketState.DOWNTREND] += 1.5
                reasons.append(f"RSI={rsi:.0f} (과매도)")
            elif rsi < 45:
                scores[MarketState.DOWNTREND] += 1
            else:
                scores[MarketState.SIDEWAYS] += 1.5
                reasons.append(f"RSI={rsi:.0f} (중립)")

        # 4. Daily trend (price change over last 7 days)
        if len(df_1d) >= 7:
            week_ago_price = df_1d["close"].iloc[-7]
            week_change_pct = (current_price - week_ago_price) / week_ago_price * 100

            if week_change_pct > 10:
                scores[MarketState.STRONG_UPTREND] += 2
                reasons.append(f"주간 변동: +{week_change_pct:.1f}%")
            elif week_change_pct > 3:
                scores[MarketState.UPTREND] += 1.5
                reasons.append(f"주간 변동: +{week_change_pct:.1f}%")
            elif week_change_pct < -10:
                scores[MarketState.DOWNTREND] += 2
                reasons.append(f"주간 변동: {week_change_pct:.1f}%")
            elif week_change_pct < -3:
                scores[MarketState.DOWNTREND] += 1.5
                reasons.append(f"주간 변동: {week_change_pct:.1f}%")
            else:
                scores[MarketState.SIDEWAYS] += 2
                reasons.append(f"주간 변동: {week_change_pct:+.1f}% (횡보)")

        # 5. Volume trend
        if "volume_sma_20" in df_1h.columns:
            current_volume = df_1h["volume"].iloc[-1]
            avg_volume = df_1h["volume_sma_20"].iloc[-1]
            if avg_volume and not pd.isna(avg_volume) and avg_volume > 0:
                vol_ratio = current_volume / avg_volume
                if vol_ratio > 2.0:
                    # High volume suggests trend change or strong trend
                    scores[MarketState.STRONG_UPTREND] += 0.5
                    scores[MarketState.DOWNTREND] += 0.5
                    reasons.append(f"거래량 {vol_ratio:.1f}x (급증)")

        # Find winning state
        best_state = max(scores, key=scores.get)
        total = sum(scores.values())
        confidence = scores[best_state] / total if total > 0 else 0.3

        indicators = {
            "sma_20": round(sma_20, 0) if sma_20 and not pd.isna(sma_20) else None,
            "sma_50": round(sma_50, 0) if sma_50 and not pd.isna(sma_50) else None,
            "rsi": round(rsi, 1) if rsi and not pd.isna(rsi) else None,
            "current_price": current_price,
            "scores": {k.value: round(v, 2) for k, v in scores.items()},
        }

        reasoning = f"시장 상태: {best_state.value} (신뢰도: {confidence:.0%}). " + ". ".join(reasons)

        return best_state, round(confidence, 2), reasoning, indicators

    def _assess_volatility(self, df_1h: pd.DataFrame) -> str:
        """Assess market volatility level."""
        if "atr_14" not in df_1h.columns:
            return "medium"

        atr = df_1h["atr_14"].iloc[-1]
        price = df_1h["close"].iloc[-1]

        if pd.isna(atr) or price <= 0:
            return "medium"

        atr_pct = atr / price * 100

        if atr_pct > 5:
            return "extreme"
        elif atr_pct > 3:
            return "high"
        elif atr_pct > 1:
            return "medium"
        return "low"

    @property
    def last_analysis(self) -> MarketAnalysis | None:
        return self._last_analysis
