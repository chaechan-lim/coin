"""
Tier1Manager — Tier 1 코인 상시 포지션 관리.

SAR(Stop-and-Reverse): 항상 포지션 유지, 방향 즉시 전환, 쿨다운 없음.
ATR 기반 연속 사이징: 변동성 낮으면 크게, 높으면 작게.
"""
import time
import structlog
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession

from core.enums import Direction
from engine.regime_detector import RegimeDetector, RegimeState
from engine.strategy_selector import StrategySelector
from engine.safe_order_pipeline import SafeOrderPipeline, OrderRequest
from engine.position_state_tracker import PositionStateTracker, PositionState
from engine.portfolio_manager import PortfolioManager
from strategies.regime_base import StrategyDecision
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
    """Tier 1 코인의 상시 포지션 관리."""

    BASE_RISK_PCT = 0.02  # 1회 리스크: 계좌의 2%

    def __init__(
        self,
        coins: list[str],
        safe_order: SafeOrderPipeline,
        position_tracker: PositionStateTracker,
        regime_detector: RegimeDetector,
        strategy_selector: StrategySelector,
        portfolio_manager: PortfolioManager,
        market_data: MarketDataService,
        leverage: int = 3,
        max_position_pct: float = 0.15,
        min_confidence: float = 0.4,
        cooldown_seconds: int = 93600,  # 26h (백테스트 최적 cd312 = 312*5min)
    ):
        self._coins = coins
        self._safe_order = safe_order
        self._positions = position_tracker
        self._regime = regime_detector
        self._strategies = strategy_selector
        self._pm = portfolio_manager
        self._market_data = market_data
        self._leverage = leverage
        self._max_position_pct = max_position_pct
        self._min_confidence = min_confidence
        self._cooldown_sec = cooldown_seconds
        self._last_exit_time: dict[str, float] = {}  # symbol → timestamp

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
            "last_cycle_at": self._last_cycle_at.isoformat() if self._last_cycle_at else None,
            "last_action_at": self._last_action_at.isoformat() if self._last_action_at else None,
            "coins": self._coins,
            "active_positions": self._positions.active_count("tier1"),
            "last_decisions": dict(self._last_decisions),
            "regime": self._regime.current.regime.value if self._regime.current else None,
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
        self, session: AsyncSession, symbol: str, regime: RegimeState,
    ) -> str:
        """단일 코인 평가 + SAR 실행. Returns outcome string."""
        # 현재 포지션 방향
        pos_state = self._positions.get(symbol)
        current_dir = pos_state.direction if pos_state else None

        # 전략 선택 + 시그널 생성
        strategy = self._strategies.select(regime.regime)
        df_5m = await self._fetch_candles(symbol, "5m", 200)
        df_1h = await self._fetch_candles(symbol, "1h", 200)

        if df_5m is None or len(df_5m) < 20:
            return "candle_error"

        decision = await strategy.evaluate(df_5m, df_1h, regime, current_dir)

        # SL/TP 체크는 전략 시그널과 무관하게 항상 수행
        if pos_state:
            price = self._last_close(df_5m)
            atr = self._last_atr(df_5m)
            if price > 0:
                pos_state.update_extreme(price)
            if await self._check_sl_tp(session, symbol, pos_state, price, atr):
                return "sl_tp"

        if decision.is_hold:
            return "hold"

        # SAR: 방향 전환
        outcome = await self._execute_decision(session, symbol, decision, current_dir, df_5m)
        return outcome

    async def _execute_decision(
        self,
        session: AsyncSession,
        symbol: str,
        decision: StrategyDecision,
        current_dir: Direction | None,
        df_5m: pd.DataFrame,
    ) -> str:
        """전략 결정을 실행한다. Returns outcome string."""
        close = self._last_close(df_5m)
        atr = self._last_atr(df_5m)

        if decision.direction == Direction.FLAT:
            # 포지션 청산
            if current_dir and current_dir != Direction.FLAT:
                await self._close_position(session, symbol, current_dir, decision.reason)
                self._last_exit_time[symbol] = time.time()
                return "flat_close"
            return "hold"

        # 최소 신뢰도 필터
        if decision.confidence < self._min_confidence:
            logger.debug("tier1_low_confidence", symbol=symbol,
                        confidence=decision.confidence, min=self._min_confidence)
            return "low_confidence"

        if current_dir and current_dir != Direction.FLAT and decision.direction != current_dir:
            # SAR: 기존 포지션 청산 → 반대 방향 진입 (쿨다운 면제)
            await self._close_position(
                session, symbol, current_dir,
                f"SAR: {current_dir.value}→{decision.direction.value}",
            )
            self._last_exit_time[symbol] = time.time()
            # 새 방향 진입
            await self._open_position(session, symbol, decision, close, atr)
            return "sar"

        elif current_dir is None or current_dir == Direction.FLAT:
            # 신규 진입 — 쿨다운 체크
            last_exit = self._last_exit_time.get(symbol, 0)
            elapsed = time.time() - last_exit
            if elapsed < self._cooldown_sec:
                remaining_h = (self._cooldown_sec - elapsed) / 3600
                logger.debug("tier1_cooldown", symbol=symbol, remaining_h=f"{remaining_h:.1f}")
                return "cooldown"
            await self._open_position(session, symbol, decision, close, atr)
            return "opened"

        # 같은 방향 포지션 이미 보유 중
        return "hold"

    async def _open_position(
        self,
        session: AsyncSession,
        symbol: str,
        decision: StrategyDecision,
        close: float,
        atr: float,
    ) -> None:
        """포지션 오픈."""
        margin = self._calc_margin(decision, close, atr)
        if margin <= 0:
            return

        quantity = (margin * self._leverage) / close if close > 0 else 0.0
        if quantity <= 0:
            return

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
                trailing_activation_atr=decision.take_profit_atr * 0.5,
                trailing_stop_atr=decision.stop_loss_atr * 0.7,
                tier="tier1",
                strategy_name=decision.strategy_name,
                confidence=decision.confidence,
                sizing_factor=decision.sizing_factor,
            )
            self._positions.open_position(state)

    async def _close_position(
        self, session: AsyncSession, symbol: str, direction: Direction, reason: str,
    ) -> None:
        """포지션 청산."""
        pos_state = self._positions.get(symbol)
        if not pos_state:
            return

        price = await self._get_price(symbol)
        if price <= 0:
            return

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

    async def _check_sl_tp(
        self,
        session: AsyncSession,
        symbol: str,
        state: PositionState,
        price: float,
        atr: float,
    ) -> bool:
        """SL/TP/trailing 체크. 히트 시 청산. Returns True if closed."""
        if price <= 0 or atr <= 0:
            return False

        state.update_extreme(price)

        if state.check_stop_loss(price, atr):
            await self._close_position(
                session, symbol, state.direction,
                f"SL hit: price={price:.2f}",
            )
            return True

        if state.check_trailing_stop(price, atr):
            await self._close_position(
                session, symbol, state.direction,
                f"Trailing stop hit: price={price:.2f}",
            )
            return True

        if state.check_take_profit(price, atr):
            await self._close_position(
                session, symbol, state.direction,
                f"TP hit: price={price:.2f}",
            )
            return True

        return False

    def _calc_margin(
        self, decision: StrategyDecision, close: float, atr: float,
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

        return final if final >= 5.0 else 0.0

    async def _fetch_candles(
        self, symbol: str, timeframe: str, limit: int,
    ) -> pd.DataFrame | None:
        """캔들 데이터 가져오기."""
        try:
            return await self._market_data.get_ohlcv_df(symbol, timeframe, limit)
        except Exception as e:
            logger.warning("candle_fetch_error", symbol=symbol, tf=timeframe, error=str(e))
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
