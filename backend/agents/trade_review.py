"""주기적 매매 회고 에이전트 — 거래 히스토리 분석 + 인사이트 생성."""
import structlog
from dataclasses import dataclass, field
from datetime import timedelta
from collections import defaultdict
from core.utils import utcnow
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.models import Trade, Order, Position

logger = structlog.get_logger(__name__)


@dataclass
class TradeReview:
    """Trade review analysis result."""
    period_hours: int
    total_trades: int          # 매수+매도 건수
    buy_count: int
    sell_count: int
    win_count: int             # 수익 매도
    loss_count: int            # 손실 매도
    win_rate: float            # 0-1
    total_realized_pnl: float  # KRW
    avg_pnl_per_trade: float
    profit_factor: float       # gross_profit / gross_loss
    largest_win: float
    largest_loss: float
    by_strategy: dict          # 전략별 성과
    by_symbol: dict            # 코인별 성과
    open_positions: list       # 현재 보유 포지션
    insights: list[str]
    recommendations: list[str]


class TradeReviewAgent:
    """
    매매 히스토리를 주기적으로 분석하고 인사이트/추천을 생성.
    기본 1시간마다 실행, 최근 24시간 데이터 분석.
    """

    def __init__(self, review_window_hours: int = 24):
        self._review_window_hours = review_window_hours
        self._last_review: TradeReview | None = None

    async def review(self, session: AsyncSession) -> TradeReview:
        """최근 거래 분석 실행."""
        cutoff = utcnow() - timedelta(hours=self._review_window_hours)

        # 기간 내 모든 주문 조회 (체결된 것만)
        result = await session.execute(
            select(Order)
            .where(Order.filled_at >= cutoff, Order.status == "filled")
            .order_by(Order.filled_at.asc())
        )
        orders = list(result.scalars().all())

        # 현재 포지션
        pos_result = await session.execute(
            select(Position).where(Position.quantity > 0)
        )
        positions = list(pos_result.scalars().all())

        open_positions = [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "avg_price": p.average_buy_price,
                "invested": p.total_invested,
                "unrealized_pnl": p.unrealized_pnl,
                "unrealized_pnl_pct": p.unrealized_pnl_pct,
            }
            for p in positions
        ]

        if not orders:
            review = TradeReview(
                period_hours=self._review_window_hours,
                total_trades=0, buy_count=0, sell_count=0,
                win_count=0, loss_count=0, win_rate=0.0,
                total_realized_pnl=0.0, avg_pnl_per_trade=0.0,
                profit_factor=0.0, largest_win=0.0, largest_loss=0.0,
                by_strategy={}, by_symbol={},
                open_positions=open_positions,
                insights=["분석 기간 내 거래 없음"],
                recommendations=["거래 데이터 축적 후 재분석 필요"],
            )
            self._last_review = review
            return review

        # 매수/매도 분류
        buys = [o for o in orders if o.side == "buy"]
        sells = [o for o in orders if o.side == "sell"]

        # 매도 건별 손익 계산 — 매도가 vs 해당 코인 최근 매수 평균가
        sell_pnls = []
        buy_prices: dict[str, list[float]] = defaultdict(list)

        for o in orders:
            if o.side == "buy":
                buy_prices[o.symbol].append(o.executed_price or o.requested_price)
            elif o.side == "sell":
                sell_price = o.executed_price or o.requested_price
                avg_buy = (
                    sum(buy_prices[o.symbol]) / len(buy_prices[o.symbol])
                    if buy_prices[o.symbol]
                    else sell_price  # 기간 이전 매수
                )
                qty = o.executed_quantity or o.requested_quantity
                fee = o.fee or 0
                pnl = (sell_price - avg_buy) * qty - fee
                sell_pnls.append({
                    "symbol": o.symbol,
                    "strategy": o.strategy_name,
                    "pnl": pnl,
                    "pnl_pct": (sell_price - avg_buy) / avg_buy * 100 if avg_buy > 0 else 0,
                    "sell_price": sell_price,
                    "avg_buy": avg_buy,
                })

        # 승/패
        wins = [t for t in sell_pnls if t["pnl"] > 0]
        losses = [t for t in sell_pnls if t["pnl"] <= 0]
        win_count = len(wins)
        loss_count = len(losses)
        total_sell = len(sell_pnls)
        win_rate = win_count / total_sell if total_sell > 0 else 0.0

        total_pnl = sum(t["pnl"] for t in sell_pnls)
        gross_profit = sum(t["pnl"] for t in wins) if wins else 0
        gross_loss = abs(sum(t["pnl"] for t in losses)) if losses else 0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (
            float("inf") if gross_profit > 0 else 0
        )

        largest_win = max((t["pnl"] for t in sell_pnls), default=0)
        largest_loss = min((t["pnl"] for t in sell_pnls), default=0)

        # 전략별 성과
        by_strategy: dict[str, dict] = defaultdict(lambda: {
            "trades": 0, "wins": 0, "total_pnl": 0.0
        })
        for t in sell_pnls:
            s = by_strategy[t["strategy"]]
            s["trades"] += 1
            if t["pnl"] > 0:
                s["wins"] += 1
            s["total_pnl"] += t["pnl"]

        for name, s in by_strategy.items():
            s["win_rate"] = s["wins"] / s["trades"] if s["trades"] > 0 else 0

        # 코인별 성과
        by_symbol: dict[str, dict] = defaultdict(lambda: {
            "trades": 0, "wins": 0, "total_pnl": 0.0
        })
        for t in sell_pnls:
            s = by_symbol[t["symbol"]]
            s["trades"] += 1
            if t["pnl"] > 0:
                s["wins"] += 1
            s["total_pnl"] += t["pnl"]

        for sym, s in by_symbol.items():
            s["win_rate"] = s["wins"] / s["trades"] if s["trades"] > 0 else 0

        # 인사이트 + 추천
        insights = self._generate_insights(
            win_rate, total_pnl, profit_factor, sell_pnls,
            dict(by_strategy), dict(by_symbol), open_positions,
        )
        recommendations = self._generate_recommendations(
            win_rate, profit_factor, sell_pnls,
            dict(by_strategy), total_sell,
        )

        review = TradeReview(
            period_hours=self._review_window_hours,
            total_trades=len(orders),
            buy_count=len(buys),
            sell_count=len(sells),
            win_count=win_count,
            loss_count=loss_count,
            win_rate=round(win_rate, 4),
            total_realized_pnl=round(total_pnl, 0),
            avg_pnl_per_trade=round(total_pnl / total_sell, 0) if total_sell > 0 else 0,
            profit_factor=round(profit_factor, 2) if profit_factor != float("inf") else 999.0,
            largest_win=round(largest_win, 0),
            largest_loss=round(largest_loss, 0),
            by_strategy=dict(by_strategy),
            by_symbol=dict(by_symbol),
            open_positions=open_positions,
            insights=insights,
            recommendations=recommendations,
        )
        self._last_review = review

        logger.info(
            "trade_review_completed",
            trades=len(orders),
            sells=total_sell,
            win_rate=f"{win_rate:.0%}",
            pnl=f"{total_pnl:+,.0f}",
            pf=f"{profit_factor:.2f}",
        )
        return review

    def _generate_insights(
        self, win_rate, total_pnl, pf, sell_pnls, by_strategy, by_symbol, positions
    ) -> list[str]:
        insights = []

        # 전체 성과
        if win_rate >= 0.6:
            insights.append(f"높은 승률 {win_rate:.0%} — 전략 효과적")
        elif win_rate > 0 and win_rate < 0.4:
            insights.append(f"낮은 승률 {win_rate:.0%} — 진입 조건 재검토 필요")

        if total_pnl > 0:
            insights.append(f"실현 수익 +{total_pnl:,.0f} KRW")
        elif total_pnl < 0:
            insights.append(f"실현 손실 {total_pnl:,.0f} KRW")

        if pf > 2.0:
            insights.append(f"우수한 Profit Factor {pf:.2f}x")
        elif 0 < pf < 1.0:
            insights.append(f"Profit Factor {pf:.2f}x — 손실이 수익 초과")

        # 전략별
        if by_strategy:
            best = max(by_strategy.items(), key=lambda x: x[1].get("total_pnl", 0))
            if best[1]["total_pnl"] > 0:
                insights.append(
                    f"최고 전략: {best[0]} (+{best[1]['total_pnl']:,.0f} KRW, "
                    f"승률 {best[1].get('win_rate', 0):.0%})"
                )
            worst = min(by_strategy.items(), key=lambda x: x[1].get("total_pnl", 0))
            if worst[1]["total_pnl"] < 0 and worst[0] != best[0]:
                insights.append(
                    f"부진 전략: {worst[0]} ({worst[1]['total_pnl']:,.0f} KRW)"
                )

        # 보유 포지션
        if positions:
            total_unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)
            insights.append(
                f"보유 중 {len(positions)}개 포지션, "
                f"미실현 손익 {total_unrealized:+,.0f} KRW"
            )

        if not insights:
            insights.append("분석 가능한 데이터 부족")

        return insights

    def _generate_recommendations(
        self, win_rate, pf, sell_pnls, by_strategy, total_sells
    ) -> list[str]:
        recs = []

        if total_sells < 3:
            recs.append("거래 데이터 부족 — 충분한 샘플 축적 후 재평가 권장")
            return recs

        if win_rate < 0.4:
            recs.append("승률 개선: 진입 조건 강화 또는 min_confidence 상향 검토")

        if pf < 1.5 and pf > 0:
            recs.append("수익/손실 비율 개선: 손절폭 축소 또는 익절폭 확대 검토")

        # 특정 전략이 전체 손실의 주범인지
        if by_strategy:
            for name, stats in by_strategy.items():
                if stats["trades"] >= 2 and stats.get("win_rate", 0) == 0:
                    recs.append(f"전략 '{name}' 연패 중 — 가중치 하향 또는 비활성화 검토")

        # 연속 손실 체크
        consecutive_losses = 0
        max_consecutive = 0
        for t in sell_pnls:
            if t["pnl"] <= 0:
                consecutive_losses += 1
                max_consecutive = max(max_consecutive, consecutive_losses)
            else:
                consecutive_losses = 0

        if max_consecutive >= 3:
            recs.append(f"최대 {max_consecutive}연패 발생 — 시장 상태 재점검 필요")

        if not recs:
            recs.append("현재 성과 양호 — 기존 전략 유지 권장")

        return recs

    @property
    def last_review(self) -> TradeReview | None:
        return self._last_review
