"""V2 포지션 관리 안전장치 테스트 (COIN-43).

5가지 누락 항목 검증:
1. Paired exit (전략 잠금 청산)
2. 교차 거래소 포지션 충돌 감지
3. 셧다운 포지션 경고
4. SL 이벤트 스팸 방지
5. Tier1 최대 보유 시간
"""

import pytest
import pytest_asyncio
import pandas as pd
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from engine.tier1_manager import Tier1Manager
from engine.direction_evaluator import DirectionDecision
from engine.regime_detector import RegimeDetector, RegimeState
from engine.safe_order_pipeline import SafeOrderPipeline, OrderResponse
from engine.position_state_tracker import PositionStateTracker, PositionState
from engine.portfolio_manager import PortfolioManager
from core.enums import Direction, Regime
from core.models import Position


# ── 세션 팩토리 모킹 ────────────────────────────────


class _MockSessionCM:
    """async context manager wrapper for mock session."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        return False


def _patch_session_factory(mock_gsf, mock_session):
    """Configure get_session_factory mock for pattern: sf = gsf(); async with sf() as s.

    mock_gsf = patched get_session_factory
    mock_session = the session object to yield
    """
    # sf = get_session_factory() → sf is mock_gsf.return_value (a MagicMock)
    # async with sf() as session → sf() is mock_gsf.return_value.return_value
    mock_gsf.return_value.return_value = _MockSessionCM(mock_session)


# ── 헬퍼 ────────────────────────────────────────


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
            "ema_9": [81000.0] * n,
            "ema_21": [80000.0] * n,
            "rsi_14": [40.0] * n,
            "atr_14": [atr] * n,
            "ema_20": [80000.0] * n,
            "ema_50": [79000.0] * n,
            "bb_upper_20": [82000.0] * n,
            "bb_lower_20": [78000.0] * n,
            "bb_mid_20": [80000.0] * n,
            "volume": [1000.0] * n,
        }
    )


def _hold_decision(strategy_name="test_strategy"):
    return DirectionDecision(
        action="hold",
        direction=None,
        confidence=0.0,
        sizing_factor=0.0,
        stop_loss_atr=0.0,
        take_profit_atr=0.0,
        reason="no_signal",
        strategy_name=strategy_name,
    )


def _long_open_decision(confidence=0.8, indicators=None):
    return DirectionDecision(
        action="open",
        direction=Direction.LONG,
        confidence=confidence,
        sizing_factor=0.7,
        stop_loss_atr=1.5,
        take_profit_atr=3.0,
        reason="long_signal",
        strategy_name="spot_eval_long",
        indicators=indicators or {"close": 80000.0, "atr": 1000.0},
    )


def _short_open_decision(confidence=0.7, indicators=None):
    return DirectionDecision(
        action="open",
        direction=Direction.SHORT,
        confidence=confidence,
        sizing_factor=0.6,
        stop_loss_atr=1.5,
        take_profit_atr=3.0,
        reason="short_signal",
        strategy_name="spot_eval_short",
        indicators=indicators or {"close": 80000.0, "atr": 1000.0},
    )


def _close_decision(strategy_name="spot_eval_long"):
    return DirectionDecision(
        action="close",
        direction=None,
        confidence=0.6,
        sizing_factor=0.5,
        stop_loss_atr=1.5,
        take_profit_atr=3.0,
        reason="exit_signal",
        strategy_name=strategy_name,
    )


class MockEvaluator:
    """테스트용 이밸류에이터."""

    def __init__(self, default_decision=None):
        self._default = default_decision or _hold_decision("test_eval")
        self._decisions: dict[str, DirectionDecision] = {}
        self.call_count = 0

    @property
    def eval_interval_sec(self) -> int:
        return 60

    async def evaluate(self, symbol, current_position, **kwargs):
        self.call_count += 1
        return self._decisions.get(symbol, self._default)

    def set_decision(self, symbol, decision):
        self._decisions[symbol] = decision


def _make_position_state(
    symbol="BTC/USDT",
    direction=Direction.LONG,
    entry_price=80000.0,
    quantity=0.01,
    margin=50.0,
    entered_at=None,
    strategy_name="spot_eval_long",
    **kwargs,
):
    return PositionState(
        symbol=symbol,
        direction=direction,
        quantity=quantity,
        entry_price=entry_price,
        margin=margin,
        leverage=3,
        extreme_price=entry_price,
        stop_loss_atr=kwargs.get("stop_loss_atr", 5.0),
        take_profit_atr=kwargs.get("take_profit_atr", 14.0),
        trailing_activation_atr=kwargs.get("trailing_activation_atr", 3.0),
        trailing_stop_atr=kwargs.get("trailing_stop_atr", 1.5),
        trailing_active=kwargs.get("trailing_active", False),
        entered_at=entered_at or datetime.now(timezone.utc),
        tier="tier1",
        strategy_name=strategy_name,
        confidence=0.7,
    )


@pytest.fixture
def mock_deps():
    regime = RegimeDetector()
    regime._current = _regime_state()

    safe_order = AsyncMock(spec=SafeOrderPipeline)
    safe_order.execute_order = AsyncMock(
        return_value=OrderResponse(
            success=True,
            order_id=1,
            executed_price=80000.0,
            executed_quantity=0.01,
            fee=0.32,
        )
    )

    tracker = PositionStateTracker()
    pm = MagicMock(spec=PortfolioManager)
    pm.cash_balance = 500.0

    market_data = AsyncMock()
    market_data.get_ohlcv_df = AsyncMock(return_value=_make_df())
    market_data.get_current_price = AsyncMock(return_value=80000.0)

    long_eval = MockEvaluator(_hold_decision("long_eval"))
    short_eval = MockEvaluator(_hold_decision("short_eval"))

    return {
        "regime": regime,
        "safe_order": safe_order,
        "tracker": tracker,
        "pm": pm,
        "market_data": market_data,
        "long_eval": long_eval,
        "short_eval": short_eval,
    }


def _make_tier1(mock_deps, **overrides):
    kwargs = dict(
        coins=["BTC/USDT", "ETH/USDT"],
        safe_order=mock_deps["safe_order"],
        position_tracker=mock_deps["tracker"],
        regime_detector=mock_deps["regime"],
        portfolio_manager=mock_deps["pm"],
        market_data=mock_deps["market_data"],
        long_evaluator=mock_deps["long_eval"],
        short_evaluator=mock_deps["short_eval"],
        leverage=3,
        max_position_pct=0.15,
        min_confidence=0.4,
    )
    kwargs.update(overrides)
    return Tier1Manager(**kwargs)


# ════════════════════════════════════════════════════════════════
# 1. Paired Exit (전략 잠금 청산)
# ════════════════════════════════════════════════════════════════


class TestPairedExit:
    """V2 paired exit: 진입 방향 evaluator만 청산 시그널 생성."""

    @pytest.mark.asyncio
    async def test_long_close_only_from_long_evaluator(self, mock_deps):
        """LONG 포지션은 long_evaluator의 close 시그널로만 청산됨."""
        tier1 = _make_tier1(mock_deps)
        session = AsyncMock()

        # LONG 포지션 보유
        state = _make_position_state(strategy_name="spot_eval_long")
        tier1._positions.open_position(state)

        # long_evaluator → close 시그널
        mock_deps["long_eval"].set_decision(
            "BTC/USDT", _close_decision("spot_eval_long")
        )
        # short_evaluator → open 시그널 (이건 SAR이 아닌 한 무시됨)
        mock_deps["short_eval"].set_decision("BTC/USDT", _short_open_decision())

        regime = _regime_state()
        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            outcome = await tier1._evaluate_coin(session, "BTC/USDT", regime)

        # long_evaluator의 close가 적용되어 flat_close
        assert outcome == "flat_close"

    @pytest.mark.asyncio
    async def test_short_close_only_from_short_evaluator(self, mock_deps):
        """SHORT 포지션은 short_evaluator의 close 시그널로만 청산됨."""
        tier1 = _make_tier1(mock_deps)
        session = AsyncMock()

        state = _make_position_state(
            direction=Direction.SHORT, strategy_name="spot_eval_short"
        )
        tier1._positions.open_position(state)

        mock_deps["short_eval"].set_decision(
            "BTC/USDT", _close_decision("spot_eval_short")
        )
        mock_deps["long_eval"].set_decision("BTC/USDT", _long_open_decision())

        regime = _regime_state()
        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            outcome = await tier1._evaluate_coin(session, "BTC/USDT", regime)

        assert outcome == "flat_close"

    @pytest.mark.asyncio
    async def test_opposite_evaluator_hold_keeps_position(self, mock_deps):
        """반대 방향 evaluator가 close를 내도 SAR이 아니면 포지션 유지."""
        tier1 = _make_tier1(mock_deps)
        session = AsyncMock()

        state = _make_position_state(strategy_name="spot_eval_long")
        tier1._positions.open_position(state)

        # long_evaluator → hold (close 아님)
        mock_deps["long_eval"].set_decision("BTC/USDT", _hold_decision("long_eval"))

        regime = _regime_state()
        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            outcome = await tier1._evaluate_coin(session, "BTC/USDT", regime)

        assert outcome == "hold"
        # 포지션 유지
        assert tier1._positions.has_position("BTC/USDT")


# ════════════════════════════════════════════════════════════════
# 2. 교차 거래소 포지션 충돌 감지
# ════════════════════════════════════════════════════════════════


class TestCrossExchangeConflict:
    """선물 숏 진입 전 현물 롱 확인."""

    @pytest.mark.asyncio
    async def test_no_checker_allows_short(self, mock_deps):
        """cross_exchange_checker 없으면 숏 정상 진행."""
        tier1 = _make_tier1(mock_deps, cross_exchange_checker=None)
        session = AsyncMock()

        mock_deps["short_eval"].set_decision(
            "BTC/USDT", _short_open_decision(confidence=0.8)
        )
        mock_deps["long_eval"].set_decision("BTC/USDT", _hold_decision("long_eval"))

        regime = _regime_state()
        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            outcome = await tier1._evaluate_coin(session, "BTC/USDT", regime)

        assert outcome == "opened"

    @pytest.mark.asyncio
    async def test_no_cross_position_allows_short(self, mock_deps):
        """교차 포지션 없으면 숏 정상 진행."""
        checker = AsyncMock(return_value=None)  # None = 교차 포지션 없음
        tier1 = _make_tier1(mock_deps, cross_exchange_checker=checker)
        session = AsyncMock()

        mock_deps["short_eval"].set_decision(
            "BTC/USDT", _short_open_decision(confidence=0.8)
        )
        mock_deps["long_eval"].set_decision("BTC/USDT", _hold_decision("long_eval"))

        regime = _regime_state()
        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            outcome = await tier1._evaluate_coin(session, "BTC/USDT", regime)

        checker.assert_called_once_with("BTC/USDT", 0.8)
        assert outcome == "opened"

    @pytest.mark.asyncio
    async def test_cross_position_blocked_low_confidence(self, mock_deps):
        """교차 포지션 있고 낮은 신뢰도 → 숏 차단."""
        checker = AsyncMock(return_value=False)  # False = 차단
        tier1 = _make_tier1(mock_deps, cross_exchange_checker=checker)
        session = AsyncMock()

        mock_deps["short_eval"].set_decision(
            "BTC/USDT", _short_open_decision(confidence=0.5)
        )
        mock_deps["long_eval"].set_decision("BTC/USDT", _hold_decision("long_eval"))

        regime = _regime_state()
        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            outcome = await tier1._evaluate_coin(session, "BTC/USDT", regime)

        assert outcome == "cross_exchange_blocked"
        # 포지션 미오픈
        assert not tier1._positions.has_position("BTC/USDT")

    @pytest.mark.asyncio
    async def test_cross_position_flipped_high_confidence(self, mock_deps):
        """교차 포지션 있고 높은 신뢰도 → 현물 청산 후 숏 진행."""
        checker = AsyncMock(return_value=True)  # True = 교차 포지션 청산 성공
        tier1 = _make_tier1(mock_deps, cross_exchange_checker=checker)
        session = AsyncMock()

        mock_deps["short_eval"].set_decision(
            "BTC/USDT", _short_open_decision(confidence=0.8)
        )
        mock_deps["long_eval"].set_decision("BTC/USDT", _hold_decision("long_eval"))

        regime = _regime_state()
        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            outcome = await tier1._evaluate_coin(session, "BTC/USDT", regime)

        checker.assert_called_once_with("BTC/USDT", 0.8)
        assert outcome == "opened"

    @pytest.mark.asyncio
    async def test_cross_check_not_called_for_long(self, mock_deps):
        """LONG 진입 시 교차 거래소 체크 호출 안 됨."""
        checker = AsyncMock(return_value=None)
        tier1 = _make_tier1(mock_deps, cross_exchange_checker=checker)
        session = AsyncMock()

        mock_deps["long_eval"].set_decision(
            "BTC/USDT", _long_open_decision(confidence=0.8)
        )
        mock_deps["short_eval"].set_decision("BTC/USDT", _hold_decision("short_eval"))

        regime = _regime_state()
        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            outcome = await tier1._evaluate_coin(session, "BTC/USDT", regime)

        assert outcome == "opened"
        checker.assert_not_called()  # LONG이므로 교차 체크 안 함

    @pytest.mark.asyncio
    async def test_cross_check_error_graceful(self, mock_deps):
        """교차 거래소 체크 에러 시 숏 진행 (graceful degradation)."""
        checker = AsyncMock(side_effect=Exception("DB error"))
        tier1 = _make_tier1(mock_deps, cross_exchange_checker=checker)
        session = AsyncMock()

        mock_deps["short_eval"].set_decision(
            "BTC/USDT", _short_open_decision(confidence=0.8)
        )
        mock_deps["long_eval"].set_decision("BTC/USDT", _hold_decision("long_eval"))

        regime = _regime_state()
        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            outcome = await tier1._evaluate_coin(session, "BTC/USDT", regime)

        assert outcome == "opened"

    @pytest.mark.asyncio
    async def test_cross_exchange_blocked_counted_in_stats(self, mock_deps):
        """교차 거래소 차단이 CycleStats에 반영됨."""
        checker = AsyncMock(return_value=False)
        tier1 = _make_tier1(mock_deps, cross_exchange_checker=checker)
        session = AsyncMock()

        # short_eval만 시그널
        mock_deps["short_eval"].set_decision(
            "BTC/USDT", _short_open_decision(confidence=0.5)
        )
        mock_deps["long_eval"].set_decision("BTC/USDT", _hold_decision("long_eval"))
        # ETH는 hold
        mock_deps["short_eval"].set_decision("ETH/USDT", _hold_decision("short_eval"))
        mock_deps["long_eval"].set_decision("ETH/USDT", _hold_decision("long_eval"))

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            stats = await tier1.evaluation_cycle(session)

        # BTC: cross_exchange_blocked (hold_count에 포함)
        assert stats.decisions.get("BTC/USDT") == "cross_exchange_blocked"
        assert stats.hold_count >= 1  # ETH hold + BTC blocked


# ════════════════════════════════════════════════════════════════
# 3. 셧다운 포지션 경고
# ════════════════════════════════════════════════════════════════


class TestShutdownPositionWarning:
    """FuturesEngineV2.stop() 시 보유 포지션 PnL 로깅 + 이벤트."""

    @pytest.mark.asyncio
    async def test_log_shutdown_positions_with_open_positions(self):
        """포지션 보유 중 stop() → 포지션 경고 로깅."""
        from engine.futures_engine_v2 import FuturesEngineV2

        mock_pos = MagicMock(spec=Position)
        mock_pos.symbol = "BTC/USDT"
        mock_pos.direction = "long"
        mock_pos.quantity = 0.01
        mock_pos.average_buy_price = 80000.0
        mock_pos.leverage = 3

        market_data = AsyncMock()
        market_data.get_current_price = AsyncMock(return_value=82000.0)

        engine = MagicMock(spec=FuturesEngineV2)
        engine.EXCHANGE_NAME = "binance_futures"
        engine._config = MagicMock()
        engine._config.futures_v2.leverage = 3
        engine._market_data = market_data

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_pos]
        mock_session.execute = AsyncMock(return_value=mock_result)

        with (
            patch("engine.futures_engine_v2.get_session_factory") as mock_gsf,
            patch(
                "engine.futures_engine_v2.emit_event", new_callable=AsyncMock
            ) as mock_emit,
        ):
            _patch_session_factory(mock_gsf, mock_session)

            await FuturesEngineV2._log_shutdown_positions(engine)

        mock_emit.assert_called_once()
        call_args = mock_emit.call_args
        assert call_args[0][0] == "warning"
        assert "1개 포지션 보유 중" in call_args[0][2]

    @pytest.mark.asyncio
    async def test_log_shutdown_no_positions(self):
        """포지션 없으면 경고 없음."""
        from engine.futures_engine_v2 import FuturesEngineV2

        engine = MagicMock(spec=FuturesEngineV2)
        engine.EXCHANGE_NAME = "binance_futures"
        engine._config = MagicMock()
        engine._config.futures_v2.leverage = 3
        engine._market_data = AsyncMock()

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        with (
            patch("engine.futures_engine_v2.get_session_factory") as mock_gsf,
            patch(
                "engine.futures_engine_v2.emit_event", new_callable=AsyncMock
            ) as mock_emit,
        ):
            _patch_session_factory(mock_gsf, mock_session)

            await FuturesEngineV2._log_shutdown_positions(engine)

        mock_emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_log_shutdown_price_fetch_failure_graceful(self):
        """가격 조회 실패 시 PnL=0으로 대체."""
        from engine.futures_engine_v2 import FuturesEngineV2

        mock_pos = MagicMock(spec=Position)
        mock_pos.symbol = "BTC/USDT"
        mock_pos.direction = "long"
        mock_pos.quantity = 0.01
        mock_pos.average_buy_price = 80000.0
        mock_pos.leverage = 3

        market_data = AsyncMock()
        market_data.get_current_price = AsyncMock(side_effect=Exception("API error"))

        engine = MagicMock(spec=FuturesEngineV2)
        engine.EXCHANGE_NAME = "binance_futures"
        engine._config = MagicMock()
        engine._config.futures_v2.leverage = 3
        engine._market_data = market_data

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_pos]
        mock_session.execute = AsyncMock(return_value=mock_result)

        with (
            patch("engine.futures_engine_v2.get_session_factory") as mock_gsf,
            patch(
                "engine.futures_engine_v2.emit_event", new_callable=AsyncMock
            ) as mock_emit,
        ):
            _patch_session_factory(mock_gsf, mock_session)

            await FuturesEngineV2._log_shutdown_positions(engine)

        mock_emit.assert_called_once()


# ════════════════════════════════════════════════════════════════
# 4. SL 이벤트 스팸 방지
# ════════════════════════════════════════════════════════════════


class TestStopEventSpamPrevention:
    """SL/TP/trailing 이벤트 5분 쿨다운."""

    def test_last_stop_event_time_initialized(self, mock_deps):
        """_last_stop_event_time이 빈 dict로 초기화."""
        tier1 = _make_tier1(mock_deps)
        assert tier1._last_stop_event_time == {}

    def test_stop_event_cooldown_constant(self, mock_deps):
        """스탑 이벤트 쿨다운이 5분(300초)."""
        tier1 = _make_tier1(mock_deps)
        assert tier1._STOP_EVENT_COOLDOWN_SEC == 300

    @pytest.mark.asyncio
    async def test_first_stop_event_emitted(self, mock_deps):
        """첫 SL 이벤트는 즉시 발화."""
        tier1 = _make_tier1(mock_deps)
        state = _make_position_state()

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            tier1._emit_stop_event_throttled("BTC/USDT", state, 75000.0, "SL hit")

        assert "BTC/USDT" in tier1._last_stop_event_time

    @pytest.mark.asyncio
    async def test_duplicate_event_suppressed_within_cooldown(self, mock_deps):
        """쿨다운 중 동일 심볼 이벤트 억제."""
        tier1 = _make_tier1(mock_deps)
        state = _make_position_state()

        # 1분 전 이벤트 기록
        tier1._last_stop_event_time["BTC/USDT"] = datetime.now(
            timezone.utc
        ) - timedelta(minutes=1)
        original_time = tier1._last_stop_event_time["BTC/USDT"]

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            tier1._emit_stop_event_throttled("BTC/USDT", state, 75000.0, "SL hit")

        # 이벤트 발화되지 않음 (emit_event는 asyncio.create_task로 호출됨, 여기선 mock)
        # 타임스탬프 변경 안 됨
        assert tier1._last_stop_event_time["BTC/USDT"] == original_time

    @pytest.mark.asyncio
    async def test_event_emitted_after_cooldown_expires(self, mock_deps):
        """쿨다운 만료 후 이벤트 재발화."""
        tier1 = _make_tier1(mock_deps)
        state = _make_position_state()

        # 6분 전 이벤트 기록 (5분 쿨다운 만료)
        tier1._last_stop_event_time["BTC/USDT"] = datetime.now(
            timezone.utc
        ) - timedelta(minutes=6)

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            tier1._emit_stop_event_throttled("BTC/USDT", state, 75000.0, "SL hit again")

        # 타임스탬프 갱신됨
        assert (
            datetime.now(timezone.utc) - tier1._last_stop_event_time["BTC/USDT"]
        ).total_seconds() < 5

    @pytest.mark.asyncio
    async def test_sl_close_clears_stop_cooldown(self, mock_deps):
        """SL 청산 완료 후 알림 쿨다운 해제."""
        tier1 = _make_tier1(mock_deps)
        session = AsyncMock()

        state = _make_position_state(
            entry_price=80000.0,
            stop_loss_atr=1.0,  # 타이트한 SL
        )
        tier1._positions.open_position(state)
        tier1._last_stop_event_time["BTC/USDT"] = datetime.now(timezone.utc)

        # SL 히트 가격 (entry - atr * sl_atr)
        price = 78500.0  # 80000 - 1000 * 1.0 = 79000, 78500 < 79000 → SL hit
        atr = 1000.0

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            result = await tier1._check_sl_tp(session, "BTC/USDT", state, price, atr)

        assert result is True
        # 청산 완료 후 쿨다운 해제
        assert "BTC/USDT" not in tier1._last_stop_event_time

    @pytest.mark.asyncio
    async def test_different_symbols_independent_cooldown(self, mock_deps):
        """심볼별 독립 쿨다운."""
        tier1 = _make_tier1(mock_deps)
        state_btc = _make_position_state(symbol="BTC/USDT")
        state_eth = _make_position_state(symbol="ETH/USDT", entry_price=3000.0)

        # BTC 쿨다운 중
        tier1._last_stop_event_time["BTC/USDT"] = datetime.now(
            timezone.utc
        ) - timedelta(minutes=1)

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            tier1._emit_stop_event_throttled("BTC/USDT", state_btc, 75000.0, "SL")
            tier1._emit_stop_event_throttled("ETH/USDT", state_eth, 2800.0, "SL")

        # BTC 쿨다운 중이므로 타임스탬프 유지 (1분 전)
        btc_age = (
            datetime.now(timezone.utc) - tier1._last_stop_event_time["BTC/USDT"]
        ).total_seconds()
        assert btc_age > 50  # ~60초 전

        # ETH는 신규이므로 타임스탬프 갱신
        assert "ETH/USDT" in tier1._last_stop_event_time
        eth_age = (
            datetime.now(timezone.utc) - tier1._last_stop_event_time["ETH/USDT"]
        ).total_seconds()
        assert eth_age < 5


# ════════════════════════════════════════════════════════════════
# 5. Tier1 최대 보유 시간
# ════════════════════════════════════════════════════════════════


class TestTier1MaxHoldHours:
    """Tier1 포지션 최대 보유 시간 체크."""

    @pytest.mark.asyncio
    async def test_max_hold_disabled_by_default(self, mock_deps):
        """기본값 0 → 보유 시간 제한 없음."""
        tier1 = _make_tier1(mock_deps, max_hold_hours=0)
        session = AsyncMock()

        # 7일 전 진입
        state = _make_position_state(
            entered_at=datetime.now(timezone.utc) - timedelta(days=7),
        )
        tier1._positions.open_position(state)

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            result = await tier1._check_sl_tp(
                session, "BTC/USDT", state, 80000.0, 1000.0
            )

        # max_hold_hours=0이므로 시간 초과 체크 안 함
        assert result is False
        assert tier1._positions.has_position("BTC/USDT")

    @pytest.mark.asyncio
    async def test_max_hold_exceeded_closes_position(self, mock_deps):
        """보유 시간 초과 → 강제 청산."""
        tier1 = _make_tier1(mock_deps, max_hold_hours=48)
        session = AsyncMock()

        # 50시간 전 진입 (48시간 초과)
        state = _make_position_state(
            entered_at=datetime.now(timezone.utc) - timedelta(hours=50),
        )
        tier1._positions.open_position(state)

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            result = await tier1._check_sl_tp(
                session, "BTC/USDT", state, 80000.0, 1000.0
            )

        assert result is True
        # 포지션 청산됨
        assert not tier1._positions.has_position("BTC/USDT")

    @pytest.mark.asyncio
    async def test_max_hold_not_exceeded_keeps_position(self, mock_deps):
        """보유 시간 미초과 → 포지션 유지."""
        tier1 = _make_tier1(mock_deps, max_hold_hours=48)
        session = AsyncMock()

        # 24시간 전 진입 (48시간 미만)
        state = _make_position_state(
            entered_at=datetime.now(timezone.utc) - timedelta(hours=24),
        )
        tier1._positions.open_position(state)

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            result = await tier1._check_sl_tp(
                session, "BTC/USDT", state, 80000.0, 1000.0
            )

        assert result is False
        assert tier1._positions.has_position("BTC/USDT")

    @pytest.mark.asyncio
    async def test_max_hold_reason_includes_pnl(self, mock_deps):
        """보유 시간 초과 사유에 PnL 정보 포함."""
        tier1 = _make_tier1(mock_deps, max_hold_hours=48)
        session = AsyncMock()

        state = _make_position_state(
            entry_price=80000.0,
            entered_at=datetime.now(timezone.utc) - timedelta(hours=50),
        )
        tier1._positions.open_position(state)

        # 가격이 올라 수익 중
        close_reasons = []
        original_close = tier1._close_position

        async def capture_close(s, sym, direction, reason):
            close_reasons.append(reason)
            return await original_close(s, sym, direction, reason)

        tier1._close_position = capture_close

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            await tier1._check_sl_tp(session, "BTC/USDT", state, 82000.0, 1000.0)

        assert len(close_reasons) == 1
        assert "보유 시간 초과" in close_reasons[0]
        assert "50." in close_reasons[0]  # 보유 시간
        assert "48" in close_reasons[0]  # 한도

    @pytest.mark.asyncio
    async def test_sl_takes_priority_over_max_hold(self, mock_deps):
        """SL이 max_hold보다 우선."""
        tier1 = _make_tier1(mock_deps, max_hold_hours=48)
        session = AsyncMock()

        state = _make_position_state(
            entry_price=80000.0,
            entered_at=datetime.now(timezone.utc) - timedelta(hours=50),
            stop_loss_atr=1.0,
        )
        tier1._positions.open_position(state)

        # SL 히트 가격
        sl_price = 78500.0  # 80000 - 1000 * 1.0 = 79000, 78500 < 79000

        close_reasons = []
        original_close = tier1._close_position

        async def capture_close(s, sym, direction, reason):
            close_reasons.append(reason)
            return await original_close(s, sym, direction, reason)

        tier1._close_position = capture_close

        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            await tier1._check_sl_tp(session, "BTC/USDT", state, sl_price, 1000.0)

        assert len(close_reasons) == 1
        assert "SL hit" in close_reasons[0]  # SL이 우선 발동

    @pytest.mark.asyncio
    async def test_max_hold_config_integration(self):
        """FuturesV2Config에 tier1_max_hold_hours 필드 존재."""
        from config import FuturesV2Config

        cfg = FuturesV2Config()
        assert hasattr(cfg, "tier1_max_hold_hours")
        assert cfg.tier1_max_hold_hours == 0  # 기본값: 무제한


# ════════════════════════════════════════════════════════════════
# Cross-Exchange Checker Callback (FuturesEngineV2 단위)
# ════════════════════════════════════════════════════════════════


class TestCrossExchangeCallback:
    """FuturesEngineV2._check_cross_exchange_position 콜백 테스트."""

    @pytest.mark.asyncio
    async def test_no_engine_registry_returns_none(self):
        """engine_registry 없으면 None 반환."""
        from engine.futures_engine_v2 import FuturesEngineV2

        engine = MagicMock(spec=FuturesEngineV2)
        engine._engine_registry = None
        engine.EXCHANGE_NAME = "binance_futures"

        result = await FuturesEngineV2._check_cross_exchange_position(
            engine, "BTC/USDT", 0.8
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_no_cross_position_returns_none(self):
        """교차 포지션 없으면 None 반환."""
        from engine.futures_engine_v2 import FuturesEngineV2

        engine = MagicMock(spec=FuturesEngineV2)
        engine._engine_registry = MagicMock()
        engine.EXCHANGE_NAME = "binance_futures"

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch("engine.futures_engine_v2.get_session_factory") as mock_gsf:
            _patch_session_factory(mock_gsf, mock_session)

            result = await FuturesEngineV2._check_cross_exchange_position(
                engine, "BTC/USDT", 0.8
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_cross_position_high_conf_flipped(self):
        """교차 포지션 + 높은 신뢰도 → 현물 청산 성공 → True."""
        from engine.futures_engine_v2 import FuturesEngineV2

        engine = MagicMock(spec=FuturesEngineV2)
        engine.EXCHANGE_NAME = "binance_futures"

        cross_pos = MagicMock()
        cross_pos.exchange = "binance_spot"
        cross_pos.quantity = 0.01

        cross_engine = AsyncMock()
        cross_engine.close_position_for_cross_exchange = AsyncMock(return_value=True)
        cross_engine._ec = MagicMock()
        cross_engine._ec.quote_currency = "USDT"

        engine._engine_registry = MagicMock()
        engine._engine_registry.get_engine = MagicMock(return_value=cross_engine)

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = cross_pos
        mock_session.execute = AsyncMock(return_value=mock_result)

        with (
            patch("engine.futures_engine_v2.get_session_factory") as mock_gsf,
            patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock),
        ):
            _patch_session_factory(mock_gsf, mock_session)

            result = await FuturesEngineV2._check_cross_exchange_position(
                engine, "BTC/USDT", 0.70
            )

        assert result is True
        cross_engine.close_position_for_cross_exchange.assert_called_once()

    @pytest.mark.asyncio
    async def test_cross_position_low_conf_blocked(self):
        """교차 포지션 + 낮은 신뢰도 → False (차단)."""
        from engine.futures_engine_v2 import FuturesEngineV2

        engine = MagicMock(spec=FuturesEngineV2)
        engine.EXCHANGE_NAME = "binance_futures"

        cross_pos = MagicMock()
        cross_pos.exchange = "binance_spot"
        cross_pos.quantity = 0.01

        engine._engine_registry = MagicMock()
        engine._engine_registry.get_engine = MagicMock(return_value=None)

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = cross_pos
        mock_session.execute = AsyncMock(return_value=mock_result)

        with (
            patch("engine.futures_engine_v2.get_session_factory") as mock_gsf,
            patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock),
        ):
            _patch_session_factory(mock_gsf, mock_session)

            result = await FuturesEngineV2._check_cross_exchange_position(
                engine, "BTC/USDT", 0.50
            )

        assert result is False


# ════════════════════════════════════════════════════════════════
# SL/TP Close Failure Path (self-review fix #2)
# ════════════════════════════════════════════════════════════════


class TestSlTpCloseFailure:
    """_check_sl_tp returns False when _close_position fails."""

    @pytest.mark.asyncio
    async def test_sl_hit_close_fails_returns_false(self, mock_deps):
        """SL 히트 but close 실패 → False 반환 (쿨다운 미설정)."""
        tier1 = _make_tier1(mock_deps)
        session = AsyncMock()

        state = _make_position_state(
            entry_price=80000.0,
            stop_loss_atr=1.0,
        )
        tier1._positions.open_position(state)

        # close 실패: execute_order returns success=False
        mock_deps["safe_order"].execute_order = AsyncMock(
            return_value=OrderResponse(
                success=False,
                order_id=None,
                executed_price=0,
                executed_quantity=0,
                fee=0,
            )
        )

        sl_price = 78500.0  # SL 히트
        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            result = await tier1._check_sl_tp(
                session, "BTC/USDT", state, sl_price, 1000.0
            )

        # close 실패 시 False 반환
        assert result is False
        # 포지션 아직 열려 있음
        assert tier1._positions.has_position("BTC/USDT")
        # 쿨다운 미설정
        assert "BTC/USDT" not in tier1._last_exit_time

    @pytest.mark.asyncio
    async def test_sl_hit_close_succeeds_returns_true(self, mock_deps):
        """SL 히트 + close 성공 → True 반환 + 쿨다운 설정."""
        tier1 = _make_tier1(mock_deps)
        session = AsyncMock()

        state = _make_position_state(
            entry_price=80000.0,
            stop_loss_atr=1.0,
        )
        tier1._positions.open_position(state)

        sl_price = 78500.0
        with patch("engine.tier1_manager.emit_event", new_callable=AsyncMock):
            result = await tier1._check_sl_tp(
                session, "BTC/USDT", state, sl_price, 1000.0
            )

        assert result is True
        assert not tier1._positions.has_position("BTC/USDT")
        assert "BTC/USDT" in tier1._last_exit_time


# ════════════════════════════════════════════════════════════════
# Cross-Exchange NULL Direction Integration (self-review fix #5)
# ════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def cross_exchange_db():
    """In-memory SQLite fixture for cross-exchange SQL query tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        from core.models import Base

        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    yield factory
    await engine.dispose()


class TestCrossExchangeNullDirection:
    """교차 거래소 SQL 쿼리가 direction=NULL인 포지션을 감지하는지 검증."""

    @pytest.mark.asyncio
    async def test_null_direction_detected_as_long(self, cross_exchange_db):
        """direction=NULL인 현물 포지션도 교차 충돌로 감지됨."""
        from engine.futures_engine_v2 import FuturesEngineV2

        async with cross_exchange_db() as session:
            # direction=None인 현물 포지션 생성 (레거시 행)
            pos = Position(
                exchange="binance_spot",
                symbol="BTC/USDT",
                quantity=0.01,
                average_buy_price=80000.0,
                total_invested=800.0,
                direction=None,  # NULL — 레거시 행
            )
            session.add(pos)
            await session.commit()

        engine = MagicMock(spec=FuturesEngineV2)
        engine.EXCHANGE_NAME = "binance_futures"
        engine._engine_registry = MagicMock()
        engine._engine_registry.get_engine = MagicMock(return_value=None)

        # 실제 DB 세션 사용하여 SQL 쿼리 검증
        with patch(
            "engine.futures_engine_v2.get_session_factory",
            return_value=cross_exchange_db,
        ):
            with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
                result = await FuturesEngineV2._check_cross_exchange_position(
                    engine, "BTC/USDT", 0.50
                )

        # NULL direction은 non-short이므로 충돌로 감지되어야 함
        assert result is False  # blocked (low confidence)

    @pytest.mark.asyncio
    async def test_short_direction_not_detected(self, cross_exchange_db):
        """direction='short'인 포지션은 교차 충돌로 감지 안 됨."""
        from engine.futures_engine_v2 import FuturesEngineV2

        async with cross_exchange_db() as session:
            pos = Position(
                exchange="binance_spot",
                symbol="BTC/USDT",
                quantity=0.01,
                average_buy_price=80000.0,
                total_invested=800.0,
                direction="short",
            )
            session.add(pos)
            await session.commit()

        engine = MagicMock(spec=FuturesEngineV2)
        engine.EXCHANGE_NAME = "binance_futures"
        engine._engine_registry = MagicMock()

        with patch(
            "engine.futures_engine_v2.get_session_factory",
            return_value=cross_exchange_db,
        ):
            result = await FuturesEngineV2._check_cross_exchange_position(
                engine, "BTC/USDT", 0.50
            )

        assert result is None  # 교차 포지션 없음 (short은 충돌 아님)

    @pytest.mark.asyncio
    async def test_explicit_long_direction_detected(self, cross_exchange_db):
        """direction='long'인 현물 포지션도 정상 감지됨."""
        from engine.futures_engine_v2 import FuturesEngineV2

        async with cross_exchange_db() as session:
            pos = Position(
                exchange="binance_spot",
                symbol="BTC/USDT",
                quantity=0.01,
                average_buy_price=80000.0,
                total_invested=800.0,
                direction="long",
            )
            session.add(pos)
            await session.commit()

        engine = MagicMock(spec=FuturesEngineV2)
        engine.EXCHANGE_NAME = "binance_futures"
        engine._engine_registry = MagicMock()
        engine._engine_registry.get_engine = MagicMock(return_value=None)

        with patch(
            "engine.futures_engine_v2.get_session_factory",
            return_value=cross_exchange_db,
        ):
            with patch("engine.futures_engine_v2.emit_event", new_callable=AsyncMock):
                result = await FuturesEngineV2._check_cross_exchange_position(
                    engine, "BTC/USDT", 0.50
                )

        assert result is False  # blocked
