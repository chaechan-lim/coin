import structlog
from dataclasses import dataclass, field
from core.enums import SignalType, MarketState
from strategies.base import Signal

logger = structlog.get_logger(__name__)


@dataclass
class CombinedDecision:
    """Result of combining multiple strategy signals."""

    action: SignalType
    combined_confidence: float
    contributing_signals: list[Signal]
    final_reason: str


class SignalCombiner:
    """Combines multiple strategy signals using weighted voting."""

    # Default strategy weights — 6전략 (0% 승률 전략 제거: vol_breakout, supertrend)
    DEFAULT_WEIGHTS = {
        "ma_crossover": 0.08,
        "rsi": 0.25,
        "macd_crossover": 0.12,
        "bollinger_rsi": 0.27,
        "stochastic_rsi": 0.15,
        "obv_divergence": 0.13,
    }

    def __init__(
        self,
        strategy_weights: dict[str, float] | None = None,
        min_confidence: float = 0.50,
    ):
        self.weights = strategy_weights or self.DEFAULT_WEIGHTS.copy()
        self.min_confidence = min_confidence

    # 시장 상태별 적응형 가중치 프로필 (8전략)
    ADAPTIVE_PROFILES: dict[str, dict[str, float]] = {
        MarketState.STRONG_UPTREND.value: {
            "ma_crossover": 0.12, "rsi": 0.18, "macd_crossover": 0.18,
            "bollinger_rsi": 0.22, "stochastic_rsi": 0.15, "obv_divergence": 0.15,
        },
        MarketState.UPTREND.value: {
            "ma_crossover": 0.10, "rsi": 0.22, "macd_crossover": 0.13,
            "bollinger_rsi": 0.25, "stochastic_rsi": 0.15, "obv_divergence": 0.15,
        },
        MarketState.SIDEWAYS.value: {
            "ma_crossover": 0.05, "rsi": 0.27, "macd_crossover": 0.10,
            "bollinger_rsi": 0.30, "stochastic_rsi": 0.15, "obv_divergence": 0.13,
        },
        MarketState.DOWNTREND.value: {
            "ma_crossover": 0.06, "rsi": 0.27, "macd_crossover": 0.10,
            "bollinger_rsi": 0.30, "stochastic_rsi": 0.15, "obv_divergence": 0.12,
        },
        MarketState.CRASH.value: {
            "ma_crossover": 0.04, "rsi": 0.28, "macd_crossover": 0.08,
            "bollinger_rsi": 0.32, "stochastic_rsi": 0.15, "obv_divergence": 0.13,
        },
    }

    def update_weights(self, new_weights: dict[str, float], source: str = "unknown") -> None:
        """Update strategy weights. source: 호출 출처 (backtest/engine 등)."""
        self.weights.update(new_weights)
        logger.info("weights_updated", weights=self.weights, source=source)

    def apply_market_state(self, market_state: str) -> None:
        """시장 상태에 맞는 적응형 가중치 적용."""
        profile = self.ADAPTIVE_PROFILES.get(market_state)
        if profile:
            # 현재 등록된 전략만 업데이트
            filtered = {k: v for k, v in profile.items() if k in self.weights}
            self.weights.update(filtered)
            logger.info("adaptive_weights_applied", market_state=market_state)

    # HOLD = 기권. BUY/SELL만 경쟁하고, 참여 전략 가중치로 정규화.
    # 참여 가중치가 너무 낮으면 (소수 약한 전략만 의견) → HOLD.
    MIN_ACTIVE_WEIGHT = 0.12

    def combine(self, signals: list[Signal]) -> CombinedDecision:
        """
        Weighted voting to combine signals (HOLD = abstain).

        1. BUY/SELL 시그널만 투표 참여, HOLD는 기권 처리
        2. 참여 전략 가중치로 정규화 → 소수 확신 전략도 기회
        3. 참여 가중치 < MIN_ACTIVE_WEIGHT → 의견 부족으로 HOLD
        4. 승리 스코어 < min_confidence → HOLD
        """
        if not signals:
            return CombinedDecision(
                action=SignalType.HOLD,
                combined_confidence=0.0,
                contributing_signals=[],
                final_reason="No signals to combine",
            )

        # Calculate weighted score: HOLD = abstain
        buy_score = 0.0
        sell_score = 0.0
        active_weight = 0.0  # BUY/SELL 전략의 가중치 합

        for signal in signals:
            weight = self.weights.get(signal.strategy_name, 0.1)
            if signal.signal_type == SignalType.BUY:
                buy_score += weight * signal.confidence
                active_weight += weight
            elif signal.signal_type == SignalType.SELL:
                sell_score += weight * signal.confidence
                active_weight += weight
            # HOLD: 기권 — 투표 미참여

        # 의견을 낸 전략이 너무 적으면 HOLD
        if active_weight < self.MIN_ACTIVE_WEIGHT:
            return CombinedDecision(
                action=SignalType.HOLD,
                combined_confidence=0.0,
                contributing_signals=signals,
                final_reason=f"Active weight {active_weight:.2f} below {self.MIN_ACTIVE_WEIGHT} (all abstain)",
            )

        # 참여 가중치로 정규화
        buy_norm = buy_score / active_weight
        sell_norm = sell_score / active_weight

        # 승리 타입 결정
        if buy_norm >= sell_norm:
            winning_type = SignalType.BUY
            winning_score = buy_norm
        else:
            winning_type = SignalType.SELL
            winning_score = sell_norm

        # Collect contributing signals for the winning type
        contributors = [s for s in signals if s.signal_type == winning_type]

        # Build combined reason
        reasons = []
        for s in contributors:
            reasons.append(f"[{s.strategy_name}] {s.reason} (conf={s.confidence:.2f})")
        final_reason = " | ".join(reasons)

        # Apply minimum confidence threshold
        if winning_score < self.min_confidence:
            return CombinedDecision(
                action=SignalType.HOLD,
                combined_confidence=winning_score,
                contributing_signals=signals,
                final_reason=f"Confidence {winning_score:.2f} below threshold {self.min_confidence}. "
                + final_reason,
            )

        logger.info(
            "signals_combined",
            action=winning_type.value,
            confidence=winning_score,
            active_weight=round(active_weight, 3),
            num_signals=len(signals),
            num_contributors=len(contributors),
        )

        return CombinedDecision(
            action=winning_type,
            combined_confidence=winning_score,
            contributing_signals=signals,
            final_reason=final_reason,
        )
