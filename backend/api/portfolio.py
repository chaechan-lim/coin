from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from datetime import timedelta
from core.utils import utcnow

from db.session import get_db
from core.models import PortfolioSnapshot, DailyPnL, Position
from core.schemas import PortfolioSummaryResponse, PortfolioHistoryPoint, DailyPnLResponse
from api.dependencies import engine_registry, ExchangeNameType

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


def _get_pm(exchange: str):
    return engine_registry.get_portfolio_manager(exchange)


async def _merge_surge_positions(summary: dict, session: AsyncSession) -> dict:
    """선물 포트폴리오 요약에 서지 포지션을 병합."""
    result = await session.execute(
        select(Position).where(
            Position.exchange == "binance_surge",
            Position.quantity > 0,
        )
    )
    surge_positions = result.scalars().all()
    if not surge_positions:
        return summary

    # 서지 엔진에서 최신 가격 가져오기
    surge_eng = engine_registry.get_engine("binance_surge")

    for pos in surge_positions:
        entry = pos.average_buy_price
        invested = pos.total_invested or 0
        direction = pos.direction or "long"
        leverage = pos.leverage or 3

        # 인메모리 최신 가격 우선, 없으면 진입가 사용
        current = entry
        if surge_eng:
            sym_state = surge_eng._symbol_states.get(pos.symbol)
            if sym_state and sym_state.last_price > 0:
                current = sym_state.last_price

        if entry > 0:
            if direction == "short":
                raw_pnl_pct = (entry - current) / entry
            else:
                raw_pnl_pct = (current - entry) / entry
            unrealized = invested * leverage * raw_pnl_pct
        else:
            raw_pnl_pct = 0.0
            unrealized = 0.0

        current_value = invested + unrealized
        pnl_pct = raw_pnl_pct * leverage * 100 if entry > 0 else 0.0

        # SL/TP 가격 계산 (DB에는 % 만 저장)
        sl_price = None
        tp_price = None
        if entry > 0 and pos.stop_loss_pct:
            if direction == "short":
                sl_price = entry * (1 + pos.stop_loss_pct / 100 / leverage)
            else:
                sl_price = entry * (1 - pos.stop_loss_pct / 100 / leverage)
        if entry > 0 and pos.take_profit_pct:
            if direction == "short":
                tp_price = entry * (1 - pos.take_profit_pct / 100 / leverage)
            else:
                tp_price = entry * (1 + pos.take_profit_pct / 100 / leverage)

        summary["positions"].append({
            "symbol": pos.symbol,
            "quantity": pos.quantity,
            "average_buy_price": entry,
            "current_price": current,
            "current_value": round(current_value, 4),
            "unrealized_pnl": round(unrealized, 4),
            "unrealized_pnl_pct": round(pnl_pct, 2),
            "total_invested": round(invested, 4),
            "margin_used": round(invested, 4),
            "entered_at": pos.entered_at.isoformat() if pos.entered_at else None,
            "direction": direction,
            "leverage": leverage,
            "liquidation_price": pos.liquidation_price,
            "stop_loss_price": sl_price,
            "take_profit_price": tp_price,
            "stop_loss_pct": pos.stop_loss_pct,
            "take_profit_pct": pos.take_profit_pct,
            "trailing_activation_pct": pos.trailing_activation_pct,
            "trailing_stop_pct": pos.trailing_stop_pct,
            "trailing_active": pos.trailing_active,
            "highest_price": pos.highest_price,
            "max_hold_hours": pos.max_hold_hours,
            "is_surge": True,
        })

        # 합산 수치 갱신
        # total_value_krw 계산:
        #   서지 진입 시 futures_pm.cash_balance -= margin 이므로 cash가 이미 margin만큼 줄어있음.
        #   따라서 current_value(= invested + unrealized) 전체를 더해야 총 자산이 올바름.
        #   unrealized만 더하면 invested(margin)만큼 총 자산이 과소계상됨.
        summary["invested_value_krw"] = round(summary.get("invested_value_krw", 0) + invested, 2)
        summary["unrealized_pnl"] = round(summary.get("unrealized_pnl", 0) + unrealized, 2)
        summary["total_value_krw"] = round(summary.get("total_value_krw", 0) + current_value, 2)

    return summary


_RND_FUTURES_ENGINES = ("binance_donchian_futures", "binance_pairs", "binance_momentum", "binance_hmm")
_RND_SPOT_ENGINES = ("binance_donchian", "binance_fgdca")


def _merge_rnd_positions(summary: dict, engine_names: tuple[str, ...]) -> dict:
    """R&D 엔진의 get_status()에서 포지션 정보를 수집하여 병합."""
    for eng_name in engine_names:
        eng = engine_registry.get_engine(eng_name)
        if eng is None or not hasattr(eng, "get_status"):
            continue
        try:
            status = eng.get_status()
        except Exception:
            continue

        # 포지션 목록 추출 (엔진마다 형태가 다름)
        positions_raw = status.get("positions") or []
        if isinstance(positions_raw, dict):
            # HMM 단일 포지션
            pos = status.get("position")
            positions_raw = [pos] if pos else []

        for p in positions_raw:
            if not isinstance(p, dict):
                continue
            symbol = p.get("symbol", "")
            entry = p.get("entry_price") or p.get("entry") or 0
            qty = p.get("quantity") or p.get("qty") or 0
            side = p.get("side") or p.get("direction") or "long"
            if qty <= 0 and entry <= 0:
                continue

            summary["positions"].append({
                "symbol": symbol,
                "quantity": qty,
                "average_buy_price": entry,
                "current_price": entry,  # 정확한 현재가는 없으므로 entry 사용
                "current_value": 0,
                "unrealized_pnl": 0,
                "unrealized_pnl_pct": 0,
                "total_invested": qty * entry if entry > 0 else 0,
                "direction": side,
                "leverage": status.get("leverage", 1),
                "is_surge": False,
                "is_rnd": True,
                "rnd_engine": eng_name,
            })

        # 누적 PnL 병합
        cum_pnl = status.get("cumulative_pnl", 0)
        summary["realized_pnl"] = round(summary.get("realized_pnl", 0) + cum_pnl, 2)

    return summary


@router.get("/summary", response_model=PortfolioSummaryResponse)
async def get_portfolio_summary(
    exchange: ExchangeNameType = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    pm = _get_pm(exchange)

    # PM이 없어도 R&D 엔진 포지션은 보여줌
    if not pm:
        if exchange == "binance_futures":
            # R&D 선물 엔진 포지션 수집
            summary = {
                "exchange": exchange, "total_value_krw": 0, "cash_balance_krw": 0,
                "invested_value_krw": 0, "initial_balance_krw": 0,
                "realized_pnl": 0, "unrealized_pnl": 0, "total_pnl": 0, "total_pnl_pct": 0,
                "total_fees": 0, "trade_count": 0, "peak_value": 0, "drawdown_pct": 0,
                "positions": [],
            }
            summary = _merge_rnd_positions(summary, _RND_FUTURES_ENGINES)
            return PortfolioSummaryResponse(**summary)
        elif exchange == "binance_spot":
            summary = {
                "exchange": exchange, "total_value_krw": 0, "cash_balance_krw": 0,
                "invested_value_krw": 0, "initial_balance_krw": 0,
                "realized_pnl": 0, "unrealized_pnl": 0, "total_pnl": 0, "total_pnl_pct": 0,
                "total_fees": 0, "trade_count": 0, "peak_value": 0, "drawdown_pct": 0,
                "positions": [],
            }
            summary = _merge_rnd_positions(summary, _RND_SPOT_ENGINES)
            return PortfolioSummaryResponse(**summary)
        return PortfolioSummaryResponse(
            exchange=exchange,
            total_value_krw=0, cash_balance_krw=0, invested_value_krw=0,
            initial_balance_krw=0,
            realized_pnl=0, unrealized_pnl=0, total_pnl=0, total_pnl_pct=0,
            total_fees=0, trade_count=0,
            peak_value=0, drawdown_pct=0, positions=[],
        )

    summary = await pm.get_portfolio_summary(session)

    # 선물 조회 시 서지 + R&D 포지션 병합
    if exchange == "binance_futures":
        summary = await _merge_surge_positions(summary, session)
        summary = _merge_rnd_positions(summary, _RND_FUTURES_ENGINES)
    elif exchange == "binance_spot":
        summary = _merge_rnd_positions(summary, _RND_SPOT_ENGINES)

    return PortfolioSummaryResponse(**summary)


@router.get("/history", response_model=list[PortfolioHistoryPoint])
async def get_portfolio_history(
    period: str = Query("7d", pattern="^(1d|7d|30d|90d|all)$"),
    exchange: ExchangeNameType = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    now = utcnow()
    period_map = {
        "1d": timedelta(days=1),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
        "90d": timedelta(days=90),
    }

    query = (
        select(PortfolioSnapshot)
        .where(PortfolioSnapshot.exchange == exchange)
        .order_by(desc(PortfolioSnapshot.snapshot_at))
    )

    if period != "all" and period in period_map:
        start = now - period_map[period]
        query = query.where(PortfolioSnapshot.snapshot_at >= start)

    query = query.limit(1000)
    result = await session.execute(query)
    snapshots = result.scalars().all()

    return [
        PortfolioHistoryPoint(
            timestamp=s.snapshot_at,
            total_value=s.total_value_krw,
            cash_balance=s.cash_balance_krw,
            unrealized_pnl=s.unrealized_pnl,
            drawdown_pct=s.drawdown_pct,
        )
        for s in reversed(list(snapshots))
    ]


@router.get("/daily-pnl", response_model=list[DailyPnLResponse])
async def get_daily_pnl(
    days: int = Query(30, ge=1, le=365),
    exchange: ExchangeNameType = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    now = utcnow()
    start_date = (now - timedelta(days=days)).date()

    result = await session.execute(
        select(DailyPnL)
        .where(
            DailyPnL.exchange == exchange,
            DailyPnL.date >= start_date,
        )
        .order_by(DailyPnL.date.asc())
    )
    records = result.scalars().all()

    return [
        DailyPnLResponse(
            date=r.date,
            open_value=r.open_value or 0,
            close_value=r.close_value or 0,
            daily_pnl=r.daily_pnl or 0,
            daily_pnl_pct=r.daily_pnl_pct or 0,
            realized_pnl=r.realized_pnl or 0,
            total_fees=r.total_fees or 0,
            trade_count=r.trade_count or 0,
            buy_count=r.buy_count or 0,
            sell_count=r.sell_count or 0,
            win_count=r.win_count or 0,
            loss_count=r.loss_count or 0,
        )
        for r in records
    ]
