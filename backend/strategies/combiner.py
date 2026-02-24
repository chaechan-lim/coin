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

    # Default strategy weights — 역발상 전략(RSI/Bollinger) 중심
    DEFAULT_WEIGHTS = {
        "volatility_breakout": 0.10,
        "ma_crossover": 0.10,
        "rsi": 0.30,
        "macd_crossover": 0.15,
        "bollinger_rsi": 0.35,
    }

    def __init__(
        self,
        strategy_weights: dict[str, float] | None = None,
        min_confidence: float = 0.4,
    ):
        self.weights = strategy_weights or self.DEFAULT_WEIGHTS.copy()
        self.min_confidence = min_confidence

    # 시장 상태별 적응형 가중치 프로필 (역발상 전략 중심)
    ADAPTIVE_PROFILES: dict[str, dict[str, float]] = {
        MarketState.STRONG_UPTREND.value: {
            "volatility_breakout": 0.15, "ma_crossover": 0.15,
            "rsi": 0.20, "macd_crossover": 0.20, "bollinger_rsi": 0.30,
        },
        MarketState.UPTREND.value: {
            "volatility_breakout": 0.10, "ma_crossover": 0.15,
            "rsi": 0.25, "macd_crossover": 0.20, "bollinger_rsi": 0.30,
        },
        MarketState.SIDEWAYS.value: {
            "volatility_breakout": 0.05, "ma_crossover": 0.05,
            "rsi": 0.35, "macd_crossover": 0.15, "bollinger_rsi": 0.40,
        },
        MarketState.DOWNTREND.value: {
            "volatility_breakout": 0.00, "ma_crossover": 0.10,
            "rsi": 0.35, "macd_crossover": 0.15, "bollinger_rsi": 0.40,
        },
        MarketState.CRASH.value: {
            "volatility_breakout": 0.00, "ma_crossover": 0.05,
            "rsi": 0.40, "macd_crossover": 0.10, "bollinger_rsi": 0.45,
        },
    }

    def update_weights(self, new_weights: dict[str, float]) -> None:
        """Update strategy weights (typically called by Market Analysis Agent)."""
        self.weights.update(new_weights)
        logger.info("weights_updated", weights=self.weights)

    def apply_market_state(self, market_state: str) -> None:
        """시장 상태에 맞는 적응형 가중치 적용."""
        profile = self.ADAPTIVE_PROFILES.get(market_state)
        if profile:
            # 현재 등록된 전략만 업데이트
            filtered = {k: v for k, v in profile.items() if k in self.weights}
            self.weights.update(filtered)
            logger.info("adaptive_weights_applied", market_state=market_state)

    def combine(self, signals: list[Signal]) -> CombinedDecision:
        """
        Weighted voting to combine signals.

        1. Group signals by type (BUY/SELL/HOLD)
        2. Sum weight * confidence for each group
        3. Highest scoring group wins
        4. Only act if combined confidence > min_confidence
        """
        if not signals:
            return CombinedDecision(
                action=SignalType.HOLD,
                combined_confidence=0.0,
                contributing_signals=[],
                final_reason="No signals to combine",
            )

        # Calculate weighted score per signal type
        scores: dict[SignalType, float] = {
            SignalType.BUY: 0.0,
            SignalType.SELL: 0.0,
            SignalType.HOLD: 0.0,
        }
        total_weight = 0.0

        for signal in signals:
            weight = self.weights.get(signal.strategy_name, 0.1)
            scores[signal.signal_type] += weight * signal.confidence
            total_weight += weight

        # Normalize scores
        if total_weight > 0:
            for sig_type in scores:
                scores[sig_type] /= total_weight

        # Find winning signal type
        winning_type = max(scores, key=scores.get)
        winning_score = scores[winning_type]

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
            num_signals=len(signals),
            num_contributors=len(contributors),
        )

        return CombinedDecision(
            action=winning_type,
            combined_confidence=winning_score,
            contributing_signals=signals,
            final_reason=final_reason,
        )
