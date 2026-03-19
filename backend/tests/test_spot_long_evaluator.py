"""SpotLongEvaluator — 현물 4전략 기반 선물 롱 이밸류에이터 테스트.

테스트 범위:
- DirectionEvaluator 프로토콜 호환성
- BUY → open LONG 매핑
- SELL → close LONG 매핑
- HOLD → hold 매핑
- 쿨다운 동작
- min_confidence 미달 시 hold
- 캔들/ticker 에러 시 hold
- 현물 전략 mock 시그널 주입
- close/atr indicators 추출
"""

import time
import pytest
import pandas as pd
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from engine.direction_evaluator import DirectionEvaluator
from engine.spot_long_evaluator import SpotLongEvaluator, _hold_decision
from engine.position_state_tracker import PositionState
from core.enums import Direction, SignalType
from exchange.data_models import Ticker
from strategies.base import Signal
from strategies.combiner import SignalCombiner, CombinedDecision


# ──── Helpers ────────────────────────────────────


def _make_ticker(symbol="BTC/USDT", last=80000.0):
    return Ticker(
        symbol=symbol,
        last=last,
        bid=last - 10,
        ask=last + 10,
        high=last + 1000,
        low=last - 1000,
        volume=1000.0,
        timestamp=datetime.now(timezone.utc),
    )


def _make_df_4h(n=50, close=80000.0, atr=1000.0):
    """4h 캔들 DataFrame 생성."""
    return pd.DataFrame(
        {
            "open": [close - 100] * n,
            "high": [close + 500] * n,
            "low": [close - 500] * n,
            "close": [close] * n,
            "volume": [1000.0] * n,
            "sma_20": [close - 200] * n,
            "sma_25": [close - 300] * n,
            "rsi_14": [50.0] * n,
            "atr_14": [atr] * n,
            "volume_sma_20": [800.0] * n,
        }
    )


def _make_df_5m(n=50, close=80000.0, atr=500.0):
    """5m 캔들 DataFrame 생성."""
    return pd.DataFrame(
        {
            "close": [close] * n,
            "atr_14": [atr] * n,
        }
    )


def _long_position(symbol="BTC/USDT"):
    return PositionState(
        symbol=symbol,
        direction=Direction.LONG,
        quantity=0.01,
        entry_price=80000.0,
        margin=100.0,
        leverage=3,
        extreme_price=80000.0,
        stop_loss_atr=5.0,
        take_profit_atr=14.0,
        trailing_activation_atr=3.0,
        trailing_stop_atr=1.5,
    )


def _short_position(symbol="BTC/USDT"):
    return PositionState(
        symbol=symbol,
        direction=Direction.SHORT,
        quantity=0.01,
        entry_price=80000.0,
        margin=100.0,
        leverage=3,
        extreme_price=80000.0,
        stop_loss_atr=5.0,
        take_profit_atr=14.0,
        trailing_activation_atr=3.0,
        trailing_stop_atr=1.5,
    )


def _buy_signal(name="cis_momentum", confidence=0.75):
    return Signal(
        signal_type=SignalType.BUY,
        confidence=confidence,
        strategy_name=name,
        reason=f"{name} buy signal",
    )


def _sell_signal(name="cis_momentum", confidence=0.70):
    return Signal(
        signal_type=SignalType.SELL,
        confidence=confidence,
        strategy_name=name,
        reason=f"{name} sell signal",
    )


def _hold_signal(name="cis_momentum"):
    return Signal(
        signal_type=SignalType.HOLD,
        confidence=0.0,
        strategy_name=name,
        reason=f"{name} hold",
    )


def _make_evaluator(
    strategies=None,
    combiner=None,
    market_data=None,
    eval_interval=300,
    min_confidence=0.50,
    cooldown_hours=60.0,
    sl_atr_mult=5.0,
    tp_atr_mult=14.0,
    trail_activation_atr_mult=3.0,
    trail_stop_atr_mult=1.5,
) -> SpotLongEvaluator:
    """테스트용 SpotLongEvaluator 생성."""
    if strategies is None:
        strategies = [MagicMock(name="strategy_mock")]
    if combiner is None:
        combiner = MagicMock(spec=SignalCombiner)
    if market_data is None:
        market_data = AsyncMock()
        market_data.get_ohlcv_df = AsyncMock(return_value=_make_df_4h())
        market_data.get_ticker = AsyncMock(return_value=_make_ticker())

    return SpotLongEvaluator(
        strategies=strategies,
        combiner=combiner,
        market_data=market_data,
        eval_interval=eval_interval,
        min_confidence=min_confidence,
        cooldown_hours=cooldown_hours,
        sl_atr_mult=sl_atr_mult,
        tp_atr_mult=tp_atr_mult,
        trail_activation_atr_mult=trail_activation_atr_mult,
        trail_stop_atr_mult=trail_stop_atr_mult,
    )


# ──── Protocol 호환성 테스트 ────────────────────


class TestSpotLongEvaluatorProtocol:
    """DirectionEvaluator 프로토콜 호환성 테스트."""

    def test_isinstance_direction_evaluator(self):
        evaluator = _make_evaluator()
        assert isinstance(evaluator, DirectionEvaluator)

    def test_eval_interval_sec_property(self):
        evaluator = _make_evaluator(eval_interval=300)
        assert evaluator.eval_interval_sec == 300

    def test_eval_interval_sec_custom(self):
        evaluator = _make_evaluator(eval_interval=600)
        assert evaluator.eval_interval_sec == 600


# ──── BUY → open LONG 매핑 테스트 ────────────────


class TestBuyToOpenLong:
    """BUY 시그널 → open LONG 매핑 테스트."""

    @pytest.mark.asyncio
    async def test_buy_signal_opens_long(self):
        """BUY 시그널 + 포지션 없음 → open LONG."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_buy_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.BUY,
                combined_confidence=0.75,
                contributing_signals=[_buy_signal()],
                final_reason="cis_momentum buy",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
        )

        decision = await evaluator.evaluate("BTC/USDT", None)

        assert decision.is_open
        assert decision.direction == Direction.LONG
        assert decision.confidence == 0.75
        assert decision.stop_loss_atr == 5.0
        assert decision.take_profit_atr == 14.0
        assert "spot_buy" in decision.reason

    @pytest.mark.asyncio
    async def test_buy_signal_confidence_as_sizing_factor(self):
        """sizing_factor는 combined_confidence와 같아야 한다."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_buy_signal(confidence=0.65))

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.BUY,
                combined_confidence=0.65,
                contributing_signals=[_buy_signal(confidence=0.65)],
                final_reason="buy",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
        )

        decision = await evaluator.evaluate("BTC/USDT", None)
        assert decision.sizing_factor == 0.65

    @pytest.mark.asyncio
    async def test_buy_signal_includes_close_atr_indicators(self):
        """open 결정에 close/atr 인디케이터가 포함되어야 한다."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_buy_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.BUY,
                combined_confidence=0.75,
                contributing_signals=[_buy_signal()],
                final_reason="buy",
            )
        )

        df_5m = _make_df_5m(close=81000.0, atr=600.0)
        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
        )

        decision = await evaluator.evaluate(
            "BTC/USDT",
            None,
            df_5m=df_5m,
        )

        assert decision.indicators["close"] == 81000.0
        assert decision.indicators["atr"] == 600.0


# ──── SELL → close LONG 매핑 테스트 ────────────────


class TestSellToCloseLong:
    """SELL 시그널 → close LONG 매핑 테스트."""

    @pytest.mark.asyncio
    async def test_sell_signal_closes_long(self):
        """SELL 시그널 + 롱 포지션 → close."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_sell_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.SELL,
                combined_confidence=0.70,
                contributing_signals=[_sell_signal()],
                final_reason="cis_momentum sell",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
        )

        decision = await evaluator.evaluate("BTC/USDT", _long_position())

        assert decision.is_close
        assert decision.direction is None
        assert decision.confidence == 0.70
        assert "spot_sell" in decision.reason

    @pytest.mark.asyncio
    async def test_sell_signal_below_min_confidence_holds(self):
        """SELL 시그널이 min_confidence 미달이면 hold."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_sell_signal(confidence=0.40))

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.SELL,
                combined_confidence=0.40,
                contributing_signals=[_sell_signal(confidence=0.40)],
                final_reason="weak sell",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
            min_confidence=0.50,
        )

        decision = await evaluator.evaluate("BTC/USDT", _long_position())

        assert decision.is_hold

    @pytest.mark.asyncio
    async def test_sell_signal_no_position_holds(self):
        """SELL 시그널 + 포지션 없음 → hold (숏 진입 안 함)."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_sell_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.SELL,
                combined_confidence=0.70,
                contributing_signals=[_sell_signal()],
                final_reason="sell",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
        )

        decision = await evaluator.evaluate("BTC/USDT", None)

        assert decision.is_hold
        assert "spot_long_no_action" in decision.reason


# ──── HOLD → hold 매핑 테스트 ────────────────────


class TestHoldMapping:
    """HOLD 시그널 → hold 매핑 테스트."""

    @pytest.mark.asyncio
    async def test_hold_signal_no_position(self):
        """HOLD 시그널 + 포지션 없음 → hold."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_hold_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.HOLD,
                combined_confidence=0.0,
                contributing_signals=[],
                final_reason="no clear signal",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
        )

        decision = await evaluator.evaluate("BTC/USDT", None)
        assert decision.is_hold

    @pytest.mark.asyncio
    async def test_hold_signal_with_long_position(self):
        """HOLD 시그널 + 롱 포지션 → hold (유지)."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_hold_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.HOLD,
                combined_confidence=0.0,
                contributing_signals=[],
                final_reason="hold",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
        )

        decision = await evaluator.evaluate("BTC/USDT", _long_position())
        assert decision.is_hold

    @pytest.mark.asyncio
    async def test_buy_signal_with_long_position_holds(self):
        """BUY 시그널 + 롱 포지션 보유 중 → hold (이미 롱 보유)."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_buy_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.BUY,
                combined_confidence=0.75,
                contributing_signals=[_buy_signal()],
                final_reason="buy",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
        )

        decision = await evaluator.evaluate("BTC/USDT", _long_position())
        assert decision.is_hold
        assert "spot_long_hold" in decision.reason


# ──── 쿨다운 테스트 ────────────────────────────


class TestCooldown:
    """쿨다운 동작 테스트."""

    @pytest.mark.asyncio
    async def test_cooldown_blocks_entry(self):
        """쿨다운 활성 시 BUY 시그널이 있어도 hold."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_buy_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.BUY,
                combined_confidence=0.75,
                contributing_signals=[_buy_signal()],
                final_reason="buy",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
            cooldown_hours=60.0,
        )

        # 쿨다운 설정
        evaluator.set_cooldown("BTC/USDT")

        decision = await evaluator.evaluate("BTC/USDT", None)

        assert decision.is_hold
        assert "spot_long_cooldown" in decision.reason

    @pytest.mark.asyncio
    async def test_cooldown_expired_allows_entry(self):
        """쿨다운 만료 후 BUY 시그널로 진입 가능."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_buy_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.BUY,
                combined_confidence=0.75,
                contributing_signals=[_buy_signal()],
                final_reason="buy",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
            cooldown_hours=60.0,
        )

        # 쿨다운을 과거로 설정 (만료됨)
        evaluator._cooldowns["BTC/USDT"] = time.time() - 300000  # 83h ago

        decision = await evaluator.evaluate("BTC/USDT", None)

        assert decision.is_open
        assert decision.direction == Direction.LONG

    @pytest.mark.asyncio
    async def test_cooldown_per_symbol(self):
        """쿨다운은 종목별 독립 동작."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_buy_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.BUY,
                combined_confidence=0.75,
                contributing_signals=[_buy_signal()],
                final_reason="buy",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
        )

        # BTC에만 쿨다운
        evaluator.set_cooldown("BTC/USDT")

        # BTC → hold (쿨다운)
        btc_decision = await evaluator.evaluate("BTC/USDT", None)
        assert btc_decision.is_hold

        # ETH → open (쿨다운 없음)
        eth_decision = await evaluator.evaluate("ETH/USDT", None)
        assert eth_decision.is_open

    def test_set_cooldown(self):
        """set_cooldown이 타임스탬프를 올바르게 설정한다."""
        evaluator = _make_evaluator()
        before = time.time()
        evaluator.set_cooldown("BTC/USDT")
        after = time.time()

        ts = evaluator._cooldowns["BTC/USDT"]
        assert before <= ts <= after


# ──── min_confidence 테스트 ────────────────────


class TestMinConfidence:
    """min_confidence 필터 테스트."""

    @pytest.mark.asyncio
    async def test_buy_below_min_confidence_holds(self):
        """BUY 시그널이 min_confidence 미달이면 hold."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_buy_signal(confidence=0.40))

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.BUY,
                combined_confidence=0.40,
                contributing_signals=[_buy_signal(confidence=0.40)],
                final_reason="weak buy",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
            min_confidence=0.50,
        )

        decision = await evaluator.evaluate("BTC/USDT", None)

        assert decision.is_hold
        assert "spot_long_no_action" in decision.reason

    @pytest.mark.asyncio
    async def test_buy_at_min_confidence_opens(self):
        """BUY 시그널이 min_confidence 이상이면 진입."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_buy_signal(confidence=0.50))

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.BUY,
                combined_confidence=0.50,
                contributing_signals=[_buy_signal(confidence=0.50)],
                final_reason="buy",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
            min_confidence=0.50,
        )

        decision = await evaluator.evaluate("BTC/USDT", None)

        assert decision.is_open


# ──── 에러 핸들링 테스트 ────────────────────────


class TestErrorHandling:
    """캔들/ticker 에러 시 hold 반환 테스트."""

    @pytest.mark.asyncio
    async def test_candle_error_returns_hold(self):
        """4h 캔들 조회 실패 → hold."""
        market_data = AsyncMock()
        market_data.get_ohlcv_df = AsyncMock(side_effect=Exception("API error"))

        evaluator = _make_evaluator(market_data=market_data)

        decision = await evaluator.evaluate("BTC/USDT", None)

        assert decision.is_hold
        assert "candle_error" in decision.reason

    @pytest.mark.asyncio
    async def test_insufficient_candles_returns_hold(self):
        """캔들 수가 30개 미만이면 hold."""
        market_data = AsyncMock()
        market_data.get_ohlcv_df = AsyncMock(return_value=_make_df_4h(n=10))

        evaluator = _make_evaluator(market_data=market_data)

        decision = await evaluator.evaluate("BTC/USDT", None)

        assert decision.is_hold
        assert "candle_error" in decision.reason

    @pytest.mark.asyncio
    async def test_ticker_error_returns_hold(self):
        """ticker 조회 실패 → hold."""
        market_data = AsyncMock()
        market_data.get_ohlcv_df = AsyncMock(return_value=_make_df_4h())
        market_data.get_ticker = AsyncMock(side_effect=Exception("Ticker error"))

        evaluator = _make_evaluator(market_data=market_data)

        decision = await evaluator.evaluate("BTC/USDT", None)

        assert decision.is_hold
        assert "ticker_error" in decision.reason

    @pytest.mark.asyncio
    async def test_strategy_error_skipped(self):
        """개별 전략 에러 시 다른 전략은 계속 실행."""
        failing_strategy = MagicMock()
        failing_strategy.name = "failing"
        failing_strategy.analyze = AsyncMock(side_effect=Exception("Strategy fail"))

        working_strategy = MagicMock()
        working_strategy.name = "cis_momentum"
        working_strategy.analyze = AsyncMock(return_value=_buy_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.BUY,
                combined_confidence=0.75,
                contributing_signals=[_buy_signal()],
                final_reason="buy",
            )
        )

        evaluator = _make_evaluator(
            strategies=[failing_strategy, working_strategy],
            combiner=combiner,
        )

        await evaluator.evaluate("BTC/USDT", None)

        # combiner.combine should be called with only 1 signal (the working one)
        combiner.combine.assert_called_once()
        call_signals = combiner.combine.call_args[0][0]
        assert len(call_signals) == 1

    @pytest.mark.asyncio
    async def test_all_strategies_fail_returns_hold(self):
        """모든 전략이 에러 → hold (no_signals)."""
        failing_strategy = MagicMock()
        failing_strategy.name = "failing"
        failing_strategy.analyze = AsyncMock(side_effect=Exception("fail"))

        evaluator = _make_evaluator(strategies=[failing_strategy])

        decision = await evaluator.evaluate("BTC/USDT", None)

        assert decision.is_hold
        assert "no_signals" in decision.reason


# ──── 현물 전략 mock 시그널 주입 테스트 ────────────


class TestMultiStrategySignals:
    """복수 전략 시그널 주입 테스트."""

    @pytest.mark.asyncio
    async def test_four_strategies_all_buy(self):
        """4전략 모두 BUY → 높은 신뢰도 진입."""
        strategies = []
        for name in [
            "cis_momentum",
            "bnf_deviation",
            "donchian_channel",
            "larry_williams",
        ]:
            s = MagicMock()
            s.name = name
            s.analyze = AsyncMock(return_value=_buy_signal(name=name, confidence=0.70))
            strategies.append(s)

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.BUY,
                combined_confidence=0.85,
                contributing_signals=[
                    _buy_signal(name=n, confidence=0.70)
                    for n in [
                        "cis_momentum",
                        "bnf_deviation",
                        "donchian_channel",
                        "larry_williams",
                    ]
                ],
                final_reason="4/4 strategies agree",
            )
        )

        evaluator = _make_evaluator(
            strategies=strategies,
            combiner=combiner,
        )

        decision = await evaluator.evaluate("BTC/USDT", None)

        assert decision.is_open
        assert decision.confidence == 0.85

        # combiner.combine에 4개 시그널이 전달됐는지 확인
        call_signals = combiner.combine.call_args[0][0]
        assert len(call_signals) == 4

    @pytest.mark.asyncio
    async def test_mixed_signals_combiner_decides(self):
        """일부 BUY/SELL/HOLD 혼합 → combiner 결정에 따름."""
        strategies = []
        signals = [
            ("cis_momentum", _buy_signal("cis_momentum", 0.75)),
            ("bnf_deviation", _sell_signal("bnf_deviation", 0.60)),
            ("donchian_channel", _hold_signal("donchian_channel")),
            ("larry_williams", _buy_signal("larry_williams", 0.65)),
        ]
        for name, signal in signals:
            s = MagicMock()
            s.name = name
            s.analyze = AsyncMock(return_value=signal)
            strategies.append(s)

        # combiner가 BUY 결정
        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.BUY,
                combined_confidence=0.65,
                contributing_signals=[
                    _buy_signal("cis_momentum", 0.75),
                    _buy_signal("larry_williams", 0.65),
                ],
                final_reason="2/3 buy consensus",
            )
        )

        evaluator = _make_evaluator(
            strategies=strategies,
            combiner=combiner,
        )

        decision = await evaluator.evaluate("BTC/USDT", None)

        assert decision.is_open
        assert decision.direction == Direction.LONG
        assert decision.confidence == 0.65


# ──── close/atr 추출 테스트 ────────────────────


class TestCloseAtrExtraction:
    """close/atr indicators 추출 테스트."""

    def test_extract_from_5m(self):
        """5m 캔들에서 close/atr 추출."""
        df_5m = _make_df_5m(close=81000.0, atr=600.0)
        df_4h = _make_df_4h(close=80000.0, atr=1000.0)

        close, atr = SpotLongEvaluator._extract_close_atr(df_5m, df_4h)

        assert close == 81000.0
        assert atr == 600.0

    def test_fallback_to_4h(self):
        """5m 없으면 4h에서 추출."""
        df_4h = _make_df_4h(close=80000.0, atr=1000.0)

        close, atr = SpotLongEvaluator._extract_close_atr(None, df_4h)

        assert close == 80000.0
        assert atr == 1000.0

    def test_both_none_returns_zeros(self):
        """둘 다 없으면 (0, 0)."""
        close, atr = SpotLongEvaluator._extract_close_atr(None, None)

        assert close == 0.0
        assert atr == 0.0

    def test_5m_missing_atr_falls_back(self):
        """5m에 atr가 없으면 4h fallback."""
        df_5m = pd.DataFrame({"close": [81000.0]})
        df_4h = _make_df_4h(close=80000.0, atr=1000.0)

        close, atr = SpotLongEvaluator._extract_close_atr(df_5m, df_4h)

        # 5m has close but no atr → falls through to 4h
        assert close == 80000.0
        assert atr == 1000.0


# ──── Short 포지션 처리 테스트 ────────────────────


class TestShortPositionHandling:
    """숏 포지션 보유 시 행동 테스트."""

    @pytest.mark.asyncio
    async def test_short_position_buy_signal_holds(self):
        """숏 포지션 보유 + BUY 시그널 → hold (롱 전용이므로)."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_buy_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.BUY,
                combined_confidence=0.75,
                contributing_signals=[_buy_signal()],
                final_reason="buy",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
        )

        decision = await evaluator.evaluate("BTC/USDT", _short_position())

        assert decision.is_hold
        assert "spot_long_no_action" in decision.reason

    @pytest.mark.asyncio
    async def test_short_position_sell_signal_holds(self):
        """숏 포지션 보유 + SELL 시그널 → hold (롱 이밸류에이터는 숏 관리 안 함)."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_sell_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.SELL,
                combined_confidence=0.70,
                contributing_signals=[_sell_signal()],
                final_reason="sell",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
        )

        decision = await evaluator.evaluate("BTC/USDT", _short_position())

        assert decision.is_hold


# ──── Config 파라미터 테스트 ────────────────────


class TestConfigParams:
    """설정 파라미터가 올바르게 전달되는지 테스트."""

    def test_sl_tp_params(self):
        evaluator = _make_evaluator(sl_atr_mult=5.0, tp_atr_mult=14.0)
        assert evaluator._sl_atr_mult == 5.0
        assert evaluator._tp_atr_mult == 14.0

    def test_cooldown_hours_to_seconds(self):
        evaluator = _make_evaluator(cooldown_hours=60.0)
        assert evaluator._cooldown_sec == 60.0 * 3600

    @pytest.mark.asyncio
    async def test_sl_tp_in_decision(self):
        """open 결정에 SL/TP 값이 전달된다."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_buy_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.BUY,
                combined_confidence=0.75,
                contributing_signals=[_buy_signal()],
                final_reason="buy",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
            sl_atr_mult=5.0,
            tp_atr_mult=14.0,
        )

        decision = await evaluator.evaluate("BTC/USDT", None)

        assert decision.stop_loss_atr == 5.0
        assert decision.take_profit_atr == 14.0


# ──── _hold_decision 헬퍼 테스트 ────────────────


class TestHoldDecisionHelper:
    """_hold_decision 헬퍼 함수 테스트."""

    def test_hold_decision_defaults(self):
        d = _hold_decision("reason", "strategy")
        assert d.is_hold
        assert d.direction is None
        assert d.confidence == 0.0
        assert d.sizing_factor == 0.0
        assert d.stop_loss_atr == 0.0
        assert d.take_profit_atr == 0.0
        assert d.reason == "reason"
        assert d.strategy_name == "strategy"
        assert d.indicators == {}

    def test_hold_decision_with_indicators(self):
        d = _hold_decision("reason", "strategy", {"close": 80000.0})
        assert d.indicators == {"close": 80000.0}


# ──── _top_strategy 테스트 ────────────────────


class TestTopStrategy:
    """_top_strategy 헬퍼 메서드 테스트."""

    def test_returns_highest_confidence_strategy(self):
        combined = CombinedDecision(
            action=SignalType.BUY,
            combined_confidence=0.75,
            contributing_signals=[
                _buy_signal("cis_momentum", 0.80),
                _buy_signal("bnf_deviation", 0.60),
            ],
            final_reason="buy",
        )

        result = SpotLongEvaluator._top_strategy(combined)
        assert result == "cis_momentum"

    def test_returns_default_when_no_signals(self):
        combined = CombinedDecision(
            action=SignalType.HOLD,
            combined_confidence=0.0,
            contributing_signals=[],
            final_reason="hold",
        )

        result = SpotLongEvaluator._top_strategy(combined)
        assert result == "spot_long"


# ──── FuturesV2Config 통합 테스트 ────────────────


class TestFuturesV2ConfigIntegration:
    """FuturesV2Config tier1_long_* 필드 테스트."""

    def test_default_config_values(self):
        from config import FuturesV2Config

        cfg = FuturesV2Config()
        assert cfg.tier1_long_eval_interval_sec == 300
        assert cfg.tier1_long_min_confidence == 0.50
        assert cfg.tier1_long_cooldown_hours == 60.0
        assert cfg.tier1_long_sl_atr_mult == 5.0
        assert cfg.tier1_long_tp_atr_mult == 14.0
        assert cfg.tier1_long_trail_activation_atr_mult == 3.0
        assert cfg.tier1_long_trail_stop_atr_mult == 1.5


# ──── 트레일링 파라미터 indicators 전달 테스트 ────────


class TestTrailingParamsInIndicators:
    """trailing_activation_atr / trailing_stop_atr가 indicators에 포함되는지 테스트."""

    @pytest.mark.asyncio
    async def test_open_decision_includes_trailing_params(self):
        """open 결정에 trailing 파라미터가 indicators에 포함되어야 한다."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_buy_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.BUY,
                combined_confidence=0.75,
                contributing_signals=[_buy_signal()],
                final_reason="buy",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
            trail_activation_atr_mult=3.0,
            trail_stop_atr_mult=1.5,
        )

        decision = await evaluator.evaluate("BTC/USDT", None)

        assert decision.is_open
        assert decision.indicators["trailing_activation_atr"] == 3.0
        assert decision.indicators["trailing_stop_atr"] == 1.5

    @pytest.mark.asyncio
    async def test_custom_trailing_params_propagated(self):
        """커스텀 trailing 값이 올바르게 전달된다."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_buy_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.BUY,
                combined_confidence=0.75,
                contributing_signals=[_buy_signal()],
                final_reason="buy",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
            trail_activation_atr_mult=5.0,
            trail_stop_atr_mult=2.5,
        )

        decision = await evaluator.evaluate("BTC/USDT", None)

        assert decision.indicators["trailing_activation_atr"] == 5.0
        assert decision.indicators["trailing_stop_atr"] == 2.5

    @pytest.mark.asyncio
    async def test_close_decision_no_trailing_params(self):
        """close 결정에는 trailing 파라미터가 불필요하다."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_sell_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.SELL,
                combined_confidence=0.70,
                contributing_signals=[_sell_signal()],
                final_reason="sell",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
        )

        decision = await evaluator.evaluate("BTC/USDT", _long_position())

        assert decision.is_close
        assert "trailing_activation_atr" not in decision.indicators
        assert "trailing_stop_atr" not in decision.indicators


# ──── 청산 시 자동 쿨다운 테스트 ────────────────────


class TestAutoSetCooldownOnClose:
    """청산 결정 시 자동으로 쿨다운이 설정되는지 테스트."""

    @pytest.mark.asyncio
    async def test_close_decision_auto_sets_cooldown(self):
        """SELL → close 결정 시 자동으로 쿨다운이 설정된다."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"
        strategy.analyze = AsyncMock(return_value=_sell_signal())

        combiner = MagicMock(spec=SignalCombiner)
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.SELL,
                combined_confidence=0.70,
                contributing_signals=[_sell_signal()],
                final_reason="sell",
            )
        )

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
            cooldown_hours=60.0,
        )

        # 청산 전 쿨다운 없음
        assert "BTC/USDT" not in evaluator._cooldowns

        # 청산 결정
        decision = await evaluator.evaluate("BTC/USDT", _long_position())
        assert decision.is_close

        # 자동 쿨다운 설정됨
        assert "BTC/USDT" in evaluator._cooldowns

    @pytest.mark.asyncio
    async def test_cooldown_blocks_reentry_after_close(self):
        """청산 후 쿨다운으로 즉시 재진입이 차단된다."""
        strategy = MagicMock()
        strategy.name = "cis_momentum"

        combiner = MagicMock(spec=SignalCombiner)

        evaluator = _make_evaluator(
            strategies=[strategy],
            combiner=combiner,
            cooldown_hours=60.0,
        )

        # 1단계: SELL 시그널로 청산
        strategy.analyze = AsyncMock(return_value=_sell_signal())
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.SELL,
                combined_confidence=0.70,
                contributing_signals=[_sell_signal()],
                final_reason="sell",
            )
        )

        close_decision = await evaluator.evaluate("BTC/USDT", _long_position())
        assert close_decision.is_close

        # 2단계: 즉시 BUY 시그널 — 쿨다운으로 차단되어야 함
        strategy.analyze = AsyncMock(return_value=_buy_signal())
        combiner.combine = MagicMock(
            return_value=CombinedDecision(
                action=SignalType.BUY,
                combined_confidence=0.75,
                contributing_signals=[_buy_signal()],
                final_reason="buy",
            )
        )

        reentry_decision = await evaluator.evaluate("BTC/USDT", None)
        assert reentry_decision.is_hold
        assert "spot_long_cooldown" in reentry_decision.reason


# ──── 4h fallback 유효성 검증 테스트 ────────────────


class TestFourHourFallbackValidation:
    """4h 캔들 fallback 경로의 close/atr 유효성 검증 테스트."""

    def test_4h_atr_zero_returns_zeros(self):
        """4h atr가 0이면 (0, 0)으로 fallthrough."""
        df_4h = pd.DataFrame({
            "close": [80000.0],
            "atr_14": [0.0],
        })

        close, atr = SpotLongEvaluator._extract_close_atr(None, df_4h)
        assert close == 0.0
        assert atr == 0.0

    def test_4h_close_zero_returns_zeros(self):
        """4h close가 0이면 (0, 0)으로 fallthrough."""
        df_4h = pd.DataFrame({
            "close": [0.0],
            "atr_14": [1000.0],
        })

        close, atr = SpotLongEvaluator._extract_close_atr(None, df_4h)
        assert close == 0.0
        assert atr == 0.0

    def test_4h_atr_nan_returns_zeros(self):
        """4h atr가 NaN이면 (0, 0)으로 fallthrough."""
        df_4h = pd.DataFrame({
            "close": [80000.0],
            "atr_14": [float("nan")],
        })

        close, atr = SpotLongEvaluator._extract_close_atr(None, df_4h)
        assert close == 0.0
        assert atr == 0.0

    def test_4h_valid_values_returned(self):
        """4h close와 atr 모두 유효하면 정상 반환."""
        df_4h = _make_df_4h(close=80000.0, atr=1000.0)

        close, atr = SpotLongEvaluator._extract_close_atr(None, df_4h)
        assert close == 80000.0
        assert atr == 1000.0


# ──── SignalCombiner + SPOT_WEIGHTS 통합 테스트 ────────


class TestSpotWeightsIntegration:
    """실제 SignalCombiner + SPOT_WEIGHTS를 사용한 통합 테스트.

    모든 4전략 이름이 SPOT_WEIGHTS 키와 일치하는지 검증.
    """

    @pytest.mark.asyncio
    async def test_real_combiner_with_spot_weights(self):
        """실제 SignalCombiner(SPOT_WEIGHTS)로 시그널 결합이 동작한다."""
        real_combiner = SignalCombiner(
            strategy_weights=SignalCombiner.SPOT_WEIGHTS.copy(),
            min_confidence=0.50,
            directional_weights=False,
            exchange_name="binance_futures",
        )

        # 4전략 이름이 SPOT_WEIGHTS 키와 일치해야 한다
        strategy_names = list(SignalCombiner.SPOT_WEIGHTS.keys())
        assert set(strategy_names) == {
            "cis_momentum", "bnf_deviation",
            "donchian_channel", "larry_williams",
        }

        # BUY 시그널 2개 + HOLD 2개 → BUY 결정
        signals = [
            Signal(
                signal_type=SignalType.BUY,
                confidence=0.80,
                strategy_name="cis_momentum",       # weight 0.42
                reason="cis buy",
            ),
            Signal(
                signal_type=SignalType.BUY,
                confidence=0.70,
                strategy_name="bnf_deviation",       # weight 0.25
                reason="bnf buy",
            ),
            Signal(
                signal_type=SignalType.HOLD,
                confidence=0.0,
                strategy_name="donchian_channel",    # weight 0.24
                reason="hold",
            ),
            Signal(
                signal_type=SignalType.HOLD,
                confidence=0.0,
                strategy_name="larry_williams",      # weight 0.10
                reason="hold",
            ),
        ]

        combined = real_combiner.combine(signals, symbol="BTC/USDT")

        assert combined.action == SignalType.BUY
        assert combined.combined_confidence >= 0.50

    @pytest.mark.asyncio
    async def test_real_combiner_evaluator_end_to_end(self):
        """SpotLongEvaluator + 실제 SignalCombiner로 end-to-end 동작 검증."""
        real_combiner = SignalCombiner(
            strategy_weights=SignalCombiner.SPOT_WEIGHTS.copy(),
            min_confidence=0.50,
            directional_weights=False,
            exchange_name="binance_futures",
        )

        # 4전략 모두 BUY → 강한 롱 진입 시그널
        strategies = []
        for name in ["cis_momentum", "bnf_deviation", "donchian_channel", "larry_williams"]:
            s = MagicMock()
            s.name = name
            s.analyze = AsyncMock(return_value=Signal(
                signal_type=SignalType.BUY,
                confidence=0.75,
                strategy_name=name,
                reason=f"{name} buy",
            ))
            strategies.append(s)

        evaluator = _make_evaluator(
            strategies=strategies,
            combiner=real_combiner,
        )

        decision = await evaluator.evaluate("BTC/USDT", None)

        assert decision.is_open
        assert decision.direction == Direction.LONG
        assert decision.confidence >= 0.50
