from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from db.session import get_db
from core.models import AgentAnalysisLog
from core.schemas import (
    EngineStatusResponse,
    ModeUpdate,
    AgentLogResponse,
    MarketAnalysisResponse,
    RiskAlertResponse,
)

router = APIRouter(tags=["dashboard"])

# Set from main.py
_engine = None
_coordinator = None
_config = None


def set_dashboard_deps(engine, coordinator, config):
    global _engine, _coordinator, _config
    _engine = engine
    _coordinator = coordinator
    _config = config


@router.get("/engine/status", response_model=EngineStatusResponse)
async def get_engine_status():
    if not _engine:
        return EngineStatusResponse(
            is_running=False, mode="paper", evaluation_interval_sec=300,
            tracked_coins=[], daily_trade_count=0, strategies_active=[],
        )
    return EngineStatusResponse(
        is_running=_engine.is_running,
        mode=_config.trading.mode,
        evaluation_interval_sec=_config.trading.evaluation_interval_sec,
        tracked_coins=_config.trading.tracked_coins,
        daily_trade_count=_engine._daily_trade_count,
        strategies_active=list(_engine.strategies.keys()),
    )


@router.post("/engine/start")
async def start_engine():
    if not _engine:
        raise HTTPException(status_code=500, detail="Engine not initialized")
    if _engine.is_running:
        return {"status": "already_running"}
    import asyncio
    asyncio.create_task(_engine.start())
    return {"status": "started"}


@router.post("/engine/stop")
async def stop_engine():
    if not _engine:
        raise HTTPException(status_code=500, detail="Engine not initialized")
    await _engine.stop()
    return {"status": "stopped"}


# -- Agent endpoints --
@router.get("/agents/market-analysis/latest")
async def get_latest_market_analysis(session: AsyncSession = Depends(get_db)):
    if _coordinator and _coordinator.last_market_analysis:
        analysis = _coordinator.last_market_analysis
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
        .where(AgentAnalysisLog.agent_name == "market_analysis")
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
    session: AsyncSession = Depends(get_db),
):
    result = await session.execute(
        select(AgentAnalysisLog)
        .where(AgentAnalysisLog.agent_name == "market_analysis")
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
async def get_latest_trade_review():
    if _coordinator and _coordinator.last_trade_review:
        r = _coordinator.last_trade_review
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
async def trigger_trade_review():
    """수동으로 매매 회고 에이전트 실행."""
    if not _coordinator:
        raise HTTPException(status_code=500, detail="Coordinator not initialized")
    review = await _coordinator.run_trade_review()
    if review:
        return {"status": "completed", "total_trades": review.total_trades, "insights": review.insights}
    return {"status": "no_data"}


@router.get("/agents/trade-review/history", response_model=list[AgentLogResponse])
async def get_trade_review_history(
    limit: int = 50,
    session: AsyncSession = Depends(get_db),
):
    result = await session.execute(
        select(AgentAnalysisLog)
        .where(AgentAnalysisLog.agent_name == "trade_review")
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
async def get_risk_alerts():
    if _coordinator:
        return [
            {
                "level": a.level.value,
                "message": a.message,
                "action": a.action,
                "affected_coins": a.affected_coins,
                "details": a.details,
            }
            for a in _coordinator.last_risk_alerts
        ]
    return []


@router.get("/agents/risk/history", response_model=list[AgentLogResponse])
async def get_risk_history(
    limit: int = 100,
    session: AsyncSession = Depends(get_db),
):
    result = await session.execute(
        select(AgentAnalysisLog)
        .where(AgentAnalysisLog.agent_name == "risk_management")
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
