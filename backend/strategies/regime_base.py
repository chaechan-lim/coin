"""
RegimeStrategy — 레짐 기반 전략 베이스 클래스.

기존 BaseStrategy(4h 캔들, Signal 반환)와 완전 분리.
5분 캔들 기반, StrategyDecision 반환.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pandas as pd

from core.enums import Direction, Regime

if TYPE_CHECKING:
    from engine.regime_detector import RegimeState


@dataclass(frozen=True)
class StrategyDecision:
    """전략 결정 — 불변 객체."""
    direction: Direction    # LONG, SHORT, FLAT
    confidence: float       # 0.0-1.0
    sizing_factor: float    # 0.0-1.0 (0.0 = 변경 없음)
    stop_loss_atr: float    # ATR 배수 (예: 1.5)
    take_profit_atr: float  # ATR 배수 (예: 3.0)
    reason: str
    strategy_name: str
    indicators: dict = field(default_factory=dict)

    @property
    def is_hold(self) -> bool:
        """변경 없음 (sizing_factor == 0)."""
        return self.sizing_factor == 0.0

    @property
    def is_entry(self) -> bool:
        """신규 진입 시그널."""
        return self.sizing_factor > 0.0 and self.direction != Direction.FLAT

    @property
    def is_exit(self) -> bool:
        """청산 시그널."""
        return self.direction == Direction.FLAT and not self.is_hold


class RegimeStrategy(ABC):
    """레짐별 전략 베이스 — 5분 캔들 기반."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def target_regimes(self) -> list[Regime]:
        ...

    @abstractmethod
    async def evaluate(
        self,
        df_5m: pd.DataFrame,
        df_1h: pd.DataFrame,
        regime: "RegimeState",
        current_position: Direction | None,
    ) -> StrategyDecision:
        ...

    def _calc_sizing(self, confidence: float, atr: float, close: float) -> float:
        """ATR 기반 사이징: 변동성 낮으면 크게, 높으면 작게."""
        if close <= 0:
            return 0.1
        atr_pct = atr / close * 100
        base = 0.5 + confidence * 0.3  # 0.5-0.8
        if atr_pct < 1.0:
            return min(1.0, base * 1.3)  # 저변동: 130%
        elif atr_pct > 3.0:
            return max(0.1, base * 0.5)  # 고변동: 50%
        return base
