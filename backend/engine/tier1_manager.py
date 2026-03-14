"""
Tier1Manager — Tier 1 코인 상시 포지션 관리.

SAR(Stop-and-Reverse): 항상 포지션 유지, 방향 즉시 전환, 쿨다운 없음.
ATR 기반 연속 사이징: 변동성 낮으면 크게, 높으면 작게.
"""
import structlog
import pandas as pd
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

    @property
    def coins(self) -> list[str]:
        return list(self._coins)

    async def evaluation_cycle(self, session: AsyncSession) -> None:
        """모든 Tier 1 코인 평가 (60초마다 호출)."""
        regime_state = self._regime.current
        if regime_state is None:
            logger.debug("tier1_skip_no_regime")
            return

        for coin in self._coins:
            try:
                await self._evaluate_coin(session, coin, regime_state)
            except Exception as e:
                logger.error("tier1_eval_error", coin=coin, error=str(e))

    async def _evaluate_coin(
        self, session: AsyncSession, symbol: str, regime: RegimeState,
    ) -> None:
        """단일 코인 평가 + SAR 실행."""
        # 현재 포지션 방향
        pos_state = self._positions.get(symbol)
        current_dir = pos_state.direction if pos_state else None

        # 전략 선택 + 시그널 생성
        strategy = self._strategies.select(regime.regime)
        df_5m = await self._fetch_candles(symbol, "5m", 200)
        df_1h = await self._fetch_candles(symbol, "1h", 200)

        if df_5m is None or len(df_5m) < 20:
            return

        decision = await strategy.evaluate(df_5m, df_1h, regime, current_dir)

        if decision.is_hold:
            # 극단 가격만 업데이트
            if pos_state:
                price = self._last_close(df_5m)
                if price > 0:
                    pos_state.update_extreme(price)
            return

        # ATR 기반 SL/TP 체크 (기존 포지션)
        if pos_state:
            price = self._last_close(df_5m)
            atr = self._last_atr(df_5m)
            if await self._check_sl_tp(session, symbol, pos_state, price, atr):
                return  # SL/TP로 청산됨

        # SAR: 방향 전환
        await self._execute_decision(session, symbol, decision, current_dir, df_5m)

    async def _execute_decision(
        self,
        session: AsyncSession,
        symbol: str,
        decision: StrategyDecision,
        current_dir: Direction | None,
        df_5m: pd.DataFrame,
    ) -> None:
        """전략 결정을 실행한다."""
        close = self._last_close(df_5m)
        atr = self._last_atr(df_5m)

        if decision.direction == Direction.FLAT:
            # 포지션 청산
            if current_dir and current_dir != Direction.FLAT:
                await self._close_position(session, symbol, current_dir, decision.reason)
            return

        if current_dir and current_dir != Direction.FLAT and decision.direction != current_dir:
            # SAR: 기존 포지션 청산 → 반대 방향 진입
            pos_state = self._positions.get(symbol)
            entry = pos_state.entry_price if pos_state else 0.0
            await self._close_position(
                session, symbol, current_dir,
                f"SAR: {current_dir.value}→{decision.direction.value}",
            )
            # 새 방향 진입
            await self._open_position(session, symbol, decision, close, atr)

        elif current_dir is None or current_dir == Direction.FLAT:
            # 신규 진입
            await self._open_position(session, symbol, decision, close, atr)

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
