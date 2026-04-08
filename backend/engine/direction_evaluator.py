"""
DirectionEvaluator — 방향별 독립 평가 프로토콜.

롱/숏 비대칭 전략 지원을 위한 평가자 인터페이스.
각 방향(롱/숏)이 독립적인 전략으로 진입/청산을 판단한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from core.enums import Direction
from engine.position_state_tracker import PositionState


@dataclass(frozen=True)
class DirectionDecision:
    """방향 평가 결정 — 불변 객체.

    action:
        'open'  — 신규 포지션 진입
        'close' — 기존 포지션 청산
        'hold'  — 유지 (아무 행동 없음)

    Note:
        frozen=True이지만 ``indicators`` dict는 내부 값이 변경 가능하다.
        새로운 key/value를 추가해야 할 경우 ``dict(d.indicators)`` 로
        복사 후 사용할 것 (regime_evaluators.py 참고).
    """

    action: Literal["open", "close", "hold"]
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
        *,
        df_5m: Any = None,
        df_1h: Any = None,
    ) -> DirectionDecision:
        """주어진 심볼에 대해 방향 결정을 반환한다.

        Args:
            symbol: 거래 심볼 (e.g., "BTC/USDT")
            current_position: 현재 포지션 상태 (없으면 None)
            df_5m: 사전 조회된 5분 캔들 (None이면 내부에서 조회)
            df_1h: 사전 조회된 1시간 캔들 (None이면 내부에서 조회)

        Returns:
            DirectionDecision: open/close/hold 결정
        """
        ...

    @property
    def eval_interval_sec(self) -> int:
        """평가 주기 (초)."""
        ...


class NoOpDirectionEvaluator:
    """항상 HOLD를 반환하는 더미 평가자.

    Long-only 모드 (현물)에서 short_evaluator 자리에 사용.
    Tier1Manager가 short_evaluator를 호출해도 결정/주문/사이드이펙트 없음.
    """

    def __init__(self, eval_interval_sec: int = 240):
        self._eval_interval_sec = eval_interval_sec

    async def evaluate(
        self,
        symbol: str,
        current_position: PositionState | None,
        *,
        df_5m: Any = None,
        df_1h: Any = None,
    ) -> DirectionDecision:
        return DirectionDecision(
            action="hold",
            direction=None,
            confidence=0.0,
            sizing_factor=0.0,
            stop_loss_atr=0.0,
            take_profit_atr=0.0,
            reason="long_only_mode",
            strategy_name="noop",
        )

    @property
    def eval_interval_sec(self) -> int:
        return self._eval_interval_sec
