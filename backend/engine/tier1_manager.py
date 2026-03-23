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
from sqlalchemy.ext.asyncio import AsyncSession

from core.constants import MIN_NOTIONAL
from core.enums import Direction, Regime
from core.models import StrategyLog
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
    decisions: dict = field(default_factory=dict)  # symbol → outcome string


class Tier1Manager:
    """Tier 1 코인의 상시 포지션 관리 — 듀얼 이밸류에이터."""

    BASE_RISK_PCT = 0.02  # 1회 리스크: 계좌의 2%

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
        }

    async def evaluation_cycle(self, session: AsyncSession) -> CycleStats:
        """모든 Tier 1 코인 평가 (60초마다 호출)."""
        start_time = time.monotonic()
        stats = CycleStats()

        regime_state = self._regime.current
        if regime_state is None:
            logger.debug("tier1_skip_no_regime")
            return stats

        for coin in self._coins:
            try:
                outcome = await self._evaluate_coin(session, coin, regime_state)
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
                elif outcome == "margin_insufficient":
                    stats.low_confidence_count += 1  # 마진 부족도 미실행 카운트
                elif outcome in ("opened", "closed", "sar", "flat_close"):
                    stats.executed_count += 1
                    self._last_action_at = datetime.now(timezone.utc)
            except Exception as e:
                stats.error_count += 1
                stats.decisions[coin] = "error"
                self._last_decisions[coin] = "error"
                logger.error("tier1_eval_error", coin=coin, error=str(e))

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
        if current_dir == Direction.LONG:
            long_decision = await self._long_evaluator.evaluate(
                symbol,
                pos_state,
                df_5m=df_5m,
                df_1h=df_1h,
            )
            if long_decision.is_close:
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

        # 진입 실행
        opened = await self._open_position_from_decision(session, symbol, decision)
        self._log_direction_decision(
            session,
            symbol,
            decision,
            regime=regime,
            was_executed=opened,
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

    async def _check_sl_tp(
        self,
        session: AsyncSession,
        symbol: str,
        state: PositionState,
        price: float,
        atr: float,
    ) -> bool:
        """SL/TP/trailing 체크. 히트 시 청산 + 쿨다운 설정. Returns True if closed.

        동적 SL 활성 시 레짐에 따라 SL ATR 배수를 조정한다 (COIN-42).
        Note: update_extreme(price)는 호출 전에 _evaluate_coin에서 이미 수행됨.
        """
        if price <= 0 or atr <= 0:
            return False

        # 동적 SL: 레짐에 따라 일시적으로 SL ATR mult를 조정 (COIN-42)
        original_sl_atr = state.stop_loss_atr
        if self._dynamic_sl:
            state.stop_loss_atr = self._apply_dynamic_sl(state.stop_loss_atr)

        try:
            if state.check_stop_loss(price, atr):
                await self._close_position(
                    session,
                    symbol,
                    state.direction,
                    f"SL hit: price={price:.2f}",
                )
                self._set_exit_cooldown(symbol, state.direction)
                return True

            if state.check_trailing_stop(price, atr):
                await self._close_position(
                    session,
                    symbol,
                    state.direction,
                    f"Trailing stop hit: price={price:.2f}",
                )
                self._set_exit_cooldown(symbol, state.direction)
                return True

            if state.check_take_profit(price, atr):
                await self._close_position(
                    session,
                    symbol,
                    state.direction,
                    f"TP hit: price={price:.2f}",
                )
                self._set_exit_cooldown(symbol, state.direction)
                return True
        finally:
            # 원본 SL 복원 (일시적 조정이므로 영속 변경하지 않음)
            if self._dynamic_sl:
                state.stop_loss_atr = original_sl_atr

        return False

    def _set_exit_cooldown(self, symbol: str, direction: Direction) -> None:
        """SL/TP/trailing 후 방향별 쿨다운 설정 (COIN-27)."""
        self._last_exit_time[symbol] = time.time()
        self._last_exit_direction[symbol] = direction

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
