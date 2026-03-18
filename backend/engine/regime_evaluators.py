"""
RegimeLongEvaluator / RegimeShortEvaluator — StrategySelector 래핑.

기존 StrategySelector를 DirectionEvaluator 프로토콜로 래핑하여
롱/숏 독립 평가를 지원한다.

RegimeLongEvaluator: 롱 시그널만 통과, 숏 시그널은 무시
RegimeShortEvaluator: 숏 시그널만 통과, 롱 시그널은 무시
"""

import structlog
import pandas as pd

from core.enums import Direction
from engine.direction_evaluator import DirectionDecision
from engine.regime_detector import RegimeDetector
from engine.strategy_selector import StrategySelector
from engine.position_state_tracker import PositionState
from services.market_data import MarketDataService

logger = structlog.get_logger(__name__)


class RegimeLongEvaluator:
    """기존 StrategySelector를 롱 전용 DirectionEvaluator로 래핑.

    - LONG 시그널 → open
    - FLAT 시그널 + 롱 포지션 → close
    - SHORT 시그널 → hold (무시)
    - HOLD → hold
    """

    def __init__(
        self,
        strategy_selector: StrategySelector,
        regime_detector: RegimeDetector,
        market_data: MarketDataService,
        *,
        eval_interval: int = 60,
    ) -> None:
        self._selector = strategy_selector
        self._regime = regime_detector
        self._market_data = market_data
        self._eval_interval = eval_interval

    @property
    def eval_interval_sec(self) -> int:
        return self._eval_interval

    async def evaluate(
        self,
        symbol: str,
        current_position: PositionState | None,
    ) -> DirectionDecision:
        """롱 방향 평가. SHORT 시그널은 hold로 변환."""
        regime = self._regime.current
        if regime is None:
            return _hold_decision("no_regime", "regime_long")

        current_dir = current_position.direction if current_position else None
        strategy = self._selector.select(regime.regime)

        df_5m = await self._fetch_candles(symbol, "5m", 200)
        df_1h = await self._fetch_candles(symbol, "1h", 200)
        if df_5m is None or len(df_5m) < 20:
            return _hold_decision("candle_error", strategy.name)

        decision = await strategy.evaluate(df_5m, df_1h, regime, current_dir)

        # 롱 전용 필터링
        if decision.is_hold:
            return _hold_decision(
                decision.reason, decision.strategy_name, decision.indicators
            )

        if decision.direction == Direction.LONG:
            # 롱 진입 시그널
            return DirectionDecision(
                action="open",
                direction=Direction.LONG,
                confidence=decision.confidence,
                sizing_factor=decision.sizing_factor,
                stop_loss_atr=decision.stop_loss_atr,
                take_profit_atr=decision.take_profit_atr,
                reason=decision.reason,
                strategy_name=decision.strategy_name,
                indicators=decision.indicators,
            )

        if decision.direction == Direction.FLAT:
            # 청산 시그널 — 롱 포지션 보유 중이면 close
            if current_position and current_position.direction == Direction.LONG:
                return DirectionDecision(
                    action="close",
                    direction=None,
                    confidence=decision.confidence,
                    sizing_factor=decision.sizing_factor,
                    stop_loss_atr=decision.stop_loss_atr,
                    take_profit_atr=decision.take_profit_atr,
                    reason=decision.reason,
                    strategy_name=decision.strategy_name,
                    indicators=decision.indicators,
                )
            return _hold_decision(
                decision.reason, decision.strategy_name, decision.indicators
            )

        # SHORT 시그널 → 롱 이밸류에이터에서는 무시
        return _hold_decision(
            f"long_eval_ignores_short: {decision.reason}",
            decision.strategy_name,
            decision.indicators,
        )

    async def _fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
    ) -> pd.DataFrame | None:
        try:
            return await self._market_data.get_ohlcv_df(symbol, timeframe, limit)
        except Exception as e:
            logger.warning(
                "long_eval_candle_error", symbol=symbol, tf=timeframe, error=str(e)
            )
            return None


class RegimeShortEvaluator:
    """기존 StrategySelector를 숏 전용 DirectionEvaluator로 래핑.

    - SHORT 시그널 → open
    - FLAT 시그널 + 숏 포지션 → close
    - LONG 시그널 → hold (무시)
    - HOLD → hold
    """

    def __init__(
        self,
        strategy_selector: StrategySelector,
        regime_detector: RegimeDetector,
        market_data: MarketDataService,
        *,
        eval_interval: int = 60,
    ) -> None:
        self._selector = strategy_selector
        self._regime = regime_detector
        self._market_data = market_data
        self._eval_interval = eval_interval

    @property
    def eval_interval_sec(self) -> int:
        return self._eval_interval

    async def evaluate(
        self,
        symbol: str,
        current_position: PositionState | None,
    ) -> DirectionDecision:
        """숏 방향 평가. LONG 시그널은 hold로 변환."""
        regime = self._regime.current
        if regime is None:
            return _hold_decision("no_regime", "regime_short")

        current_dir = current_position.direction if current_position else None
        strategy = self._selector.select(regime.regime)

        df_5m = await self._fetch_candles(symbol, "5m", 200)
        df_1h = await self._fetch_candles(symbol, "1h", 200)
        if df_5m is None or len(df_5m) < 20:
            return _hold_decision("candle_error", strategy.name)

        decision = await strategy.evaluate(df_5m, df_1h, regime, current_dir)

        # 숏 전용 필터링
        if decision.is_hold:
            return _hold_decision(
                decision.reason, decision.strategy_name, decision.indicators
            )

        if decision.direction == Direction.SHORT:
            # 숏 진입 시그널
            return DirectionDecision(
                action="open",
                direction=Direction.SHORT,
                confidence=decision.confidence,
                sizing_factor=decision.sizing_factor,
                stop_loss_atr=decision.stop_loss_atr,
                take_profit_atr=decision.take_profit_atr,
                reason=decision.reason,
                strategy_name=decision.strategy_name,
                indicators=decision.indicators,
            )

        if decision.direction == Direction.FLAT:
            # 청산 시그널 — 숏 포지션 보유 중이면 close
            if current_position and current_position.direction == Direction.SHORT:
                return DirectionDecision(
                    action="close",
                    direction=None,
                    confidence=decision.confidence,
                    sizing_factor=decision.sizing_factor,
                    stop_loss_atr=decision.stop_loss_atr,
                    take_profit_atr=decision.take_profit_atr,
                    reason=decision.reason,
                    strategy_name=decision.strategy_name,
                    indicators=decision.indicators,
                )
            return _hold_decision(
                decision.reason, decision.strategy_name, decision.indicators
            )

        # LONG 시그널 → 숏 이밸류에이터에서는 무시
        return _hold_decision(
            f"short_eval_ignores_long: {decision.reason}",
            decision.strategy_name,
            decision.indicators,
        )

    async def _fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
    ) -> pd.DataFrame | None:
        try:
            return await self._market_data.get_ohlcv_df(symbol, timeframe, limit)
        except Exception as e:
            logger.warning(
                "short_eval_candle_error", symbol=symbol, tf=timeframe, error=str(e)
            )
            return None


def _hold_decision(
    reason: str,
    strategy_name: str,
    indicators: dict | None = None,
) -> DirectionDecision:
    """HOLD 결정 생성 헬퍼."""
    return DirectionDecision(
        action="hold",
        direction=None,
        confidence=0.0,
        sizing_factor=0.0,
        stop_loss_atr=0.0,
        take_profit_atr=0.0,
        reason=reason,
        strategy_name=strategy_name,
        indicators=indicators or {},
    )
