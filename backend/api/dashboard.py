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
from api.dependencies import engine_registry, validate_exchange

router = APIRouter(tags=["dashboard"])


def _get_engine(exchange: str):
    validate_exchange(exchange)
    return engine_registry.get_engine(exchange)


def _get_coordinator(exchange: str):
    validate_exchange(exchange)
    return engine_registry.get_coordinator(exchange)


@router.get("/exchanges")
async def list_exchanges():
    """사용 가능한 거래소 목록."""
    exchanges = engine_registry.available_exchanges
    if not exchanges:
        exchanges = ["binance_spot"]
    default = "binance_spot" if "binance_spot" in exchanges else exchanges[0]
    return {"exchanges": exchanges, "default": default}


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

    ec = getattr(eng, '_ec', None)
    if ec:
        mode = ec.mode
        eval_interval = ec.evaluation_interval_sec
    else:
        # v2 엔진 또는 서지 엔진
        cfg = getattr(eng, '_config', None)
        if cfg and hasattr(cfg, 'futures_v2'):
            mode = cfg.futures_v2.mode
            eval_interval = cfg.futures_v2.tier1_eval_interval_sec
        else:
            mode = getattr(eng, '_mode', 'paper')
            eval_interval = getattr(eng, '_scan_interval', 300)

    strategies = getattr(eng, 'strategies', None)
    strategies_active = list(strategies.keys()) if strategies else []

    comb = engine_registry.get_combiner(exchange)
    min_confidence = getattr(comb, 'min_confidence', 0.55) if comb else 0.55

    return EngineStatusResponse(
        exchange=exchange,
        is_running=eng.is_running,
        mode=mode,
        evaluation_interval_sec=eval_interval,
        tracked_coins=getattr(eng, 'tracked_coins', []),
        daily_trade_count=daily_count,
        strategies_active=strategies_active,
        min_confidence=min_confidence,
    )


@router.post("/engine/start")
async def start_engine(exchange: str = Query("bithumb")):
    eng = _get_engine(exchange)
    if not eng:
        raise HTTPException(status_code=500, detail=f"Engine '{exchange}' not initialized")
    if eng.is_running:
        return {"status": "already_running", "exchange": exchange}
    import asyncio
    asyncio.create_task(eng.start(), name=f"engine_{exchange}")
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


@router.get("/engine/v2/tier1-status")
async def get_tier1_status():
    """V2 Tier1 평가 사이클 운영 상태 — 관측용 (COIN-17)."""
    eng = engine_registry.get_engine("binance_futures")
    if not eng:
        raise HTTPException(status_code=500, detail="Futures engine not initialized")

    get_tier1 = getattr(eng, "get_tier1_status", None)
    if not get_tier1:
        raise HTTPException(status_code=400, detail="Engine does not support tier1 status")

    return get_tier1()


@router.get("/engine/surge-scan")
async def get_surge_scan_status():
    """서지 엔진 스캔 상태 — 심볼별 점수/RSI/포지션 정보."""
    eng = engine_registry.get_engine("binance_surge")
    if not eng:
        return {"status": "not_initialized", "scores": []}
    scan = eng.scan_status()
    return scan


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
    limit: int = Query(100, ge=1, le=500),
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
async def get_latest_trade_review(
    exchange: str = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
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
            "analyzed_at": r.analyzed_at,
        }

    # Fallback to DB (서버 재시작 후 인메모리 캐시 비어있을 때)
    result = await session.execute(
        select(AgentAnalysisLog)
        .where(AgentAnalysisLog.agent_name == "trade_review", AgentAnalysisLog.exchange == exchange)
        .order_by(desc(AgentAnalysisLog.analyzed_at))
        .limit(1)
    )
    log = result.scalar_one_or_none()
    if log:
        return log.result
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
    limit: int = Query(50, ge=1, le=500),
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
    limit: int = Query(100, ge=1, le=500),
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


# ── Performance Analytics ─────────────────────────────────────

@router.get("/agents/performance/latest")
async def get_performance_latest(
    exchange: str = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    coord = _get_coordinator(exchange)
    if coord and coord.last_performance_report:
        r = coord.last_performance_report
        return {
            "exchange": r.exchange,
            "generated_at": r.generated_at,
            "windows": {k: vars(v) for k, v in r.windows.items()},
            "by_strategy": {k: vars(v) for k, v in r.by_strategy.items()},
            "by_symbol": {k: vars(v) for k, v in r.by_symbol.items()},
            "degradation_alerts": r.degradation_alerts,
            "insights": r.insights,
            "recommendations": r.recommendations,
        }
    # DB fallback
    result = await session.execute(
        select(AgentAnalysisLog)
        .where(AgentAnalysisLog.agent_name == "performance_analytics", AgentAnalysisLog.exchange == exchange)
        .order_by(desc(AgentAnalysisLog.analyzed_at))
        .limit(1)
    )
    log = result.scalar_one_or_none()
    if log:
        return {"generated_at": log.analyzed_at.isoformat(), **(log.result or {})}
    return {"status": "no_data"}


@router.post("/agents/performance/run")
async def trigger_performance_analysis(exchange: str = Query("bithumb")):
    coord = _get_coordinator(exchange)
    if coord:
        report = await coord.run_performance_analysis()
        if report:
            w30 = report.windows.get("30d")
            return {
                "status": "completed",
                "trades_30d": w30.total_trades if w30 else 0,
                "degradation_alerts": report.degradation_alerts,
                "insights": report.insights,
            }
    return {"status": "error", "message": "coordinator not found"}


# ── Strategy Advisor ──────────────────────────────────────────

@router.get("/agents/strategy-advice/latest")
async def get_strategy_advice_latest(
    exchange: str = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    coord = _get_coordinator(exchange)
    if coord and coord.last_strategy_advice:
        a = coord.last_strategy_advice
        return {
            "exchange": a.exchange,
            "generated_at": a.generated_at,
            "exit_analysis": a.exit_analysis,
            "param_sensitivities": [vars(p) for p in a.param_sensitivities],
            "direction_analysis": a.direction_analysis,
            "analysis_summary": a.analysis_summary,
            "suggestions": a.suggestions,
        }
    # DB fallback
    result = await session.execute(
        select(AgentAnalysisLog)
        .where(AgentAnalysisLog.agent_name == "strategy_advisor", AgentAnalysisLog.exchange == exchange)
        .order_by(desc(AgentAnalysisLog.analyzed_at))
        .limit(1)
    )
    log = result.scalar_one_or_none()
    if log:
        return {"generated_at": log.analyzed_at.isoformat(), **(log.result or {})}
    return {"status": "no_data"}


@router.post("/agents/strategy-advice/run")
async def trigger_strategy_advice(exchange: str = Query("bithumb")):
    coord = _get_coordinator(exchange)
    if coord:
        advice = await coord.run_strategy_advice()
        if advice:
            return {
                "status": "completed",
                "analysis_summary": advice.analysis_summary,
                "suggestions": advice.suggestions,
            }
    return {"status": "error", "message": "coordinator not found"}


# ── BalanceGuard Admin API ───────────────────────────────────


def _get_balance_guard(exchange: str):
    """엔진에서 BalanceGuard 인스턴스를 가져온다."""
    eng = _get_engine(exchange)
    if not eng:
        raise HTTPException(status_code=500, detail=f"Engine '{exchange}' not initialized")
    guard = getattr(eng, "balance_guard", None)
    if guard is None:
        # fallback: _guard 직접 접근
        guard = getattr(eng, "_guard", None)
    if guard is None:
        raise HTTPException(status_code=404, detail="BalanceGuard not available for this engine")
    return eng, guard


@router.get("/engine/balance-guard/status")
async def get_balance_guard_status(exchange: str = Query("binance_futures")):
    """BalanceGuard 현재 상태 조회."""
    _eng, guard = _get_balance_guard(exchange)
    status = guard.get_status()
    status["exchange"] = exchange
    return status


@router.post("/engine/balance-guard/resume")
async def resume_balance_guard(exchange: str = Query("binance_futures")):
    """BalanceGuard 수동 재개."""
    _eng, guard = _get_balance_guard(exchange)
    was_paused = guard.is_paused
    if was_paused:
        guard.resume(reason="manual_api")
    return {
        "exchange": exchange,
        "was_paused": was_paused,
        "is_paused": guard.is_paused,
        "status": "resumed" if was_paused else "already_running",
    }


@router.post("/engine/balance-guard/sync")
async def sync_balance_guard(exchange: str = Query("binance_futures")):
    """내부 현금 → 거래소 잔고 동기화."""
    eng = _get_engine(exchange)
    if not eng:
        raise HTTPException(status_code=500, detail=f"Engine '{exchange}' not initialized")
    sync_fn = getattr(eng, "sync_balance_to_exchange", None)
    if sync_fn is None:
        raise HTTPException(status_code=404, detail="sync_balance_to_exchange not available")
    result = await sync_fn()
    result["exchange"] = exchange
    return result
