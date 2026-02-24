from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from typing import Optional
from datetime import timedelta
from core.utils import utcnow

from db.session import get_db
from core.models import StrategyLog, Order
from core.schemas import (
    StrategyResponse,
    StrategyPerformanceResponse,
    StrategyLogResponse,
    StrategyParamsUpdate,
    StrategyWeightUpdate,
)

router = APIRouter(prefix="/strategies", tags=["strategies"])

# Will be set from main.py
_engine = None
_combiner = None


def set_engine_and_combiner(engine, combiner):
    global _engine, _combiner
    _engine = engine
    _combiner = combiner


@router.get("", response_model=list[StrategyResponse])
async def list_strategies():
    if not _engine:
        return []

    strategies = []
    for name, strategy in _engine.strategies.items():
        weight = _combiner.weights.get(name, 0.0) if _combiner else 0.0
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
    session: AsyncSession = Depends(get_db),
):
    now = utcnow()
    period_map = {"7d": 7, "30d": 30, "90d": 90}
    days = period_map.get(period, 30)
    start = now - timedelta(days=days)

    # Get all filled orders for this strategy
    result = await session.execute(
        select(Order)
        .where(Order.strategy_name == name, Order.status == "filled", Order.created_at >= start)
    )
    orders = list(result.scalars().all())

    sell_orders = [o for o in orders if o.side == "sell"]
    winning = 0
    losing = 0
    total_pnl = 0.0
    returns = []

    for sell in sell_orders:
        if sell.executed_price and sell.requested_quantity:
            # Approximate P&L
            cost = (sell.requested_price or sell.executed_price) * sell.requested_quantity
            revenue = sell.executed_price * (sell.executed_quantity or sell.requested_quantity)
            pnl = revenue - cost - (sell.fee or 0)
            total_pnl += pnl
            ret_pct = pnl / cost * 100 if cost > 0 else 0
            returns.append(ret_pct)
            if pnl > 0:
                winning += 1
            else:
                losing += 1

    total_trades = len(orders)
    win_rate = winning / (winning + losing) * 100 if (winning + losing) > 0 else 0
    avg_return = sum(returns) / len(returns) if returns else 0

    return StrategyPerformanceResponse(
        strategy_name=name,
        total_trades=total_trades,
        winning_trades=winning,
        losing_trades=losing,
        win_rate=round(win_rate, 1),
        total_pnl=round(total_pnl, 0),
        avg_return_pct=round(avg_return, 2),
    )


@router.put("/{name}/params")
async def update_strategy_params(name: str, update: StrategyParamsUpdate):
    if not _engine or name not in _engine.strategies:
        raise HTTPException(status_code=404, detail=f"Strategy '{name}' not found")

    _engine.strategies[name].set_params(update.params)
    return {"status": "ok", "strategy": name, "params": _engine.strategies[name].get_params()}


@router.put("/{name}/weight")
async def update_strategy_weight(name: str, update: StrategyWeightUpdate):
    if not _combiner:
        raise HTTPException(status_code=500, detail="Combiner not initialized")

    _combiner.weights[name] = update.weight
    return {"status": "ok", "strategy": name, "weight": update.weight}


@router.get("/comparison")
async def compare_strategies(
    period: str = Query("30d"),
    session: AsyncSession = Depends(get_db),
):
    if not _engine:
        return []

    results = []
    for name in _engine.strategies:
        perf = await get_strategy_performance(name, period, session)
        results.append(perf)
    return results


# -- Strategy Logs --
@router.get("/logs", response_model=list[StrategyLogResponse])
async def get_strategy_logs(
    symbol: Optional[str] = None,
    strategy: Optional[str] = None,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
):
    query = select(StrategyLog).order_by(desc(StrategyLog.logged_at))

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
