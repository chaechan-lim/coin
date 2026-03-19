from collections import defaultdict, OrderedDict

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
    SignalCycleGroupResponse,
    StrategySignalItem,
)
from api.dependencies import engine_registry, ExchangeNameType
from strategies.combiner import SignalCombiner

router = APIRouter(prefix="/strategies", tags=["strategies"])


def _get_engine(exchange: str):
    return engine_registry.get_engine(exchange)


def _get_combiner(exchange: str):
    return engine_registry.get_combiner(exchange)


@router.get("", response_model=list[StrategyResponse])
async def list_strategies(exchange: ExchangeNameType = Query("bithumb")):
    eng = _get_engine(exchange)
    comb = _get_combiner(exchange)
    if not eng:
        return []

    strategies = []
    for name, strategy in eng.strategies.items():
        weight = comb.weights.get(name, 0.0) if comb else 0.0
        # v2 RegimeStrategy는 BaseStrategy 인터페이스의 일부 속성이 없을 수 있음
        strategies.append(
            StrategyResponse(
                name=getattr(strategy, "name", name),
                display_name=getattr(strategy, "display_name", name),
                applicable_market_types=getattr(
                    strategy, "applicable_market_types", ["futures"]
                ),
                default_coins=getattr(strategy, "default_coins", []),
                required_timeframe=getattr(strategy, "required_timeframe", "5m"),
                params=strategy.get_params() if hasattr(strategy, "get_params") else {},
                current_weight=weight,
            )
        )
    return strategies


@router.get("/{name}/performance", response_model=StrategyPerformanceResponse)
async def get_strategy_performance(
    name: str,
    period: str = Query("30d"),
    exchange: ExchangeNameType = Query("bithumb"),
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

    # 2) 심볼별 로트 추적 (Lot-based FIFO — 진입 전략에 PnL 귀속)
    is_futures = "futures" in exchange

    # 각 로트: {"strategy": str, "qty": float, "cost": float, "time": datetime}
    long_lots: dict[str, list[dict]] = defaultdict(list)
    short_lots: dict[str, list[dict]] = defaultdict(list)

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

        direction = getattr(order, "direction", None) or "long"
        is_short = is_futures and direction == "short"

        if is_short:
            # 숏: sell=진입, buy=청산
            if order.side == "sell":
                short_lots[sym].append(
                    {
                        "strategy": order.strategy_name,
                        "qty": qty,
                        "cost": price * qty + fee,
                        "time": order.created_at,
                    }
                )
                # 진입 카운트: 진입 전략 기준
                if (
                    order.strategy_name == name
                    and ensure_aware(order.created_at) >= start
                ):
                    trade_count += 1
            elif order.side == "buy":
                remaining = qty
                fee_remaining = fee
                while remaining > 0 and short_lots[sym]:
                    lot = short_lots[sym][0]
                    close_qty = min(remaining, lot["qty"])
                    avg_entry = lot["cost"] / lot["qty"]
                    lot_fee = fee_remaining * (close_qty / qty) if qty > 0 else 0
                    pnl = (avg_entry - price) * close_qty - lot_fee

                    in_period = (
                        lot["strategy"] == name and ensure_aware(lot["time"]) >= start
                    )
                    if in_period:
                        total_pnl += pnl
                        ret_pct = (
                            pnl / (avg_entry * close_qty) * 100 if avg_entry > 0 else 0
                        )
                        returns.append(ret_pct)
                        trade_count += 1
                        if pnl > 0:
                            winning += 1
                        else:
                            losing += 1

                    lot["qty"] -= close_qty
                    lot["cost"] -= avg_entry * close_qty
                    fee_remaining -= lot_fee
                    remaining -= close_qty
                    if lot["qty"] <= 1e-12:
                        short_lots[sym].pop(0)
        else:
            # 롱/현물: buy=진입, sell=청산
            if order.side == "buy":
                long_lots[sym].append(
                    {
                        "strategy": order.strategy_name,
                        "qty": qty,
                        "cost": price * qty + fee,
                        "time": order.created_at,
                    }
                )
                if (
                    order.strategy_name == name
                    and ensure_aware(order.created_at) >= start
                ):
                    trade_count += 1

            elif order.side == "sell":
                remaining = qty
                fee_remaining = fee
                while remaining > 0 and long_lots[sym]:
                    lot = long_lots[sym][0]
                    close_qty = min(remaining, lot["qty"])
                    avg_entry = lot["cost"] / lot["qty"]
                    lot_fee = fee_remaining * (close_qty / qty) if qty > 0 else 0
                    pnl = (price - avg_entry) * close_qty - lot_fee

                    in_period = (
                        lot["strategy"] == name and ensure_aware(lot["time"]) >= start
                    )
                    if in_period:
                        total_pnl += pnl
                        ret_pct = (
                            pnl / (avg_entry * close_qty) * 100 if avg_entry > 0 else 0
                        )
                        returns.append(ret_pct)
                        trade_count += 1
                        if pnl > 0:
                            winning += 1
                        else:
                            losing += 1

                    lot["qty"] -= close_qty
                    lot["cost"] -= avg_entry * close_qty
                    fee_remaining -= lot_fee
                    remaining -= close_qty
                    if lot["qty"] <= 1e-12:
                        long_lots[sym].pop(0)

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
async def update_strategy_params(
    name: str, update: StrategyParamsUpdate, exchange: str = Query("bithumb")
):
    eng = _get_engine(exchange)
    if not eng or name not in eng.strategies:
        raise HTTPException(status_code=404, detail=f"Strategy '{name}' not found")

    eng.strategies[name].set_params(update.params)
    return {
        "status": "ok",
        "strategy": name,
        "params": eng.strategies[name].get_params(),
    }


@router.put("/{name}/weight")
async def update_strategy_weight(
    name: str, update: StrategyWeightUpdate, exchange: str = Query("bithumb")
):
    comb = _get_combiner(exchange)
    if not comb:
        raise HTTPException(status_code=500, detail="Combiner not initialized")

    comb.weights[name] = update.weight
    return {"status": "ok", "strategy": name, "weight": update.weight}


@router.get("/comparison")
async def compare_strategies(
    period: str = Query("30d"),
    exchange: ExchangeNameType = Query("bithumb"),
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
    exchange: ExchangeNameType = Query("bithumb"),
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


def _compute_combined_signal(
    logs: list,
    weights: dict[str, float],
    min_confidence: float,
) -> tuple[str, float]:
    """Compute a **simplified/approximate** combined signal for display purposes.

    Uses the same HOLD-as-abstain model and weighted voting as
    ``SignalCombiner.combine()`` (backend/strategies/combiner.py), but omits
    several features of the real combiner:

    * Adaptive profiles (market-state-dependent weight adjustments)
    * Directional weights (separate BUY_WEIGHTS / SELL_WEIGHTS with
      per-direction normalization)
    * Crash market override (MIN_ACTIVE_WEIGHT relaxed to 0.06)
    * MIN_SELL_ACTIVE_WEIGHT single-strategy short blocking

    The actual combined signal used at evaluation time is computed by the
    combiner instance; this function is only for API display of historical
    log groups.
    """
    MIN_ACTIVE_WEIGHT = SignalCombiner.MIN_ACTIVE_WEIGHT
    buy_score = 0.0
    sell_score = 0.0
    buy_active = 0.0
    sell_active = 0.0

    for log in logs:
        w = weights.get(log.strategy_name, 0.1)
        conf = log.confidence or 0.0
        if log.signal_type == "BUY":
            buy_score += w * conf
            buy_active += w
        elif log.signal_type == "SELL":
            sell_score += w * conf
            sell_active += w

    active_weight = buy_active + sell_active
    if active_weight < MIN_ACTIVE_WEIGHT:
        return "HOLD", 0.0

    buy_norm = buy_score / active_weight
    sell_norm = sell_score / active_weight
    is_long = buy_norm >= sell_norm
    winning_score = buy_norm if is_long else sell_norm

    if winning_score < min_confidence:
        return "HOLD", round(winning_score, 4)

    action = "BUY" if is_long else "SELL"
    return action, round(winning_score, 4)


@router.get("/logs/grouped", response_model=list[SignalCycleGroupResponse])
async def get_grouped_strategy_logs(
    symbol: Optional[str] = None,
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=50),
    exchange: ExchangeNameType = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    """평가 사이클 단위로 그룹핑된 신호 로그 반환.

    같은 timestamp(1분 버킷) + symbol 기준으로 묶어서 반환.
    각 그룹에 최종 combined signal, confidence, 개별 전략 판단 포함.

    Pagination operates on complete groups (not raw rows) to prevent
    cycles from being split across page boundaries.  We fetch enough
    raw rows to cover all groups up to the requested page, group in
    Python, then slice the correct page of groups.
    """
    # Fetch enough raw rows to cover all groups through the requested page.
    # Each evaluation cycle produces ~4-8 strategy logs.  Over-fetch with
    # a generous multiplier and buffer to guarantee we never split a cycle.
    total_groups_needed = page * size
    raw_limit = total_groups_needed * 8 + 16  # buffer for partial trailing group

    query = (
        select(StrategyLog)
        .where(StrategyLog.exchange == exchange)
        .order_by(desc(StrategyLog.logged_at))
    )
    if symbol:
        query = query.where(StrategyLog.symbol == symbol)

    # No raw offset — always fetch from the start so grouping is correct.
    query = query.limit(raw_limit)
    db_result = await session.execute(query)
    logs = list(db_result.scalars().all())

    # Group by symbol + 1-minute time bucket
    groups: OrderedDict[str, list] = OrderedDict()
    for log in logs:
        # Truncate to minute for bucket key
        logged = log.logged_at
        minute_key = logged.strftime("%Y-%m-%dT%H:%M") if logged else "unknown"
        key = f"{log.symbol}::{minute_key}"
        if key not in groups:
            groups[key] = []
        groups[key].append(log)

    # Get combiner weights for combined signal computation
    comb = engine_registry.get_combiner(exchange)
    weights = comb.weights if comb else {}
    min_confidence = getattr(comb, "min_confidence", 0.55) if comb else 0.55

    # Slice groups for the requested page (pagination on complete groups)
    all_group_items = list(groups.items())
    start_idx = (page - 1) * size
    page_items = all_group_items[start_idx : start_idx + size]

    # Build response from the page slice
    result_groups: list[SignalCycleGroupResponse] = []
    for _key, group_logs in page_items:
        first_log = group_logs[0]
        combined_action, combined_conf = _compute_combined_signal(
            group_logs,
            weights,
            min_confidence,
        )
        any_executed = any(log.was_executed for log in group_logs)

        signals = [
            StrategySignalItem(
                strategy_name=log.strategy_name,
                signal_type=log.signal_type,
                confidence=log.confidence,
                reason=log.reason,
                was_executed=log.was_executed,
            )
            for log in group_logs
        ]

        result_groups.append(
            SignalCycleGroupResponse(
                symbol=first_log.symbol,
                cycle_time=first_log.logged_at,
                combined_signal=combined_action,
                combined_confidence=combined_conf,
                strategy_count=len(group_logs),
                executed=any_executed,
                signals=signals,
            )
        )

    return result_groups
