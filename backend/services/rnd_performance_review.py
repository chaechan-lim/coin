"""
R&D 성과 분석 서비스.

매일 1회 6개 R&D 전략의 성과를 분석하여 Discord 알림.
LLM 분석은 데이터 충분 시 활성화 (10건+ 거래).

분석 내용:
- 전략별: PnL, 승률, 거래 수, Sharpe 추정
- 전략 간: 상관관계, 최적 비중 제안
- 저성과: 폐기 추천
- 고성과: 증액 추천
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone, timedelta

import structlog
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from core.event_bus import emit_event
from core.models import Order
from db.session import get_session_factory

logger = structlog.get_logger(__name__)

RND_EXCHANGES = (
    "binance_donchian", "binance_donchian_futures", "binance_pairs",
    "binance_momentum", "binance_hmm", "binance_fgdca",
    "binance_breakout_pb", "binance_vol_mom", "binance_btc_neutral",
)

ENGINE_LABELS = {
    "binance_donchian": "Donchian Spot",
    "binance_donchian_futures": "Donchian Futures",
    "binance_pairs": "Pairs Trading",
    "binance_momentum": "Momentum Rotation",
    "binance_hmm": "HMM Regime",
    "binance_fgdca": "Fear & Greed DCA",
    "binance_breakout_pb": "Breakout-Pullback",
    "binance_vol_mom": "Volume Momentum",
    "binance_btc_neutral": "BTC-neutral MR",
}

MIN_TRADES_FOR_LLM = 10  # LLM 분석 최소 거래 수


async def run_rnd_performance_review():
    """R&D 전략 성과 분석 — 매일 1회 실행."""
    sf = get_session_factory()
    async with sf() as session:
        report_lines = ["📊 **R&D 일일 성과 리포트**\n"]
        total_pnl = 0.0
        total_trades = 0
        total_wins = 0
        engine_stats = {}

        for exchange in RND_EXCHANGES:
            label = ENGINE_LABELS.get(exchange, exchange)

            # 전체 거래 수
            result = await session.execute(
                select(func.count(Order.id))
                .where(Order.exchange == exchange, Order.status == "filled")
            )
            trade_count = result.scalar() or 0

            # 총 실현 PnL (매도/청산 거래)
            result = await session.execute(
                select(func.sum(Order.realized_pnl))
                .where(
                    Order.exchange == exchange,
                    Order.status == "filled",
                    Order.realized_pnl != 0,
                )
            )
            pnl = result.scalar() or 0.0

            # 승/패
            result = await session.execute(
                select(func.count(Order.id))
                .where(
                    Order.exchange == exchange,
                    Order.status == "filled",
                    Order.realized_pnl > 0,
                )
            )
            wins = result.scalar() or 0

            result = await session.execute(
                select(func.count(Order.id))
                .where(
                    Order.exchange == exchange,
                    Order.status == "filled",
                    Order.realized_pnl < 0,
                )
            )
            losses = result.scalar() or 0

            # 최근 24h 거래
            cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
            result = await session.execute(
                select(func.count(Order.id))
                .where(
                    Order.exchange == exchange,
                    Order.status == "filled",
                    Order.created_at >= cutoff_24h,
                )
            )
            trades_24h = result.scalar() or 0

            result = await session.execute(
                select(func.sum(Order.realized_pnl))
                .where(
                    Order.exchange == exchange,
                    Order.status == "filled",
                    Order.realized_pnl != 0,
                    Order.created_at >= cutoff_24h,
                )
            )
            pnl_24h = result.scalar() or 0.0

            closed = wins + losses
            win_rate = wins / closed * 100 if closed > 0 else 0

            engine_stats[exchange] = {
                "label": label,
                "trades": trade_count,
                "closed": closed,
                "wins": wins,
                "losses": losses,
                "pnl": pnl,
                "win_rate": win_rate,
                "trades_24h": trades_24h,
                "pnl_24h": pnl_24h,
            }

            total_pnl += pnl
            total_trades += trade_count
            total_wins += wins

            # 전략별 라인
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            status = f"{trade_count}건"
            if closed > 0:
                status += f" (승률 {win_rate:.0f}%)"
            report_lines.append(
                f"{pnl_emoji} **{label}**: PnL {pnl:+.2f} | 24h {pnl_24h:+.2f} | {status}"
            )

        # 요약
        report_lines.insert(1, f"총 PnL: **{total_pnl:+.2f} USDT** | 총 거래: {total_trades}건\n")

        # 추천 (단순 규칙)
        recommendations = []
        for ex, stats in engine_stats.items():
            if stats["closed"] >= 5 and stats["win_rate"] < 30:
                recommendations.append(f"⚠️ {stats['label']}: 승률 {stats['win_rate']:.0f}% — 검토 필요")
            if stats["closed"] >= 5 and stats["pnl"] > 0 and stats["win_rate"] > 60:
                recommendations.append(f"✅ {stats['label']}: 양호 (승률 {stats['win_rate']:.0f}%, PnL +{stats['pnl']:.2f})")
            if stats["trades"] == 0:
                recommendations.append(f"⏸ {stats['label']}: 거래 없음 — 시그널 대기 중")

        if recommendations:
            report_lines.append("\n**진단:**")
            report_lines.extend(recommendations)

        # LLM 분석 (데이터 충분 시)
        if total_trades >= MIN_TRADES_FOR_LLM:
            report_lines.append("\n_LLM 심층 분석은 추후 활성화 예정_")
        else:
            report_lines.append(f"\n_LLM 분석: 거래 {total_trades}/{MIN_TRADES_FOR_LLM}건 — 데이터 축적 중_")

        report = "\n".join(report_lines)

        # Discord 알림
        await emit_event("info", "strategy", report,
                         detail=f"총 PnL {total_pnl:+.2f} USDT, {total_trades}건 거래")

        logger.info("rnd_performance_review_complete",
                     total_pnl=round(total_pnl, 2),
                     total_trades=total_trades,
                     engines=len(engine_stats))

        return {
            "total_pnl": total_pnl,
            "total_trades": total_trades,
            "engines": engine_stats,
            "report": report,
        }
