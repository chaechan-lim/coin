from datetime import datetime, timezone
import inspect

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
    ResearchCandidateResponse,
    ResearchOverviewResponse,
    ResearchStageStateResponse,
    ResearchStageHistoryEntryResponse,
    ResearchStageUpdateRequest,
    RotationStatusResponse,
    SurgeScoreItem,
)
from api.dependencies import engine_registry, validate_exchange, ExchangeNameType
from research.evaluator import get_auto_review, serialize_auto_review
from research.registry import RESEARCH_CANDIDATES, get_candidate, get_candidate_by_venue, get_stage_rule

router = APIRouter(tags=["dashboard"])


def _get_engine(exchange: str):
    validate_exchange(exchange)
    return engine_registry.get_engine(exchange)


def _get_coordinator(exchange: str):
    validate_exchange(exchange)
    return engine_registry.get_coordinator(exchange)


def _get_shared_service(name: str):
    return engine_registry.get_shared(name)


def _get_research_stage_service():
    return _get_shared_service("research_stage_gate_service")


async def _assert_engine_start_allowed(exchange: str):
    service = _get_research_stage_service()
    candidate = get_candidate_by_venue(exchange)
    if candidate is None or service is None:
        return
    snapshot = await service.get_snapshot(candidate.key)
    if snapshot.execution_allowed:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "error": "stage_gate_blocked",
            "candidate_key": candidate.key,
            "effective_stage": snapshot.effective_stage,
            "message": f"{candidate.title} is not approved for live execution",
        },
    )


def _pending_auto_review(candidate_key: str, stage: str) -> dict:
    return {
        "candidate_key": candidate_key,
        "decision": "pending",
        "recommended_stage": stage,
        "summary": "background_auto_review_pending",
        "blockers": ["백그라운드 자동 판정 갱신 대기 중"],
        "metrics": [],
    }


@router.get("/research/auto-review/status")
async def get_research_auto_review_status():
    service = _get_shared_service("research_auto_review_service")
    if service is None:
        return {"status": "not_enabled", "ready": False}
    if hasattr(service, "get_status"):
        result = service.get_status()
        if inspect.isawaitable(result):
            return await result
        return result
    return {"status": "unsupported", "ready": False}


@router.get("/exchanges")
async def list_exchanges():
    """사용 가능한 거래소 목록."""
    exchanges = engine_registry.available_exchanges
    if not exchanges:
        exchanges = ["binance_spot"]
    default = "binance_spot" if "binance_spot" in exchanges else exchanges[0]
    return {"exchanges": exchanges, "default": default}


@router.get("/research/overview", response_model=ResearchOverviewResponse)
async def get_research_overview(include_auto_review: bool = Query(True)):
    """운영 시스템 개선용 R&D 전략 보드."""
    items: list[ResearchCandidateResponse] = []
    live_count = 0
    research_count = 0
    planned_count = 0
    review_service = _get_shared_service("research_auto_review_service")
    stage_service = _get_research_stage_service()
    stage_snapshots = {}
    if stage_service is not None:
        for snapshot in await stage_service.list_snapshots():
            stage_snapshots[snapshot.candidate_key] = snapshot

    for candidate in RESEARCH_CANDIDATES:
        eng = engine_registry.get_engine(candidate.venue) if candidate.venue and candidate.stage_managed else None
        is_registered = eng is not None
        is_running = bool(getattr(eng, "is_running", False)) if eng is not None else False
        stage_snapshot = stage_snapshots.get(candidate.key)
        effective_stage = stage_snapshot.effective_stage if stage_snapshot is not None else candidate.stage
        rule = get_stage_rule(effective_stage)

        if effective_stage == "live_rnd":
            live_count += 1
        elif effective_stage in ("research", "candidate", "shadow"):
            research_count += 1
        else:
            planned_count += 1

        auto_review = None
        if include_auto_review:
            if review_service is not None:
                auto_review = review_service.get_snapshot(candidate.key) or _pending_auto_review(candidate.key, effective_stage)
            else:
                live_context = {}
                if candidate.key == "pairs_trading_futures":
                    live_context["live_capital_usdt"] = getattr(eng, "_initial_capital", None) if eng is not None else None
                if candidate.key == "donchian_futures_bi":
                    live_context["live_capital_usdt"] = getattr(eng, "_initial_capital", None) if eng is not None else None
                try:
                    auto_review = serialize_auto_review(await get_auto_review(candidate.key, live_context=live_context))
                except Exception as exc:
                    auto_review = {
                        "candidate_key": candidate.key,
                        "decision": "error",
                        "recommended_stage": effective_stage,
                        "summary": f"auto_review_failed: {type(exc).__name__}",
                        "blockers": ["자동 판정 중 예외 발생"],
                        "metrics": [],
                    }

        items.append(
            ResearchCandidateResponse(
                key=candidate.key,
                title=candidate.title,
                market=candidate.market,
                directionality=candidate.directionality,
                stage=effective_stage,
                catalog_stage=candidate.stage,
                stage_source=stage_snapshot.stage_source if stage_snapshot is not None else "catalog",
                execution_allowed=stage_snapshot.execution_allowed if stage_snapshot is not None else effective_stage in {"live_rnd", "production"},
                venue=candidate.venue,
                stage_managed=candidate.stage_managed,
                status=candidate.status,
                objective=candidate.objective,
                rationale=candidate.rationale,
                recommended_next_step=candidate.recommended_next_step,
                approved_by=stage_snapshot.approved_by if stage_snapshot is not None else None,
                approval_note=stage_snapshot.approval_note if stage_snapshot is not None else None,
                approved_at=stage_snapshot.approved_at if stage_snapshot is not None else None,
                is_live_engine_registered=is_registered,
                is_live_engine_running=is_running,
                promotion_criteria=list(rule.promotion_criteria),
                demotion_criteria=list(rule.demotion_criteria),
                next_stages=list(rule.next_stages),
                auto_review=auto_review,
            )
        )

    recommended_focus = (
        "pairs_trading_futures_regime_filter"
        if any(item.key == "pairs_trading_futures" for item in items)
        else "donchian_daily_monitoring"
    )

    return ResearchOverviewResponse(
        generated_at=datetime.now(timezone.utc),
        live_candidates=live_count,
        research_candidates=research_count,
        planned_candidates=planned_count,
        recommended_focus=recommended_focus,
        items=items,
    )


@router.get("/engine/status", response_model=EngineStatusResponse)
async def get_engine_status(
    exchange: ExchangeNameType = Query("bithumb"),
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
async def start_engine(exchange: ExchangeNameType = Query("bithumb")):
    eng = _get_engine(exchange)
    if not eng:
        raise HTTPException(status_code=500, detail=f"Engine '{exchange}' not initialized")
    await _assert_engine_start_allowed(exchange)
    if eng.is_running:
        return {"status": "already_running", "exchange": exchange}
    import asyncio
    asyncio.create_task(eng.start(), name=f"engine_{exchange}")
    return {"status": "started", "exchange": exchange}


@router.get("/research/stages", response_model=list[ResearchStageStateResponse])
async def list_research_stages():
    service = _get_research_stage_service()
    if service is None:
        return []
    snapshots = await service.list_snapshots()
    return [
        ResearchStageStateResponse(
            candidate_key=snapshot.candidate_key,
            title=snapshot.title,
            venue=snapshot.venue,
            catalog_stage=snapshot.catalog_stage,
            effective_stage=snapshot.effective_stage,
            approved_stage=snapshot.approved_stage,
            stage_source=snapshot.stage_source,
            execution_allowed=snapshot.execution_allowed,
            approved_by=snapshot.approved_by,
            approval_note=snapshot.approval_note,
            approved_at=snapshot.approved_at,
        )
        for snapshot in snapshots
    ]


@router.get("/research/stage-history", response_model=list[ResearchStageHistoryEntryResponse])
async def list_research_stage_history(
    candidate_key: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    service = _get_research_stage_service()
    if service is None:
        return []
    entries = await service.list_history(candidate_key=candidate_key, limit=limit)
    return [
        ResearchStageHistoryEntryResponse(
            id=entry.id,
            candidate_key=entry.candidate_key,
            title=entry.title,
            from_stage=entry.from_stage,
            to_stage=entry.to_stage,
            approval_source=entry.approval_source,
            approved_by=entry.approved_by,
            approval_note=entry.approval_note,
            created_at=entry.created_at,
        )
        for entry in entries
    ]


@router.put("/research/candidates/{candidate_key}/stage", response_model=ResearchStageStateResponse)
async def update_research_stage(candidate_key: str, payload: ResearchStageUpdateRequest):
    service = _get_research_stage_service()
    if service is None:
        raise HTTPException(status_code=503, detail="research_stage_gate_not_enabled")

    try:
        snapshot = await service.approve_stage(
            candidate_key,
            payload.stage,
            approved_by=payload.approved_by,
            approval_note=payload.note,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ResearchStageStateResponse(
        candidate_key=snapshot.candidate_key,
        title=snapshot.title,
        venue=snapshot.venue,
        catalog_stage=snapshot.catalog_stage,
        effective_stage=snapshot.effective_stage,
        approved_stage=snapshot.approved_stage,
        stage_source=snapshot.stage_source,
        execution_allowed=snapshot.execution_allowed,
        approved_by=snapshot.approved_by,
        approval_note=snapshot.approval_note,
        approved_at=snapshot.approved_at,
    )


@router.get("/engine/rnd/overview")
async def get_rnd_overview():
    """R&D 전략 전체 현황 — 한눈에 보기."""
    # 현재가 캐시 (같은 심볼 중복 조회 방지)
    price_cache: dict[str, float] = {}

    async def _get_price(symbol: str) -> float:
        if not symbol:
            return 0.0
        if symbol in price_cache:
            return price_cache[symbol]
        # 아무 R&D 엔진의 market_data로 조회
        for ename in ("binance_hmm", "binance_donchian_futures", "binance_donchian"):
            eng = _get_engine(ename)
            if eng and hasattr(eng, "_market_data"):
                try:
                    p = await eng._market_data.get_current_price(symbol)
                    if p > 0:
                        price_cache[symbol] = p
                        return p
                except Exception:
                    pass
        return 0.0

    engines_info = []
    rnd_names = [
        ("Donchian Spot", "binance_donchian"),
        ("Donchian Futures", "binance_donchian_futures"),
        ("Pairs Trading", "binance_pairs"),
        ("Momentum Rotation", "binance_momentum"),
        ("HMM Regime", "binance_hmm"),
        ("Fear & Greed DCA", "binance_fgdca"),
        ("Breakout-Pullback", "binance_breakout_pb"),
        ("Volume Momentum", "binance_vol_mom"),
        ("BTC-neutral MR", "binance_btc_neutral"),
    ]
    for label, name in rnd_names:
        eng = _get_engine(name)
        if eng is None:
            continue
        status = eng.get_status() if hasattr(eng, "get_status") else {}
        # 포지션 정리 + 현재가/미실현PnL
        positions = []
        raw_positions = status.get("positions") or []
        single = status.get("position")
        if single and isinstance(single, dict):
            # Pairs Trading: pair_direction + qty_a/b → 2개 레그로 분리
            if "pair_direction" in single:
                coin_a = status.get("coin_a", "")
                coin_b = status.get("coin_b", "")
                pd_ = single.get("pair_direction", "long_a")
                side_a = "long" if "long_a" in pd_ else "short"
                side_b = "short" if "long_a" in pd_ else "long"
                raw_positions = [
                    {"symbol": coin_a, "side": side_a, "entry_price": single.get("entry_price_a", 0),
                     "qty": single.get("qty_a", 0), "entry_z": single.get("entry_z", 0)},
                    {"symbol": coin_b, "side": side_b, "entry_price": single.get("entry_price_b", 0),
                     "qty": single.get("qty_b", 0), "entry_z": single.get("entry_z", 0)},
                ]
            else:
                raw_positions = [single]
        leverage = status.get("leverage", 1)
        for p in raw_positions:
            if isinstance(p, dict):
                symbol = p.get("symbol", "")
                side = p.get("side") or p.get("direction", "")
                entry = p.get("entry_price") or p.get("entry", 0)
                qty = p.get("quantity") or p.get("qty", 0)
                current_price = await _get_price(symbol)
                # 미실현 PnL
                if entry > 0 and qty > 0 and current_price > 0:
                    if side == "long":
                        unrealized = (current_price - entry) * qty
                    else:
                        unrealized = (entry - current_price) * qty
                    pnl_pct = unrealized / (entry * qty) * 100 * leverage
                else:
                    unrealized = 0.0
                    pnl_pct = 0.0
                positions.append({
                    "symbol": symbol,
                    "side": side,
                    "entry": entry,
                    "qty": qty,
                    "current_price": round(current_price, 2),
                    "unrealized_pnl": round(unrealized, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "sl_price": p.get("sl_price", 0),
                    "tp_price": p.get("tp_price", 0),
                    "entry_z": p.get("entry_z", 0),
                })
        # holdings (DCA)
        holdings = status.get("holdings")
        if holdings and isinstance(holdings, dict):
            for sym, h in holdings.items():
                entry = h.get("avg_price", 0)
                qty = h.get("qty", 0)
                current_price = await _get_price(sym)
                unrealized = (current_price - entry) * qty if entry > 0 and current_price > 0 else 0.0
                pnl_pct = (current_price - entry) / entry * 100 if entry > 0 and current_price > 0 else 0.0
                positions.append({
                    "symbol": sym,
                    "side": "long",
                    "entry": entry,
                    "qty": qty,
                    "current_price": round(current_price, 2),
                    "unrealized_pnl": round(unrealized, 2),
                    "pnl_pct": round(pnl_pct, 2),
                })

        engines_info.append({
            "name": label,
            "exchange": name,
            "running": status.get("is_running", eng.is_running),
            "paused": status.get("paused") or status.get("paused_total_loss", False),
            "capital": status.get("capital_usdt") or status.get("initial_capital", 0),
            "cumulative_pnl": status.get("cumulative_pnl", 0),
            "daily_pnl": status.get("daily_pnl") or status.get("daily_realized_pnl", 0),
            "positions": positions,
            "leverage": status.get("leverage", 1),
            "idle_reason": status.get("recent_idle_reason"),
            "last_evaluated_at": status.get("last_evaluated_at"),
            "next_evaluation_at": status.get("next_evaluation_at"),
        })

    total_capital = sum(e["capital"] for e in engines_info)
    total_pnl = sum(e["cumulative_pnl"] for e in engines_info)
    total_positions = sum(len(e["positions"]) for e in engines_info)

    return {
        "total_capital": round(total_capital, 2),
        "total_cumulative_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / total_capital * 100, 2) if total_capital > 0 else 0,
        "total_positions": total_positions,
        "engines": engines_info,
    }


@router.post("/engine/rnd/performance-review")
async def trigger_rnd_performance_review():
    """R&D 성과 분석 수동 실행."""
    from services.rnd_performance_review import run_rnd_performance_review
    result = await run_rnd_performance_review()
    return result


@router.post("/engine/donchian/evaluate")
async def evaluate_donchian_now():
    """Donchian Daily 엔진 즉시 평가 트리거 (테스트/디버그용)."""
    eng = _get_engine("binance_donchian")
    if not eng:
        raise HTTPException(status_code=500, detail="Donchian engine not initialized")
    if not hasattr(eng, "evaluate_now"):
        raise HTTPException(status_code=500, detail="Engine does not support manual evaluation")
    await eng.evaluate_now()
    status = eng.get_status() if hasattr(eng, "get_status") else {}
    return {"status": "evaluated", "result": status}


@router.get("/engine/donchian/status")
async def get_donchian_status():
    """Donchian Daily 엔진 현재 상태."""
    eng = _get_engine("binance_donchian")
    if not eng:
        raise HTTPException(status_code=500, detail="Donchian engine not initialized")
    if hasattr(eng, "get_status"):
        return eng.get_status()
    return {"is_running": eng.is_running}


@router.post("/engine/donchian-futures/evaluate")
async def evaluate_donchian_futures_now():
    eng = _get_engine("binance_donchian_futures")
    if not eng:
        raise HTTPException(status_code=500, detail="Donchian futures engine not initialized")
    if not hasattr(eng, "evaluate_now"):
        raise HTTPException(status_code=500, detail="Engine does not support manual evaluation")
    await eng.evaluate_now()
    status = eng.get_status() if hasattr(eng, "get_status") else {}
    return {"status": "evaluated", "result": status}


@router.get("/engine/donchian-futures/status")
async def get_donchian_futures_status():
    eng = _get_engine("binance_donchian_futures")
    if not eng:
        raise HTTPException(status_code=500, detail="Donchian futures engine not initialized")
    if hasattr(eng, "get_status"):
        return eng.get_status()
    return {"is_running": eng.is_running}


@router.post("/engine/pairs/evaluate")
async def evaluate_pairs_now():
    eng = _get_engine("binance_pairs")
    if not eng:
        raise HTTPException(status_code=500, detail="Pairs engine not initialized")
    if not hasattr(eng, "evaluate_now"):
        raise HTTPException(status_code=500, detail="Engine does not support manual evaluation")
    await eng.evaluate_now()
    status = eng.get_status() if hasattr(eng, "get_status") else {}
    return {"status": "evaluated", "result": status}


@router.get("/engine/pairs/status")
async def get_pairs_status():
    eng = _get_engine("binance_pairs")
    if not eng:
        raise HTTPException(status_code=500, detail="Pairs engine not initialized")
    if hasattr(eng, "get_status"):
        return eng.get_status()
    return {"is_running": eng.is_running}


@router.get("/engine/futures-rnd/status")
async def get_futures_rnd_status():
    coordinator = _get_shared_service("futures_rnd_coordinator")
    if coordinator is None:
        return {"status": "not_enabled"}
    if hasattr(coordinator, "get_status"):
        result = coordinator.get_status()
        if inspect.isawaitable(result):
            return await result
        return result
    return {"status": "unsupported"}


@router.post("/engine/stop")
async def stop_engine(
    exchange: ExchangeNameType = Query("bithumb"),
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
async def get_rotation_status(exchange: ExchangeNameType = Query("bithumb")):
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


@router.post("/engine/balance-guard/resume")
async def resume_balance_guard(exchange: ExchangeNameType = Query("binance_futures")):
    """BalanceGuard 수동 재개 — 관리자 확인 후 호출.

    잔고 괴리로 일시 정지된 엔진의 주문 차단을 해제한다.
    """
    eng = _get_engine(exchange)
    if not eng:
        raise HTTPException(status_code=500, detail=f"Engine '{exchange}' not initialized")

    resume_fn = getattr(eng, "resume_balance_guard", None)
    if not resume_fn:
        raise HTTPException(
            status_code=400,
            detail=f"Engine '{exchange}' does not support balance guard resume",
        )

    result = resume_fn()
    return {"status": "resumed", "exchange": exchange, **result}


@router.get("/engine/balance-guard/status")
async def get_balance_guard_status(exchange: ExchangeNameType = Query("binance_futures")):
    """BalanceGuard 상태 조회."""
    eng = _get_engine(exchange)
    if not eng:
        raise HTTPException(status_code=500, detail=f"Engine '{exchange}' not initialized")

    status_fn = getattr(eng, "get_balance_guard_status", None)
    if not status_fn:
        raise HTTPException(
            status_code=400,
            detail=f"Engine '{exchange}' does not support balance guard status",
        )

    return {"exchange": exchange, **status_fn()}


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


def _get_v2_regime(exchange: str) -> dict | None:
    """V2 엔진의 RegimeDetector 상태를 가져온다 (없으면 None)."""
    eng = engine_registry.get_engine(exchange)
    if eng is None:
        return None
    regime_detector = getattr(eng, "_regime", None)
    if regime_detector is None:
        return None
    regime_state = regime_detector.current
    if regime_state is None:
        return None
    return {
        "regime": regime_state.regime.value,
        "confidence": round(regime_state.confidence, 3),
        "adx": round(regime_state.adx, 1),
        "atr_pct": round(regime_state.atr_pct, 2),
        "trend_direction": regime_state.trend_direction,
        "timestamp": regime_state.timestamp.isoformat(),
    }


# COIN-53: 에이전트 비활성 상태 플래그 (API 응답에 포함)
_MARKET_ANALYSIS_DISABLED_REASON = "매매 미사용 — 엔진 자체 시장 판정 사용 중 (현물: _detect_market_state, 선물: RegimeDetector)"


# -- Agent endpoints --
@router.get("/agents/market-analysis/latest")
async def get_latest_market_analysis(
    exchange: ExchangeNameType = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    from agents.coordinator import MARKET_ANALYSIS_ENABLED

    v2_regime = _get_v2_regime(exchange)

    coord = _get_coordinator(exchange)
    if coord and coord.last_market_analysis:
        analysis = coord.last_market_analysis
        resp = {
            "state": analysis.state.value,
            "confidence": analysis.confidence,
            "volatility_level": analysis.volatility_level,
            "recommended_weights": analysis.recommended_weights,
            "reasoning": analysis.reasoning,
            "disabled": not MARKET_ANALYSIS_ENABLED,
            "disabled_reason": _MARKET_ANALYSIS_DISABLED_REASON if not MARKET_ANALYSIS_ENABLED else None,
        }
        if v2_regime:
            resp["v2_regime"] = v2_regime
        return resp

    # Fallback to DB
    result = await session.execute(
        select(AgentAnalysisLog)
        .where(AgentAnalysisLog.agent_name == "market_analysis", AgentAnalysisLog.exchange == exchange)
        .order_by(desc(AgentAnalysisLog.analyzed_at))
        .limit(1)
    )
    log = result.scalar_one_or_none()
    if log:
        resp = dict(log.result) if log.result else {}
        resp["disabled"] = not MARKET_ANALYSIS_ENABLED
        if not MARKET_ANALYSIS_ENABLED:
            resp["disabled_reason"] = _MARKET_ANALYSIS_DISABLED_REASON
        if v2_regime:
            resp["v2_regime"] = v2_regime
        return resp
    resp = {
        "state": "unknown",
        "message": "No analysis available yet",
        "disabled": not MARKET_ANALYSIS_ENABLED,
    }
    if not MARKET_ANALYSIS_ENABLED:
        resp["disabled_reason"] = _MARKET_ANALYSIS_DISABLED_REASON
    if v2_regime:
        resp["v2_regime"] = v2_regime
    return resp


@router.get("/agents/market-analysis/history", response_model=list[AgentLogResponse])
async def get_market_analysis_history(
    limit: int = Query(100, ge=1, le=500),
    exchange: ExchangeNameType = Query("bithumb"),
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
    exchange: ExchangeNameType = Query("bithumb"),
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
        data = {"analyzed_at": log.analyzed_at.isoformat(), **(log.result or {})} if log.analyzed_at else dict(log.result or {})
        return data
    return {"message": "아직 매매 회고 데이터 없음", "insights": [], "recommendations": []}


@router.post("/agents/trade-review/run")
async def trigger_trade_review(exchange: ExchangeNameType = Query("bithumb")):
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
    exchange: ExchangeNameType = Query("bithumb"),
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
async def get_risk_alerts(exchange: ExchangeNameType = Query("bithumb")):
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
    exchange: ExchangeNameType = Query("bithumb"),
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
    exchange: ExchangeNameType = Query("bithumb"),
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
async def trigger_performance_analysis(exchange: ExchangeNameType = Query("bithumb")):
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
    exchange: ExchangeNameType = Query("bithumb"),
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
async def trigger_strategy_advice(exchange: ExchangeNameType = Query("bithumb")):
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
