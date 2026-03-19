"""SAR(Stop And Reverse) + direction-specific cooldown 통합 테스트 (COIN-27)."""

import pytest
import time
import pandas as pd
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

from engine.tier1_manager import Tier1Manager
from engine.direction_evaluator import DirectionDecision
from engine.regime_detector import RegimeDetector, RegimeState
from engine.safe_order_pipeline import SafeOrderPipeline, OrderResponse
from engine.position_state_tracker import PositionStateTracker, PositionState
from engine.portfolio_manager import PortfolioManager
from core.enums import Direction, Regime


# ── helpers ──────────────────────────────────────────────────────


def _regime_state(regime=Regime.TRENDING_UP):
    return RegimeState(
        regime=regime,
        confidence=0.8,
        adx=30,
        bb_width=3.0,
        atr_pct=1.5,
        volume_ratio=1.2,
        trend_direction=1,
        timestamp=datetime.now(timezone.utc),
    )


def _make_df(n=50, close=80000.0, atr=1000.0):
    return pd.DataFrame(
        {
            "close": [close] * n,
            "atr_14": [atr] * n,
            "rsi_14": [40.0] * n,
            "ema_9": [81000.0] * n,
            "ema_21": [80000.0] * n,
            "ema_20": [80000.0] * n,
            "ema_50": [79000.0] * n,
            "bb_upper_20": [82000.0] * n,
            "bb_lower_20": [78000.0] * n,
            "bb_mid_20": [80000.0] * n,
            "volume": [1000.0] * n,
        }
    )


def _hold():
    return DirectionDecision(
        action="hold", direction=None, confidence=0.0,
        sizing_factor=0.0, stop_loss_atr=0.0, take_profit_atr=0.0,
        reason="no_signal", strategy_name="test",
    )


def _long_open(confidence=0.8):
    return DirectionDecision(
        action="open", direction=Direction.LONG, confidence=confidence,
        sizing_factor=0.7, stop_loss_atr=1.5, take_profit_atr=3.0,
        reason="long_signal", strategy_name="spot_long",
        indicators={"close": 80000.0, "atr": 1000.0},
    )


def _short_open(confidence=0.7):
    return DirectionDecision(
        action="open", direction=Direction.SHORT, confidence=confidence,
        sizing_factor=0.6, stop_loss_atr=1.5, take_profit_atr=3.0,
        reason="short_signal", strategy_name="regime_short",
        indicators={"close": 80000.0, "atr": 1000.0},
    )


def _close(strategy="test"):
    return DirectionDecision(
        action="close", direction=None, confidence=0.6,
        sizing_factor=0.5, stop_loss_atr=1.5, take_profit_atr=3.0,
        reason="exit_signal", strategy_name=strategy,
    )


class MockEvaluator:
    """단일 mock evaluator — 심볼별 결정 설정 가능."""

    def __init__(self, default=None):
        self._default = default or _hold()
        self._decisions: dict[str, DirectionDecision] = {}

    @property
    def eval_interval_sec(self) -> int:
        return 60

    async def evaluate(self, symbol, current_position, **kwargs):
        return self._decisions.get(symbol, self._default)

    def set(self, symbol, decision):
        self._decisions[symbol] = decision


@pytest.fixture
def deps():
    regime = RegimeDetector()
    regime._current = _regime_state()

    safe_order = AsyncMock(spec=SafeOrderPipeline)
    safe_order.execute_order = AsyncMock(
        return_value=OrderResponse(
            success=True, order_id=1,
            executed_price=80000.0, executed_quantity=0.01, fee=0.32,
        )
    )

    tracker = PositionStateTracker()
    pm = MagicMock(spec=PortfolioManager)
    pm.cash_balance = 500.0

    market_data = AsyncMock()
    market_data.get_ohlcv_df = AsyncMock(return_value=_make_df())
    market_data.get_current_price = AsyncMock(return_value=80000.0)

    long_eval = MockEvaluator()
    short_eval = MockEvaluator()

    return {
        "regime": regime,
        "safe_order": safe_order,
        "tracker": tracker,
        "pm": pm,
        "market_data": market_data,
        "long_eval": long_eval,
        "short_eval": short_eval,
    }


def _make_tier1(deps, long_cd=43200, short_cd=93600):
    """Tier1Manager with direction-specific cooldowns."""
    return Tier1Manager(
        coins=["BTC/USDT"],
        safe_order=deps["safe_order"],
        position_tracker=deps["tracker"],
        regime_detector=deps["regime"],
        portfolio_manager=deps["pm"],
        market_data=deps["market_data"],
        long_evaluator=deps["long_eval"],
        short_evaluator=deps["short_eval"],
        leverage=3,
        max_position_pct=0.15,
        long_cooldown_seconds=long_cd,    # 12h
        short_cooldown_seconds=short_cd,  # 26h
    )


def _inject_long_position(tracker, symbol="BTC/USDT"):
    """트래커에 롱 포지션 직접 주입."""
    state = PositionState(
        symbol=symbol, direction=Direction.LONG,
        quantity=0.01, entry_price=80000.0, margin=100.0,
        leverage=3, extreme_price=80000.0,
        stop_loss_atr=1.5, take_profit_atr=3.0,
        trailing_activation_atr=1.5, trailing_stop_atr=1.0,
        tier="tier1", strategy_name="spot_long",
        confidence=0.8, sizing_factor=0.7,
    )
    tracker.open_position(state)
    return state


def _inject_short_position(tracker, symbol="BTC/USDT"):
    """트래커에 숏 포지션 직접 주입."""
    state = PositionState(
        symbol=symbol, direction=Direction.SHORT,
        quantity=0.01, entry_price=80000.0, margin=100.0,
        leverage=3, extreme_price=80000.0,
        stop_loss_atr=1.5, take_profit_atr=3.0,
        trailing_activation_atr=1.5, trailing_stop_atr=1.0,
        tier="tier1", strategy_name="regime_short",
        confidence=0.7, sizing_factor=0.6,
    )
    tracker.open_position(state)
    return state


# ── SAR Transition Tests ─────────────────────────────────────────


class TestSARLongToShort:
    """LONG 보유 중 short evaluator가 open SHORT → SAR 실행."""

    @pytest.mark.asyncio
    async def test_sar_long_to_short(self, deps):
        """LONG + short open → close LONG + open SHORT."""
        tier1 = _make_tier1(deps)
        _inject_long_position(deps["tracker"])
        deps["long_eval"].set("BTC/USDT", _hold())  # long says hold
        deps["short_eval"].set("BTC/USDT", _short_open(0.7))  # short says open

        session = AsyncMock()
        outcome = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())
        assert outcome == "sar"

    @pytest.mark.asyncio
    async def test_sar_close_takes_priority(self, deps):
        """Long evaluator가 close → flat_close (SAR보다 우선)."""
        tier1 = _make_tier1(deps)
        _inject_long_position(deps["tracker"])
        deps["long_eval"].set("BTC/USDT", _close())  # long says close
        deps["short_eval"].set("BTC/USDT", _short_open(0.7))

        session = AsyncMock()
        outcome = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())
        assert outcome == "flat_close"

    @pytest.mark.asyncio
    async def test_sar_below_min_confidence_skipped(self, deps):
        """Short evaluator confidence가 min_confidence 미만 → SAR 안 함."""
        tier1 = _make_tier1(deps)
        tier1._min_confidence = 0.5
        _inject_long_position(deps["tracker"])
        deps["long_eval"].set("BTC/USDT", _hold())
        deps["short_eval"].set("BTC/USDT", _short_open(0.3))  # below min

        session = AsyncMock()
        outcome = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())
        assert outcome == "hold"

    @pytest.mark.asyncio
    async def test_sar_no_cooldown_set(self, deps):
        """SAR 전환은 쿨다운을 설정하지 않는다."""
        tier1 = _make_tier1(deps)
        _inject_long_position(deps["tracker"])
        deps["long_eval"].set("BTC/USDT", _hold())
        deps["short_eval"].set("BTC/USDT", _short_open(0.7))

        session = AsyncMock()
        await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())

        # SAR 후 쿨다운이 설정되지 않아야 함
        assert "BTC/USDT" not in tier1._last_exit_time


class TestSARShortToLong:
    """SHORT 보유 중 long evaluator가 open LONG → SAR 실행."""

    @pytest.mark.asyncio
    async def test_sar_short_to_long(self, deps):
        """SHORT + long open → close SHORT + open LONG."""
        tier1 = _make_tier1(deps)
        _inject_short_position(deps["tracker"])
        deps["short_eval"].set("BTC/USDT", _hold())  # short says hold
        deps["long_eval"].set("BTC/USDT", _long_open(0.8))  # long says open

        session = AsyncMock()
        outcome = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())
        assert outcome == "sar"

    @pytest.mark.asyncio
    async def test_sar_short_close_priority(self, deps):
        """Short evaluator가 close → flat_close (SAR보다 우선)."""
        tier1 = _make_tier1(deps)
        _inject_short_position(deps["tracker"])
        deps["short_eval"].set("BTC/USDT", _close())
        deps["long_eval"].set("BTC/USDT", _long_open(0.8))

        session = AsyncMock()
        outcome = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())
        assert outcome == "flat_close"

    @pytest.mark.asyncio
    async def test_sar_short_to_long_no_cooldown(self, deps):
        """SHORT→LONG SAR도 쿨다운 미설정."""
        tier1 = _make_tier1(deps)
        _inject_short_position(deps["tracker"])
        deps["short_eval"].set("BTC/USDT", _hold())
        deps["long_eval"].set("BTC/USDT", _long_open(0.8))

        session = AsyncMock()
        await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())
        assert "BTC/USDT" not in tier1._last_exit_time


class TestSAROpenFails:
    """SAR 시 open이 실패하면 (마진 부족 등)."""

    @pytest.mark.asyncio
    async def test_sar_open_fails_returns_flat_close(self, deps):
        """SAR open 실패 → close는 수행, 결과는 flat_close (포지션 flat)."""
        tier1 = _make_tier1(deps)
        _inject_long_position(deps["tracker"])
        deps["long_eval"].set("BTC/USDT", _hold())
        deps["short_eval"].set("BTC/USDT", _short_open(0.7))
        # open 실패: cash 0
        deps["pm"].cash_balance = 0

        session = AsyncMock()
        outcome = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())
        # _execute_sar: close succeeded, open failed → "flat_close"
        assert outcome == "flat_close"

    @pytest.mark.asyncio
    async def test_sar_close_fails_returns_hold(self, deps):
        """SAR close 실패 → 아무 변경 없음, hold 반환."""
        tier1 = _make_tier1(deps)
        _inject_long_position(deps["tracker"])
        deps["long_eval"].set("BTC/USDT", _hold())
        deps["short_eval"].set("BTC/USDT", _short_open(0.7))
        # close 실패: execute_order returns success=False
        deps["safe_order"].execute_order = AsyncMock(
            return_value=OrderResponse(
                success=False, order_id=0,
                executed_price=0, executed_quantity=0, fee=0,
            )
        )

        session = AsyncMock()
        outcome = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())
        # _execute_sar: close failed → None → falls through to "hold"
        assert outcome == "hold"
        # Position should still be tracked
        assert deps["tracker"].get("BTC/USDT") is not None


class TestSARGuardCondition:
    """SAR은 같은 방향 evaluator가 hold일 때만 발동."""

    @pytest.mark.asyncio
    async def test_both_hold_no_sar(self, deps):
        """양쪽 evaluator 모두 hold → SAR 안 함."""
        tier1 = _make_tier1(deps)
        _inject_long_position(deps["tracker"])
        deps["long_eval"].set("BTC/USDT", _hold())
        deps["short_eval"].set("BTC/USDT", _hold())

        session = AsyncMock()
        outcome = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())
        assert outcome == "hold"

    @pytest.mark.asyncio
    async def test_sar_skipped_when_long_evaluator_says_open(self, deps):
        """LONG 보유 + long_eval=open(bullish) + short_eval=open → SAR 안 함.

        같은 방향 evaluator가 여전히 진입 시그널을 보내고 있으면
        반대 evaluator의 시그널과 관계없이 포지션 유지.
        """
        tier1 = _make_tier1(deps)
        _inject_long_position(deps["tracker"])
        deps["long_eval"].set("BTC/USDT", _long_open(0.8))  # still bullish
        deps["short_eval"].set("BTC/USDT", _short_open(0.7))

        session = AsyncMock()
        outcome = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())
        assert outcome == "hold"  # Should NOT SAR
        # Position should still be intact
        assert deps["tracker"].get("BTC/USDT") is not None

    @pytest.mark.asyncio
    async def test_sar_skipped_when_short_evaluator_says_open(self, deps):
        """SHORT 보유 + short_eval=open(bearish) + long_eval=open → SAR 안 함.

        같은 방향 evaluator가 여전히 진입 시그널을 보내고 있으면
        반대 evaluator의 시그널과 관계없이 포지션 유지.
        """
        tier1 = _make_tier1(deps)
        _inject_short_position(deps["tracker"])
        deps["short_eval"].set("BTC/USDT", _short_open(0.7))  # still bearish
        deps["long_eval"].set("BTC/USDT", _long_open(0.8))

        session = AsyncMock()
        outcome = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())
        assert outcome == "hold"  # Should NOT SAR
        # Position should still be intact
        assert deps["tracker"].get("BTC/USDT") is not None


# ── Direction-Specific Cooldown Tests ────────────────────────────


class TestDirectionalCooldown:
    """방향별 쿨다운: SL/TP 후 같은 방향 재진입만 차단."""

    @pytest.mark.asyncio
    async def test_long_sl_blocks_long_reentry(self, deps):
        """롱 SL → 롱 재진입 차단."""
        tier1 = _make_tier1(deps, long_cd=43200, short_cd=93600)
        tier1._set_exit_cooldown("BTC/USDT", Direction.LONG)

        assert tier1._in_cooldown("BTC/USDT", Direction.LONG) is True

    @pytest.mark.asyncio
    async def test_long_sl_allows_short_entry(self, deps):
        """롱 SL → 숏 진입 허용."""
        tier1 = _make_tier1(deps)
        tier1._set_exit_cooldown("BTC/USDT", Direction.LONG)

        assert tier1._in_cooldown("BTC/USDT", Direction.SHORT) is False

    @pytest.mark.asyncio
    async def test_short_sl_blocks_short_reentry(self, deps):
        """숏 SL → 숏 재진입 차단."""
        tier1 = _make_tier1(deps)
        tier1._set_exit_cooldown("BTC/USDT", Direction.SHORT)

        assert tier1._in_cooldown("BTC/USDT", Direction.SHORT) is True

    @pytest.mark.asyncio
    async def test_short_sl_allows_long_entry(self, deps):
        """숏 SL → 롱 진입 허용."""
        tier1 = _make_tier1(deps)
        tier1._set_exit_cooldown("BTC/USDT", Direction.SHORT)

        assert tier1._in_cooldown("BTC/USDT", Direction.LONG) is False

    def test_long_cooldown_duration(self, deps):
        """롱 쿨다운 12h = 43200초."""
        tier1 = _make_tier1(deps, long_cd=43200, short_cd=93600)
        tier1._last_exit_time["BTC/USDT"] = time.time() - 43201  # 12h + 1s 경과
        tier1._last_exit_direction["BTC/USDT"] = Direction.LONG

        assert tier1._in_cooldown("BTC/USDT", Direction.LONG) is False

    def test_short_cooldown_duration(self, deps):
        """숏 쿨다운 26h = 93600초."""
        tier1 = _make_tier1(deps, long_cd=43200, short_cd=93600)
        tier1._last_exit_time["BTC/USDT"] = time.time() - 50000  # ~14h 경과 (아직 26h 미만)
        tier1._last_exit_direction["BTC/USDT"] = Direction.SHORT

        assert tier1._in_cooldown("BTC/USDT", Direction.SHORT) is True

    def test_short_cooldown_expired(self, deps):
        """숏 쿨다운 26h 경과 후 해제."""
        tier1 = _make_tier1(deps, long_cd=43200, short_cd=93600)
        tier1._last_exit_time["BTC/USDT"] = time.time() - 93601  # 26h + 1s 경과
        tier1._last_exit_direction["BTC/USDT"] = Direction.SHORT

        assert tier1._in_cooldown("BTC/USDT", Direction.SHORT) is False

    def test_no_exit_no_cooldown(self, deps):
        """exit 기록 없으면 쿨다운 없음."""
        tier1 = _make_tier1(deps)
        assert tier1._in_cooldown("BTC/USDT", Direction.LONG) is False
        assert tier1._in_cooldown("BTC/USDT", Direction.SHORT) is False

    def test_none_direction_blocks_any(self, deps):
        """entry_direction=None → exit 방향 무시, 무조건 체크."""
        tier1 = _make_tier1(deps)
        tier1._set_exit_cooldown("BTC/USDT", Direction.LONG)

        assert tier1._in_cooldown("BTC/USDT", None) is True

    def test_unknown_exit_dir_uses_max_cooldown(self, deps):
        """exit_dir 불명 시 max(long, short) 쿨다운 적용 (보수적)."""
        tier1 = _make_tier1(deps, long_cd=43200, short_cd=93600)
        # exit_dir 없이 exit_time만 설정 (edge case)
        tier1._last_exit_time["BTC/USDT"] = time.time() - 50000  # ~14h
        # exit_direction 미설정 → fallback to max(43200, 93600) = 93600
        # 50000 < 93600 → 쿨다운 중
        assert tier1._in_cooldown("BTC/USDT", Direction.LONG) is True

        # 93601초 경과 → 해제
        tier1._last_exit_time["BTC/USDT"] = time.time() - 93601
        assert tier1._in_cooldown("BTC/USDT", Direction.LONG) is False


class TestSetExitCooldown:
    """_set_exit_cooldown 동작 검증."""

    def test_records_time_and_direction(self, deps):
        tier1 = _make_tier1(deps)
        before = time.time()
        tier1._set_exit_cooldown("ETH/USDT", Direction.SHORT)
        after = time.time()

        assert before <= tier1._last_exit_time["ETH/USDT"] <= after
        assert tier1._last_exit_direction["ETH/USDT"] == Direction.SHORT


class TestSLTPSetsDirectionalCooldown:
    """SL/TP 히트 시 방향별 쿨다운 설정."""

    @pytest.mark.asyncio
    async def test_sl_sets_long_direction(self, deps):
        """LONG SL → exit_direction=LONG 기록."""
        tier1 = _make_tier1(deps)
        pos = _inject_long_position(deps["tracker"])
        # SL 트리거: close가 entry - SL*ATR 이하
        sl_price = pos.entry_price - (pos.stop_loss_atr * 1000.0) - 1
        deps["market_data"].get_ohlcv_df = AsyncMock(
            return_value=_make_df(close=sl_price)
        )
        deps["market_data"].get_current_price = AsyncMock(return_value=sl_price)
        deps["long_eval"].set("BTC/USDT", _hold())
        deps["short_eval"].set("BTC/USDT", _hold())

        session = AsyncMock()
        outcome = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())
        assert outcome == "sl_tp"
        assert tier1._last_exit_direction.get("BTC/USDT") == Direction.LONG

    @pytest.mark.asyncio
    async def test_sl_sets_short_direction(self, deps):
        """SHORT SL → exit_direction=SHORT 기록."""
        tier1 = _make_tier1(deps)
        pos = _inject_short_position(deps["tracker"])
        # SHORT SL 트리거: close가 entry + SL*ATR 이상
        sl_price = pos.entry_price + (pos.stop_loss_atr * 1000.0) + 1
        deps["market_data"].get_ohlcv_df = AsyncMock(
            return_value=_make_df(close=sl_price)
        )
        deps["market_data"].get_current_price = AsyncMock(return_value=sl_price)
        deps["short_eval"].set("BTC/USDT", _hold())
        deps["long_eval"].set("BTC/USDT", _hold())

        session = AsyncMock()
        outcome = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())
        assert outcome == "sl_tp"
        assert tier1._last_exit_direction.get("BTC/USDT") == Direction.SHORT


# ── Config Default Tests ─────────────────────────────────────────


class TestConfigDefaults:
    """FuturesV2Config direction-specific cooldown 기본값."""

    def test_config_has_sl_cooldown_fields(self):
        from config import FuturesV2Config
        cfg = FuturesV2Config()
        assert cfg.tier1_sl_long_cooldown_hours == 12.0
        assert cfg.tier1_sl_short_cooldown_hours == 26.0

    def test_cooldown_seconds_conversion(self):
        from config import FuturesV2Config
        cfg = FuturesV2Config()
        assert int(cfg.tier1_sl_long_cooldown_hours * 3600) == 43200
        assert int(cfg.tier1_sl_short_cooldown_hours * 3600) == 93600


# ── Integration: SAR + Cooldown Combined ─────────────────────────


class TestSARCooldownIntegration:
    """SAR과 쿨다운의 상호작용."""

    @pytest.mark.asyncio
    async def test_sar_then_immediate_reentry_allowed(self, deps):
        """SAR 전환 후 즉시 같은 방향 재진입 가능 (SAR은 쿨다운 면제)."""
        tier1 = _make_tier1(deps)
        _inject_long_position(deps["tracker"])
        deps["long_eval"].set("BTC/USDT", _hold())
        deps["short_eval"].set("BTC/USDT", _short_open(0.7))

        session = AsyncMock()
        outcome = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())
        assert outcome == "sar"

        # SAR 후 쿨다운 없으므로 롱 재진입도 즉시 가능
        assert tier1._in_cooldown("BTC/USDT", Direction.LONG) is False
        assert tier1._in_cooldown("BTC/USDT", Direction.SHORT) is False

    @pytest.mark.asyncio
    async def test_flat_close_sets_cooldown_blocks_same_direction(self, deps):
        """flat_close 후 같은 방향 쿨다운 → 재진입 차단."""
        tier1 = _make_tier1(deps)
        _inject_long_position(deps["tracker"])
        deps["long_eval"].set("BTC/USDT", _close())

        session = AsyncMock()
        outcome = await tier1._evaluate_coin(session, "BTC/USDT", _regime_state())
        assert outcome == "flat_close"

        # 롱 쿨다운 설정됨
        assert tier1._in_cooldown("BTC/USDT", Direction.LONG) is True
        # 숏은 허용
        assert tier1._in_cooldown("BTC/USDT", Direction.SHORT) is False


class TestCycleWithSAR:
    """evaluation_cycle 내에서 SAR 결과 통계."""

    @pytest.mark.asyncio
    async def test_sar_counted_as_executed(self, deps):
        """SAR 결과는 executed_count에 포함."""
        tier1 = _make_tier1(deps)
        _inject_long_position(deps["tracker"])
        deps["long_eval"].set("BTC/USDT", _hold())
        deps["short_eval"].set("BTC/USDT", _short_open(0.7))

        session = AsyncMock()
        stats = await tier1.evaluation_cycle(session)

        assert stats.executed_count >= 1
        assert stats.decisions.get("BTC/USDT") == "sar"


# ── Backward Compatibility ───────────────────────────────────────


class TestBackwardCompatibility:
    """long_cooldown_seconds / short_cooldown_seconds 미지정 시 fallback."""

    def test_fallback_to_single_cooldown(self, deps):
        """direction-specific 미지정 → cooldown_seconds로 fallback."""
        tier1 = Tier1Manager(
            coins=["BTC/USDT"],
            safe_order=deps["safe_order"],
            position_tracker=deps["tracker"],
            regime_detector=deps["regime"],
            portfolio_manager=deps["pm"],
            market_data=deps["market_data"],
            long_evaluator=deps["long_eval"],
            short_evaluator=deps["short_eval"],
            cooldown_seconds=7200,  # 2h
        )
        assert tier1._long_cooldown_sec == 7200
        assert tier1._short_cooldown_sec == 7200

    def test_partial_override(self, deps):
        """한쪽만 지정하면 나머지는 fallback."""
        tier1 = Tier1Manager(
            coins=["BTC/USDT"],
            safe_order=deps["safe_order"],
            position_tracker=deps["tracker"],
            regime_detector=deps["regime"],
            portfolio_manager=deps["pm"],
            market_data=deps["market_data"],
            long_evaluator=deps["long_eval"],
            short_evaluator=deps["short_eval"],
            cooldown_seconds=7200,
            long_cooldown_seconds=3600,
        )
        assert tier1._long_cooldown_sec == 3600
        assert tier1._short_cooldown_sec == 7200  # fallback
