from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func

from db.session import get_db
from core.models import AgentAnalysisLog, Order
from core.schemas import (
    EngineStatusResponse,
    ModeUpdate,
    AgentLogResponse,
    MarketAnalysisResponse,
    RiskAlertResponse,
    RotationStatusResponse,
    SurgeScoreItem,
)
from api.dependencies import engine_registry

router = APIRouter(tags=["dashboard"])

# Legacy setters for backward compatibility
_engine = None
_coordinator = None
_config = None


def set_dashboard_deps(engine, coordinator, config):
    global _engine, _coordinator, _config
    _engine = engine
    _coordinator = coordinator
    _config = config


def _get_engine(exchange: str):
    eng = engine_registry.get_engine(exchange)
    if eng:
        return eng
    return _engine


def _get_coordinator(exchange: str):
    coord = engine_registry.get_coordinator(exchange)
    if coord:
        return coord
    return _coordinator


@router.get("/exchanges")
async def list_exchanges():
    """사용 가능한 거래소 목록."""
    exchanges = engine_registry.available_exchanges
    if not exchanges:
        exchanges = ["bithumb"]
    return {"exchanges": exchanges, "default": "bithumb"}


@router.get("/engine/status", response_model=EngineStatusResponse)
async def get_engine_status(
    exchange: str = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    eng = _get_engine(exchange)
    if not eng:
        return EngineStatusResponse(
            exchange=exchange,
            is_running=False, mode="paper", evaluation_interval_sec=300,
            tracked_coins=[], daily_trade_count=0, strategies_active=[],
        )
    # DB 기반 오늘 거래 횟수 (UTC 0시 기준)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await session.execute(
        select(func.count(Order.id)).where(
            Order.created_at >= today_start,
            Order.exchange == exchange,
        )
    )
    daily_count = result.scalar() or 0

    if exchange == "binance_futures" and _config:
        mode = _config.binance_trading.mode
        eval_interval = _config.binance_trading.evaluation_interval_sec
    else:
        mode = _config.trading.mode if _config else "paper"
        eval_interval = _config.trading.evaluation_interval_sec if _config else 300

    return EngineStatusResponse(
        exchange=exchange,
        is_running=eng.is_running,
        mode=mode,
        evaluation_interval_sec=eval_interval,
        tracked_coins=getattr(eng, 'tracked_coins', _config.trading.tracked_coins if _config else []),
        daily_trade_count=daily_count,
        strategies_active=list(eng.strategies.keys()),
    )


@router.post("/engine/start")
async def start_engine(exchange: str = Query("bithumb")):
    eng = _get_engine(exchange)
    if not eng:
        raise HTTPException(status_code=500, detail=f"Engine '{exchange}' not initialized")
    if eng.is_running:
        return {"status": "already_running", "exchange": exchange}
    import asyncio
    asyncio.create_task(eng.start())
    return {"status": "started", "exchange": exchange}


@router.post("/engine/stop")
async def stop_engine(
    exchange: str = Query("bithumb"),
    force: bool = Query(False),
):
    eng = _get_engine(exchange)
    if not eng:
        raise HTTPException(status_code=500, detail=f"Engine '{exchange}' not initialized")

    # 선물 엔진: 포지션 있으면 force 없이 경고만 반환
    if exchange == "binance_futures" and not force:
        has_positions = getattr(eng, "has_open_positions", False)
        if has_positions:
            return {
                "status": "warning",
                "exchange": exchange,
                "message": "레버리지 포지션 보유 중입니다. 강제 중지하려면 force=true를 사용하세요.",
            }

    await eng.stop()
    return {"status": "stopped", "exchange": exchange}


@router.get("/engine/rotation-status", response_model=RotationStatusResponse)
async def get_rotation_status(exchange: str = Query("bithumb")):
    eng = _get_engine(exchange)
    if not eng:
        raise HTTPException(status_code=500, detail="Engine not initialized")
    rs = eng.rotation_status
    scores = [
        SurgeScoreItem(
            symbol=s,
            score=sc,
            above_threshold=sc >= rs["surge_threshold"],
        )
        for s, sc in sorted(
            rs["all_surge_scores"].items(), key=lambda x: x[1], reverse=True
        )
    ]
    return RotationStatusResponse(
        exchange=exchange,
        rotation_enabled=rs["rotation_enabled"],
        surge_threshold=rs["surge_threshold"],
        market_state=rs["market_state"],
        current_surge_symbol=rs["current_surge_symbol"],
        last_rotation_time=rs["last_rotation_time"],
        last_scan_time=rs["last_scan_time"],
        rotation_cooldown_sec=rs["rotation_cooldown_sec"],
        tracked_coins=rs["tracked_coins"],
        rotation_coins=rs["rotation_coins"],
        surge_scores=scores,
    )


# -- Agent endpoints --
@router.get("/agents/market-analysis/latest")
async def get_latest_market_analysis(
    exchange: str = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    coord = _get_coordinator(exchange)
    if coord and coord.last_market_analysis:
        analysis = coord.last_market_analysis
        return {
            "state": analysis.state.value,
            "confidence": analysis.confidence,
            "volatility_level": analysis.volatility_level,
            "recommended_weights": analysis.recommended_weights,
            "reasoning": analysis.reasoning,
        }

    # Fallback to DB
    result = await session.execute(
        select(AgentAnalysisLog)
        .where(AgentAnalysisLog.agent_name == "market_analysis", AgentAnalysisLog.exchange == exchange)
        .order_by(desc(AgentAnalysisLog.analyzed_at))
        .limit(1)
    )
    log = result.scalar_one_or_none()
    if log:
        return log.result
    return {"state": "unknown", "message": "No analysis available yet"}


@router.get("/agents/market-analysis/history", response_model=list[AgentLogResponse])
async def get_market_analysis_history(
    limit: int = 100,
    exchange: str = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    result = await session.execute(
        select(AgentAnalysisLog)
        .where(AgentAnalysisLog.agent_name == "market_analysis", AgentAnalysisLog.exchange == exchange)
        .order_by(desc(AgentAnalysisLog.analyzed_at))
        .limit(limit)
    )
    logs = result.scalars().all()
    return [
        AgentLogResponse(
            id=l.id, agent_name=l.agent_name, analysis_type=l.analysis_type,
            result=l.result, risk_level=l.risk_level, analyzed_at=l.analyzed_at,
        )
        for l in logs
    ]


@router.get("/agents/trade-review/latest")
async def get_latest_trade_review(exchange: str = Query("bithumb")):
    coord = _get_coordinator(exchange)
    if coord and coord.last_trade_review:
        r = coord.last_trade_review
        return {
            "period_hours": r.period_hours,
            "total_trades": r.total_trades,
            "buy_count": r.buy_count,
            "sell_count": r.sell_count,
            "win_count": r.win_count,
            "loss_count": r.loss_count,
            "win_rate": r.win_rate,
            "total_realized_pnl": r.total_realized_pnl,
            "avg_pnl_per_trade": r.avg_pnl_per_trade,
            "profit_factor": r.profit_factor,
            "largest_win": r.largest_win,
            "largest_loss": r.largest_loss,
            "by_strategy": r.by_strategy,
            "by_symbol": r.by_symbol,
            "open_positions": r.open_positions,
            "insights": r.insights,
            "recommendations": r.recommendations,
        }
    return {"message": "아직 매매 회고 데이터 없음", "insights": [], "recommendations": []}


@router.post("/agents/trade-review/run")
async def trigger_trade_review(exchange: str = Query("bithumb")):
    """수동으로 매매 회고 에이전트 실행."""
    coord = _get_coordinator(exchange)
    if not coord:
        raise HTTPException(status_code=500, detail="Coordinator not initialized")
    review = await coord.run_trade_review()
    if review:
        return {"status": "completed", "total_trades": review.total_trades, "insights": review.insights}
    return {"status": "no_data"}


@router.get("/agents/trade-review/history", response_model=list[AgentLogResponse])
async def get_trade_review_history(
    limit: int = 50,
    exchange: str = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    result = await session.execute(
        select(AgentAnalysisLog)
        .where(AgentAnalysisLog.agent_name == "trade_review", AgentAnalysisLog.exchange == exchange)
        .order_by(desc(AgentAnalysisLog.analyzed_at))
        .limit(limit)
    )
    logs = result.scalars().all()
    return [
        AgentLogResponse(
            id=l.id, agent_name=l.agent_name, analysis_type=l.analysis_type,
            result=l.result, risk_level=l.risk_level, analyzed_at=l.analyzed_at,
        )
        for l in logs
    ]


@router.get("/agents/risk/alerts")
async def get_risk_alerts(exchange: str = Query("bithumb")):
    coord = _get_coordinator(exchange)
    if coord:
        return [
            {
                "level": a.level.value,
                "message": a.message,
                "action": a.action,
                "affected_coins": a.affected_coins,
                "details": a.details,
            }
            for a in coord.last_risk_alerts
        ]
    return []


@router.get("/agents/risk/history", response_model=list[AgentLogResponse])
async def get_risk_history(
    limit: int = 100,
    exchange: str = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    result = await session.execute(
        select(AgentAnalysisLog)
        .where(AgentAnalysisLog.agent_name == "risk_management", AgentAnalysisLog.exchange == exchange)
        .order_by(desc(AgentAnalysisLog.analyzed_at))
        .limit(limit)
    )
    logs = result.scalars().all()
    return [
        AgentLogResponse(
            id=l.id, agent_name=l.agent_name, analysis_type=l.analysis_type,
            result=l.result, risk_level=l.risk_level, analyzed_at=l.analyzed_at,
        )
        for l in logs
    ]
