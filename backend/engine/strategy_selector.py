"""
StrategySelector — 레짐 기반 단일 전략 선택.

투표 없음. 레짐이 전략을 결정한다.
"""
import structlog

from core.enums import Regime
from strategies.regime_base import RegimeStrategy
from strategies.trend_follower import TrendFollowerStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.vol_breakout import VolBreakoutStrategy

logger = structlog.get_logger(__name__)


class StrategySelector:
    """레짐 → 전략 매핑."""

    def __init__(self) -> None:
        self._strategies: dict[Regime, RegimeStrategy] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        trend = TrendFollowerStrategy()
        mean_rev = MeanReversionStrategy()
        vol_brk = VolBreakoutStrategy()

        self._strategies[Regime.TRENDING_UP] = mean_rev
        self._strategies[Regime.TRENDING_DOWN] = trend
        self._strategies[Regime.RANGING] = mean_rev
        self._strategies[Regime.VOLATILE] = vol_brk

    def select(self, regime: Regime) -> RegimeStrategy:
        """레짐에 맞는 전략을 반환한다."""
        strategy = self._strategies.get(regime)
        if strategy is None:
            logger.warning("no_strategy_for_regime", regime=regime.value)
            return self._strategies[Regime.RANGING]
        return strategy

    def register(self, regime: Regime, strategy: RegimeStrategy) -> None:
        """커스텀 전략 등록 (테스트/확장용)."""
        self._strategies[regime] = strategy

    @property
    def all_strategies(self) -> dict[Regime, RegimeStrategy]:
        return dict(self._strategies)
