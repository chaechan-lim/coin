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
        combiner = SignalCombiner(
            strategy_weights={"rsi": 0.05, "bollinger_rsi": 0.30},
            min_confidence=0.10,
        )
        signals = [
            _signal("rsi", SignalType.BUY, 0.90),           # 0.05 * 0.90 = 0.045
            _signal("bollinger_rsi", SignalType.SELL, 0.80),  # 0.30 * 0.80 = 0.240
        ]
        result = combiner.combine(signals)
        assert result.action == SignalType.SELL


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
