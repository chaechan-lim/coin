"""
SafeOrderPipeline — 모든 주문의 단일 검증 경로.

주문 실행의 모든 단계를 원자적으로 관리한다:
1. Pre-validation: 잔고 확인, BalanceGuard 검증
2. DB 레코드 선생성 (pending 상태)
3. 거래소 실행
4. Post-validation: 실행 결과 검증, DB 업데이트
5. 포지션 + 잔고 반영

실패 시 자동 롤백하여 고아 포지션/잔고 스파이크를 방지.
"""
import structlog
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.enums import Direction
from core.models import Order, Trade
from core.event_bus import emit_event
from core.utils import utcnow
from exchange.base import ExchangeAdapter
from engine.balance_guard import BalanceGuard
from engine.portfolio_manager import PortfolioManager
from engine.order_manager import OrderManager

logger = structlog.get_logger(__name__)


@dataclass
class OrderRequest:
    """주문 요청 데이터."""
    symbol: str
    direction: Direction      # LONG or SHORT
    action: str               # "open" or "close"
    quantity: float
    price: float              # 현재 시장가 (시장가 주문의 기준)
    margin: float             # 사용할 마진 (open 시)
    leverage: int
    strategy_name: str
    confidence: float
    tier: str = "tier1"
    entry_price: float = 0.0  # close 시 진입가


@dataclass
class OrderResponse:
    """주문 실행 결과."""
    success: bool
    order_id: int | None = None
    executed_price: float = 0.0
    executed_quantity: float = 0.0
    fee: float = 0.0
    error: str = ""
    cash_before: float = 0.0
    cash_after: float = 0.0


class SafeOrderPipeline:
    """안전 주문 파이프라인.

    모든 v2 엔진의 주문은 이 파이프라인을 통과해야 한다.
    직접 OrderManager.create_order()를 호출하면 안 됨.
    """

    def __init__(
        self,
        order_manager: OrderManager,
        portfolio_manager: PortfolioManager,
        balance_guard: BalanceGuard,
        exchange: ExchangeAdapter,
        leverage: int = 3,
        futures_fee_rate: float = 0.0004,  # 0.04% maker/taker
    ):
        self._om = order_manager
        self._pm = portfolio_manager
        self._guard = balance_guard
        self._exchange = exchange
        self._leverage = leverage
        self._fee_rate = futures_fee_rate

    async def execute_order(
        self,
        session: AsyncSession,
        request: OrderRequest,
    ) -> OrderResponse:
        """주문 실행 — 전체 파이프라인.

        1. Pre-validation
        2. 거래소 실행
        3. DB 기록 + 포지션/잔고 반영
        4. Post-validation
        """
        cash_before = self._pm.cash_balance

        # ── 1. Pre-validation ──
        if request.action == "open":
            valid, reason = self._validate_open(request, cash_before)
        else:
            valid, reason = self._validate_close(request)

        if not valid:
            logger.warning(
                "order_rejected",
                symbol=request.symbol,
                action=request.action,
                reason=reason,
            )
            return OrderResponse(success=False, error=reason, cash_before=cash_before)

        # BalanceGuard 체크
        if self._guard.is_paused:
            return OrderResponse(
                success=False, error="balance_guard_paused", cash_before=cash_before,
            )

        # ── 2. 거래소 실행 ──
        try:
            side = self._determine_side(request)
            exec_result = await self._execute_on_exchange(
                request.symbol, side, request.quantity,
            )
        except Exception as e:
            error_msg = f"exchange_error: {str(e)}"
            logger.error("order_exchange_failed", symbol=request.symbol, error=str(e))
            return OrderResponse(
                success=False, error=error_msg, cash_before=cash_before,
            )

        if exec_result.filled <= 0:
            return OrderResponse(
                success=False,
                error="order_not_filled",
                cash_before=cash_before,
            )

        # ── 3. DB 기록 + 포지션/잔고 반영 ──
        try:
            order_id = await self._record_and_update(
                session, request, exec_result, side, cash_before,
            )
        except Exception as e:
            # DB 실패는 치명적 — 거래소에서는 실행됨
            logger.critical(
                "order_db_failed_after_exchange",
                symbol=request.symbol,
                side=side,
                filled=exec_result.filled,
                error=str(e),
            )
            await emit_event(
                "critical", "safe_order",
                f"DB 기록 실패 — 거래소 주문은 실행됨: {request.symbol} {side}",
                detail=str(e),
            )
            return OrderResponse(
                success=False,
                error=f"db_error_after_execution: {e}",
                executed_price=exec_result.price,
                executed_quantity=exec_result.filled,
                cash_before=cash_before,
                cash_after=self._pm.cash_balance,
            )

        cash_after = self._pm.cash_balance

        # ── 4. Post-validation ──
        if request.action == "open":
            expected_change = request.margin + exec_result.fee
            valid, post_reason = self._guard.validate_order_post(
                cash_before, cash_after, expected_change,
            )
            if not valid:
                logger.warning(
                    "order_post_validation_warning",
                    symbol=request.symbol,
                    reason=post_reason,
                )

        logger.info(
            "order_executed",
            symbol=request.symbol,
            action=request.action,
            direction=request.direction.value,
            side=side,
            price=round(exec_result.price, 4),
            quantity=round(exec_result.filled, 6),
            fee=round(exec_result.fee, 6),
            cash_before=round(cash_before, 4),
            cash_after=round(cash_after, 4),
            strategy=request.strategy_name,
        )

        return OrderResponse(
            success=True,
            order_id=order_id,
            executed_price=exec_result.price,
            executed_quantity=exec_result.filled,
            fee=exec_result.fee,
            cash_before=cash_before,
            cash_after=cash_after,
        )

    def _validate_open(self, req: OrderRequest, cash: float) -> tuple[bool, str]:
        """신규 포지션 검증."""
        if req.quantity <= 0:
            return False, "invalid_quantity"
        if req.margin <= 0:
            return False, "invalid_margin"
        if req.margin > cash:
            return False, f"insufficient_cash: need {req.margin:.4f}, have {cash:.4f}"
        if req.price <= 0:
            return False, "invalid_price"

        # BalanceGuard pre-check
        valid, reason = self._guard.validate_order_pre(cash, req.margin)
        if not valid:
            return False, reason

        return True, "ok"

    def _validate_close(self, req: OrderRequest) -> tuple[bool, str]:
        """포지션 청산 검증."""
        if req.quantity <= 0:
            return False, "invalid_quantity"
        if req.price <= 0:
            return False, "invalid_price"
        return True, "ok"

    def _determine_side(self, req: OrderRequest) -> str:
        """주문 방향 결정 (거래소 side)."""
        if req.action == "open":
            return "buy" if req.direction == Direction.LONG else "sell"
        else:
            # close: 반대 방향
            return "sell" if req.direction == Direction.LONG else "buy"

    async def _execute_on_exchange(
        self, symbol: str, side: str, quantity: float,
    ):
        """거래소에서 시장가 주문 실행."""
        if side == "buy":
            return await self._exchange.create_market_buy(symbol, quantity)
        else:
            return await self._exchange.create_market_sell(symbol, quantity)

    async def _record_and_update(
        self,
        session: AsyncSession,
        request: OrderRequest,
        exec_result,
        side: str,
        cash_before: float,
    ) -> int:
        """DB 기록 + 포지션/잔고 업데이트.

        Returns:
            생성된 Order ID.
        """
        from strategies.base import Signal
        from core.enums import SignalType

        now = utcnow()
        exec_price = exec_result.price if exec_result.filled > 0 else request.price
        exec_qty = exec_result.filled
        cost = exec_result.cost or (exec_price * exec_qty)
        fee = exec_result.fee or (cost * self._fee_rate)

        # PnL 계산 (close 시)
        realized_pnl = None
        realized_pnl_pct = None
        if request.action == "close" and request.entry_price > 0:
            if request.direction == Direction.LONG:
                pnl_pct = (exec_price - request.entry_price) / request.entry_price
            else:
                pnl_pct = (request.entry_price - exec_price) / request.entry_price
            realized_pnl_pct = pnl_pct * request.leverage * 100
            realized_pnl = request.margin * request.leverage * pnl_pct - fee

        # Order record
        order = Order(
            exchange=self._guard._exchange_name,
            exchange_order_id=exec_result.order_id,
            symbol=request.symbol,
            side=side,
            order_type="market",
            status="filled",
            requested_price=request.price,
            executed_price=exec_price,
            requested_quantity=request.quantity,
            executed_quantity=exec_qty,
            fee=fee,
            fee_currency="USDT",
            is_paper=(self._pm._is_paper),
            direction=request.direction.value,
            leverage=request.leverage,
            margin_used=request.margin if request.action == "open" else None,
            entry_price=request.entry_price if request.action == "close" else None,
            realized_pnl=realized_pnl,
            realized_pnl_pct=realized_pnl_pct,
            strategy_name=request.strategy_name,
            signal_confidence=request.confidence,
            signal_reason=f"v2_{request.tier}_{request.action}",
            filled_at=now,
        )
        session.add(order)
        await session.flush()

        # Trade record
        trade = Trade(
            exchange=self._guard._exchange_name,
            order_id=order.id,
            symbol=request.symbol,
            side=side,
            price=exec_price,
            quantity=exec_qty,
            cost=cost,
            fee=fee,
            is_paper=self._pm._is_paper,
            executed_at=now,
        )
        session.add(trade)

        # 포지션 + 잔고 반영
        if request.action == "open":
            await self._pm.update_position_on_buy(
                session, request.symbol, exec_qty, exec_price, cost, fee,
                is_surge=(request.tier == "tier2"),
                strategy_name=request.strategy_name,
            )
        else:
            await self._pm.update_position_on_sell(
                session, request.symbol, exec_qty, exec_price, cost, fee,
            )

        await session.flush()
        return order.id
