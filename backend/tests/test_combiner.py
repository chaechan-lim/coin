"""
Tests for SignalCombiner: weighted voting, HOLD=abstain, adaptive weights.
"""
import os
os.environ.setdefault("DB_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("EXCHANGE_API_KEY", "test")
os.environ.setdefault("EXCHANGE_API_SECRET", "test")
os.environ.setdefault("TRADING_MODE", "paper")

import pytest

from core.enums import SignalType
from strategies.base import Signal
from strategies.combiner import SignalCombiner, CombinedDecision


def _signal(name: str, typ: SignalType, confidence: float = 0.7) -> Signal:
    return Signal(
        strategy_name=name,
        signal_type=typ,
        confidence=confidence,
        reason=f"test_{name}_{typ.value}",
    )


# ── 기본 결합 ─────────────────────────────────────────────────


class TestBasicCombine:
    def test_empty_signals_returns_hold(self):
        combiner = SignalCombiner()
        result = combiner.combine([])
        assert result.action == SignalType.HOLD
        assert result.combined_confidence == 0.0

    def test_single_buy_above_threshold(self):
        combiner = SignalCombiner(
            strategy_weights={"rsi": 0.25},
            min_confidence=0.50,
        )
        result = combiner.combine([_signal("rsi", SignalType.BUY, 0.80)])
        assert result.action == SignalType.BUY
        assert result.combined_confidence == 0.80

    def test_single_buy_below_threshold(self):
        combiner = SignalCombiner(
            strategy_weights={"rsi": 0.25},
            min_confidence=0.50,
        )
        result = combiner.combine([_signal("rsi", SignalType.BUY, 0.30)])
        assert result.action == SignalType.HOLD  # below threshold

    def test_single_sell_signal(self):
        combiner = SignalCombiner(
            strategy_weights={"rsi": 0.25},
            min_confidence=0.50,
        )
        result = combiner.combine([_signal("rsi", SignalType.SELL, 0.80)])
        assert result.action == SignalType.SELL


# ── HOLD = 기권 ───────────────────────────────────────────────


class TestHoldAbstain:
    def test_hold_signal_not_counted_in_voting(self):
        """HOLD 시그널은 투표에 참여하지 않음."""
        combiner = SignalCombiner(
            strategy_weights={"rsi": 0.25, "macd": 0.12, "ma": 0.08},
            min_confidence=0.50,
        )
        signals = [
            _signal("rsi", SignalType.BUY, 0.80),
            _signal("macd", SignalType.HOLD, 0.30),
            _signal("ma", SignalType.HOLD, 0.30),
        ]
        result = combiner.combine(signals)
        # rsi만 BUY 참여 → confidence = 0.80 (정규화)
        assert result.action == SignalType.BUY
        assert result.combined_confidence == 0.80

    def test_all_hold_returns_hold(self):
        combiner = SignalCombiner(
            strategy_weights={"rsi": 0.25, "macd": 0.12},
        )
        signals = [
            _signal("rsi", SignalType.HOLD, 0.30),
            _signal("macd", SignalType.HOLD, 0.30),
        ]
        result = combiner.combine(signals)
        assert result.action == SignalType.HOLD
        assert "Active weight" in result.final_reason


# ── 가중치 정규화 ─────────────────────────────────────────────


class TestWeightNormalization:
    def test_buy_sell_competition(self):
        """BUY와 SELL이 경쟁할 때 가중치 * 신뢰도가 높은 쪽이 승리."""
        combiner = SignalCombiner(
            strategy_weights={"rsi": 0.25, "macd": 0.12},
            min_confidence=0.10,
        )
        signals = [
            _signal("rsi", SignalType.BUY, 0.90),    # 0.25 * 0.90 = 0.225
            _signal("macd", SignalType.SELL, 0.50),   # 0.12 * 0.50 = 0.060
        ]
        result = combiner.combine(signals)
        assert result.action == SignalType.BUY

    def test_sell_wins_with_higher_weighted_score(self):
        # COIN-61 fix: normalization now uses direction-specific active weight
        # Instead of dividing both by combined active_weight, each is normalized by its own.
        # This test now verifies that SELL wins when it has HIGHER confidence in its signals.
        combiner = SignalCombiner(
            strategy_weights={"rsi": 0.05, "bollinger_rsi": 0.30},
            min_confidence=0.10,
        )
        signals = [
            _signal("rsi", SignalType.BUY, 0.70),           # 0.05 * 0.70 = 0.035, norm = 0.70
            _signal("bollinger_rsi", SignalType.SELL, 0.90),  # 0.30 * 0.90 = 0.270, norm = 0.90
        ]
        result = combiner.combine(signals)
        assert result.action == SignalType.SELL
        assert abs(result.combined_confidence - 0.90) < 0.01


# ── MIN_ACTIVE_WEIGHT ─────────────────────────────────────────


class TestMinActiveWeight:
    def test_below_min_active_weight_returns_hold(self):
        """참여 전략 가중치가 MIN_ACTIVE_WEIGHT 미만이면 HOLD."""
        combiner = SignalCombiner(
            strategy_weights={"tiny_strategy": 0.05},
            min_confidence=0.10,
        )
        signals = [_signal("tiny_strategy", SignalType.BUY, 0.99)]
        result = combiner.combine(signals)
        assert result.action == SignalType.HOLD
        assert "Active weight" in result.final_reason

    def test_above_min_active_weight_proceeds(self):
        combiner = SignalCombiner(
            strategy_weights={"rsi": 0.15},
            min_confidence=0.10,
        )
        signals = [_signal("rsi", SignalType.BUY, 0.80)]
        result = combiner.combine(signals)
        assert result.action == SignalType.BUY


# ── 적응형 가중치 ─────────────────────────────────────────────


class TestAdaptiveWeights:
    def test_apply_market_state_changes_weights(self):
        combiner = SignalCombiner()
        original_rsi = combiner.weights["rsi"]
        combiner.apply_market_state("crash")
        # crash 프로필에서 rsi는 0.19 (7전략 체제, bb_squeeze 추가)
        assert combiner.weights["rsi"] == 0.19
        assert combiner.weights["rsi"] != original_rsi or original_rsi == 0.22

    def test_apply_unknown_market_state_no_change(self):
        combiner = SignalCombiner()
        original = combiner.weights.copy()
        combiner.apply_market_state("unknown_state")
        assert combiner.weights == original

    def test_update_weights(self):
        combiner = SignalCombiner()
        combiner.update_weights({"rsi": 0.99}, source="test")
        assert combiner.weights["rsi"] == 0.99


# ── 기본 가중치 검증 ──────────────────────────────────────────


class TestDefaultWeights:
    def test_default_weights_sum_to_one(self):
        combiner = SignalCombiner()
        total = sum(combiner.weights.values())
        assert abs(total - 1.0) < 0.01, f"Weights sum to {total}, expected ~1.0"

    def test_default_has_seven_strategies(self):
        combiner = SignalCombiner()
        assert len(combiner.weights) == 7

    def test_default_strategies_names(self):
        combiner = SignalCombiner()
        expected = {"ma_crossover", "rsi", "macd_crossover", "bollinger_rsi", "stochastic_rsi", "obv_divergence", "bb_squeeze"}
        assert set(combiner.weights.keys()) == expected


# ── SPOT_WEIGHTS 검증 (COIN-61) ──────────────────────────────

class TestSpotWeights:
    """현물용 SPOT_WEIGHTS 검증 (COIN-61 L55-60 수정)."""

    def test_spot_weights_sum_to_exactly_one(self):
        """SPOT_WEIGHTS는 정확히 1.00으로 합산되어야 함."""
        combiner = SignalCombiner()
        total = sum(combiner.SPOT_WEIGHTS.values())
        assert abs(total - 1.0) < 0.001, f"SPOT_WEIGHTS sum to {total}, expected exactly 1.0"

    def test_spot_weights_has_four_strategies(self):
        """SPOT_WEIGHTS는 4개 전략을 포함."""
        combiner = SignalCombiner()
        assert len(combiner.SPOT_WEIGHTS) == 4

    def test_spot_weights_strategies(self):
        """SPOT_WEIGHTS는 올바른 전략들을 포함."""
        combiner = SignalCombiner()
        expected = {"bnf_deviation", "cis_momentum", "larry_williams", "donchian_channel"}
        assert set(combiner.SPOT_WEIGHTS.keys()) == expected


# ── 정규화 버그 수정 검증 (COIN-61) ──────────────────────────

class TestNormalizationFix:
    """비방향 모드에서 방향별 active weight로 정규화 (COIN-61 L186-188)."""

    def test_non_directional_buy_normalized_by_buy_active(self):
        """
        비방향 모드: BUY 신호는 buy_active로만 정규화.
        RSI(w=0.21) BUY@0.80 + MACD(w=0.07) SELL@0.60
        → buy_norm = 0.168 / 0.21 = 0.80 (이전 버그: 0.60)
        """
        combiner = SignalCombiner(
            strategy_weights={"rsi": 0.21, "macd_crossover": 0.07},
            min_confidence=0.10,
            directional_weights=False,  # 비방향 모드
        )
        signals = [
            _signal("rsi", SignalType.BUY, 0.80),
            _signal("macd_crossover", SignalType.SELL, 0.60),
        ]
        result = combiner.combine(signals)
        assert result.action == SignalType.BUY
        # 예상: buy_norm = 0.168 / 0.21 = 0.80
        assert abs(result.combined_confidence - 0.80) < 0.01, \
            f"Expected buy_norm ~0.80, got {result.combined_confidence}"

    def test_non_directional_sell_normalized_by_sell_active(self):
        """
        비방향 모드: SELL 신호는 sell_active로만 정규화.
        RSI(w=0.21) BUY@0.50 + Bollinger(w=0.26) SELL@0.90
        → sell_norm = 0.234 / 0.26 = 0.90 (이전 버그: 0.51)
        """
        combiner = SignalCombiner(
            strategy_weights={"rsi": 0.21, "bollinger_rsi": 0.26},
            min_confidence=0.10,
            directional_weights=False,  # 비방향 모드
        )
        signals = [
            _signal("rsi", SignalType.BUY, 0.50),
            _signal("bollinger_rsi", SignalType.SELL, 0.90),
        ]
        result = combiner.combine(signals)
        assert result.action == SignalType.SELL
        # 예상: sell_norm = 0.234 / 0.26 = 0.90
        assert abs(result.combined_confidence - 0.90) < 0.01, \
            f"Expected sell_norm ~0.90, got {result.combined_confidence}"

    def test_directional_mode_unchanged(self):
        """
        방향별 모드는 이미 올바르게 구현됨 (L184-185).
        이 테스트는 회귀 방지용.
        """
        combiner = SignalCombiner(
            strategy_weights={"rsi": 0.21, "macd_crossover": 0.07},
            min_confidence=0.10,
            directional_weights=True,  # 방향별 모드
        )
        signals = [
            _signal("rsi", SignalType.BUY, 0.80),
            _signal("macd_crossover", SignalType.SELL, 0.60),
        ]
        result = combiner.combine(signals)
        assert result.action == SignalType.BUY
        # 방향별 모드는 BUY_WEIGHTS, SELL_WEIGHTS 사용
        # 정규화: buy_norm = (0.09*0.80 + ...) / buy_active
        # (정확한 값은 BUY_WEIGHTS 의존)


# ── symbol 파라미터 ─────────────────────────────────────────


class TestSymbolParameter:
    def test_combine_with_symbol(self):
        """symbol 파라미터 전달 시 정상 동작."""
        combiner = SignalCombiner(
            strategy_weights={"rsi": 0.25},
            min_confidence=0.50,
        )
        result = combiner.combine(
            [_signal("rsi", SignalType.BUY, 0.80)],
            symbol="BTC/KRW",
        )
        assert result.action == SignalType.BUY

    def test_combine_without_symbol(self):
        """symbol 없이도 기존과 동일하게 동작."""
        combiner = SignalCombiner(
            strategy_weights={"rsi": 0.25},
            min_confidence=0.50,
        )
        result = combiner.combine([_signal("rsi", SignalType.BUY, 0.80)])
        assert result.action == SignalType.BUY


# ── 미등록 전략 기본 가중치 ───────────────────────────────────


class TestUnknownStrategy:
    def test_unknown_strategy_uses_default_weight(self):
        """미등록 전략은 기본 가중치 0.1 사용."""
        combiner = SignalCombiner(
            strategy_weights={"rsi": 0.25},
            min_confidence=0.10,
        )
        signals = [
            _signal("rsi", SignalType.BUY, 0.70),
            _signal("unknown_strategy", SignalType.BUY, 0.70),
        ]
        result = combiner.combine(signals)
        assert result.action == SignalType.BUY
        # active_weight = 0.25 + 0.10 = 0.35
        # buy_score = 0.25*0.70 + 0.10*0.70 = 0.245
        # normalized = 0.245 / 0.35 = 0.70
        assert abs(result.combined_confidence - 0.70) < 0.01


# ── 프론트엔드 최종 판단 계산 계약 ───────────────────────────
# OrderLog.tsx의 computeCombinedSignal() 함수는 이 클래스의 로직을 미러링함.
# 프론트엔드가 올바른 최종 판단을 계산할 수 있도록 백엔드 계약을 검증.


class TestFrontendCombinedSignalContract:
    """
    프론트엔드 신호 로그 개선 (COIN-11):
    코인별 최종 판단을 표시하기 위해 frontend/OrderLog.tsx는 전략 로그 +
    현재 가중치로 최종 신호를 계산한다.
    여기서는 그 계산 로직이 백엔드 SignalCombiner와 동일함을 검증한다.
    """

    def _compute_combined(
        self,
        signals: list[Signal],
        weights: dict[str, float],
        min_confidence: float = 0.55,
        min_active_weight: float = 0.12,
    ) -> dict:
        """Frontend computeCombinedSignal() 파이썬 복제."""
        buy_score = sell_score = 0.0
        buy_active = sell_active = 0.0
        for s in signals:
            w = weights.get(s.strategy_name, 0.1)
            conf = s.confidence
            if s.signal_type == SignalType.BUY:
                buy_score += w * conf
                buy_active += w
            elif s.signal_type == SignalType.SELL:
                sell_score += w * conf
                sell_active += w
            # HOLD → abstain
        active_weight = buy_active + sell_active
        if active_weight < min_active_weight:
            return {"action": "HOLD", "confidence": 0.0}
        # 정규화: 각 방향을 독립적으로 정규화 (방향별 참여 가중치로 나눔)
        buy_norm = buy_score / buy_active if buy_active > 0 else 0.0
        sell_norm = sell_score / sell_active if sell_active > 0 else 0.0
        is_long = buy_norm >= sell_norm
        winning_score = buy_norm if is_long else sell_norm
        if winning_score < min_confidence:
            return {"action": "HOLD", "confidence": winning_score}
        return {"action": "BUY" if is_long else "SELL", "confidence": winning_score}

    def test_buy_signals_produce_buy_verdict(self):
        """다수 BUY 신호 → 최종 BUY 판단."""
        weights = {"bollinger_rsi": 0.26, "rsi": 0.21, "bb_squeeze": 0.15}
        signals = [
            _signal("bollinger_rsi", SignalType.BUY, 0.80),
            _signal("rsi", SignalType.BUY, 0.70),
            _signal("bb_squeeze", SignalType.HOLD, 0.30),
        ]
        result = self._compute_combined(signals, weights)
        assert result["action"] == "BUY"
        assert result["confidence"] > 0.55

        # 백엔드 SignalCombiner와 동일한 결과 확인
        combiner = SignalCombiner(strategy_weights=weights, min_confidence=0.55)
        backend_result = combiner.combine(signals)
        assert backend_result.action == SignalType.BUY
        assert abs(backend_result.combined_confidence - result["confidence"]) < 0.001

    def test_sell_signals_produce_sell_verdict(self):
        """다수 SELL 신호 → 최종 SELL 판단."""
        weights = {"bollinger_rsi": 0.26, "rsi": 0.21, "stochastic_rsi": 0.13}
        signals = [
            _signal("bollinger_rsi", SignalType.SELL, 0.80),
            _signal("rsi", SignalType.SELL, 0.75),
            _signal("stochastic_rsi", SignalType.HOLD, 0.20),
        ]
        result = self._compute_combined(signals, weights)
        assert result["action"] == "SELL"

        combiner = SignalCombiner(strategy_weights=weights, min_confidence=0.55)
        backend_result = combiner.combine(signals)
        assert backend_result.action == SignalType.SELL
        assert abs(backend_result.combined_confidence - result["confidence"]) < 0.001

    def test_low_confidence_shows_hold(self):
        """낮은 신뢰도 → 최종 HOLD."""
        weights = {"rsi": 0.25, "macd": 0.20}
        signals = [
            _signal("rsi", SignalType.BUY, 0.40),
            _signal("macd", SignalType.BUY, 0.35),
        ]
        result = self._compute_combined(signals, weights, min_confidence=0.55)
        assert result["action"] == "HOLD"
        assert result["confidence"] > 0  # confidence는 0보다 크지만 threshold 미달

    def test_all_hold_returns_hold_with_zero_confidence(self):
        """모든 전략 HOLD → 최종 HOLD, confidence=0."""
        weights = {"rsi": 0.25, "macd": 0.20}
        signals = [
            _signal("rsi", SignalType.HOLD, 0.50),
            _signal("macd", SignalType.HOLD, 0.50),
        ]
        result = self._compute_combined(signals, weights)
        assert result["action"] == "HOLD"
        assert result["confidence"] == 0.0

    def test_mixed_signals_buy_wins(self):
        """BUY와 SELL 혼합 — 가중 점수가 높은 BUY 승리."""
        weights = {"bollinger_rsi": 0.26, "rsi": 0.21}
        signals = [
            _signal("bollinger_rsi", SignalType.BUY, 0.90),   # 0.26 * 0.90 = 0.234
            _signal("rsi", SignalType.SELL, 0.60),             # 0.21 * 0.60 = 0.126
        ]
        result = self._compute_combined(signals, weights, min_confidence=0.10)
        assert result["action"] == "BUY"

    def test_min_active_weight_guard(self):
        """active_weight < 0.12 → HOLD (단일 약소 전략 방지)."""
        weights = {"tiny": 0.05}
        signals = [_signal("tiny", SignalType.BUY, 0.99)]
        result = self._compute_combined(signals, weights)
        assert result["action"] == "HOLD"
        assert result["confidence"] == 0.0
