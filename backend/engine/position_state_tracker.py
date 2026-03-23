"""
PositionStateTracker — 인메모리 포지션 상태 관리.

DB 포지션과 별도로 실시간 SL/TP/trailing/sizing 상태를 추적한다.
DB 복원과 실시간 업데이트를 모두 지원.
"""
import structlog
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.models import Position
from core.enums import Direction

logger = structlog.get_logger(__name__)


@dataclass
class PositionState:
    """단일 포지션의 실시간 상태."""
    symbol: str
    direction: Direction
    quantity: float
    entry_price: float
    margin: float           # total_invested (마진)
    leverage: int
    extreme_price: float    # 롱: highest, 숏: lowest
    stop_loss_atr: float    # ATR 배수 기반 SL
    take_profit_atr: float  # ATR 배수 기반 TP
    trailing_activation_atr: float
    trailing_stop_atr: float
    trailing_active: bool = False
    entered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tier: str = "tier1"     # "tier1" or "tier2"
    strategy_name: str = ""
    confidence: float = 0.0
    sizing_factor: float = 1.0  # ATR 기반 포지션 비율 (0.1~1.0)

    @property
    def is_long(self) -> bool:
        return self.direction == Direction.LONG

    @property
    def is_short(self) -> bool:
        return self.direction == Direction.SHORT

    def check_stop_loss(self, current_price: float, atr: float) -> bool:
        """SL 히트 여부 확인."""
        if atr <= 0 or self.entry_price <= 0:
            return False
        sl_distance = atr * self.stop_loss_atr
        if self.is_long:
            sl_price = self.entry_price - sl_distance
            return current_price <= sl_price
        else:
            sl_price = self.entry_price + sl_distance
            return current_price >= sl_price

    def check_take_profit(self, current_price: float, atr: float) -> bool:
        """TP 히트 여부 확인."""
        if atr <= 0 or self.entry_price <= 0:
            return False
        tp_distance = atr * self.take_profit_atr
        if self.is_long:
            tp_price = self.entry_price + tp_distance
            return current_price >= tp_price
        else:
            tp_price = self.entry_price - tp_distance
            return current_price <= tp_price

    def check_trailing_stop(self, current_price: float, atr: float) -> bool:
        """트레일링 스탑 히트 여부. 활성화 조건도 체크."""
        if atr <= 0 or self.entry_price <= 0:
            return False

        # 트레일링 활성화 확인
        activation_dist = atr * self.trailing_activation_atr
        if self.is_long:
            if current_price >= self.entry_price + activation_dist:
                self.trailing_active = True
        else:
            if current_price <= self.entry_price - activation_dist:
                self.trailing_active = True

        if not self.trailing_active:
            return False

        # 트레일링 스탑 체크
        trail_dist = atr * self.trailing_stop_atr
        if self.is_long:
            trail_price = self.extreme_price - trail_dist
            return current_price <= trail_price
        else:
            trail_price = self.extreme_price + trail_dist
            return current_price >= trail_price

    def update_extreme(self, current_price: float) -> None:
        """극단 가격 업데이트 (트레일링용)."""
        if self.is_long:
            if current_price > self.extreme_price:
                self.extreme_price = current_price
        else:
            if current_price < self.extreme_price:
                self.extreme_price = current_price

    def sl_price(self, atr: float) -> float | None:
        """현재 SL 가격 계산."""
        if atr <= 0 or self.entry_price <= 0:
            return None
        dist = atr * self.stop_loss_atr
        return self.entry_price - dist if self.is_long else self.entry_price + dist

    def tp_price(self, atr: float) -> float | None:
        """현재 TP 가격 계산."""
        if atr <= 0 or self.entry_price <= 0:
            return None
        dist = atr * self.take_profit_atr
        return self.entry_price + dist if self.is_long else self.entry_price - dist


class PositionStateTracker:
    """포지션 상태 관리자 — 인메모리 + DB 복원."""

    def __init__(self) -> None:
        self._positions: dict[str, PositionState] = {}
        self._last_atr: dict[str, float] = {}  # WS SL/TP 체크용 ATR 캐시

    @property
    def positions(self) -> dict[str, PositionState]:
        return self._positions

    def get(self, symbol: str) -> PositionState | None:
        return self._positions.get(symbol)

    def has_position(self, symbol: str) -> bool:
        return symbol in self._positions

    def open_position(self, state: PositionState) -> None:
        """새 포지션 등록."""
        self._positions[state.symbol] = state
        logger.info(
            "position_opened",
            symbol=state.symbol,
            direction=state.direction.value,
            entry=state.entry_price,
            sizing=state.sizing_factor,
            tier=state.tier,
        )

    def close_position(self, symbol: str) -> PositionState | None:
        """포지션 제거. 반환: 제거된 상태 or None."""
        state = self._positions.pop(symbol, None)
        if state:
            logger.info(
                "position_closed",
                symbol=symbol,
                direction=state.direction.value,
                entry=state.entry_price,
            )
        return state

    def update_direction(self, symbol: str, direction: Direction) -> None:
        """SAR: 방향 전환."""
        state = self._positions.get(symbol)
        if state:
            old = state.direction
            state.direction = direction
            logger.info("position_direction_changed", symbol=symbol, old=old.value, new=direction.value)

    def active_count(self, tier: str | None = None) -> int:
        """활성 포지션 수."""
        if tier is None:
            return len(self._positions)
        return sum(1 for p in self._positions.values() if p.tier == tier)

    def all_symbols(self) -> list[str]:
        return list(self._positions.keys())

    def update_atr(self, symbol: str, atr: float) -> None:
        """WS SL/TP 체크용 ATR 캐시 업데이트."""
        if atr > 0:
            self._last_atr[symbol] = atr

    def get_atr(self, symbol: str) -> float:
        """캐시된 ATR 반환 (없으면 0.0)."""
        return self._last_atr.get(symbol, 0.0)

    async def restore_from_db(self, session: AsyncSession, exchange_name: str) -> int:
        """DB에서 기존 포지션을 복원한다.

        Returns:
            복원된 포지션 수.
        """
        result = await session.execute(
            select(Position).where(
                Position.quantity > 0,
                Position.exchange == exchange_name,
            )
        )
        positions = list(result.scalars().all())
        restored = 0

        for pos in positions:
            direction = Direction.SHORT if pos.direction == "short" else Direction.LONG
            state = PositionState(
                symbol=pos.symbol,
                direction=direction,
                quantity=pos.quantity,
                entry_price=pos.average_buy_price,
                margin=pos.total_invested,
                leverage=pos.leverage or 3,
                extreme_price=pos.highest_price or pos.average_buy_price,
                stop_loss_atr=pos.stop_loss_pct or 1.5,
                take_profit_atr=pos.take_profit_pct or 3.0,
                trailing_activation_atr=pos.trailing_activation_pct or 2.0,
                trailing_stop_atr=pos.trailing_stop_pct or 1.0,
                trailing_active=pos.trailing_active or False,
                entered_at=pos.entered_at or datetime.now(timezone.utc),
                tier="tier2" if pos.is_surge else "tier1",
                strategy_name=pos.strategy_name or "",
            )
            self._positions[pos.symbol] = state
            restored += 1

        logger.info("positions_restored", count=restored, exchange=exchange_name)
        return restored

    async def persist_to_db(self, session: AsyncSession, exchange_name: str) -> int:
        """인메모리 상태를 DB에 영속화한다 (주기적 호출).

        Returns:
            업데이트된 포지션 수.
        """
        updated = 0
        for symbol, state in self._positions.items():
            result = await session.execute(
                select(Position).where(
                    Position.symbol == symbol,
                    Position.exchange == exchange_name,
                )
            )
            pos = result.scalar_one_or_none()
            if not pos:
                continue

            pos.stop_loss_pct = state.stop_loss_atr
            pos.take_profit_pct = state.take_profit_atr
            pos.trailing_activation_pct = state.trailing_activation_atr
            pos.trailing_stop_pct = state.trailing_stop_atr
            pos.trailing_active = state.trailing_active
            pos.highest_price = state.extreme_price
            pos.direction = state.direction.value
            pos.strategy_name = state.strategy_name
            updated += 1

        if updated > 0:
            await session.flush()

        return updated
