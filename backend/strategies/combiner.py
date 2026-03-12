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

    # Default strategy weights — 7전략 (선물용, bb_squeeze 추가)
    DEFAULT_WEIGHTS = {
        "ma_crossover": 0.07,
        "rsi": 0.21,
        "macd_crossover": 0.07,
        "bollinger_rsi": 0.26,
        "stochastic_rsi": 0.13,
        "obv_divergence": 0.11,
        "bb_squeeze": 0.15,
    }

    # 방향별 가중치 — 롱은 추세추종 중심, 숏은 평균회귀 중심
    # 백테스트 근거: MACD 롱 100% 승률, 평균회귀 숏 35% 승률
    BUY_WEIGHTS = {
        "ma_crossover": 0.16,
        "rsi": 0.09,
        "macd_crossover": 0.22,
        "bollinger_rsi": 0.13,
        "stochastic_rsi": 0.09,
        "obv_divergence": 0.19,
        "bb_squeeze": 0.12,
    }
    SELL_WEIGHTS = {
        "ma_crossover": 0.04,
        "rsi": 0.22,
        "macd_crossover": 0.09,
        "bollinger_rsi": 0.27,
        "stochastic_rsi": 0.18,
        "obv_divergence": 0.09,
        "bb_squeeze": 0.11,
    }

    # 현물용 4전략 가중치 (Optuna 50trials 다중기간 최적화, 2026-03-08)
    SPOT_WEIGHTS = {
        "bnf_deviation": 0.23,
        "cis_momentum": 0.22,
        "larry_williams": 0.31,
        "donchian_channel": 0.24,
    }

    def __init__(
        self,
        strategy_weights: dict[str, float] | None = None,
        min_confidence: float = 0.50,
        directional_weights: bool = False,
        exchange_name: str = "",
        min_sell_active_weight: float = 0.0,
    ):
        self.weights = strategy_weights or self.DEFAULT_WEIGHTS.copy()
        self.min_confidence = min_confidence
        self.directional_weights = directional_weights
        self.exchange_name = exchange_name
        if min_sell_active_weight > 0:
            self.MIN_SELL_ACTIVE_WEIGHT = min_sell_active_weight

    # 시장 상태별 적응형 가중치 프로필 (선물 6전략 전용, 현물은 SPOT_WEIGHTS 고정)
    ADAPTIVE_PROFILES: dict[str, dict[str, float]] = {
        MarketState.STRONG_UPTREND.value: {
            "ma_crossover": 0.10, "rsi": 0.16, "macd_crossover": 0.10,
            "bollinger_rsi": 0.24, "stochastic_rsi": 0.13, "obv_divergence": 0.13,
            "bb_squeeze": 0.14,
        },
        MarketState.UPTREND.value: {
            "ma_crossover": 0.08, "rsi": 0.19, "macd_crossover": 0.08,
            "bollinger_rsi": 0.24, "stochastic_rsi": 0.13, "obv_divergence": 0.13,
            "bb_squeeze": 0.15,
        },
        MarketState.SIDEWAYS.value: {
            "ma_crossover": 0.04, "rsi": 0.22, "macd_crossover": 0.06,
            "bollinger_rsi": 0.26, "stochastic_rsi": 0.12, "obv_divergence": 0.10,
            "bb_squeeze": 0.20,  # 횡보장에서 스퀴즈 가중치 최대
        },
        MarketState.DOWNTREND.value: {
            "ma_crossover": 0.10, "rsi": 0.19, "macd_crossover": 0.13,
            "bollinger_rsi": 0.22, "stochastic_rsi": 0.11, "obv_divergence": 0.10,
            "bb_squeeze": 0.15,
        },
        MarketState.CRASH.value: {
            "ma_crossover": 0.08, "rsi": 0.19, "macd_crossover": 0.10,
            "bollinger_rsi": 0.24, "stochastic_rsi": 0.13, "obv_divergence": 0.11,
            "bb_squeeze": 0.15,
        },
    }

    def update_weights(self, new_weights: dict[str, float], source: str = "unknown") -> None:
        """Update strategy weights. source: 호출 출처 (backtest/engine 등)."""
        self.weights.update(new_weights)
        logger.info("weights_updated", weights=self.weights, source=source)

    def apply_market_state(self, market_state: str) -> None:
        """시장 상태에 맞는 적응형 가중치 적용 (선물 전용, 현물은 고정 가중치)."""
        profile = self.ADAPTIVE_PROFILES.get(market_state)
        if profile:
            # 현재 등록된 전략만 업데이트
            filtered = {k: v for k, v in profile.items() if k in self.weights}
            if not filtered:
                return  # 현물 combiner — 적응형 프로필 없음 (의도된 동작)
            self.weights.update(filtered)
            logger.info("adaptive_weights_applied", market_state=market_state)

    # HOLD = 기권. BUY/SELL만 경쟁하고, 참여 전략 가중치로 정규화.
    # 참여 가중치가 너무 낮으면 (소수 약한 전략만 의견) → HOLD.
    MIN_ACTIVE_WEIGHT = 0.12
    # SELL(숏) 전용 최소 참여 가중치 — 단일 전략 숏 진입 방지 (0이면 비활성)
    MIN_SELL_ACTIVE_WEIGHT = 0.0

    def combine(self, signals: list[Signal], market_state: str | None = None, symbol: str | None = None) -> CombinedDecision:
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
        buy_active = 0.0
        sell_active = 0.0

        for signal in signals:
            if signal.signal_type == SignalType.BUY:
                w = (self.BUY_WEIGHTS.get(signal.strategy_name, 0.1)
                     if self.directional_weights
                     else self.weights.get(signal.strategy_name, 0.1))
                buy_score += w * signal.confidence
                buy_active += w
            elif signal.signal_type == SignalType.SELL:
                w = (self.SELL_WEIGHTS.get(signal.strategy_name, 0.1)
                     if self.directional_weights
                     else self.weights.get(signal.strategy_name, 0.1))
                sell_score += w * signal.confidence
                sell_active += w
            # HOLD: 기권 — 투표 미참여

        active_weight = buy_active + sell_active

        # crash 시장에서는 단일 전략 SELL도 허용 (숏 진입 활성화)
        effective_min = self.MIN_ACTIVE_WEIGHT
        if market_state == MarketState.CRASH.value:
            effective_min = 0.06

        # 의견을 낸 전략이 너무 적으면 HOLD
        if active_weight < effective_min:
            return CombinedDecision(
                action=SignalType.HOLD,
                combined_confidence=0.0,
                contributing_signals=signals,
                final_reason=f"Active weight {active_weight:.2f} below {effective_min} (all abstain)",
            )

        # 정규화: 방향별 모드면 각 방향 독립, 아니면 공유 active_weight
        if self.directional_weights:
            buy_norm = buy_score / buy_active if buy_active > 0 else 0.0
            sell_norm = sell_score / sell_active if sell_active > 0 else 0.0
        else:
            buy_norm = buy_score / active_weight
            sell_norm = sell_score / active_weight

        # 승리 타입 결정
        if buy_norm >= sell_norm:
            winning_type = SignalType.BUY
            winning_score = buy_norm
        else:
            winning_type = SignalType.SELL
            winning_score = sell_norm

        # SELL 전용 최소 참여 가중치 체크 (단일 전략 숏 진입 방지)
        min_sell_w = self.MIN_SELL_ACTIVE_WEIGHT
        if min_sell_w > 0 and winning_type == SignalType.SELL:
            sell_min_eff = 0.06 if market_state == MarketState.CRASH.value else min_sell_w
            if sell_active < sell_min_eff:
                return CombinedDecision(
                    action=SignalType.HOLD,
                    combined_confidence=winning_score,
                    contributing_signals=signals,
                    final_reason=f"Sell active weight {sell_active:.2f} below {sell_min_eff} (weak sell consensus)",
                )

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
            exchange=self.exchange_name or "unknown",
            symbol=symbol or "unknown",
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
