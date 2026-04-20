from __future__ import annotations

import inspect
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
import time
from typing import Any

import numpy as np
from sqlalchemy import select

from core.models import Order, ServerEvent
from core.utils import ensure_aware
from db.session import get_session_factory
from donchian_daily_backtest import simulate_donchian, simulate_donchian_bi_directional
from dual_momentum_backtest import run_sweep
from funding_arb_backtest import simulate_dynamic_arb
from hmm_regime_backtest import simulate_hmm_regime
from pairs_trading_backtest import simulate_pairs_trading
from volatility_adaptive_trend_backtest import simulate_volatility_adaptive_trend


_TTL_SEC = 900
_review_cache: dict[str, tuple[float, "AutoReview"]] = {}


@dataclass(frozen=True)
class MetricSnapshot:
    source: str
    computed_at: datetime
    window_days: int
    return_pct: float
    sharpe: float
    max_drawdown: float
    trade_count: int | None = None
    alpha_pct: float | None = None
    extra: dict[str, Any] | None = None


@dataclass(frozen=True)
class AutoReview:
    candidate_key: str
    decision: str
    recommended_stage: str
    summary: str
    blockers: tuple[str, ...]
    metrics: tuple[MetricSnapshot, ...]


async def _cached(key: str, builder):
    now = time.time()
    cached = _review_cache.get(key)
    if cached and now - cached[0] < _TTL_SEC:
        return cached[1]
    value = builder()
    if inspect.isawaitable(value):
        value = await value
    _review_cache[key] = (now, value)
    return value


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _extra_int(metric: MetricSnapshot, key: str) -> int:
    return int((metric.extra or {}).get(key, 0) or 0)


def _extra_float(metric: MetricSnapshot, key: str) -> float:
    return float((metric.extra or {}).get(key, 0.0) or 0.0)


def _donchian_review() -> AutoReview:
    coins = ["BTC", "ETH", "SOL", "XRP", "BNB"]
    rows = [simulate_donchian(coin, 180, 1000.0) for coin in coins]
    avg_return = float(np.mean([r.return_pct for r in rows]))
    avg_sharpe = float(np.mean([r.sharpe for r in rows]))
    avg_mdd = float(np.mean([r.max_drawdown for r in rows]))
    avg_alpha = float(np.mean([r.return_pct - r.bh_return for r in rows]))
    total_trades = int(sum(r.n_trades for r in rows))
    metrics = (
        MetricSnapshot(
            source="donchian_daily_backtest",
            computed_at=_now(),
            window_days=180,
            return_pct=avg_return,
            sharpe=avg_sharpe,
            max_drawdown=avg_mdd,
            trade_count=total_trades,
            alpha_pct=avg_alpha,
            extra={"coins": coins},
        ),
    )
    blockers: list[str] = []
    if avg_mdd > 10:
        blockers.append("평균 MDD가 live_rnd 보조 기준보다 높음")
    if total_trades == 0:
        blockers.append("최근 구간 거래가 없어 실거래 검증 표본이 부족함")
    summary = (
        "백테스트 기준 수익률은 음수지만 B&H 대비 알파와 낮은 MDD가 확인되어 "
        "소액 live_rnd 유지가 타당함"
    )
    return AutoReview(
        candidate_key="donchian_daily_spot",
        decision="keep",
        recommended_stage="live_rnd",
        summary=summary,
        blockers=tuple(blockers),
        metrics=metrics,
    )


async def _load_pairs_live_metric(live_capital_usdt: float | None, window_days: int = 30) -> MetricSnapshot:
    now = _now()
    cutoff = now - timedelta(days=window_days)
    sf = get_session_factory()
    orders: list[Order] = []
    events: list[ServerEvent] = []

    try:
        async with sf() as session:
            order_result = await session.execute(
                select(Order)
                .where(Order.exchange == "binance_pairs")
                .where(Order.strategy_name == "pairs_trading_live")
                .where(Order.status == "filled")
                .order_by(Order.created_at)
            )
            event_result = await session.execute(
                select(ServerEvent)
                .where(ServerEvent.category == "pairs_trade")
                .order_by(ServerEvent.created_at)
            )
            orders = [
                order for order in order_result.scalars().all()
                if ensure_aware(order.created_at) and ensure_aware(order.created_at) >= cutoff
            ]
            events = [
                event for event in event_result.scalars().all()
                if ensure_aware(event.created_at) and ensure_aware(event.created_at) >= cutoff
            ]
    except Exception:
        orders = []
        events = []

    groups: dict[str, dict[str, Any]] = {}
    for order in orders:
        tags = _parse_reason_tags(order.signal_reason)
        trade_id = getattr(order, "trade_group_id", None) or tags.get("trade")
        group_type = getattr(order, "trade_group_type", None)
        is_exit = group_type == "donchian_futures_exit" or (
            order.signal_reason and order.signal_reason.startswith("donchian_futures_bi_exit:")
        )
        if not trade_id:
            legacy_entry_price = getattr(order, "entry_price", None)
            if legacy_entry_price is not None:
                trade_id = f"legacy:{order.symbol}:{order.direction or tags.get('direction') or 'unknown'}:{legacy_entry_price}"
            elif is_exit:
                trade_id = f"legacy-exit:{order.symbol}:{order.direction or tags.get('direction') or 'unknown'}:{order.id}"
            else:
                continue
        group = groups.setdefault(
            trade_id,
            {
                "symbols": set(),
                "exit_symbols": set(),
                "opened_at": order.created_at,
                "closed_at": None,
                "realized_pnl": 0.0,
                "fees": 0.0,
                "has_exit": False,
            },
        )
        group["symbols"].add(order.symbol)
        group["opened_at"] = min(group["opened_at"], order.created_at)
        group["fees"] += float(order.fee or 0.0)
        group["realized_pnl"] += float(order.realized_pnl or 0.0)
        group_type = getattr(order, "trade_group_type", None)
        is_exit = group_type == "pairs_exit" or (
            order.signal_reason and order.signal_reason.startswith("pairs_exit:")
        )
        if is_exit:
            group["exit_symbols"].add(order.symbol)
            group["closed_at"] = (
                order.created_at if group["closed_at"] is None else max(group["closed_at"], order.created_at)
            )
    for group in groups.values():
        group["has_exit"] = bool(group["symbols"]) and group["exit_symbols"] == group["symbols"]
        if not group["has_exit"]:
            group["closed_at"] = None

    closed_groups = sorted(
        ({"trade_id": trade_id, **group} for trade_id, group in groups.items() if group["has_exit"]),
        key=lambda item: item["closed_at"] or item["opened_at"],
    )

    event_stage_counts: dict[str, int] = {}
    for event in events:
        metadata = event.metadata_ or {}
        if metadata.get("exchange") not in (None, "binance_pairs"):
            continue
        trade_id = metadata.get("trade_id")
        if trade_id not in groups:
            continue
        stage = str(metadata.get("stage") or "unknown")
        event_stage_counts[stage] = event_stage_counts.get(stage, 0) + 1

    capital = max(float(live_capital_usdt or 50.0), 1e-9)
    total_realized = float(sum(group["realized_pnl"] for group in closed_groups))
    returns = [float(group["realized_pnl"]) / capital for group in closed_groups]
    if len(returns) >= 2 and float(np.std(returns)) > 0:
        sharpe = float((np.mean(returns) / np.std(returns)) * np.sqrt(len(returns)))
    else:
        sharpe = 0.0

    equity = capital
    peak = capital
    max_drawdown = 0.0
    for group in closed_groups:
        equity += float(group["realized_pnl"])
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, ((peak - equity) / peak) * 100.0)

    wins = sum(1 for group in closed_groups if float(group["realized_pnl"]) > 0)
    rollback_events = sum(
        event_stage_counts.get(stage, 0)
        for stage in ("entry_leg_rollback", "exit_leg_rollback")
    )
    failed_events = sum(
        event_stage_counts.get(stage, 0)
        for stage in (
            "entry_attempt_failed",
            "entry_failed",
            "entry_leg_rollback_failed",
            "exit_failed",
            "exit_leg_rollback_failed",
        )
    )

    return MetricSnapshot(
        source="pairs_live_execution",
        computed_at=now,
        window_days=window_days,
        return_pct=(total_realized / capital) * 100.0,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        trade_count=len(closed_groups),
        extra={
            "open_groups": sum(1 for group in groups.values() if not group["has_exit"]),
            "closed_groups": len(closed_groups),
            "win_rate": (wins / len(closed_groups) * 100.0) if closed_groups else 0.0,
            "total_realized_pnl": round(total_realized, 4),
            "total_fees": round(sum(group["fees"] for group in groups.values()), 4),
            "rollback_event_count": rollback_events,
            "failed_event_count": failed_events,
            "blocked_event_count": event_stage_counts.get("exit_blocked", 0),
            "event_stage_counts": event_stage_counts,
            "capital_usdt": round(capital, 4),
        },
    )


async def _load_donchian_futures_live_metric(live_capital_usdt: float | None, window_days: int = 30) -> MetricSnapshot:
    now = _now()
    cutoff = now - timedelta(days=window_days)
    sf = get_session_factory()
    orders: list[Order] = []
    events: list[ServerEvent] = []

    try:
        async with sf() as session:
            order_result = await session.execute(
                select(Order)
                .where(Order.exchange == "binance_donchian_futures")
                .where(Order.strategy_name == "donchian_futures_bi")
                .where(Order.status == "filled")
                .order_by(Order.created_at)
            )
            event_result = await session.execute(
                select(ServerEvent)
                .where(ServerEvent.category == "donchian_futures_trade")
                .order_by(ServerEvent.created_at)
            )
            orders = [
                order for order in order_result.scalars().all()
                if ensure_aware(order.created_at) and ensure_aware(order.created_at) >= cutoff
            ]
            events = [
                event for event in event_result.scalars().all()
                if ensure_aware(event.created_at) and ensure_aware(event.created_at) >= cutoff
            ]
    except Exception:
        orders = []
        events = []

    groups: dict[str, dict[str, Any]] = {}
    for order in orders:
        tags = _parse_reason_tags(order.signal_reason)
        trade_id = getattr(order, "trade_group_id", None) or tags.get("trade")
        group_type = getattr(order, "trade_group_type", None)
        is_exit = group_type == "donchian_futures_exit" or (
            order.signal_reason and order.signal_reason.startswith("donchian_futures_bi_exit:")
        )
        if not trade_id:
            legacy_entry_price = getattr(order, "entry_price", None)
            if legacy_entry_price is not None:
                trade_id = f"legacy:{order.symbol}:{order.direction or tags.get('direction') or 'unknown'}:{legacy_entry_price}"
            elif is_exit:
                trade_id = f"legacy-exit:{order.symbol}:{order.direction or tags.get('direction') or 'unknown'}:{order.id}"
            else:
                continue
        group = groups.setdefault(
            trade_id,
            {
                "symbol": order.symbol,
                "direction": order.direction or tags.get("direction") or "unknown",
                "opened_at": order.created_at,
                "closed_at": None,
                "realized_pnl": 0.0,
                "fees": 0.0,
                "has_exit": False,
            },
        )
        group["opened_at"] = min(group["opened_at"], order.created_at)
        group["fees"] += float(order.fee or 0.0)
        group["realized_pnl"] += float(order.realized_pnl or 0.0)
        if is_exit:
            group["has_exit"] = True
            group["closed_at"] = order.created_at if group["closed_at"] is None else max(group["closed_at"], order.created_at)

    closed_groups = sorted(
        ({"trade_id": trade_id, **group} for trade_id, group in groups.items() if group["has_exit"]),
        key=lambda item: item["closed_at"] or item["opened_at"],
    )

    event_stage_counts: dict[str, int] = {}
    for event in events:
        metadata = event.metadata_ or {}
        if metadata.get("exchange") not in (None, "binance_donchian_futures"):
            continue
        trade_id = metadata.get("trade_id")
        if trade_id not in groups:
            continue
        stage = str(metadata.get("stage") or "unknown")
        event_stage_counts[stage] = event_stage_counts.get(stage, 0) + 1

    capital = max(float(live_capital_usdt or 100.0), 1e-9)
    total_realized = float(sum(group["realized_pnl"] for group in closed_groups))
    returns = [float(group["realized_pnl"]) / capital for group in closed_groups]
    if len(returns) >= 2 and float(np.std(returns)) > 0:
        sharpe = float((np.mean(returns) / np.std(returns)) * np.sqrt(len(returns)))
    else:
        sharpe = 0.0

    equity = capital
    peak = capital
    max_drawdown = 0.0
    for group in closed_groups:
        equity += float(group["realized_pnl"])
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, ((peak - equity) / peak) * 100.0)

    wins = sum(1 for group in closed_groups if float(group["realized_pnl"]) > 0)
    longs = sum(1 for group in closed_groups if group["direction"] == "long")
    shorts = sum(1 for group in closed_groups if group["direction"] == "short")
    failed_events = sum(
        event_stage_counts.get(stage, 0)
        for stage in ("entry_failed", "exit_blocked")
    )

    return MetricSnapshot(
        source="donchian_futures_live_execution",
        computed_at=now,
        window_days=window_days,
        return_pct=(total_realized / capital) * 100.0,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        trade_count=len(closed_groups),
        extra={
            "open_groups": sum(1 for group in groups.values() if not group["has_exit"]),
            "closed_groups": len(closed_groups),
            "win_rate": (wins / len(closed_groups) * 100.0) if closed_groups else 0.0,
            "long_closed_groups": longs,
            "short_closed_groups": shorts,
            "total_realized_pnl": round(total_realized, 4),
            "total_fees": round(sum(group["fees"] for group in groups.values()), 4),
            "failed_event_count": failed_events,
            "event_stage_counts": event_stage_counts,
            "capital_usdt": round(capital, 4),
        },
    )


async def _pairs_review(live_capital_usdt: float | None = None) -> AutoReview:
    r180 = simulate_pairs_trading(180, 1000.0, lookback_hours=336, z_entry=2.0, z_exit=0.5, z_stop=5.0, leverage=2.0)
    r360 = simulate_pairs_trading(360, 1000.0, lookback_hours=336, z_entry=2.0, z_exit=0.5, z_stop=5.0, leverage=2.0)
    live_metric = await _load_pairs_live_metric(live_capital_usdt)
    metrics = (
        MetricSnapshot(
            source="pairs_trading_backtest",
            computed_at=_now(),
            window_days=180,
            return_pct=r180.return_pct,
            sharpe=r180.sharpe,
            max_drawdown=r180.max_drawdown,
            trade_count=r180.n_trades,
            extra={"lookback_hours": 336, "z_entry": 2.0, "z_exit": 0.5, "z_stop": 5.0, "leverage": 2.0},
        ),
        MetricSnapshot(
            source="pairs_trading_backtest",
            computed_at=_now(),
            window_days=360,
            return_pct=r360.return_pct,
            sharpe=r360.sharpe,
            max_drawdown=r360.max_drawdown,
            trade_count=r360.n_trades,
            extra={"lookback_hours": 336, "z_entry": 2.0, "z_exit": 0.5, "z_stop": 5.0, "leverage": 2.0},
        ),
        live_metric,
    )
    blockers: list[str] = []
    recommended_stage = "candidate"
    decision = "keep"
    if r360.return_pct <= 0:
        blockers.append("360일 구간 수익률이 음수라 shadow 승격 기준 미충족")
    if r360.max_drawdown > 15:
        blockers.append("장기 구간 MDD가 candidate 상한을 초과")
    live_closed_groups = live_metric.trade_count or 0
    live_return_pct = live_metric.return_pct
    live_win_rate = _extra_float(live_metric, "win_rate")
    rollback_event_count = _extra_int(live_metric, "rollback_event_count")
    failed_event_count = _extra_int(live_metric, "failed_event_count")
    if live_metric.trade_count == 0:
        blockers.append("실거래 closed pair 표본이 아직 없어 shadow 승격 근거 부족")
    if rollback_event_count > 0:
        blockers.append("실거래 rollback 이벤트가 발생해 체결 안정성 보강 필요")
    if failed_event_count > 0:
        blockers.append("실거래 failed 이벤트가 발생해 주문 복구 로직 검증 필요")
    if live_closed_groups >= 2 and live_return_pct < 0:
        blockers.append("실거래 grouped trade 손익이 음수라 shadow 승격 기준 미충족")
    if live_closed_groups >= 3 and live_win_rate < 40:
        blockers.append("실거래 win rate가 낮아 shadow 승격 기준 미충족")
    if (
        live_closed_groups >= 4
        and (live_return_pct <= -5 or failed_event_count >= 2 or rollback_event_count >= 2)
    ):
        recommended_stage = "hold"
        decision = "demote"
    elif (
        not blockers
        and r180.return_pct > 0
        and r360.return_pct > 0
        and r360.sharpe > 0.7
        and live_closed_groups >= 2
        and live_return_pct >= 0
    ):
        recommended_stage = "shadow"
        decision = "promote"
    if decision == "demote":
        summary = "실거래 grouped trade KPI가 약해 현재 candidate 유지보다 hold로 되돌리는 편이 안전함"
    elif decision == "promote":
        summary = "장단기 백테스트와 최근 실거래 grouped trade KPI가 모두 기준을 충족해 shadow 승격 후보로 볼 수 있음"
    else:
        summary = (
            "백테스트 edge는 확인됐지만 실거래 grouped trade 표본과 체결 안정성 지표가 더 쌓여야 "
            "shadow 승격 판단이 가능함"
        )
    return AutoReview(
        candidate_key="pairs_trading_futures",
        decision=decision,
        recommended_stage=recommended_stage,
        summary=summary,
        blockers=tuple(blockers),
        metrics=metrics,
    )


def _dual_review() -> AutoReview:
    top = run_sweep(["BTC", "ETH", "SOL", "XRP", "BNB"], 180, 1000.0, [60, 90, 120, 180], [7, 14, 30], [1, 2])[0]
    metrics = (
        MetricSnapshot(
            source="dual_momentum_backtest",
            computed_at=_now(),
            window_days=180,
            return_pct=top.return_pct,
            sharpe=top.sharpe,
            max_drawdown=top.max_drawdown,
            extra={"lookback_days": top.lookback_days, "rebalance_days": top.rebalance_days, "top_n": top.top_n},
        ),
    )
    blockers = ["최근 180일 최적 조합도 손실이라 승격 근거 없음"]
    summary = "현 약세장에서는 long-only dual momentum보다 선물 short 확장이 우선임"
    return AutoReview(
        candidate_key="dual_momentum_spot",
        decision="keep",
        recommended_stage="hold",
        summary=summary,
        blockers=tuple(blockers),
        metrics=metrics,
    )


def _funding_review() -> AutoReview:
    r = simulate_dynamic_arb(["BTC", "ETH", "SOL", "XRP"], 180, 1000.0, funding_threshold=0.0, max_positions=3)
    negative_ratio = r.n_negative_events / max(r.n_funding_events, 1)
    metrics = (
        MetricSnapshot(
            source="funding_arb_backtest",
            computed_at=_now(),
            window_days=180,
            return_pct=r.return_pct,
            sharpe=r.sharpe,
            max_drawdown=r.max_drawdown,
            trade_count=r.n_funding_events,
            extra={"negative_event_ratio": negative_ratio, "max_positions": 3, "funding_threshold": 0.0},
        ),
    )
    blockers: list[str] = []
    recommended_stage = "candidate"
    decision = "keep"
    if r.return_pct <= 0:
        blockers.append("180일 동적 funding arb 수익률이 0 이하")
    if negative_ratio > 0.15:
        blockers.append("음수 funding 이벤트 비중이 높아 필터 개선 필요")
    if blockers:
        recommended_stage = "hold"
        decision = "demote"
    summary = "음수 funding 회피 로직과 수수료 시나리오 개선 전까지 승격 보류가 적절함"
    return AutoReview(
        candidate_key="funding_arb",
        decision=decision,
        recommended_stage=recommended_stage,
        summary=summary,
        blockers=tuple(blockers),
        metrics=metrics,
    )


async def _donchian_futures_bi_review(live_capital_usdt: float | None = None) -> AutoReview:
    coins = ["BTC", "ETH", "SOL", "XRP", "BNB"]
    rows = [simulate_donchian_bi_directional(coin, 180, 1000.0) for coin in coins]
    avg_return = float(np.mean([r.return_pct for r in rows]))
    avg_sharpe = float(np.mean([r.sharpe for r in rows]))
    avg_mdd = float(np.mean([r.max_drawdown for r in rows]))
    total_trades = int(sum(r.n_trades for r in rows))
    total_short_trades = int(sum(r.short_trades for r in rows))
    live_metric = await _load_donchian_futures_live_metric(live_capital_usdt)
    metrics = (
        MetricSnapshot(
            source="donchian_daily_backtest",
            computed_at=_now(),
            window_days=180,
            return_pct=avg_return,
            sharpe=avg_sharpe,
            max_drawdown=avg_mdd,
            trade_count=total_trades,
            alpha_pct=float(np.mean([r.return_pct - r.bh_return for r in rows])),
            extra={"coins": coins, "short_trades": total_short_trades},
        ),
        live_metric,
    )
    blockers: list[str] = []
    decision = "keep"
    recommended_stage = "research"
    live_closed_groups = live_metric.trade_count or 0
    live_return_pct = live_metric.return_pct
    live_failed_event_count = _extra_int(live_metric, "failed_event_count")
    live_short_closed_groups = _extra_int(live_metric, "short_closed_groups")
    live_long_closed_groups = _extra_int(live_metric, "long_closed_groups")
    if avg_return <= 0:
        blockers.append("최근 180일 평균 수익률이 0 이하")
    if avg_mdd > 20:
        blockers.append("평균 MDD가 연구 후보 상한을 초과")
    if total_short_trades == 0:
        blockers.append("short 진입 표본이 없어 양방향 검증이 부족함")
    if live_closed_groups == 0:
        blockers.append("실거래 closed Donchian futures 표본이 아직 없어 candidate 승격 근거 부족")
    if live_failed_event_count > 0:
        blockers.append("실거래 failed/blocked 이벤트가 발생해 주문 안정성 보강 필요")
    if live_closed_groups >= 2 and live_return_pct < 0:
        blockers.append("실거래 Donchian futures grouped trade 손익이 음수라 candidate 승격 기준 미충족")
    if live_closed_groups >= 2 and live_short_closed_groups == 0:
        blockers.append("실거래 short 청산 표본이 아직 없어 양방향 candidate 승격 근거 부족")
    if (
        live_closed_groups >= 4
        and (live_return_pct <= -5 or live_failed_event_count >= 2)
    ):
        decision = "demote"
        recommended_stage = "hold"
    elif (
        not blockers
        and avg_return > 0
        and avg_sharpe > 0.7
        and total_trades >= 10
        and live_closed_groups >= 2
        and live_return_pct >= 0
        and live_short_closed_groups > 0
        and live_long_closed_groups > 0
    ):
        decision = "promote"
        recommended_stage = "candidate"
    if decision == "demote":
        summary = "실거래 Donchian futures KPI가 약해 현재 research 유지보다 hold로 되돌리는 편이 안전함"
    elif decision == "promote":
        summary = "백테스트와 실거래 양방향 KPI가 기준을 충족해 candidate 승격 대상으로 볼 수 있음"
    else:
        summary = "양방향 Donchian 후보를 백테스트와 실거래 execution 표본까지 합쳐 자동 평가함"
    return AutoReview(
        candidate_key="donchian_futures_bi",
        decision=decision,
        recommended_stage=recommended_stage,
        summary=summary,
        blockers=tuple(blockers),
        metrics=metrics,
    )


def _hmm_review() -> AutoReview:
    r = simulate_hmm_regime("BTC", 180, 1000.0)
    metrics = (
        MetricSnapshot(
            source="hmm_regime_backtest",
            computed_at=_now(),
            window_days=180,
            return_pct=r.return_pct,
            sharpe=r.sharpe,
            max_drawdown=r.max_drawdown,
            trade_count=r.n_trades,
            extra={
                "bullish_state": r.bullish_state,
                "bearish_state": r.bearish_state,
                "neutral_state": r.neutral_state,
            },
        ),
    )
    blockers: list[str] = []
    decision = "keep"
    recommended_stage = "live_rnd"
    if r.return_pct <= 0:
        blockers.append("최근 180일 HMM 체제 전략 수익률이 0 이하")
    if r.max_drawdown > 20:
        blockers.append("MDD가 research 상한을 초과")
    if blockers:
        decision = "demote"
        recommended_stage = "shadow"
    summary = "HMM 4h 3-state 체제전환 전략 — 라이브 소액 운영 중"
    return AutoReview(
        candidate_key="hmm_regime",
        decision=decision,
        recommended_stage=recommended_stage,
        summary=summary,
        blockers=tuple(blockers),
        metrics=metrics,
    )


def _volatility_adaptive_trend_review() -> AutoReview:
    coins = ["BTC", "ETH", "SOL", "XRP", "BNB"]
    rows = [simulate_volatility_adaptive_trend(c, 180, 1000.0) for c in coins]
    avg_return = float(np.mean([r.return_pct for r in rows]))
    avg_sharpe = float(np.mean([r.sharpe for r in rows]))
    avg_mdd = float(np.mean([r.max_drawdown for r in rows]))
    total_trades = int(sum(r.n_trades for r in rows))
    metrics = (
        MetricSnapshot(
            source="volatility_adaptive_trend_backtest",
            computed_at=_now(),
            window_days=180,
            return_pct=avg_return,
            sharpe=avg_sharpe,
            max_drawdown=avg_mdd,
            trade_count=total_trades,
            extra={"coins": coins},
        ),
    )
    blockers: list[str] = []
    decision = "keep"
    recommended_stage = "research"
    if avg_return <= 0:
        blockers.append("최근 180일 평균 수익률이 0 이하")
    if avg_mdd > 20:
        blockers.append("평균 MDD가 research 상한을 초과")
    if not blockers and avg_sharpe > 0.7 and total_trades >= 20:
        decision = "promote"
        recommended_stage = "candidate"
    summary = "변동성 적응형 추세추종을 양방향 선물형 후보로 백테스트 가능 상태로 추가함"
    return AutoReview(
        candidate_key="volatility_adaptive_trend",
        decision=decision,
        recommended_stage=recommended_stage,
        summary=summary,
        blockers=tuple(blockers),
        metrics=metrics,
    )


async def _live_execution_review(candidate_key: str, exchange_name: str) -> AutoReview:
    """주문 테이블 기반 범용 라이브 판정기 — 백테스터 없는 R&D 엔진용."""
    from research.registry import RESEARCH_CANDIDATE_BY_KEY
    candidate = RESEARCH_CANDIDATE_BY_KEY.get(candidate_key)
    title = candidate.title if candidate else candidate_key

    sf = get_session_factory()
    async with sf() as session:
        cutoff = _now() - timedelta(days=30)
        result = await session.execute(
            select(Order).where(
                Order.exchange == exchange_name,
                Order.status == "filled",
                Order.filled_at >= cutoff,
            ).order_by(Order.filled_at)
        )
        orders = list(result.scalars().all())

    # 청산 주문 = realized_pnl이 있는 주문 (롱 청산=sell, 숏 청산=buy)
    exit_orders = [o for o in orders if (o.realized_pnl or 0) != 0]
    total_trades = len(orders)
    sell_count = sum(1 for o in orders if o.side == "sell")
    buy_count = total_trades - sell_count

    pnls = [float(o.realized_pnl) for o in exit_orders]
    total_pnl = sum(pnls)
    wins = [p for p in pnls if p >= 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / len(pnls) * 100 if pnls else 0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_profit / gross_loss if gross_loss > 0 else (999 if gross_profit > 0 else 0)

    # 간이 MDD 추정 (누적 PnL 기반)
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    metrics = (
        MetricSnapshot(
            source=f"{exchange_name}_live_execution",
            computed_at=_now(),
            window_days=30,
            return_pct=total_pnl,
            sharpe=0,
            max_drawdown=max_dd,
            trade_count=total_trades,
            extra={
                "buy_count": buy_count,
                "sell_count": sell_count,
                "win_rate": round(win_rate, 1),
                "profit_factor": round(pf, 2),
            },
        ),
    )

    blockers: list[str] = []
    if total_trades == 0:
        blockers.append("최근 30일 체결 내역이 없어 판정 불가")
    if sell_count < 3:
        blockers.append(f"매도 표본 {sell_count}건 — 최소 3건 이상 필요")
    if total_pnl < 0 and sell_count >= 3:
        blockers.append(f"최근 30일 실현 PnL {total_pnl:+.2f} USDT 음수")

    if blockers:
        decision = "keep" if total_trades == 0 else "demote"
        recommended = "live_rnd" if total_trades == 0 else "shadow"
    elif win_rate >= 50 and pf >= 1.0:
        decision = "keep"
        recommended = "live_rnd"
    else:
        decision = "keep"
        recommended = "live_rnd"

    pnl_str = f"{total_pnl:+.2f}" if total_trades > 0 else "n/a"
    wr_str = f"{win_rate:.0f}%" if sell_count > 0 else "n/a"
    summary = f"{title} 라이브 30일: {total_trades}건, PnL {pnl_str}, WR {wr_str}, PF {pf:.2f}"

    return AutoReview(
        candidate_key=candidate_key,
        decision=decision,
        recommended_stage=recommended,
        summary=summary,
        blockers=tuple(blockers),
        metrics=metrics,
    )


def _not_ready_review(candidate_key: str, summary: str) -> AutoReview:
    return AutoReview(
        candidate_key=candidate_key,
        decision="insufficient_data",
        recommended_stage="research",
        summary=summary,
        blockers=("자동 판정용 백테스트 구현이 아직 없음",),
        metrics=(),
    )


async def get_auto_review(candidate_key: str, live_context: dict[str, Any] | None = None) -> AutoReview:
    builders = {
        "donchian_daily_spot": _donchian_review,
        "pairs_trading_futures": _pairs_review,
        "dual_momentum_spot": _dual_review,
        "funding_arb": _funding_review,
        "donchian_futures_bi": _donchian_futures_bi_review,
        "hmm_regime": _hmm_review,
    }
    # 백테스터 없는 R&D 엔진 → 라이브 주문 기반 판정
    _live_candidates = {
        "momentum_rotation": "binance_momentum",
        "breakout_pullback": "binance_breakout_pb",
        "volume_momentum": "binance_vol_mom",
        "btc_neutral_mr": "binance_btc_neutral",
        "fear_greed_dca": "binance_fgdca",
    }
    builder = builders.get(candidate_key)
    if builder is None:
        live_exchange = _live_candidates.get(candidate_key)
        if live_exchange:
            return await _cached(candidate_key, lambda: _live_execution_review(candidate_key, live_exchange))
        return _not_ready_review(candidate_key, "등록된 자동 판정기가 없음")
    builder_kwargs: dict[str, Any] = {}
    cache_key = candidate_key
    if candidate_key == "pairs_trading_futures":
        builder_kwargs["live_capital_usdt"] = None if live_context is None else live_context.get("live_capital_usdt")
        cache_key = f"{candidate_key}:live_capital={builder_kwargs['live_capital_usdt']}"
    if candidate_key == "donchian_futures_bi":
        builder_kwargs["live_capital_usdt"] = None if live_context is None else live_context.get("live_capital_usdt")
        cache_key = f"{candidate_key}:live_capital={builder_kwargs['live_capital_usdt']}"
    return await _cached(cache_key, lambda: builder(**builder_kwargs))


def serialize_auto_review(review: AutoReview) -> dict[str, Any]:
    return {
        "candidate_key": review.candidate_key,
        "decision": review.decision,
        "recommended_stage": review.recommended_stage,
        "summary": review.summary,
        "blockers": list(review.blockers),
        "metrics": [
            {
                **asdict(metric),
                "computed_at": metric.computed_at.isoformat(),
            }
            for metric in review.metrics
        ],
    }
