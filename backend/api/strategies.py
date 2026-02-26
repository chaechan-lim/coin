from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from typing import Optional
from datetime import timedelta
from core.utils import utcnow, ensure_aware

from db.session import get_db
from core.models import StrategyLog, Order
from core.schemas import (
    StrategyResponse,
    StrategyPerformanceResponse,
    StrategyLogResponse,
    StrategyParamsUpdate,
    StrategyWeightUpdate,
)
from api.dependencies import engine_registry

router = APIRouter(prefix="/strategies", tags=["strategies"])

# Legacy setters for backward compatibility
_engine = None
_combiner = None


def set_engine_and_combiner(engine, combiner):
    global _engine, _combiner
    _engine = engine
    _combiner = combiner


def _get_engine(exchange: str):
    eng = engine_registry.get_engine(exchange)
    if eng:
        return eng
    return _engine


def _get_combiner(exchange: str):
    comb = engine_registry.get_combiner(exchange)
    if comb:
        return comb
    return _combiner


@router.get("", response_model=list[StrategyResponse])
async def list_strategies(exchange: str = Query("bithumb")):
    eng = _get_engine(exchange)
    comb = _get_combiner(exchange)
    if not eng:
        return []

    strategies = []
    for name, strategy in eng.strategies.items():
        weight = comb.weights.get(name, 0.0) if comb else 0.0
        strategies.append(
            StrategyResponse(
                name=strategy.name,
                display_name=strategy.display_name,
                applicable_market_types=strategy.applicable_market_types,
                default_coins=strategy.default_coins,
                required_timeframe=strategy.required_timeframe,
                params=strategy.get_params(),
                current_weight=weight,
            )
        )
    return strategies


@router.get("/{name}/performance", response_model=StrategyPerformanceResponse)
async def get_strategy_performance(
    name: str,
    period: str = Query("30d"),
    exchange: str = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    now = utcnow()
    period_map = {"7d": 7, "30d": 30, "90d": 90}
    days = period_map.get(period, 30)
    start = now - timedelta(days=days)

    # 1) 모든 체결 주문을 시간순 조회 (매수 원가 계산을 위해 전체 기간)
    result = await session.execute(
        select(Order)
        .where(Order.status == "filled", Order.exchange == exchange)
        .order_by(Order.created_at)
    )
    all_orders = list(result.scalars().all())

    # 2) 심볼별 포지션 추적 (FIFO 원가 계산)
    from collections import defaultdict
    positions: dict[str, dict] = defaultdict(lambda: {"qty": 0.0, "cost": 0.0})

    winning = 0
    losing = 0
    total_pnl = 0.0
    returns: list[float] = []
    trade_count = 0

    for order in all_orders:
        sym = order.symbol
        qty = order.executed_quantity or order.requested_quantity
        price = order.executed_price or order.requested_price
        fee = order.fee or 0

        if not price or not qty:
            continue

        if order.side == "buy":
            positions[sym]["cost"] += price * qty + fee
            positions[sym]["qty"] += qty
            # 기간 내 매수만 카운트
            if order.strategy_name == name and ensure_aware(order.created_at) >= start:
                trade_count += 1

        elif order.side == "sell":
            pos = positions[sym]
            if pos["qty"] > 0:
                avg_buy = pos["cost"] / pos["qty"]
                sell_qty = min(qty, pos["qty"])
                pnl = (price - avg_buy) * sell_qty - fee

                # 기간 내 + 해당 전략 매도만 성과에 반영
                if order.strategy_name == name and ensure_aware(order.created_at) >= start:
                    total_pnl += pnl
                    ret_pct = pnl / (avg_buy * sell_qty) * 100 if avg_buy > 0 else 0
                    returns.append(ret_pct)
                    trade_count += 1
                    if pnl > 0:
                        winning += 1
                    else:
                        losing += 1

                # 포지션 차감 (항상)
                pos["cost"] -= avg_buy * sell_qty
                pos["qty"] -= sell_qty

    win_rate = winning / (winning + losing) * 100 if (winning + losing) > 0 else 0
    avg_return = sum(returns) / len(returns) if returns else 0

    return StrategyPerformanceResponse(
        strategy_name=name,
        total_trades=trade_count,
        winning_trades=winning,
        losing_trades=losing,
        win_rate=round(win_rate, 1),
        total_pnl=round(total_pnl, 0),
        avg_return_pct=round(avg_return, 2),
    )


@router.put("/{name}/params")
async def update_strategy_params(name: str, update: StrategyParamsUpdate, exchange: str = Query("bithumb")):
    eng = _get_engine(exchange)
    if not eng or name not in eng.strategies:
        raise HTTPException(status_code=404, detail=f"Strategy '{name}' not found")

    eng.strategies[name].set_params(update.params)
    return {"status": "ok", "strategy": name, "params": eng.strategies[name].get_params()}


@router.put("/{name}/weight")
async def update_strategy_weight(name: str, update: StrategyWeightUpdate, exchange: str = Query("bithumb")):
    comb = _get_combiner(exchange)
    if not comb:
        raise HTTPException(status_code=500, detail="Combiner not initialized")

    comb.weights[name] = update.weight
    return {"status": "ok", "strategy": name, "weight": update.weight}


@router.get("/comparison")
async def compare_strategies(
    period: str = Query("30d"),
    exchange: str = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    eng = _get_engine(exchange)
    if not eng:
        return []

    results = []
    for name in eng.strategies:
        perf = await get_strategy_performance(name, period, exchange, session)
        results.append(perf)
    return results


# -- Strategy Logs --
@router.get("/logs", response_model=list[StrategyLogResponse])
async def get_strategy_logs(
    symbol: Optional[str] = None,
    strategy: Optional[str] = None,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    exchange: str = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    query = (
        select(StrategyLog)
        .where(StrategyLog.exchange == exchange)
        .order_by(desc(StrategyLog.logged_at))
    )

    if symbol:
        query = query.where(StrategyLog.symbol == symbol)
    if strategy:
        query = query.where(StrategyLog.strategy_name == strategy)

    query = query.offset((page - 1) * size).limit(size)
    result = await session.execute(query)
    logs = result.scalars().all()

    return [
        StrategyLogResponse(
            id=l.id,
            strategy_name=l.strategy_name,
            symbol=l.symbol,
            signal_type=l.signal_type,
            confidence=l.confidence,
            reason=l.reason,
            indicators=l.indicators,
            was_executed=l.was_executed,
            order_id=l.order_id,
            logged_at=l.logged_at,
        )
        for l in logs
    ]
