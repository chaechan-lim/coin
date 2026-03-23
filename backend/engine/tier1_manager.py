"""
Tier1Manager — Tier 1 코인 상시 포지션 관리.

듀얼 이밸류에이터 아키텍처: 롱/숏 독립 평가.
각 방향(롱/숏)의 진입/청산을 독립적인 이밸류에이터가 판단한다.

ATR 기반 연속 사이징: 변동성 낮으면 크게, 높으면 작게.

리스크 관리 (COIN-42):
- 비대칭 모드: TRENDING_DOWN/VOLATILE(bearish) 시 신규 롱 차단
- 동적 SL: 레짐별 SL ATR mult 스케일링
- ATR 레버리지 스케일링: 고변동성에서 레버리지 자동 축소
- 시장 상태별 포지션 사이징: 하락장→50%, 변동성→60%
"""

import time
import structlog
import pandas as pd
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from core.constants import MIN_NOTIONAL
from core.enums import Direction, Regime
from core.event_bus import emit_event
from core.models import Order, Position, StrategyLog
from engine.direction_evaluator import DirectionDecision, DirectionEvaluator
from engine.regime_detector import RegimeDetector, RegimeState
from engine.safe_order_pipeline import SafeOrderPipeline, OrderRequest
from engine.position_state_tracker import PositionStateTracker, PositionState
from engine.portfolio_manager import PortfolioManager
from services.market_data import MarketDataService

logger = structlog.get_logger(__name__)

# ── 동적 SL 프로필 (COIN-42) ────────────────────────────────────
# 레짐별 SL ATR mult 스케일링: (multiplier, floor_atr_mult, cap_atr_mult)
_DYNAMIC_SL_PROFILES: dict[Regime, tuple[float, float, float]] = {
    Regime.TRENDING_UP: (1.0, 1.0, 8.0),  # 상승장: 기본 SL, 넓은 상한
    Regime.TRENDING_DOWN: (0.6, 0.8, 4.0),  # 하락장: SL 타이트 (60%)
    Regime.RANGING: (0.8, 1.0, 6.0),  # 횡보: SL 약간 타이트
    Regime.VOLATILE: (0.7, 0.8, 5.0),  # 변동성: SL 타이트 (70%)
}
_DEFAULT_SL_PROFILE = (0.8, 1.0, 6.0)

# ── ATR 레버리지 스케일링 (COIN-42) ──────────────────────────────
_ATR_LEVERAGE_TIERS: list[tuple[float, int]] = [
    (20.0, 1),  # ATR > 20% → 1x
    (10.0, 2),  # ATR > 10% → 2x
    (7.0, 3),  # ATR > 7%  → 3x
    (5.0, 4),  # ATR > 5%  → 4x
    (3.0, 5),  # ATR > 3%  → 5x
    (0.0, 5),  # ATR <= 3% → 5x (max)
]

# ── 레짐별 포지션 사이징 팩터 (COIN-42) ──────────────────────────
_REGIME_SIZING_FACTORS: dict[Regime, float] = {
    Regime.TRENDING_UP: 1.0,  # 상승장: 풀 사이즈
    Regime.TRENDING_DOWN: 0.5,  # 하락장: 50%
    Regime.RANGING: 0.8,  # 횡보: 80%
    Regime.VOLATILE: 0.6,  # 변동성: 60%
}


@dataclass
class CycleStats:
    """단일 evaluation_cycle 실행 결과 통계."""

    coins_evaluated: int = 0
    hold_count: int = 0
    low_confidence_count: int = 0
    cooldown_count: int = 0
    sl_tp_count: int = 0
    executed_count: int = 0
    error_count: int = 0
    candle_error_count: int = 0
    ml_filtered_count: int = 0
    daily_limit_count: int = 0
    decisions: dict = field(default_factory=dict)  # symbol → outcome string


class Tier1Manager:
    """Tier 1 코인의 상시 포지션 관리 — 듀얼 이밸류에이터."""

    BASE_RISK_PCT = 0.02  # 1회 리스크: 계좌의 2%
    CROSS_FLIP_MIN_CONFIDENCE = 0.65  # 교차 거래소 포지션 전환 최소 신뢰도
    _STOP_EVENT_COOLDOWN_SEC = 300  # SL/TP/trailing 이벤트 스팸 방지 5분 쿨다운

    def __init__(
        self,
        coins: list[str],
        safe_order: SafeOrderPipeline,
        position_tracker: PositionStateTracker,
        regime_detector: RegimeDetector,
        portfolio_manager: PortfolioManager,
        market_data: MarketDataService,
        *,
        long_evaluator: DirectionEvaluator,
        short_evaluator: DirectionEvaluator,
        leverage: int = 3,
        max_position_pct: float = 0.15,
        min_confidence: float = 0.4,
        cooldown_seconds: int = 93600,  # 26h (후방 호환 기본값)
        long_cooldown_seconds: int | None = None,
        short_cooldown_seconds: int | None = None,
        exchange_name: str = "binance_futures",
        on_close_callback: Callable[[], Awaitable[None]] | None = None,
        ml_filter=None,
        # Risk management (COIN-42)
        asymmetric_mode: bool = False,
        dynamic_sl: bool = False,
        atr_leverage_scaling: bool = False,
        daily_buy_limit: int = 20,
        max_daily_coin_buys: int = 3,
        max_eval_errors: int = 3,
        # COIN-43: 최대 보유 시간 (0=무제한)
        max_hold_hours: float = 0,
        # COIN-43: 교차 거래소 포지션 충돌 체크 콜백
        cross_exchange_checker: (
            Callable[[str, float], Awaitable[bool | None]] | None
        ) = None,
    ):
        self._coins = coins
        self._safe_order = safe_order
        self._positions = position_tracker
        self._regime = regime_detector
        self._long_evaluator = long_evaluator
        self._short_evaluator = short_evaluator
        self._pm = portfolio_manager
        self._market_data = market_data
        self._leverage = leverage
        self._max_position_pct = max_position_pct
        self._min_confidence = min_confidence
        self._ml_filter = ml_filter
        # Risk management flags (COIN-42)
        self._asymmetric_mode = asymmetric_mode
        self._dynamic_sl = dynamic_sl
        self._atr_leverage_scaling = atr_leverage_scaling
        # Direction-specific cooldowns (COIN-27): fallback to single cooldown_seconds
        self._long_cooldown_sec = (
            long_cooldown_seconds
            if long_cooldown_seconds is not None
            else cooldown_seconds
        )
        self._short_cooldown_sec = (
            short_cooldown_seconds
            if short_cooldown_seconds is not None
            else cooldown_seconds
        )
        self._exchange_name = exchange_name
        self._on_close_callback = on_close_callback
        self._last_exit_time: dict[str, float] = {}  # symbol → timestamp
        self._last_exit_direction: dict[str, Direction] = {}  # symbol → exit direction

        # COIN-41: 일일 매수 한도
        self._daily_buy_limit = daily_buy_limit
        self._max_daily_coin_buys = max_daily_coin_buys
        self._daily_buy_count: int = 0
        self._daily_coin_buy_count: dict[str, int] = {}
        self._daily_reset_date: datetime | None = None

        # COIN-41: 연속 에러 강제 청산
        self._max_eval_errors = max_eval_errors
        self._eval_error_counts: dict[str, int] = {}

        # COIN-43: SL/TP/trailing 이벤트 스팸 방지
        self._last_stop_event_time: dict[str, datetime] = {}

        # COIN-43: 최대 보유 시간 (0=무제한)
        self._max_hold_hours = max_hold_hours

        # COIN-43: 교차 거래소 포지션 충돌 체크 콜백
        self._cross_exchange_checker = cross_exchange_checker

        # 관측용 상태 (COIN-17)
        self._cycle_count: int = 0
        self._last_cycle_at: datetime | None = None
        self._last_action_at: datetime | None = None
        self._last_decisions: dict[str, str] = {}  # symbol → 최근 결정

    @property
    def coins(self) -> list[str]:
        return list(self._coins)

    def get_status(self) -> dict:
        """현재 Tier1 운영 상태 반환 (API/모니터링용)."""
        return {
            "cycle_count": self._cycle_count,
            "last_cycle_at": self._last_cycle_at.isoformat()
            if self._last_cycle_at
            else None,
            "last_action_at": self._last_action_at.isoformat()
            if self._last_action_at
            else None,
            "coins": self._coins,
            "active_positions": self._positions.active_count("tier1"),
            "last_decisions": dict(self._last_decisions),
            "regime": self._regime.current.regime.value
            if self._regime.current
            else None,
            "ml_filter_active": self._ml_filter is not None,
            "daily_buy_count": self._daily_buy_count,
            "daily_buy_limit": self._daily_buy_limit,
            "eval_error_counts": dict(self._eval_error_counts),
        }

    async def evaluation_cycle(self, session: AsyncSession) -> CycleStats:
        """모든 Tier 1 코인 평가 (60초마다 호출)."""
        start_time = time.monotonic()
        stats = CycleStats()

        regime_state = self._regime.current
        if regime_state is None:
            logger.debug("tier1_skip_no_regime")
            return stats

        # COIN-41: 일일 카운터 리셋 (UTC 자정)
        self._reset_daily_counter()

        for coin in self._coins:
            try:
                outcome = await self._evaluate_coin(session, coin, regime_state)
                self._eval_error_counts.pop(coin, None)  # 성공 시 에러 카운터 리셋
                stats.coins_evaluated += 1
                stats.decisions[coin] = outcome
                self._last_decisions[coin] = outcome

                if outcome == "hold":
                    stats.hold_count += 1
                elif outcome == "low_confidence":
                    stats.low_confidence_count += 1
                elif outcome == "cooldown":
                    stats.cooldown_count += 1
                elif outcome == "sl_tp":
                    stats.sl_tp_count += 1
                elif outcome == "candle_error":
                    stats.candle_error_count += 1
                elif outcome == "ml_filtered":
                    stats.ml_filtered_count += 1
                elif outcome == "daily_limit":
                    stats.daily_limit_count += 1
                elif outcome == "margin_insufficient":
                    stats.low_confidence_count += 1  # 마진 부족도 미실행 카운트
                elif outcome == "cross_exchange_blocked":
                    stats.hold_count += 1  # 교차 거래소 충돌도 미실행
                elif outcome in ("opened", "closed", "sar", "flat_close"):
                    stats.executed_count += 1
                    self._last_action_at = datetime.now(timezone.utc)
            except Exception as e:
                # COIN-41: 연속 에러 카운터 + 강제 청산
                err_count = self._eval_error_counts.get(coin, 0) + 1
                self._eval_error_counts[coin] = err_count
                stats.error_count += 1
                stats.decisions[coin] = "error"
                self._last_decisions[coin] = "error"
                logger.error(
                    "tier1_eval_error",
                    coin=coin,
                    error=str(e),
                    consecutive_errors=err_count,
                )

                level = "critical" if err_count >= self._max_eval_errors else "warning"
                await emit_event(
                    level,
                    "engine",
                    f"Tier1 평가 실패: {coin} ({err_count}회 연속)",
                    detail=str(e),
                    metadata={
                        "symbol": coin,
                        "consecutive_errors": err_count,
                        "exchange": self._exchange_name,
                    },
                )

                # 연속 N회 실패 + 보유 포지션 → 강제 청산
                if err_count >= self._max_eval_errors and self._positions.has_position(
                    coin
                ):
                    try:
                        await self._force_close_stuck_position(session, coin, str(e))
                    except Exception as fc_err:
                        logger.error(
                            "tier1_force_close_failed",
                            coin=coin,
                            error=str(fc_err),
                        )

        elapsed_ms = (time.monotonic() - start_time) * 1000
        self._cycle_count += 1
        self._last_cycle_at = datetime.now(timezone.utc)

        logger.info(
            "tier1_cycle_complete",
            cycle=self._cycle_count,
            coins_evaluated=stats.coins_evaluated,
            hold=stats.hold_count,
            low_conf=stats.low_confidence_count,
            cooldown=stats.cooldown_count,
            daily_limit=stats.daily_limit_count,
            ml_filtered=stats.ml_filtered_count,
            sl_tp=stats.sl_tp_count,
            executed=stats.executed_count,
            errors=stats.error_count,
            active_positions=self._positions.active_count("tier1"),
            regime=regime_state.regime.value,
            elapsed_ms=round(elapsed_ms, 1),
        )

        return stats

    async def _evaluate_coin(
        self,
        session: AsyncSession,
        symbol: str,
        regime: RegimeState,
    ) -> str:
        """단일 코인 평가 — 듀얼 이밸류에이터. Returns outcome string."""
        pos_state = self._positions.get(symbol)
        current_dir = pos_state.direction if pos_state else None

        # 캔들을 한 번만 조회하여 SL/TP 체크 + 이밸류에이터에서 공유
        df_5m = await self._fetch_candles(symbol, "5m", 200)
        if df_5m is None or len(df_5m) < 20:
            return "candle_error"
        df_1h = await self._fetch_candles(symbol, "1h", 200)

        price = self._last_close(df_5m)
        atr = self._last_atr(df_5m)

        # WS 실시간 SL/TP 체크용 ATR 캐시 업데이트
        if atr > 0:
            self._positions.update_atr(symbol, atr)

        # 1. SL/TP/trailing 체크는 전략 시그널과 무관하게 항상 수행
        if pos_state:
            if price > 0:
                pos_state.update_extreme(price)
            if await self._check_sl_tp(session, symbol, pos_state, price, atr):
                return "sl_tp"

        # 2. 포지션 방향에 따라 해당 이밸류에이터의 청산 시그널 체크
        #    (COIN-43 paired exit: 진입 방향 evaluator만 청산 평가)
        if current_dir == Direction.LONG:
            long_decision = await self._long_evaluator.evaluate(
                symbol,
                pos_state,
                df_5m=df_5m,
                df_1h=df_1h,
            )
            if long_decision.is_close:
                logger.info(
                    "paired_exit_close",
                    symbol=symbol,
                    direction="long",
                    entry_strategy=pos_state.strategy_name,
                    close_strategy=long_decision.strategy_name,
                    confidence=long_decision.confidence,
                )
                await self._close_position(
                    session, symbol, pos_state.direction, long_decision.reason
                )
                self._set_exit_cooldown(symbol, Direction.LONG)
                self._log_direction_decision(
                    session,
                    symbol,
                    long_decision,
                    regime=regime,
                    was_executed=True,
                    closing_direction=Direction.LONG,
                )
                return "flat_close"

            # SAR 체크: 같은 방향 evaluator가 hold일 때만 반대 evaluator 시그널 확인.
            # is_open(현재 방향 재확인)이면 SAR하지 않음 — 포지션 유지.
            # 같은 인스턴스면 SAR 불가 — close 미달 시그널로 open도 불가 (COIN-29).
            if (
                long_decision.is_hold
                and self._long_evaluator is not self._short_evaluator
            ):
                short_decision = await self._short_evaluator.evaluate(
                    symbol,
                    None,
                    df_5m=df_5m,
                    df_1h=df_1h,
                )
                if (
                    short_decision.is_open
                    and short_decision.confidence >= self._min_confidence
                    and self._check_ml_filter(symbol, short_decision, regime)
                ):
                    sar_outcome = await self._execute_sar(
                        session, symbol, pos_state.direction, short_decision
                    )
                    if sar_outcome is not None:
                        if sar_outcome == "sar":
                            self._log_direction_decision(
                                session,
                                symbol,
                                short_decision,
                                regime=regime,
                                was_executed=True,
                            )
                        return sar_outcome

            # open 또는 hold — 이미 같은 방향 포지션 보유 중이므로 유지
            self._log_direction_decision(
                session,
                symbol,
                long_decision,
                regime=regime,
                was_executed=False,
            )
            return "hold"

        elif current_dir == Direction.SHORT:
            short_decision = await self._short_evaluator.evaluate(
                symbol,
                pos_state,
                df_5m=df_5m,
                df_1h=df_1h,
            )
            if short_decision.is_close:
                logger.info(
                    "paired_exit_close",
                    symbol=symbol,
                    direction="short",
                    entry_strategy=pos_state.strategy_name,
                    close_strategy=short_decision.strategy_name,
                    confidence=short_decision.confidence,
                )
                await self._close_position(
                    session, symbol, pos_state.direction, short_decision.reason
                )
                self._set_exit_cooldown(symbol, Direction.SHORT)
                self._log_direction_decision(
                    session,
                    symbol,
                    short_decision,
                    regime=regime,
                    was_executed=True,
                    closing_direction=Direction.SHORT,
                )
                return "flat_close"

            # SAR 체크: 같은 방향 evaluator가 hold일 때만 반대 evaluator 시그널 확인.
            # is_open(현재 방향 재확인)이면 SAR하지 않음 — 포지션 유지.
            # 같은 인스턴스면 SAR 불가 — close 미달 시그널로 open도 불가 (COIN-29).
            if (
                short_decision.is_hold
                and self._long_evaluator is not self._short_evaluator
            ):
                long_decision = await self._long_evaluator.evaluate(
                    symbol,
                    None,
                    df_5m=df_5m,
                    df_1h=df_1h,
                )
                if (
                    long_decision.is_open
                    and long_decision.confidence >= self._min_confidence
                    and self._check_ml_filter(symbol, long_decision, regime)
                ):
                    sar_outcome = await self._execute_sar(
                        session, symbol, pos_state.direction, long_decision
                    )
                    if sar_outcome is not None:
                        if sar_outcome == "sar":
                            self._log_direction_decision(
                                session,
                                symbol,
                                long_decision,
                                regime=regime,
                                was_executed=True,
                            )
                        return sar_outcome

            # open 또는 hold — 이미 같은 방향 포지션 보유 중이므로 유지
            self._log_direction_decision(
                session,
                symbol,
                short_decision,
                regime=regime,
                was_executed=False,
            )
            return "hold"

        # 3. 포지션 없으면 양쪽 이밸류에이터에서 진입 시그널 탐색
        #    사전 조회된 캔들을 전달하여 중복 API 호출 방지
        #    같은 인스턴스면 1번만 호출 (COIN-28 최적화: API 중복 방지)
        long_decision = await self._long_evaluator.evaluate(
            symbol,
            None,
            df_5m=df_5m,
            df_1h=df_1h,
        )
        if self._long_evaluator is self._short_evaluator:
            short_decision = long_decision
        else:
            short_decision = await self._short_evaluator.evaluate(
                symbol,
                None,
                df_5m=df_5m,
                df_1h=df_1h,
            )

        # 비대칭 모드: 하락장/변동성(bearish) 시 롱 진입 차단 (COIN-42)
        if self._asymmetric_mode and long_decision.is_open:
            if self._is_bearish_regime(regime):
                logger.info(
                    "asymmetric_long_blocked",
                    symbol=symbol,
                    regime=regime.regime.value,
                    confidence=long_decision.confidence,
                )
                long_decision = DirectionDecision(
                    action="hold",
                    direction=None,
                    confidence=0.0,
                    sizing_factor=0.0,
                    stop_loss_atr=0.0,
                    take_profit_atr=0.0,
                    reason=f"asymmetric_blocked: {regime.regime.value}",
                    strategy_name=long_decision.strategy_name,
                    indicators=long_decision.indicators,
                )
                # 같은 인스턴스면 short_decision도 업데이트
                if self._long_evaluator is self._short_evaluator:
                    short_decision = long_decision

        # 진입 시그널 선택
        decision, loser = self._resolve_entry(long_decision, short_decision)
        if decision is None:
            # 양쪽 다 hold — 같은 인스턴스면 1회만 로깅 (COIN-29)
            self._log_direction_decision(
                session,
                symbol,
                long_decision,
                regime=regime,
                was_executed=False,
            )
            if self._long_evaluator is not self._short_evaluator:
                self._log_direction_decision(
                    session,
                    symbol,
                    short_decision,
                    regime=regime,
                    was_executed=False,
                )
            return "hold"

        # 충돌로 탈락한 결정 로깅 (관측성)
        if loser is not None:
            logger.info(
                "tier1_conflict_resolved",
                symbol=symbol,
                winner_direction=decision.direction.value
                if decision.direction
                else None,
                winner_confidence=decision.confidence,
                winner_strategy=decision.strategy_name,
                loser_direction=loser.direction.value if loser.direction else None,
                loser_confidence=loser.confidence,
                loser_strategy=loser.strategy_name,
            )
            self._log_direction_decision(
                session,
                symbol,
                loser,
                regime=regime,
                was_executed=False,
            )

        # 최소 신뢰도 필터
        if decision.confidence < self._min_confidence:
            self._log_direction_decision(
                session,
                symbol,
                decision,
                regime=regime,
                was_executed=False,
            )
            logger.debug(
                "tier1_low_confidence",
                symbol=symbol,
                confidence=decision.confidence,
                min=self._min_confidence,
            )
            return "low_confidence"

        # 쿨다운 체크 — 방향별 차단 (COIN-27)
        if self._in_cooldown(symbol, decision.direction):
            self._log_direction_decision(
                session,
                symbol,
                decision,
                regime=regime,
                was_executed=False,
            )
            return "cooldown"

        # ML 시그널 필터: 신규 진입만 필터링 (COIN-40)
        if not self._check_ml_filter(symbol, decision, regime):
            self._log_direction_decision(
                session,
                symbol,
                decision,
                regime=regime,
                was_executed=False,
            )
            return "ml_filtered"

        # COIN-41: 일일 매수 한도 체크
        can_trade, reason = self._can_trade(symbol)
        if not can_trade:
            self._log_direction_decision(
                session,
                symbol,
                decision,
                regime=regime,
                was_executed=False,
            )
            logger.debug("tier1_daily_limit", symbol=symbol, reason=reason)
            return "daily_limit"

        # COIN-43: 교차 거래소 포지션 충돌 감지 (선물 숏 vs 현물 롱)
        if decision.direction == Direction.SHORT and self._cross_exchange_checker:
            cross_result = await self._check_cross_exchange(
                session, symbol, decision.confidence
            )
            if cross_result == "blocked":
                self._log_direction_decision(
                    session,
                    symbol,
                    decision,
                    regime=regime,
                    was_executed=False,
                )
                return "cross_exchange_blocked"

        # 진입 실행
        opened = await self._open_position_from_decision(session, symbol, decision)
        self._log_direction_decision(
            session,
            symbol,
            decision,
            regime=regime,
            was_executed=opened,
        )
        if opened:
            self._daily_buy_count += 1
            self._daily_coin_buy_count[symbol] = (
                self._daily_coin_buy_count.get(symbol, 0) + 1
            )
        return "opened" if opened else "margin_insufficient"

    def _resolve_entry(
        self,
        long_decision: DirectionDecision,
        short_decision: DirectionDecision,
    ) -> tuple[DirectionDecision | None, DirectionDecision | None]:
        """양쪽 이밸류에이터의 진입 시그널 충돌 해소.

        둘 다 open이면 confidence 높은 쪽 선택.
        하나만 open이면 그쪽 선택.
        둘 다 hold이면 (None, None) 반환.

        Returns:
            (winner, loser): winner는 실행할 결정, loser는 충돌로 탈락한 결정.
            충돌이 없으면 loser는 None.
        """
        long_open = long_decision.is_open
        short_open = short_decision.is_open

        if long_open and short_open:
            # 충돌 방지: confidence 높은 쪽 선택
            if long_decision.confidence >= short_decision.confidence:
                return long_decision, short_decision
            return short_decision, long_decision
        elif long_open:
            return long_decision, None
        elif short_open:
            return short_decision, None
        return None, None

    def _in_cooldown(
        self, symbol: str, entry_direction: Direction | None = None
    ) -> bool:
        """Direction-aware 쿨다운 체크.

        SL/TP 후 같은 방향 재진입만 차단. 반대 방향은 허용.
        entry_direction이 None이면 무조건 체크 (후방 호환).
        """
        last_exit = self._last_exit_time.get(symbol, 0)
        if last_exit == 0:
            return False

        # 방향 체크: 마지막 exit 방향과 다르면 쿨다운 면제
        exit_dir = self._last_exit_direction.get(symbol)
        if (
            entry_direction is not None
            and exit_dir is not None
            and entry_direction != exit_dir
        ):
            return False

        # 방향에 따라 쿨다운 시간 결정
        if exit_dir == Direction.LONG:
            cooldown_sec = self._long_cooldown_sec
        elif exit_dir == Direction.SHORT:
            cooldown_sec = self._short_cooldown_sec
        else:
            # exit_dir 불명 시 보수적으로 긴 쪽 적용
            cooldown_sec = max(self._long_cooldown_sec, self._short_cooldown_sec)

        elapsed = time.time() - last_exit
        if elapsed < cooldown_sec:
            remaining_h = (cooldown_sec - elapsed) / 3600
            logger.debug(
                "tier1_cooldown",
                symbol=symbol,
                direction=exit_dir.value if exit_dir else None,
                remaining_h=f"{remaining_h:.1f}",
            )
            return True
        return False

    def _check_ml_filter(
        self,
        symbol: str,
        decision: DirectionDecision,
        regime: RegimeState,
    ) -> bool:
        """ML 시그널 필터: 신규 진입만 필터링 (청산은 허용).

        Returns:
            True if trade should proceed, False if blocked by ML filter.
        """
        if self._ml_filter is None:
            return True

        try:
            # evaluator가 indicators에 담아준 시그널+캔들 사용
            signals = decision.indicators.get("_signals", [])
            candle_row = decision.indicators.get("_candle_row")
            combined_confidence = decision.indicators.get(
                "_combined_confidence", decision.confidence
            )
            price = decision.indicators.get("close", 0.0)
            market_state = regime.regime.value

            if candle_row is None:
                # 캔들 데이터 없으면 필터 통과 (graceful degradation)
                logger.debug("ml_filter_skip_no_candle", symbol=symbol)
                return True

            # extract_features is a static method — callable on instances
            features = self._ml_filter.extract_features(
                signals=signals,
                row=candle_row,
                price=price,
                market_state=market_state,
                combined_confidence=combined_confidence,
            )
            pred = self._ml_filter.predict(features)

            if not pred.should_trade:
                logger.info(
                    "ml_filter_blocked",
                    symbol=symbol,
                    win_prob=round(pred.win_probability, 3),
                    direction=decision.direction.value if decision.direction else None,
                )
                return False

            logger.debug(
                "ml_filter_passed",
                symbol=symbol,
                win_prob=round(pred.win_probability, 3),
            )
            return True

        except Exception as e:
            logger.warning("ml_filter_error", symbol=symbol, error=str(e))
            return True  # 에러 시 필터 통과 (graceful degradation)

    async def _open_position_from_decision(
        self,
        session: AsyncSession,
        symbol: str,
        decision: DirectionDecision,
    ) -> bool:
        """DirectionDecision으로 포지션 오픈.

        이밸류에이터가 캔들을 이미 조회했으므로, indicators에 close/atr가 있으면
        그 값을 재사용하여 불필요한 API 호출을 줄인다.

        ATR 레버리지 스케일링 (COIN-42): atr_pct에 따라 레버리지를 동적으로 축소.

        Returns:
            True if position was successfully opened, False otherwise.
        """
        close = decision.indicators.get("close", 0.0)
        atr = decision.indicators.get("atr", 0.0)
        if close <= 0 or atr <= 0:
            # fallback: indicators에 없으면 직접 조회
            df_5m = await self._fetch_candles(symbol, "5m", 200)
            if df_5m is None or len(df_5m) < 20:
                return False
            close = self._last_close(df_5m)
            atr = self._last_atr(df_5m)

        # ATR 레버리지 스케일링 (COIN-42)
        effective_leverage = self._leverage
        if self._atr_leverage_scaling and close > 0:
            effective_leverage = self._calc_atr_leverage(atr, close)

        margin = self._calc_margin(decision, close, atr)
        if margin <= 0:
            return False

        quantity = (margin * effective_leverage) / close if close > 0 else 0.0
        if quantity <= 0:
            return False

        request = OrderRequest(
            symbol=symbol,
            direction=decision.direction,
            action="open",
            quantity=quantity,
            price=close,
            margin=margin,
            leverage=effective_leverage,
            strategy_name=decision.strategy_name,
            confidence=decision.confidence,
            tier="tier1",
        )

        resp = await self._safe_order.execute_order(session, request)
        if resp.success:
            # 이밸류에이터가 indicators에 trailing 값을 제공하면 사용,
            # 없으면 SL/TP 기반 기본 공식 적용 (숏 이밸류에이터 등)
            trail_act = decision.indicators.get(
                "trailing_activation_atr",
                decision.take_profit_atr * 0.5,
            )
            trail_stop = decision.indicators.get(
                "trailing_stop_atr",
                decision.stop_loss_atr * 0.7,
            )
            state = PositionState(
                symbol=symbol,
                direction=decision.direction,
                quantity=resp.executed_quantity,
                entry_price=resp.executed_price,
                margin=margin,
                leverage=effective_leverage,
                extreme_price=resp.executed_price,
                stop_loss_atr=decision.stop_loss_atr,
                take_profit_atr=decision.take_profit_atr,
                trailing_activation_atr=trail_act,
                trailing_stop_atr=trail_stop,
                tier="tier1",
                strategy_name=decision.strategy_name,
                confidence=decision.confidence,
                sizing_factor=decision.sizing_factor,
            )
            self._positions.open_position(state)
            return True
        return False

    async def _close_position(
        self,
        session: AsyncSession,
        symbol: str,
        direction: Direction,
        reason: str,
    ) -> bool:
        """포지션 청산. Returns True if successfully closed."""
        pos_state = self._positions.get(symbol)
        if not pos_state:
            return False

        price = await self._get_price(symbol)
        if price <= 0:
            return False

        request = OrderRequest(
            symbol=symbol,
            direction=direction,
            action="close",
            quantity=pos_state.quantity,
            price=price,
            margin=pos_state.margin,
            leverage=self._leverage,
            strategy_name=pos_state.strategy_name,
            confidence=pos_state.confidence,
            tier="tier1",
            entry_price=pos_state.entry_price,
        )

        resp = await self._safe_order.execute_order(session, request)
        if resp.success:
            self._positions.close_position(symbol)
            if self._on_close_callback:
                try:
                    await self._on_close_callback()
                except Exception as exc:
                    logger.warning("on_close_callback_failed", error=str(exc))
            return True
        return False

    async def check_position_stop(
        self,
        session: AsyncSession,
        symbol: str,
        state: PositionState,
        price: float,
        atr: float,
    ) -> bool:
        """SL/TP/trailing 체크 (public). 히트 시 청산 + 쿨다운 설정. Returns True if closed."""
        return await self._check_sl_tp(session, symbol, state, price, atr)

    async def _check_sl_tp(
        self,
        session: AsyncSession,
        symbol: str,
        state: PositionState,
        price: float,
        atr: float,
    ) -> bool:
        """SL/TP/trailing/max_hold 체크. 히트 시 청산 + 쿨다운 설정. Returns True if closed.

        동적 SL 활성 시 레짐에 따라 SL ATR 배수를 조정한다 (COIN-42).
        이벤트 스팸 방지: 5분 쿨다운 (COIN-43).
        Note: update_extreme(price)는 호출 전에 _evaluate_coin에서 이미 수행됨.
        """
        if price <= 0 or atr <= 0:
            return False

        sell_reason: str | None = None

        # 동적 SL: 레짐에 따라 일시적으로 SL ATR mult를 조정 (COIN-42)
        original_sl_atr = state.stop_loss_atr
        if self._dynamic_sl:
            state.stop_loss_atr = self._apply_dynamic_sl(state.stop_loss_atr)

        try:
            if state.check_stop_loss(price, atr):
                sell_reason = f"SL hit: price={price:.2f}"

            elif state.check_trailing_stop(price, atr):
                sell_reason = f"Trailing stop hit: price={price:.2f}"

            elif state.check_take_profit(price, atr):
                sell_reason = f"TP hit: price={price:.2f}"
        finally:
            # 원본 SL 복원 (일시적 조정이므로 영속 변경하지 않음)
            if self._dynamic_sl:
                state.stop_loss_atr = original_sl_atr

        # COIN-43: 최대 보유 시간 체크
        if not sell_reason and self._max_hold_hours > 0:
            held_hours = (
                datetime.now(timezone.utc) - state.entered_at
            ).total_seconds() / 3600
            if held_hours >= self._max_hold_hours:
                if state.entry_price > 0:
                    if state.is_long:
                        pnl_pct = (price - state.entry_price) / state.entry_price * 100
                    else:
                        pnl_pct = (state.entry_price - price) / state.entry_price * 100
                else:
                    pnl_pct = 0.0
                sell_reason = (
                    f"보유 시간 초과: {held_hours:.1f}h "
                    f"(한도 {self._max_hold_hours:.0f}h, 수익 {pnl_pct:+.1f}%)"
                )

        if not sell_reason:
            return False

        # COIN-43: 스탑 경고 이벤트 — 5분 쿨다운으로 스팸 방지
        self._emit_stop_event_throttled(symbol, state, price, sell_reason)

        closed = await self._close_position(
            session,
            symbol,
            state.direction,
            sell_reason,
        )
        if closed:
            self._set_exit_cooldown(symbol, state.direction)
            # 청산 완료 시 알림 쿨다운 해제
            self._last_stop_event_time.pop(symbol, None)
        return closed

    def _emit_stop_event_throttled(
        self,
        symbol: str,
        state: PositionState,
        price: float,
        reason: str,
    ) -> None:
        """SL/TP/trailing 이벤트 발화 — 심볼당 5분 쿨다운 (COIN-43).

        동기 함수로 fire-and-forget asyncio.create_task 패턴 사용.
        """
        now = datetime.now(timezone.utc)
        last_event = self._last_stop_event_time.get(symbol)
        if (
            last_event
            and (now - last_event).total_seconds() < self._STOP_EVENT_COOLDOWN_SEC
        ):
            return  # 쿨다운 중

        self._last_stop_event_time[symbol] = now

        pnl_pct = 0.0
        if state.entry_price > 0:
            if state.is_long:
                pnl_pct = (price - state.entry_price) / state.entry_price * 100
            else:
                pnl_pct = (state.entry_price - price) / state.entry_price * 100
        leveraged_pnl = pnl_pct * state.leverage

        import asyncio

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                emit_event(
                    "warning",
                    "futures_trade",
                    f"선물 {state.direction.value} 스탑: {symbol}",
                    detail=reason,
                    metadata={
                        "symbol": symbol,
                        "price": price,
                        "entry_price": state.entry_price,
                        "pnl_pct": round(pnl_pct, 2),
                        "leveraged_pnl_pct": round(leveraged_pnl, 2),
                        "reason": reason,
                        "direction": state.direction.value,
                        "leverage": state.leverage,
                    },
                )
            )
        except RuntimeError:
            pass  # no running loop (test 환경)

    def _set_exit_cooldown(self, symbol: str, direction: Direction) -> None:
        """SL/TP/trailing 후 방향별 쿨다운 설정 (COIN-27)."""
        self._last_exit_time[symbol] = time.time()
        self._last_exit_direction[symbol] = direction

    # ── COIN-41: 일일 매수 한도 ──────────────────────

    def _reset_daily_counter(self) -> None:
        """UTC 자정에 일일 카운터 리셋."""
        today = datetime.now(timezone.utc).date()
        if self._daily_reset_date != today:
            self._daily_buy_count = 0
            self._daily_coin_buy_count.clear()
            self._daily_reset_date = today

    def _can_trade(self, symbol: str) -> tuple[bool, str]:
        """일일 매수 한도 체크. Returns (allowed, reason)."""
        if self._daily_buy_count >= self._daily_buy_limit:
            return False, f"Daily buy limit reached ({self._daily_buy_limit})"
        coin_buys = self._daily_coin_buy_count.get(symbol, 0)
        if coin_buys >= self._max_daily_coin_buys:
            return (
                False,
                f"Coin daily limit reached ({symbol}: {coin_buys}/{self._max_daily_coin_buys})",
            )
        return True, "OK"

    # ── COIN-43: 교차 거래소 포지션 충돌 감지 ──────────────

    async def _check_cross_exchange(
        self,
        session: AsyncSession,
        symbol: str,
        confidence: float,
    ) -> str:
        """선물 숏 진입 전 현물 롱 확인 (COIN-43).

        교차 거래소에 같은 기초 자산의 롱 포지션이 있으면:
        - confidence >= 0.65: 현물 롱 청산 후 숏 진행 ("flipped")
        - confidence < 0.65: 숏 차단 ("blocked")
        - 현물 포지션 없음: "clear"

        cross_exchange_checker 콜백은 (symbol, confidence) → bool|None:
        - True: 교차 포지션 성공적으로 청산됨
        - False: 교차 포지션 있지만 청산 실패/차단
        - None: 교차 포지션 없음
        """
        if not self._cross_exchange_checker:
            return "clear"

        try:
            result = await self._cross_exchange_checker(symbol, confidence)
            if result is None:
                return "clear"  # 교차 포지션 없음
            if result is True:
                return "flipped"  # 현물 청산 후 숏 진행
            # False: 교차 포지션 있지만 청산 안 됨 (낮은 신뢰도 또는 실패)
            logger.warning(
                "cross_exchange_conflict_blocked",
                symbol=symbol,
                confidence=confidence,
            )
            return "blocked"
        except Exception as e:
            logger.warning("cross_exchange_check_failed", symbol=symbol, error=str(e))
            return "clear"  # 에러 시 통과 (graceful degradation)

    async def restore_daily_buy_count(self, session: AsyncSession) -> None:
        """DB에서 오늘 매수 카운터 복원 (재시작 시)."""
        today = datetime.now(timezone.utc).date()
        today_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        result = await session.execute(
            select(Order.symbol, func.count())
            .where(
                Order.exchange == self._exchange_name,
                Order.margin_used.isnot(
                    None
                ),  # open orders only (long buy + short sell)
                Order.status == "filled",
                Order.created_at >= today_start,
            )
            .group_by(Order.symbol)
        )
        total_buys = 0
        for symbol, count in result.all():
            self._daily_coin_buy_count[symbol] = count
            total_buys += count
        self._daily_buy_count = total_buys
        self._daily_reset_date = today
        if total_buys:
            logger.info(
                "tier1_daily_buy_count_restored",
                total=total_buys,
                coins=dict(self._daily_coin_buy_count),
            )

    # ── COIN-41: 연속 에러 강제 청산 ──────────────────

    async def _force_close_stuck_position(
        self,
        session: AsyncSession,
        symbol: str,
        last_error: str,
    ) -> None:
        """연속 평가 실패 포지션 강제 청산. 가격 조회 불가 시 DB에서 직접 제거."""
        pos_state = self._positions.get(symbol)
        if not pos_state:
            self._eval_error_counts.pop(symbol, None)
            return

        err_count = self._eval_error_counts.get(symbol, 0)
        logger.warning(
            "tier1_force_close",
            symbol=symbol,
            direction=pos_state.direction.value,
            quantity=pos_state.quantity,
            consecutive_errors=err_count,
            last_error=last_error,
        )

        # SafeOrderPipeline으로 청산 시도
        try:
            price = await self._get_price(symbol)
            if price > 0:
                request = OrderRequest(
                    symbol=symbol,
                    direction=pos_state.direction,
                    action="close",
                    quantity=pos_state.quantity,
                    price=price,
                    margin=pos_state.margin,
                    leverage=self._leverage,
                    strategy_name="force_close",
                    confidence=0.0,
                    tier="tier1",
                    entry_price=pos_state.entry_price,
                )
                resp = await self._safe_order.execute_order(session, request)
                if resp.success:
                    self._positions.close_position(symbol)
                    self._eval_error_counts.pop(symbol, None)
                    # 강제 청산은 에러 기반이므로 쿨다운 면제
                    self._last_exit_time.pop(symbol, None)
                    self._last_exit_direction.pop(symbol, None)
                    if self._on_close_callback:
                        try:
                            await self._on_close_callback()
                        except Exception:
                            pass
                    logger.warning(
                        "tier1_force_close_success",
                        symbol=symbol,
                        consecutive_errors=err_count,
                    )
                    return
                # 주문 거부 (에러 아닌 실패) — 로그 후 DB 리셋 경로로 진행
                logger.warning(
                    "tier1_force_close_order_rejected",
                    symbol=symbol,
                    error=resp.error if hasattr(resp, "error") else str(resp),
                )
        except Exception as close_err:
            logger.warning(
                "tier1_force_close_market_failed",
                symbol=symbol,
                error=str(close_err),
            )

        # 2차: 거래소 매도 불가 → DB 포지션 강제 리셋
        try:
            result = await session.execute(
                select(Position).where(
                    Position.symbol == symbol,
                    Position.quantity > 0,
                    Position.exchange == self._exchange_name,
                )
            )
            position = result.scalar_one_or_none()
            if position:
                position.quantity = 0
                position.current_value = 0
                position.margin_used = 0
                position.total_invested = 0
                await session.flush()

                logger.error(
                    "tier1_force_close_db_reset",
                    symbol=symbol,
                    detail="거래소 매도 실패 → DB 포지션 강제 리셋",
                )
                await emit_event(
                    "critical",
                    "engine",
                    f"Tier1 강제 청산 (DB 리셋): {symbol}",
                    detail=f"연속 {err_count}회 평가 실패, 거래소 매도 불가 → DB 포지션 0으로 리셋. "
                    f"수동으로 거래소에서 {symbol} 포지션을 확인하세요.",
                    metadata={"symbol": symbol, "consecutive_errors": err_count},
                )
            else:
                logger.info(
                    "tier1_force_close_no_db_position",
                    symbol=symbol,
                    detail="DB에 활성 포지션 없음 — 이미 청산됨",
                )
        except Exception as db_err:
            logger.error(
                "tier1_force_close_db_reset_failed",
                symbol=symbol,
                error=str(db_err),
            )
        finally:
            # 항상 인메모리 상태 정리 — 무한 재시도 방지
            self._positions.close_position(symbol)
            self._eval_error_counts.pop(symbol, None)
            # 강제 청산은 에러 기반이므로 쿨다운 면제
            self._last_exit_time.pop(symbol, None)
            self._last_exit_direction.pop(symbol, None)

    # ── COIN-41: 쿨다운 DB 영속화 ──────────────────

    async def persist_cooldowns(self, session: AsyncSession) -> int:
        """인메모리 쿨다운을 DB Position.last_sell_at/last_sell_direction에 영속화.

        Returns:
            업데이트된 포지션 수.
        """
        updated = 0
        for symbol, exit_ts in list(self._last_exit_time.items()):
            exit_dir = self._last_exit_direction.get(symbol)
            if exit_dir is None:
                continue

            # 해당 심볼 포지션 조회 (활성/비활성 모두)
            result = await session.execute(
                select(Position).where(
                    Position.symbol == symbol,
                    Position.exchange == self._exchange_name,
                )
            )
            pos = result.scalar_one_or_none()
            if pos:
                pos.last_sell_at = datetime.fromtimestamp(exit_ts, tz=timezone.utc)
                pos.last_sell_direction = exit_dir.value
                updated += 1

        if updated > 0:
            await session.flush()
            logger.debug("tier1_cooldowns_persisted", count=updated)
        return updated

    async def restore_cooldowns(self, session: AsyncSession) -> int:
        """DB에서 쿨다운 복원 (재시작 시).

        Position.last_sell_at + last_sell_direction이 쿨다운 범위 내이면
        인메모리 _last_exit_time / _last_exit_direction에 복원.

        Returns:
            복원된 쿨다운 수.
        """
        result = await session.execute(
            select(Position).where(
                Position.exchange == self._exchange_name,
                Position.last_sell_at.isnot(None),
            )
        )
        restored = 0
        max_cooldown = max(self._long_cooldown_sec, self._short_cooldown_sec)

        for pos in result.scalars().all():
            if pos.last_sell_at is None:
                continue

            sell_at = pos.last_sell_at
            # timezone-aware 변환 (DB에서 naive일 수 있음)
            if sell_at.tzinfo is None:
                sell_at = sell_at.replace(tzinfo=timezone.utc)

            elapsed = (datetime.now(timezone.utc) - sell_at).total_seconds()
            if elapsed >= max_cooldown:
                continue  # 쿨다운 만료

            self._last_exit_time[pos.symbol] = sell_at.timestamp()

            # direction 복원: last_sell_direction 우선, 없으면 현재 position direction 사용
            if pos.last_sell_direction:
                dir_str = pos.last_sell_direction
                self._last_exit_direction[pos.symbol] = (
                    Direction.SHORT if dir_str == "short" else Direction.LONG
                )
            elif pos.direction:
                self._last_exit_direction[pos.symbol] = (
                    Direction.SHORT if pos.direction == "short" else Direction.LONG
                )
            else:
                # 방향 정보 없음 — 쿨다운 스킵 (잘못된 기본값보다 안전)
                logger.warning(
                    "tier1_cooldown_restore_skip_no_direction",
                    symbol=pos.symbol,
                    detail="last_sell_direction과 position.direction 모두 NULL",
                )
                self._last_exit_time.pop(pos.symbol, None)
                continue

            restored += 1
            logger.debug(
                "tier1_cooldown_restored",
                symbol=pos.symbol,
                direction=self._last_exit_direction[pos.symbol].value,
                remaining_h=round((max_cooldown - elapsed) / 3600, 1),
            )

        if restored:
            logger.info("tier1_cooldowns_restored", count=restored)
        return restored

    async def _execute_sar(
        self,
        session: AsyncSession,
        symbol: str,
        current_direction: Direction,
        new_decision: DirectionDecision,
    ) -> str | None:
        """Stop And Reverse: 현재 포지션 청산 + 반대 포지션 즉시 오픈.

        SAR은 쿨다운을 설정하지 않는다 — 전략적 방향 전환이므로.

        Returns:
            "sar" — 청산 + 오픈 모두 성공.
            "flat_close" — 청산 성공, 오픈 실패 (포지션 flat 상태).
            None — 청산 실패, 아무 변경 없음.
        """
        # 1. 현재 포지션 청산
        closed = await self._close_position(
            session,
            symbol,
            current_direction,
            f"SAR: {current_direction.value} → {new_decision.direction.value}",
        )
        if not closed:
            logger.warning(
                "tier1_sar_close_failed",
                symbol=symbol,
                from_dir=current_direction.value,
                to_dir=new_decision.direction.value,
            )
            return None
        # SAR은 쿨다운 면제 — _set_exit_cooldown 호출 안 함

        # 2. 반대 방향 즉시 오픈
        opened = await self._open_position_from_decision(session, symbol, new_decision)
        if opened:
            # COIN-41: SAR도 일일 매수 카운터에 반영 (진단/관측용)
            self._daily_buy_count += 1
            self._daily_coin_buy_count[symbol] = (
                self._daily_coin_buy_count.get(symbol, 0) + 1
            )
            logger.info(
                "tier1_sar_executed",
                symbol=symbol,
                from_dir=current_direction.value,
                to_dir=new_decision.direction.value,
                confidence=new_decision.confidence,
            )
            return "sar"
        else:
            logger.warning(
                "tier1_sar_open_failed",
                symbol=symbol,
                from_dir=current_direction.value,
                to_dir=new_decision.direction.value,
            )
            return "flat_close"

    def _calc_margin(
        self,
        decision: DirectionDecision,
        close: float,
        atr: float,
    ) -> float:
        """ATR 기반 마진 계산. 레짐별 사이징 팩터 적용 (COIN-42)."""
        cash = self._pm.cash_balance
        if cash <= 0 or close <= 0 or atr <= 0:
            return 0.0

        atr_pct = atr / close
        risk_per_unit = atr_pct * max(decision.stop_loss_atr, 0.5)

        raw_margin = (cash * self.BASE_RISK_PCT) / risk_per_unit
        adjusted = raw_margin * decision.sizing_factor * decision.confidence

        # 레짐별 포지션 사이징 팩터 (COIN-42)
        adjusted *= self._get_regime_sizing_factor()

        max_margin = cash * self._max_position_pct
        final = min(adjusted, max_margin)

        # 최소 notional 보장: margin × leverage >= MIN_NOTIONAL
        # BTC처럼 가격이 높은 코인은 precision 절삭으로 notional이 $100 미만으로 떨어질 수 있음
        min_margin = MIN_NOTIONAL / self._leverage
        if final < min_margin:
            if max_margin < min_margin:
                # 계좌가 이 코인을 이 레버리지로 안전하게 거래하기엔 너무 작음
                logger.warning(
                    "min_notional_overrides_max_margin",
                    cash=round(cash, 2),
                    max_margin=round(max_margin, 2),
                    min_margin=round(min_margin, 2),
                    leverage=self._leverage,
                    min_notional=MIN_NOTIONAL,
                )
                return 0.0
            if cash >= min_margin:
                final = min_margin

        return final if final >= 5.0 else 0.0

    async def _fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
    ) -> pd.DataFrame | None:
        """캔들 데이터 가져오기."""
        try:
            return await self._market_data.get_ohlcv_df(symbol, timeframe, limit)
        except Exception as e:
            logger.warning(
                "candle_fetch_error", symbol=symbol, tf=timeframe, error=str(e)
            )
            return None

    async def _get_price(self, symbol: str) -> float:
        try:
            return await self._market_data.get_current_price(symbol)
        except Exception:
            return 0.0

    @staticmethod
    def _last_close(df: pd.DataFrame) -> float:
        if "close" not in df.columns or len(df) == 0:
            return 0.0
        val = df["close"].iloc[-1]
        return float(val) if pd.notna(val) else 0.0

    @staticmethod
    def _last_atr(df: pd.DataFrame) -> float:
        if "atr_14" not in df.columns or len(df) == 0:
            return 0.0
        val = df["atr_14"].iloc[-1]
        return float(val) if pd.notna(val) else 0.0

    # ── 리스크 관리 헬퍼 (COIN-42) ──────────────────────────────────

    @staticmethod
    def _is_bearish_regime(regime: RegimeState) -> bool:
        """하락/변동성(bearish) 레짐 여부.

        TRENDING_DOWN은 항상 bearish.
        VOLATILE은 trend_direction이 하락(-1)일 때만 bearish.
        """
        if regime.regime == Regime.TRENDING_DOWN:
            return True
        if regime.regime == Regime.VOLATILE and regime.trend_direction < 0:
            return True
        return False

    def _apply_dynamic_sl(self, base_sl_atr: float) -> float:
        """레짐별 동적 SL ATR mult 계산."""
        regime_state = self._regime.current
        if regime_state is None:
            return base_sl_atr

        mult, floor, cap = _DYNAMIC_SL_PROFILES.get(
            regime_state.regime, _DEFAULT_SL_PROFILE
        )
        adjusted = base_sl_atr * mult
        return max(floor, min(adjusted, cap))

    def _calc_atr_leverage(self, atr: float, close: float) -> int:
        """ATR% 기반 최대 레버리지 계산.

        고변동성(높은 ATR%)에서 레버리지를 자동 축소하여
        과도한 리스크를 방지한다.
        """
        if close <= 0:
            return 1
        atr_pct = (atr / close) * 100

        max_lev = self._leverage
        for threshold, lev in _ATR_LEVERAGE_TIERS:
            if atr_pct > threshold:
                max_lev = lev
                break

        effective = min(self._leverage, max_lev)
        if effective < self._leverage:
            logger.info(
                "atr_leverage_scaled",
                atr_pct=round(atr_pct, 2),
                base_leverage=self._leverage,
                effective_leverage=effective,
            )
        return max(effective, 1)

    def _get_regime_sizing_factor(self) -> float:
        """현재 레짐에 따른 포지션 사이징 팩터 반환."""
        regime_state = self._regime.current
        if regime_state is None:
            return 1.0
        return _REGIME_SIZING_FACTORS.get(regime_state.regime, 0.8)

    @staticmethod
    def _direction_to_signal_type(direction: Direction | None) -> str:
        """Direction enum을 StrategyLog signal_type 문자열로 변환."""
        if direction == Direction.LONG:
            return "BUY"
        elif direction == Direction.SHORT:
            return "SELL"
        return "HOLD"

    def _log_direction_decision(
        self,
        session: AsyncSession,
        symbol: str,
        decision: DirectionDecision,
        *,
        was_executed: bool,
        regime: RegimeState,
        closing_direction: Direction | None = None,
    ) -> None:
        """DirectionDecision을 StrategyLog 테이블에 기록.

        V2 전략의 매 평가마다 HOLD 포함 모든 판단을 기록하여
        전략 추적 가능하게 한다.

        closing_direction: 청산 시 닫히는 포지션의 방향. close 결정의
            direction은 None이므로, 포지션 방향을 별도로 전달하여
            올바른 signal_type(SELL for LONG close, BUY for SHORT close)을 기록.
        """
        if decision.is_hold:
            signal_type = "HOLD"
        elif decision.is_close and closing_direction is not None:
            # 청산 시그널: 롱 청산 → SELL, 숏 청산 → BUY (방향 반전)
            signal_type = "SELL" if closing_direction == Direction.LONG else "BUY"
        else:
            signal_type = self._direction_to_signal_type(decision.direction)

        # 지표 딕셔너리: 전략 제공 지표 + 레짐 정보
        indicators = dict(decision.indicators) if decision.indicators else {}
        indicators["regime"] = regime.regime.value
        indicators["regime_confidence"] = round(regime.confidence, 3)
        # _-prefixed keys are internal transport fields (e.g. _signals, _candle_row)
        # that are not JSON-serializable — strip before DB store (COIN-40)
        cleaned = {}
        for k, v in indicators.items():
            if k.startswith("_"):
                continue
            try:
                cleaned[k] = float(v) if hasattr(v, "__float__") else v
            except (TypeError, ValueError):
                cleaned[k] = str(v)

        strategy_log = StrategyLog(
            exchange=self._exchange_name,
            strategy_name=decision.strategy_name,
            symbol=symbol,
            signal_type=signal_type,
            confidence=float(decision.confidence)
            if decision.confidence is not None
            else None,
            reason=decision.reason,
            indicators=cleaned,
            was_executed=was_executed,
        )
        session.add(strategy_log)
