"""
Discord 봇 도구 정의 + 실행 핸들러.

Claude API tool_use에 전달할 도구 스키마와,
각 도구를 내부 Python 호출로 실행하는 핸들러.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.models import Position, Order, DailyPnL, AgentAnalysisLog

logger = structlog.get_logger(__name__)


# ── Tool Definitions (Claude API schema) ────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "get_engine_status",
        "description": "거래소별 엔진 상태 조회 (실행 중 여부, 모드, 추적 코인). exchange 미지정 시 전체 조회.",
        "input_schema": {
            "type": "object",
            "properties": {
                "exchange": {
                    "type": "string",
                    "enum": ["bithumb", "binance_futures", "binance_spot"],
                    "description": "거래소 이름. 미지정 시 전체.",
                },
            },
        },
    },
    {
        "name": "get_portfolio_summary",
        "description": "포트폴리오 요약 (총자산, 현금, 수익률, 포지션 수). exchange 미지정 시 전체.",
        "input_schema": {
            "type": "object",
            "properties": {
                "exchange": {
                    "type": "string",
                    "enum": ["bithumb", "binance_futures", "binance_spot"],
                },
            },
        },
    },
    {
        "name": "get_positions",
        "description": "현재 보유 포지션 목록 (코인, 수량, 평단가, 미실현PnL, 방향). exchange 미지정 시 전체.",
        "input_schema": {
            "type": "object",
            "properties": {
                "exchange": {
                    "type": "string",
                    "enum": ["bithumb", "binance_futures", "binance_spot"],
                },
            },
        },
    },
    {
        "name": "get_recent_trades",
        "description": "최근 거래 내역 조회. 기본 10건.",
        "input_schema": {
            "type": "object",
            "properties": {
                "exchange": {
                    "type": "string",
                    "enum": ["bithumb", "binance_futures", "binance_spot"],
                },
                "limit": {
                    "type": "integer",
                    "description": "조회 건수 (기본 10, 최대 30)",
                },
                "symbol": {
                    "type": "string",
                    "description": "특정 코인 필터 (예: BTC/USDT)",
                },
                "side": {
                    "type": "string",
                    "enum": ["buy", "sell"],
                    "description": "매수/매도 필터",
                },
            },
        },
    },
    {
        "name": "get_daily_pnl",
        "description": "일일 손익 기록 조회. 기본 7일.",
        "input_schema": {
            "type": "object",
            "properties": {
                "exchange": {
                    "type": "string",
                    "enum": ["bithumb", "binance_futures", "binance_spot"],
                },
                "days": {
                    "type": "integer",
                    "description": "조회 일수 (기본 7, 최대 90)",
                },
            },
        },
    },
    {
        "name": "get_market_analysis",
        "description": "최신 시장 분석 결과 (시장 상태, 추세, RSI 등).",
        "input_schema": {
            "type": "object",
            "properties": {
                "exchange": {
                    "type": "string",
                    "enum": ["bithumb", "binance_futures", "binance_spot"],
                },
            },
        },
    },
    {
        "name": "get_performance_report",
        "description": "성과 분석 보고서 (7d/14d/30d 롤링 윈도우, 전략별 성과, 성과 저하 경고).",
        "input_schema": {
            "type": "object",
            "properties": {
                "exchange": {
                    "type": "string",
                    "enum": ["bithumb", "binance_futures", "binance_spot"],
                },
            },
        },
    },
    {
        "name": "get_strategy_advice",
        "description": "전략 조언 (청산 사유 분석, 파라미터 민감도, 방향별 분석).",
        "input_schema": {
            "type": "object",
            "properties": {
                "exchange": {
                    "type": "string",
                    "enum": ["bithumb", "binance_futures", "binance_spot"],
                },
            },
        },
    },
    {
        "name": "start_engine",
        "description": "거래소 엔진 시작. 주의: 실제 거래가 시작됩니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "exchange": {
                    "type": "string",
                    "enum": ["bithumb", "binance_futures", "binance_spot"],
                    "description": "시작할 거래소",
                },
            },
            "required": ["exchange"],
        },
    },
    {
        "name": "stop_engine",
        "description": "거래소 엔진 중지. 열린 포지션은 유지됩니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "exchange": {
                    "type": "string",
                    "enum": ["bithumb", "binance_futures", "binance_spot"],
                    "description": "중지할 거래소",
                },
            },
            "required": ["exchange"],
        },
    },
    {
        "name": "trigger_analysis",
        "description": "에이전트 분석 수동 트리거 (성과 분석, 전략 조언, 거래 리뷰).",
        "input_schema": {
            "type": "object",
            "properties": {
                "exchange": {
                    "type": "string",
                    "enum": ["bithumb", "binance_futures", "binance_spot"],
                },
                "analysis_type": {
                    "type": "string",
                    "enum": ["performance", "strategy_advice", "trade_review"],
                    "description": "분석 유형",
                },
            },
            "required": ["analysis_type"],
        },
    },
    {
        "name": "save_memory",
        "description": "중요한 정보를 영구 메모리에 저장. 사용자가 '기억해', '메모해', '잊지마' 등 요청 시 사용. 시스템 상태 변경이나 운영 방침도 저장 가능.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "저장할 내용 (간결하게)",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "delete_memory",
        "description": "저장된 메모리 삭제. 사용자가 '잊어', '삭제해' 등 요청 시 사용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "memory_index": {
                    "type": "integer",
                    "description": "삭제할 메모리 번호 (0부터)",
                },
            },
            "required": ["memory_index"],
        },
    },
    {
        "name": "list_memories",
        "description": "저장된 모든 메모리 목록 조회.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_health_status",
        "description": "시스템 헬스체크 결과 조회 (현금 일관성, 포지션 정합성, API 상태, 에러율). exchange 미지정 시 전체.",
        "input_schema": {
            "type": "object",
            "properties": {
                "exchange": {
                    "type": "string",
                    "enum": ["bithumb", "binance_futures", "binance_spot"],
                },
            },
        },
    },
    {
        "name": "get_funding_rates",
        "description": "바이낸스 선물 현재 펀딩비율 조회. 보유 포지션 코인 대상.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_system_stats",
        "description": "시스템 리소스 현황 (메모리, CPU, 업타임, DB 크기, 엔진 상태 요약).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "close_position",
        "description": "특정 코인 포지션 즉시 청산 (시장가 매도). 위험한 작업이므로 사용자 확인 후 실행.",
        "input_schema": {
            "type": "object",
            "properties": {
                "exchange": {
                    "type": "string",
                    "enum": ["bithumb", "binance_futures", "binance_spot"],
                    "description": "거래소",
                },
                "symbol": {
                    "type": "string",
                    "description": "코인 심볼 (예: BTC/USDT)",
                },
            },
            "required": ["exchange", "symbol"],
        },
    },
]

WRITE_TOOLS = {"start_engine", "stop_engine", "trigger_analysis", "save_memory", "delete_memory", "close_position"}


# ── Bot Memory ─────────────────────────────────────────────────

MEMORY_FILE = Path(__file__).parent.parent.parent / "data" / "bot_memories.json"


def load_memories() -> list[dict]:
    """저장된 메모리 목록 로드."""
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_memories(memories: list[dict]):
    """메모리 목록을 파일에 저장."""
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(
        json.dumps(memories, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── Tool Context ───────────────────────────────────────────────

@dataclass
class ToolContext:
    """도구 핸들러에 주입되는 공유 컨텍스트."""
    engine_registry: Any  # EngineRegistry
    session_factory: async_sessionmaker[AsyncSession]


# ── Tool Handlers ──────────────────────────────────────────────

async def execute_tool(ctx: ToolContext, name: str, input_data: dict) -> dict:
    """도구 이름으로 핸들러를 디스패치."""
    handler = _HANDLERS.get(name)
    if not handler:
        return {"error": f"알 수 없는 도구: {name}"}
    try:
        return await handler(ctx, input_data)
    except Exception as e:
        logger.warning("tool_execution_error", tool=name, error=str(e))
        return {"error": str(e)}


def _get_exchanges(ctx: ToolContext, exchange: str | None) -> list[str]:
    """exchange 파라미터 → 대상 거래소 목록."""
    if exchange:
        if exchange in ctx.engine_registry.available_exchanges:
            return [exchange]
        return []
    return list(ctx.engine_registry.available_exchanges)


async def _handle_engine_status(ctx: ToolContext, input_data: dict) -> dict:
    exchanges = _get_exchanges(ctx, input_data.get("exchange"))
    if not exchanges:
        return {"error": "등록된 거래소가 없습니다."}

    result = {}
    for ex in exchanges:
        eng = ctx.engine_registry.get_engine(ex)
        if not eng:
            continue
        result[ex] = {
            "running": eng.is_running,
            "mode": eng._ec.mode if hasattr(eng, '_ec') else "unknown",
            "tracked_coins": eng.tracked_coins if hasattr(eng, 'tracked_coins') else [],
        }
    return result


async def _handle_portfolio_summary(ctx: ToolContext, input_data: dict) -> dict:
    exchanges = _get_exchanges(ctx, input_data.get("exchange"))
    result = {}
    for ex in exchanges:
        pm = ctx.engine_registry.get_portfolio_manager(ex)
        if not pm:
            continue
        currency = "USDT" if "binance" in ex else "KRW"
        async with ctx.session_factory() as session:
            pos_result = await session.execute(
                select(Position).where(Position.exchange == ex, Position.quantity > 0)
            )
            positions = pos_result.scalars().all()
            invested = sum(p.total_invested for p in positions)
            unrealized = sum(p.unrealized_pnl for p in positions)

        initial = pm._initial_balance
        total_value = pm.cash_balance + invested + unrealized
        result[ex] = {
            "cash_balance": round(pm.cash_balance, 2),
            "invested": round(invested, 2),
            "total_value": round(total_value, 2),
            "unrealized_pnl": round(unrealized, 2),
            "initial_balance": round(initial, 2),
            "return_pct": round((total_value - initial) / initial * 100, 2) if initial > 0 else 0,
            "position_count": len(positions),
            "currency": currency,
        }
    return result


async def _handle_positions(ctx: ToolContext, input_data: dict) -> dict:
    exchanges = _get_exchanges(ctx, input_data.get("exchange"))
    result = {}
    for ex in exchanges:
        async with ctx.session_factory() as session:
            pos_result = await session.execute(
                select(Position).where(Position.exchange == ex, Position.quantity > 0)
            )
            positions = pos_result.scalars().all()
            result[ex] = [
                {
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "average_buy_price": round(p.average_buy_price, 4),
                    "current_value": round(p.current_value, 2),
                    "unrealized_pnl": round(p.unrealized_pnl, 2),
                    "unrealized_pnl_pct": round(p.unrealized_pnl_pct, 2),
                    "direction": getattr(p, "direction", "long") or "long",
                    "leverage": getattr(p, "leverage", 1) or 1,
                    "stop_loss_pct": p.stop_loss_pct,
                    "take_profit_pct": p.take_profit_pct,
                }
                for p in positions
            ]
    return result


async def _handle_recent_trades(ctx: ToolContext, input_data: dict) -> dict:
    exchanges = _get_exchanges(ctx, input_data.get("exchange"))
    limit = min(input_data.get("limit", 10), 30)
    symbol = input_data.get("symbol")
    side = input_data.get("side")

    result = {}
    for ex in exchanges:
        async with ctx.session_factory() as session:
            q = select(Order).where(
                Order.exchange == ex,
                Order.status == "filled",
            ).order_by(desc(Order.filled_at)).limit(limit)
            if symbol:
                q = q.where(Order.symbol == symbol)
            if side:
                q = q.where(Order.side == side)
            rows = (await session.execute(q)).scalars().all()
            result[ex] = [
                {
                    "symbol": o.symbol,
                    "side": o.side,
                    "price": round(o.executed_price, 4) if o.executed_price else None,
                    "quantity": o.executed_quantity,
                    "strategy": o.strategy_name,
                    "direction": o.direction,
                    "pnl": round(o.realized_pnl, 2) if o.realized_pnl is not None else None,
                    "pnl_pct": round(o.realized_pnl_pct, 2) if o.realized_pnl_pct is not None else None,
                    "filled_at": o.filled_at.isoformat() if o.filled_at else None,
                }
                for o in rows
            ]
    return result


async def _handle_daily_pnl(ctx: ToolContext, input_data: dict) -> dict:
    exchanges = _get_exchanges(ctx, input_data.get("exchange"))
    days = min(input_data.get("days", 7), 90)

    result = {}
    for ex in exchanges:
        async with ctx.session_factory() as session:
            since = datetime.now(timezone.utc).date() - timedelta(days=days)
            rows = (await session.execute(
                select(DailyPnL).where(
                    DailyPnL.exchange == ex,
                    DailyPnL.date >= since,
                ).order_by(desc(DailyPnL.date))
            )).scalars().all()
            result[ex] = [
                {
                    "date": str(r.date),
                    "daily_pnl": round(r.daily_pnl, 2),
                    "daily_pnl_pct": round(r.daily_pnl_pct, 2),
                    "realized_pnl": round(r.realized_pnl, 2),
                    "win_count": r.win_count,
                    "loss_count": r.loss_count,
                    "trade_count": r.trade_count,
                }
                for r in rows
            ]
    return result


async def _handle_market_analysis(ctx: ToolContext, input_data: dict) -> dict:
    exchanges = _get_exchanges(ctx, input_data.get("exchange"))
    result = {}
    for ex in exchanges:
        coord = ctx.engine_registry.get_coordinator(ex)
        if not coord:
            continue
        analysis = coord.last_market_analysis
        if analysis:
            result[ex] = {
                "market_state": analysis.state.value if hasattr(analysis.state, 'value') else str(analysis.state),
                "confidence": analysis.confidence,
                "volatility": analysis.volatility_level,
                "reasoning": analysis.reasoning,
                "recommended_weights": analysis.recommended_weights,
                "indicators": analysis.indicators,
            }
        else:
            # DB fallback
            async with ctx.session_factory() as session:
                row = (await session.execute(
                    select(AgentAnalysisLog).where(
                        AgentAnalysisLog.exchange == ex,
                        AgentAnalysisLog.agent_name == "market_analysis",
                    ).order_by(desc(AgentAnalysisLog.analyzed_at)).limit(1)
                )).scalar_one_or_none()
                if row:
                    result[ex] = row.result
                else:
                    result[ex] = {"message": "분석 데이터 없음"}
    return result


async def _handle_performance_report(ctx: ToolContext, input_data: dict) -> dict:
    exchanges = _get_exchanges(ctx, input_data.get("exchange"))
    result = {}
    for ex in exchanges:
        async with ctx.session_factory() as session:
            row = (await session.execute(
                select(AgentAnalysisLog).where(
                    AgentAnalysisLog.exchange == ex,
                    AgentAnalysisLog.agent_name == "performance_analytics",
                ).order_by(desc(AgentAnalysisLog.analyzed_at)).limit(1)
            )).scalar_one_or_none()
            if row:
                result[ex] = row.result
            else:
                result[ex] = {"message": "성과 분석 데이터 없음"}
    return result


async def _handle_strategy_advice(ctx: ToolContext, input_data: dict) -> dict:
    exchanges = _get_exchanges(ctx, input_data.get("exchange"))
    result = {}
    for ex in exchanges:
        async with ctx.session_factory() as session:
            row = (await session.execute(
                select(AgentAnalysisLog).where(
                    AgentAnalysisLog.exchange == ex,
                    AgentAnalysisLog.agent_name == "strategy_advisor",
                ).order_by(desc(AgentAnalysisLog.analyzed_at)).limit(1)
            )).scalar_one_or_none()
            if row:
                result[ex] = row.result
            else:
                result[ex] = {"message": "전략 조언 데이터 없음"}
    return result


async def _handle_start_engine(ctx: ToolContext, input_data: dict) -> dict:
    exchange = input_data["exchange"]
    eng = ctx.engine_registry.get_engine(exchange)
    if not eng:
        return {"error": f"거래소 '{exchange}' 미등록"}
    if eng.is_running:
        return {"status": "already_running", "exchange": exchange}
    import asyncio
    asyncio.create_task(eng.start())
    return {"status": "started", "exchange": exchange}


async def _handle_stop_engine(ctx: ToolContext, input_data: dict) -> dict:
    exchange = input_data["exchange"]
    eng = ctx.engine_registry.get_engine(exchange)
    if not eng:
        return {"error": f"거래소 '{exchange}' 미등록"}
    if not eng.is_running:
        return {"status": "already_stopped", "exchange": exchange}

    # 열린 포지션 경고
    async with ctx.session_factory() as session:
        pos_count = (await session.execute(
            select(func.count()).select_from(Position).where(
                Position.exchange == exchange, Position.quantity > 0
            )
        )).scalar()

    await eng.stop()
    result = {"status": "stopped", "exchange": exchange}
    if pos_count:
        result["warning"] = f"열린 포지션 {pos_count}개가 있습니다. 포지션은 유지되지만 SL/TP 모니터링이 중단됩니다."
    return result


async def _handle_trigger_analysis(ctx: ToolContext, input_data: dict) -> dict:
    analysis_type = input_data["analysis_type"]
    exchanges = _get_exchanges(ctx, input_data.get("exchange"))
    if not exchanges:
        exchanges = list(ctx.engine_registry.available_exchanges)

    results = {}
    for ex in exchanges:
        coord = ctx.engine_registry.get_coordinator(ex)
        if not coord:
            continue
        try:
            if analysis_type == "performance":
                await coord.run_performance_analysis()
                results[ex] = "성과 분석 완료"
            elif analysis_type == "strategy_advice":
                await coord.run_strategy_advice()
                results[ex] = "전략 조언 완료"
            elif analysis_type == "trade_review":
                await coord.run_trade_review()
                results[ex] = "거래 리뷰 완료"
            else:
                results[ex] = f"알 수 없는 분석 유형: {analysis_type}"
        except Exception as e:
            results[ex] = f"실패: {str(e)}"
    return results


async def _handle_save_memory(ctx: ToolContext, input_data: dict) -> dict:
    content = input_data["content"].strip()
    if not content:
        return {"error": "내용이 비어있습니다."}
    memories = load_memories()
    memories.append({
        "content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_memories(memories)
    logger.info("bot_memory_saved", content=content[:80], total=len(memories))
    return {"status": "saved", "total_memories": len(memories)}


async def _handle_delete_memory(ctx: ToolContext, input_data: dict) -> dict:
    idx = input_data["memory_index"]
    memories = load_memories()
    if idx < 0 or idx >= len(memories):
        return {"error": f"유효하지 않은 번호: {idx} (총 {len(memories)}개)"}
    removed = memories.pop(idx)
    _save_memories(memories)
    logger.info("bot_memory_deleted", content=removed["content"][:80])
    return {"status": "deleted", "removed": removed["content"], "remaining": len(memories)}


async def _handle_list_memories(ctx: ToolContext, input_data: dict) -> dict:
    memories = load_memories()
    return {
        "total": len(memories),
        "memories": [
            {"index": i, "content": m["content"], "created_at": m["created_at"]}
            for i, m in enumerate(memories)
        ],
    }


async def _handle_health_status(ctx: ToolContext, input_data: dict) -> dict:
    exchanges = _get_exchanges(ctx, input_data.get("exchange"))
    if not exchanges:
        return {"error": "등록된 거래소가 없습니다."}

    result = {}
    for ex in exchanges:
        eng = ctx.engine_registry.get_engine(ex)
        if not eng:
            continue
        health_monitor = getattr(eng, '_health_monitor', None)
        if not health_monitor:
            result[ex] = {"status": "no_health_monitor"}
            continue
        try:
            checks = await health_monitor.run_checks()
            result[ex] = {
                "total_checks": len(checks),
                "healthy": sum(1 for c in checks if c.healthy),
                "unhealthy": sum(1 for c in checks if not c.healthy),
                "details": [
                    {"name": c.name, "healthy": c.healthy, "detail": c.detail, "auto_fixed": c.auto_fixed}
                    for c in checks
                ],
            }
        except Exception as e:
            result[ex] = {"error": str(e)}
    return result


async def _handle_funding_rates(ctx: ToolContext, input_data: dict) -> dict:
    eng = ctx.engine_registry.get_engine("binance_futures")
    if not eng:
        return {"error": "선물 엔진이 등록되지 않았습니다."}

    adapter = eng._exchange
    results = {}
    # 보유 포지션 코인의 펀딩비 조회
    async with ctx.session_factory() as session:
        pos_result = await session.execute(
            select(Position).where(
                Position.exchange == "binance_futures",
                Position.quantity > 0,
            )
        )
        positions = pos_result.scalars().all()

    symbols = [p.symbol for p in positions] or eng.tracked_coins[:5]
    for symbol in symbols:
        try:
            rate = await adapter.fetch_funding_rate(symbol)
            results[symbol] = {
                "funding_rate": round(rate * 100, 4),
                "annualized": round(rate * 3 * 365 * 100, 2),
            }
        except Exception as e:
            results[symbol] = {"error": str(e)}
    return results


async def _handle_system_stats(ctx: ToolContext, input_data: dict) -> dict:
    import os
    import psutil

    process = psutil.Process(os.getpid())
    mem = process.memory_info()

    # DB 크기
    db_size = "unknown"
    try:
        async with ctx.session_factory() as session:
            result = await session.execute(
                select(func.count()).select_from(Order)
            )
            order_count = result.scalar() or 0
            result2 = await session.execute(
                select(func.count()).select_from(Position)
            )
            pos_count = result2.scalar() or 0
    except Exception:
        order_count = 0
        pos_count = 0

    # 엔진 상태 요약
    engines = {}
    for ex in ctx.engine_registry.available_exchanges:
        eng = ctx.engine_registry.get_engine(ex)
        if eng:
            engines[ex] = {
                "running": eng.is_running,
                "mode": eng._ec.mode if hasattr(eng, '_ec') else "unknown",
                "positions": len(getattr(eng, '_position_trackers', {})),
            }

    return {
        "memory_rss_mb": round(mem.rss / 1024 / 1024, 1),
        "memory_vms_mb": round(mem.vms / 1024 / 1024, 1),
        "cpu_percent": process.cpu_percent(),
        "uptime_hours": round((datetime.now(timezone.utc).timestamp() - process.create_time()) / 3600, 1),
        "db_orders": order_count,
        "db_positions": pos_count,
        "engines": engines,
    }


async def _handle_close_position(ctx: ToolContext, input_data: dict) -> dict:
    exchange = input_data["exchange"]
    symbol = input_data["symbol"]

    eng = ctx.engine_registry.get_engine(exchange)
    if not eng:
        return {"error": f"거래소 '{exchange}'가 등록되지 않았습니다."}
    if not eng.is_running:
        return {"error": f"'{exchange}' 엔진이 실행 중이 아닙니다."}

    # DB에서 포지션 확인
    async with ctx.session_factory() as session:
        result = await session.execute(
            select(Position).where(
                Position.symbol == symbol,
                Position.exchange == exchange,
                Position.quantity > 0,
            )
        )
        position = result.scalar_one_or_none()
        if not position:
            return {"error": f"'{symbol}' 포지션이 없습니다."}

        qty = position.quantity
        direction = position.direction or "long"

    # 시장가 청산
    try:
        adapter = eng._exchange
        if direction == "short":
            order = await adapter.create_market_buy(symbol, qty)
        else:
            order = await adapter.create_market_sell(symbol, qty)
        logger.info("bot_close_position", exchange=exchange, symbol=symbol,
                     direction=direction, qty=qty)
        return {
            "status": "closed",
            "symbol": symbol,
            "direction": direction,
            "quantity": qty,
            "order_id": order.order_id,
        }
    except Exception as e:
        return {"error": f"청산 실패: {str(e)}"}


# ── Handler Dispatch Map ───────────────────────────────────────

_HANDLERS = {
    "get_engine_status": _handle_engine_status,
    "get_portfolio_summary": _handle_portfolio_summary,
    "get_positions": _handle_positions,
    "get_recent_trades": _handle_recent_trades,
    "get_daily_pnl": _handle_daily_pnl,
    "get_market_analysis": _handle_market_analysis,
    "get_performance_report": _handle_performance_report,
    "get_strategy_advice": _handle_strategy_advice,
    "start_engine": _handle_start_engine,
    "stop_engine": _handle_stop_engine,
    "trigger_analysis": _handle_trigger_analysis,
    "save_memory": _handle_save_memory,
    "delete_memory": _handle_delete_memory,
    "list_memories": _handle_list_memories,
    "get_health_status": _handle_health_status,
    "get_funding_rates": _handle_funding_rates,
    "get_system_stats": _handle_system_stats,
    "close_position": _handle_close_position,
}
