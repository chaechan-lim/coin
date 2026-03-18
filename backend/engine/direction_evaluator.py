"""
DirectionEvaluator — 방향별 독립 평가 프로토콜.

롱/숏 비대칭 전략 지원을 위한 평가자 인터페이스.
각 방향(롱/숏)이 독립적인 전략으로 진입/청산을 판단한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from core.enums import Direction
from engine.position_state_tracker import PositionState


@dataclass(frozen=True)
class DirectionDecision:
    """방향 평가 결정 — 불변 객체.

    action:
        'open'  — 신규 포지션 진입
        'close' — 기존 포지션 청산
        'hold'  — 유지 (아무 행동 없음)
    """

    action: str  # 'open', 'close', 'hold'
    direction: Direction | None  # LONG or SHORT (open 시 필수)
    confidence: float  # 0.0-1.0
    sizing_factor: float  # 0.0-1.0
    stop_loss_atr: float
    take_profit_atr: float
    reason: str
    strategy_name: str
    indicators: dict = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.action == "open"

    @property
    def is_close(self) -> bool:
        return self.action == "close"

    @property
    def is_hold(self) -> bool:
        return self.action == "hold"


@runtime_checkable
class DirectionEvaluator(Protocol):
    """방향별 독립 평가자 프로토콜.

    롱/숏 각각의 진입/청산을 독립적으로 판단한다.
    """

    async def evaluate(
        self,
        symbol: str,
        current_position: PositionState | None,
    ) -> DirectionDecision:
        """주어진 심볼에 대해 방향 결정을 반환한다.

        Args:
            symbol: 거래 심볼 (e.g., "BTC/USDT")
            current_position: 현재 포지션 상태 (없으면 None)

        Returns:
            DirectionDecision: open/close/hold 결정
        """
        ...

    @property
    def eval_interval_sec(self) -> int:
        """평가 주기 (초)."""
        ...
