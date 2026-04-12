from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, or_
from typing import Optional
from datetime import timedelta
from core.utils import utcnow, ensure_aware

from db.session import get_db
from core.models import Order, Trade, ServerEvent
from core.schemas import (
    OrderResponse,
    TradeResponse,
    PairsTradeGroupResponse,
    PairsTradeGroupDetailResponse,
    DonchianFuturesTradeGroupResponse,
    DonchianFuturesTradeGroupDetailResponse,
    ServerEventResponse,
)
from api.dependencies import ExchangeNameType

router = APIRouter(prefix="/trades", tags=["trades"])

# 선물 조회 시 서지 거래도 병합
_FUTURES_EXCHANGES = (
    "binance_futures", "binance_surge",
    "binance_donchian_futures", "binance_pairs",
    "binance_momentum", "binance_hmm",
)
_SPOT_EXCHANGES = (
    "binance_spot",
    "binance_donchian", "binance_fgdca",
)


def _parse_reason_tags(reason: str | None) -> dict[str, str]:
    if not reason:
        return {}
    tags: dict[str, str] = {}
    for token in reason.split(":"):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        tags[key] = value
    return tags


def _order_to_response(o: Order) -> OrderResponse:
    return OrderResponse(
        id=o.id,
        exchange=o.exchange,
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
        direction=o.direction,
        leverage=o.leverage,
        margin_used=o.margin_used,
        entry_price=getattr(o, 'entry_price', None),
        realized_pnl=getattr(o, 'realized_pnl', None),
        realized_pnl_pct=getattr(o, 'realized_pnl_pct', None),
        strategy_name=o.strategy_name,
        signal_confidence=o.signal_confidence,
        signal_reason=o.signal_reason,
        trade_group_id=getattr(o, "trade_group_id", None),
        trade_group_type=getattr(o, "trade_group_type", None),
        combined_score=o.combined_score,
        contributing_strategies=o.contributing_strategies,
        created_at=o.created_at,
        filled_at=o.filled_at,
    )


def _build_pairs_groups(orders: list[Order]) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for order in orders:
        tags = _parse_reason_tags(order.signal_reason)
        trade_id = getattr(order, "trade_group_id", None) or tags.get("trade")
        if not trade_id:
            continue
        group_type = getattr(order, "trade_group_type", None)
        group = groups.setdefault(
            trade_id,
            {
                "trade_id": trade_id,
                "pair_direction": tags.get("pair_direction", "unknown"),
                "symbols": set(),
                "exit_symbols": set(),
                "orders": [],
                "opened_at": order.created_at,
                "closed_at": None,
                "realized_pnl": 0.0,
                "total_fees": 0.0,
                "has_exit": False,
            },
        )
        group["orders"].append(order)
        group["symbols"].add(order.symbol)
        group["opened_at"] = min(group["opened_at"], order.created_at)
        group["total_fees"] += float(order.fee or 0.0)
        group["realized_pnl"] += float(order.realized_pnl or 0.0)
        if group["pair_direction"] == "unknown" and tags.get("pair_direction"):
            group["pair_direction"] = tags["pair_direction"]
        is_exit = group_type == "pairs_exit" or (
            order.signal_reason and order.signal_reason.startswith("pairs_exit:")
        )
        if is_exit:
            group["exit_symbols"].add(order.symbol)
            group["closed_at"] = order.created_at if group["closed_at"] is None else max(group["closed_at"], order.created_at)
    for group in groups.values():
        group["has_exit"] = bool(group["symbols"]) and group["exit_symbols"] == group["symbols"]
        if not group["has_exit"]:
            group["closed_at"] = None
    return groups


def _group_to_response(group: dict) -> PairsTradeGroupResponse:
    return PairsTradeGroupResponse(
        trade_id=group["trade_id"],
        pair_direction=group["pair_direction"],
        status="closed" if group["has_exit"] else "open",
        symbols=sorted(group["symbols"]),
        opened_at=group["opened_at"],
        closed_at=group["closed_at"],
        realized_pnl=round(group["realized_pnl"], 4),
        total_fees=round(group["total_fees"], 4),
        order_ids=[order.id for order in sorted(group["orders"], key=lambda x: x.created_at)],
    )


def _build_donchian_futures_groups(orders: list[Order]) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for order in orders:
        tags = _parse_reason_tags(order.signal_reason)
        trade_id = getattr(order, "trade_group_id", None) or tags.get("trade")
        if not trade_id:
            continue
        group_type = getattr(order, "trade_group_type", None)
        group = groups.setdefault(
            trade_id,
            {
                "trade_id": trade_id,
                "symbol": order.symbol,
                "direction": order.direction or tags.get("direction", "unknown"),
                "orders": [],
                "opened_at": order.created_at,
                "closed_at": None,
                "realized_pnl": 0.0,
                "total_fees": 0.0,
                "has_exit": False,
            },
        )
        group["orders"].append(order)
        group["opened_at"] = min(group["opened_at"], order.created_at)
        group["total_fees"] += float(order.fee or 0.0)
        group["realized_pnl"] += float(order.realized_pnl or 0.0)
        if group["direction"] == "unknown" and (order.direction or tags.get("direction")):
            group["direction"] = order.direction or tags.get("direction", "unknown")
        is_exit = group_type == "donchian_futures_exit" or (
            order.signal_reason and order.signal_reason.startswith("donchian_futures_bi_exit:")
        )
        if is_exit:
            group["has_exit"] = True
            group["closed_at"] = order.created_at if group["closed_at"] is None else max(group["closed_at"], order.created_at)
    return groups


def _donchian_group_to_response(group: dict) -> DonchianFuturesTradeGroupResponse:
    return DonchianFuturesTradeGroupResponse(
        trade_id=group["trade_id"],
        symbol=group["symbol"],
        direction=group["direction"],
        status="closed" if group["has_exit"] else "open",
        opened_at=group["opened_at"],
        closed_at=group["closed_at"],
        realized_pnl=round(group["realized_pnl"], 4),
        total_fees=round(group["total_fees"], 4),
        order_ids=[order.id for order in sorted(group["orders"], key=lambda x: x.created_at)],
    )


@router.get("", response_model=list[OrderResponse])
async def get_trades(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    symbol: Optional[str] = None,
    strategy: Optional[str] = None,
    side: Optional[str] = None,
    status: Optional[str] = Query(None, pattern="^(filled|open|cancelled|failed|all)$"),
    exchange: ExchangeNameType = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    if exchange == "binance_futures":
        exchange_filter = Order.exchange.in_(_FUTURES_EXCHANGES)
    elif exchange == "binance_spot":
        exchange_filter = Order.exchange.in_(_SPOT_EXCHANGES)
    else:
        exchange_filter = Order.exchange == exchange
    query = (
        select(Order)
        .where(exchange_filter)
        .order_by(desc(Order.created_at))
    )

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

    return [_order_to_response(o) for o in orders]


@router.get("/pairs/groups", response_model=list[PairsTradeGroupResponse])
async def get_pairs_trade_groups(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str = Query("all", pattern="^(open|closed|all)$"),
    exchange: ExchangeNameType = Query("binance_pairs"),
    session: AsyncSession = Depends(get_db),
):
    result = await session.execute(
        select(Order)
        .where(Order.exchange == exchange)
        .where(Order.strategy_name == "pairs_trading_live")
        .where(Order.status == "filled")
        .order_by(desc(Order.created_at))
    )
    groups = list(_build_pairs_groups(list(result.scalars().all())).values())
    groups.sort(key=lambda item: item["opened_at"], reverse=True)
    if status != "all":
        want_closed = status == "closed"
        groups = [group for group in groups if bool(group["has_exit"]) is want_closed]
    groups = groups[(page - 1) * size : page * size]
    return [_group_to_response(group) for group in groups]


@router.get("/pairs/groups/{trade_id}", response_model=PairsTradeGroupDetailResponse)
async def get_pairs_trade_group_detail(
    trade_id: str,
    exchange: ExchangeNameType = Query("binance_pairs"),
    session: AsyncSession = Depends(get_db),
):
    result = await session.execute(
        select(Order)
        .where(Order.exchange == exchange)
        .where(Order.strategy_name == "pairs_trading_live")
        .where(Order.status == "filled")
        .order_by(Order.created_at)
    )
    groups = _build_pairs_groups(list(result.scalars().all()))
    group = groups.get(trade_id)
    if group is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Pairs trade group not found")

    event_result = await session.execute(
        select(ServerEvent)
        .where(ServerEvent.category == "pairs_trade")
        .order_by(ServerEvent.created_at)
    )
    journal = []
    for event in event_result.scalars().all():
        metadata = event.metadata_ or {}
        if metadata.get("exchange") and metadata.get("exchange") != exchange:
            continue
        if metadata.get("trade_id") != trade_id:
            continue
        journal.append(
            ServerEventResponse(
                id=event.id,
                level=event.level,
                category=event.category,
                title=event.title,
                detail=event.detail,
                metadata=metadata,
                created_at=event.created_at,
            )
        )

    base = _group_to_response(group)
    return PairsTradeGroupDetailResponse(
        **base.model_dump(),
        orders=[_order_to_response(order) for order in sorted(group["orders"], key=lambda x: x.created_at)],
        journal=journal,
    )


@router.get("/donchian-futures/groups", response_model=list[DonchianFuturesTradeGroupResponse])
async def get_donchian_futures_trade_groups(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str = Query("all", pattern="^(open|closed|all)$"),
    exchange: ExchangeNameType = Query("binance_donchian_futures"),
    session: AsyncSession = Depends(get_db),
):
    result = await session.execute(
        select(Order)
        .where(Order.exchange == exchange)
        .where(Order.strategy_name == "donchian_futures_bi")
        .where(Order.status == "filled")
        .order_by(desc(Order.created_at))
    )
    groups = list(_build_donchian_futures_groups(list(result.scalars().all())).values())
    groups.sort(key=lambda item: item["opened_at"], reverse=True)
    if status != "all":
        want_closed = status == "closed"
        groups = [group for group in groups if bool(group["has_exit"]) is want_closed]
    groups = groups[(page - 1) * size : page * size]
    return [_donchian_group_to_response(group) for group in groups]


@router.get("/donchian-futures/groups/{trade_id}", response_model=DonchianFuturesTradeGroupDetailResponse)
async def get_donchian_futures_trade_group_detail(
    trade_id: str,
    exchange: ExchangeNameType = Query("binance_donchian_futures"),
    session: AsyncSession = Depends(get_db),
):
    result = await session.execute(
        select(Order)
        .where(Order.exchange == exchange)
        .where(Order.strategy_name == "donchian_futures_bi")
        .where(Order.status == "filled")
        .order_by(Order.created_at)
    )
    groups = _build_donchian_futures_groups(list(result.scalars().all()))
    group = groups.get(trade_id)
    if group is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Donchian futures trade group not found")

    event_result = await session.execute(
        select(ServerEvent)
        .where(ServerEvent.category == "donchian_futures_trade")
        .order_by(ServerEvent.created_at)
    )
    journal = []
    for event in event_result.scalars().all():
        metadata = event.metadata_ or {}
        if metadata.get("exchange") and metadata.get("exchange") != exchange:
            continue
        if metadata.get("trade_id") != trade_id:
            continue
        journal.append(
            ServerEventResponse(
                id=event.id,
                level=event.level,
                category=event.category,
                title=event.title,
                detail=event.detail,
                metadata=metadata,
                created_at=event.created_at,
            )
        )

    base = _donchian_group_to_response(group)
    return DonchianFuturesTradeGroupDetailResponse(
        **base.model_dump(),
        orders=[_order_to_response(order) for order in sorted(group["orders"], key=lambda x: x.created_at)],
        journal=journal,
    )


@router.get("/summary")
async def get_trade_summary(
    period: str = Query("7d", pattern="^(today|7d|30d|all)$"),
    exchange: ExchangeNameType = Query("bithumb"),
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

    # 전체 체결 주문 시간순 (선물/현물 R&D 병합)
    if exchange == "binance_futures":
        ex_filter = Order.exchange.in_(_FUTURES_EXCHANGES)
    elif exchange == "binance_spot":
        ex_filter = Order.exchange.in_(_SPOT_EXCHANGES)
    else:
        ex_filter = Order.exchange == exchange
    result = await session.execute(
        select(Order)
        .where(Order.status == "filled", ex_filter)
        .order_by(Order.created_at)
    )
    all_orders = list(result.scalars().all())

    from collections import defaultdict
    positions: dict[str, dict] = defaultdict(lambda: {"qty": 0.0, "cost": 0.0})

    open_count = 0
    close_count = 0
    winning = 0
    losing = 0
    total_pnl = 0.0

    for order in all_orders:
        sym = order.symbol
        qty = order.executed_quantity or order.requested_quantity
        price = order.executed_price or order.requested_price
        fee = order.fee or 0
        in_period = start is None or ensure_aware(order.created_at) >= start

        if not price or not qty:
            continue

        # 선물: realized_pnl이 있으면 청산 주문 (롱 sell / 숏 buy 모두 처리)
        if order.realized_pnl is not None:
            if in_period:
                close_count += 1
                total_pnl += order.realized_pnl
                if order.realized_pnl > 0:
                    winning += 1
                else:
                    losing += 1
        else:
            # 진입 주문 또는 PnL 미계산 현물 매도
            is_opening = (order.side == "buy" and order.direction != "short") or \
                         (order.side == "sell" and order.direction == "short")
            is_spot_sell = order.side == "sell" and not order.direction

            if is_opening or (order.side == "buy" and not order.direction):
                # 현물 매수 또는 선물 진입
                positions[sym]["cost"] += price * qty + fee
                positions[sym]["qty"] += qty
                if in_period:
                    open_count += 1
            elif is_spot_sell:
                # 현물 매도 (realized_pnl 없는 경우 FIFO 계산)
                pos = positions[sym]
                if pos["qty"] > 0:
                    avg_buy = pos["cost"] / pos["qty"]
                    sell_qty = min(qty, pos["qty"])
                    pnl = (price - avg_buy) * sell_qty - fee

                    if in_period:
                        close_count += 1
                        total_pnl += pnl
                        if pnl > 0:
                            winning += 1
                        else:
                            losing += 1

                    pos["cost"] -= avg_buy * sell_qty
                    pos["qty"] -= sell_qty
            else:
                if in_period:
                    open_count += 1

    return {
        "period": period,
        "total_trades": open_count + close_count,
        "buy_count": open_count,
        "sell_count": close_count,
        "winning_trades": winning,
        "losing_trades": losing,
        "win_rate": round(winning / (winning + losing) * 100, 1) if (winning + losing) > 0 else 0,
        "total_pnl": round(total_pnl, 2),
    }


@router.get("/{order_id}", response_model=OrderResponse)
async def get_trade_detail(order_id: int, session: AsyncSession = Depends(get_db)):
    result = await session.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Order not found")

    return _order_to_response(order)
