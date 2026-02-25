from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from typing import Optional
from datetime import timedelta
from core.utils import utcnow

from db.session import get_db
from core.models import Order, Trade
from core.schemas import OrderResponse, TradeResponse

router = APIRouter(prefix="/trades", tags=["trades"])


@router.get("", response_model=list[OrderResponse])
async def get_trades(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    symbol: Optional[str] = None,
    strategy: Optional[str] = None,
    side: Optional[str] = None,
    status: Optional[str] = Query(None, pattern="^(filled|open|cancelled|failed|all)$"),
    session: AsyncSession = Depends(get_db),
):
    query = select(Order).order_by(desc(Order.created_at))

    # 기본: filled만 표시 (실패/취소 거래 숨김)
    if status == "all":
        pass  # 전체 표시
    elif status:
        query = query.where(Order.status == status)
    else:
        query = query.where(Order.status == "filled")

    if symbol:
        query = query.where(Order.symbol == symbol)
    if strategy:
        query = query.where(Order.strategy_name == strategy)
    if side:
        query = query.where(Order.side == side)

    query = query.offset((page - 1) * size).limit(size)
    result = await session.execute(query)
    orders = result.scalars().all()

    return [
        OrderResponse(
            id=o.id,
            symbol=o.symbol,
            side=o.side,
            order_type=o.order_type,
            status=o.status,
            requested_price=o.requested_price,
            executed_price=o.executed_price,
            requested_quantity=o.requested_quantity,
            executed_quantity=o.executed_quantity,
            fee=o.fee,
            is_paper=o.is_paper,
            strategy_name=o.strategy_name,
            signal_confidence=o.signal_confidence,
            signal_reason=o.signal_reason,
            combined_score=o.combined_score,
            contributing_strategies=o.contributing_strategies,
            created_at=o.created_at,
            filled_at=o.filled_at,
        )
        for o in orders
    ]


@router.get("/summary")
async def get_trade_summary(
    period: str = Query("7d", pattern="^(today|7d|30d|all)$"),
    session: AsyncSession = Depends(get_db),
):
    now = utcnow()

    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "7d":
        start = now - timedelta(days=7)
    elif period == "30d":
        start = now - timedelta(days=30)
    else:
        start = None

    # 전체 체결 주문 시간순 (매수 원가 계산용)
    result = await session.execute(
        select(Order).where(Order.status == "filled").order_by(Order.created_at)
    )
    all_orders = list(result.scalars().all())

    from collections import defaultdict
    positions: dict[str, dict] = defaultdict(lambda: {"qty": 0.0, "cost": 0.0})

    buy_count = 0
    sell_count = 0
    winning = 0
    losing = 0
    total_pnl = 0.0

    for order in all_orders:
        sym = order.symbol
        qty = order.executed_quantity or order.requested_quantity
        price = order.executed_price or order.requested_price
        fee = order.fee or 0
        in_period = start is None or order.created_at >= start

        if not price or not qty:
            continue

        if order.side == "buy":
            positions[sym]["cost"] += price * qty + fee
            positions[sym]["qty"] += qty
            if in_period:
                buy_count += 1
        elif order.side == "sell":
            pos = positions[sym]
            if pos["qty"] > 0:
                avg_buy = pos["cost"] / pos["qty"]
                sell_qty = min(qty, pos["qty"])
                pnl = (price - avg_buy) * sell_qty - fee

                if in_period:
                    sell_count += 1
                    total_pnl += pnl
                    if pnl > 0:
                        winning += 1
                    else:
                        losing += 1

                pos["cost"] -= avg_buy * sell_qty
                pos["qty"] -= sell_qty

    return {
        "period": period,
        "total_trades": buy_count + sell_count,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "winning_trades": winning,
        "losing_trades": losing,
        "win_rate": round(winning / (winning + losing) * 100, 1) if (winning + losing) > 0 else 0,
        "total_pnl": round(total_pnl, 0),
    }


@router.get("/{order_id}", response_model=OrderResponse)
async def get_trade_detail(order_id: int, session: AsyncSession = Depends(get_db)):
    result = await session.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Order not found")

    return OrderResponse(
        id=order.id,
        symbol=order.symbol,
        side=order.side,
        order_type=order.order_type,
        status=order.status,
        requested_price=order.requested_price,
        executed_price=order.executed_price,
        requested_quantity=order.requested_quantity,
        executed_quantity=order.executed_quantity,
        fee=order.fee,
        is_paper=order.is_paper,
        strategy_name=order.strategy_name,
        signal_confidence=order.signal_confidence,
        signal_reason=order.signal_reason,
        combined_score=order.combined_score,
        contributing_strategies=order.contributing_strategies,
        created_at=order.created_at,
        filled_at=order.filled_at,
    )
