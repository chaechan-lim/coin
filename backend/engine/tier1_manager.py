"""
Tier1Manager — Tier 1 코인 상시 포지션 관리.

듀얼 이밸류에이터 아키텍처: 롱/숏 독립 평가.
각 방향(롱/숏)의 진입/청산을 독립적인 이밸류에이터가 판단한다.

ATR 기반 연속 사이징: 변동성 낮으면 크게, 높으면 작게.
"""

import time
import structlog
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession

from core.enums import Direction
from core.models import StrategyLog
from engine.direction_evaluator import DirectionDecision, DirectionEvaluator
from engine.regime_detector import RegimeDetector, RegimeState
from engine.safe_order_pipeline import SafeOrderPipeline, OrderRequest
from engine.position_state_tracker import PositionStateTracker, PositionState
from engine.portfolio_manager import PortfolioManager
from services.market_data import MarketDataService

logger = structlog.get_logger(__name__)


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
    decisions: dict = field(default_factory=dict)  # symbol → outcome string


class Tier1Manager:
    """Tier 1 코인의 상시 포지션 관리 — 듀얼 이밸류에이터."""

    BASE_RISK_PCT = 0.02  # 1회 리스크: 계좌의 2%
    MIN_NOTIONAL = 105.0  # 바이낸스 USDM 최소 notional $100 + 여유

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

    async def _open_position_from_decision(
        self,
        session: AsyncSession,
        symbol: str,
        decision: DirectionDecision,
    ) -> bool:
        """DirectionDecision으로 포지션 오픈.

        이밸류에이터가 캔들을 이미 조회했으므로, indicators에 close/atr가 있으면
        그 값을 재사용하여 불필요한 API 호출을 줄인다.

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

        margin = self._calc_margin(decision, close, atr)
        if margin <= 0:
            return False

        quantity = (margin * self._leverage) / close if close > 0 else 0.0
        if quantity <= 0:
            return False

        request = OrderRequest(
            symbol=symbol,
            direction=decision.direction,
            action="open",
            quantity=quantity,
            price=close,
            margin=margin,
            leverage=self._leverage,
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
                leverage=self._leverage,
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

        Note: update_extreme(price)는 호출 전에 _evaluate_coin에서 이미 수행됨.
        """
        if price <= 0 or atr <= 0:
            return False

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
        """ATR 기반 마진 계산."""
        cash = self._pm.cash_balance
        if cash <= 0 or close <= 0 or atr <= 0:
            return 0.0

        atr_pct = atr / close
        risk_per_unit = atr_pct * max(decision.stop_loss_atr, 0.5)

        raw_margin = (cash * self.BASE_RISK_PCT) / risk_per_unit
        adjusted = raw_margin * decision.sizing_factor * decision.confidence
        max_margin = cash * self._max_position_pct
        final = min(adjusted, max_margin)

        # 최소 notional 보장: margin × leverage >= MIN_NOTIONAL
        # BTC처럼 가격이 높은 코인은 precision 절삭으로 notional이 $100 미만으로 떨어질 수 있음
        min_margin = self.MIN_NOTIONAL / self._leverage
        if final < min_margin and cash >= min_margin:
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
        # numpy float → Python float 변환
        cleaned = {}
        for k, v in indicators.items():
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
