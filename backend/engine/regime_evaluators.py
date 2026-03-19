"""
RegimeLongEvaluator / RegimeShortEvaluator — StrategySelector 래핑.

기존 StrategySelector를 DirectionEvaluator 프로토콜로 래핑하여
롱/숏 독립 평가를 지원한다.

공통 로직은 _RegimeDirectionEvaluator 기반 클래스에 통합하고,
방향 필터링만 서브클래스에서 override.
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


class _RegimeDirectionEvaluator:
    """방향별 StrategySelector 래핑 기반 클래스.

    공통 로직(레짐 체크, 캔들 조회, 전략 평가)을 통합하고,
    방향 필터링만 서브클래스에서 구현한다.
    """

    def __init__(
        self,
        direction: Direction,
        strategy_selector: StrategySelector,
        regime_detector: RegimeDetector,
        market_data: MarketDataService,
        *,
        eval_interval: int = 60,
    ) -> None:
        self._direction = direction
        self._selector = strategy_selector
        self._regime = regime_detector
        self._market_data = market_data
        self._eval_interval = eval_interval
        self._label = "regime_long" if direction == Direction.LONG else "regime_short"
        self._opposite_label = (
            "long_eval_ignores_short"
            if direction == Direction.LONG
            else "short_eval_ignores_long"
        )

    @property
    def eval_interval_sec(self) -> int:
        return self._eval_interval

    async def evaluate(
        self,
        symbol: str,
        current_position: PositionState | None,
        *,
        df_5m: pd.DataFrame | None = None,
        df_1h: pd.DataFrame | None = None,
    ) -> DirectionDecision:
        """방향 평가. 반대 방향 시그널은 hold로 변환.

        Args:
            df_5m: 사전 조회된 5분 캔들 (None이면 내부에서 조회)
            df_1h: 사전 조회된 1시간 캔들 (None이면 내부에서 조회)
        """
        regime = self._regime.current
        if regime is None:
            return _hold_decision("no_regime", self._label)

        current_dir = current_position.direction if current_position else None
        strategy = self._selector.select(regime.regime)

        # 사전 조회된 캔들이 없으면 직접 조회
        if df_5m is None:
            df_5m = await self._fetch_candles(symbol, "5m", 200)
        if df_1h is None:
            df_1h = await self._fetch_candles(symbol, "1h", 200)
        if df_5m is None or len(df_5m) < 20 or df_1h is None or len(df_1h) < 20:
            return _hold_decision("candle_error", strategy.name)

        decision = await strategy.evaluate(df_5m, df_1h, regime, current_dir)

        # 5m 캔들에서 close/atr 추출 (Tier1Manager가 재사용)
        if "close" not in df_5m.columns:
            logger.warning(f"{self._label}_missing_close_column", symbol=symbol)
        if "atr_14" not in df_5m.columns:
            logger.warning(f"{self._label}_missing_atr_column", symbol=symbol)
        last_close = (
            float(df_5m["close"].iloc[-1])
            if "close" in df_5m.columns and pd.notna(df_5m["close"].iloc[-1])
            else 0.0
        )
        last_atr = (
            float(df_5m["atr_14"].iloc[-1])
            if "atr_14" in df_5m.columns and pd.notna(df_5m["atr_14"].iloc[-1])
            else 0.0
        )

        # HOLD → hold
        if decision.is_hold:
            return _hold_decision(
                decision.reason, decision.strategy_name, decision.indicators
            )

        # 같은 방향 시그널 → open (close/atr를 indicators에 포함)
        if decision.direction == self._direction:
            indicators = dict(decision.indicators) if decision.indicators else {}
            indicators["close"] = last_close
            indicators["atr"] = last_atr
            return DirectionDecision(
                action="open",
                direction=self._direction,
                confidence=decision.confidence,
                sizing_factor=decision.sizing_factor,
                stop_loss_atr=decision.stop_loss_atr,
                take_profit_atr=decision.take_profit_atr,
                reason=decision.reason,
                strategy_name=decision.strategy_name,
                indicators=indicators,
            )

        # FLAT 시그널 → 같은 방향 포지션 보유 중이면 close
        if decision.direction == Direction.FLAT:
            if current_position and current_position.direction == self._direction:
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

        # 반대 방향 시그널 → 무시
        return _hold_decision(
            f"{self._opposite_label}: {decision.reason}",
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
                f"{self._label}_candle_error",
                symbol=symbol,
                tf=timeframe,
                error=str(e),
            )
            return None


class RegimeLongEvaluator(_RegimeDirectionEvaluator):
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
        super().__init__(
            Direction.LONG,
            strategy_selector,
            regime_detector,
            market_data,
            eval_interval=eval_interval,
        )


class RegimeShortEvaluator(_RegimeDirectionEvaluator):
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
        super().__init__(
            Direction.SHORT,
            strategy_selector,
            regime_detector,
            market_data,
            eval_interval=eval_interval,
        )


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
