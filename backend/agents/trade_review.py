"""주기적 매매 회고 에이전트 — 거래 히스토리 분석 + 구체적 인사이트 생성."""
import structlog
from dataclasses import dataclass, field
from datetime import timedelta
from collections import defaultdict
from core.utils import utcnow
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.models import Trade, Order, Position
from config import get_config

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
    LLM 활성화 시 Claude API로 심층 분석.
    """

    def __init__(self, review_window_hours: int = 24, exchange_name: str = "bithumb"):
        self._review_window_hours = review_window_hours
        self._exchange_name = exchange_name
        self._last_review: TradeReview | None = None
        self._llm_client = None
        self._llm_config = None
        self._init_llm()

    async def review(self, session: AsyncSession) -> TradeReview:
        """최근 거래 분석 실행."""
        cutoff = utcnow() - timedelta(hours=self._review_window_hours)

        # 기간 내 모든 주문 조회 (체결된 것만)
        result = await session.execute(
            select(Order)
            .where(Order.filled_at >= cutoff, Order.status == "filled", Order.exchange == self._exchange_name)
            .order_by(Order.filled_at.asc())
        )
        orders = list(result.scalars().all())

        # 현재 포지션
        pos_result = await session.execute(
            select(Position).where(Position.quantity > 0, Position.exchange == self._exchange_name)
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
                "is_surge": getattr(p, "is_surge", False),
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

        # 매수/매도 매칭: 코인별 매수 기록 추적
        sell_pnls = []
        buy_records: dict[str, list[dict]] = defaultdict(list)
        total_fees = 0.0

        for o in orders:
            fee = o.fee or 0
            total_fees += fee
            if o.side == "buy":
                buy_records[o.symbol].append({
                    "price": o.executed_price or o.requested_price,
                    "qty": o.executed_quantity or o.requested_quantity,
                    "strategy": o.strategy_name,
                    "time": o.filled_at,
                    "fee": fee,
                })
            elif o.side == "sell":
                sell_price = o.executed_price or o.requested_price
                sell_qty = o.executed_quantity or o.requested_quantity
                coin_buys = buy_records.get(o.symbol, [])
                avg_buy = (
                    sum(b["price"] for b in coin_buys) / len(coin_buys)
                    if coin_buys
                    else sell_price
                )
                # 보유 시간 계산 (마지막 매수 ~ 매도)
                hold_minutes = None
                buy_strategy = None
                if coin_buys:
                    last_buy = coin_buys[-1]
                    buy_strategy = last_buy["strategy"]
                    if last_buy["time"] and o.filled_at:
                        hold_minutes = (o.filled_at - last_buy["time"]).total_seconds() / 60

                pnl = (sell_price - avg_buy) * sell_qty - fee
                pnl_pct = (sell_price - avg_buy) / avg_buy * 100 if avg_buy > 0 else 0
                sell_pnls.append({
                    "symbol": o.symbol,
                    "strategy": o.strategy_name,      # 매도 전략
                    "buy_strategy": buy_strategy,      # 매수 전략
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "sell_price": sell_price,
                    "avg_buy": avg_buy,
                    "sell_time": o.filled_at,
                    "hold_minutes": hold_minutes,
                    "fee": fee,
                    "reason": o.signal_reason or "",
                    "is_surge": buy_strategy == "rotation_surge",
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

        # 인사이트 + 추천 (규칙 기반)
        insights = self._generate_insights(
            win_rate, total_pnl, profit_factor, sell_pnls,
            dict(by_strategy), dict(by_symbol), open_positions,
            total_fees, buys, sells,
        )
        recommendations = self._generate_recommendations(
            win_rate, profit_factor, sell_pnls,
            dict(by_strategy), dict(by_symbol), total_sell, total_fees,
        )

        # LLM 인사이트 (활성화 시 규칙 기반 대체)
        if self._llm_client:
            llm_insights, llm_recs = await self._generate_llm_insights(
                sell_pnls, dict(by_strategy), dict(by_symbol), open_positions,
                total_pnl, win_rate, profit_factor, total_fees,
                len(buys), len(sells),
            )
            if llm_insights:
                insights = llm_insights
            if llm_recs:
                recommendations = llm_recs

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
        self, win_rate, total_pnl, pf, sell_pnls, by_strategy, by_symbol,
        positions, total_fees, buys, sells,
    ) -> list[str]:
        insights = []

        # ── 1. 전체 성과 요약 ──
        if win_rate >= 0.6:
            insights.append(f"승률 {win_rate:.0%} — 양호")
        elif win_rate > 0 and win_rate < 0.4:
            insights.append(f"승률 {win_rate:.0%} — 저조")

        if total_pnl > 0:
            insights.append(f"실현 수익 +{total_pnl:,.0f} KRW")
        elif total_pnl < 0:
            insights.append(f"실현 손실 {total_pnl:,.0f} KRW")

        if pf > 2.0:
            insights.append(f"Profit Factor {pf:.2f}x — 우수")
        elif 0 < pf < 1.0:
            insights.append(f"Profit Factor {pf:.2f}x — 손실>수익")

        # ── 2. 수수료 영향 분석 ──
        if total_fees > 0:
            if total_pnl != 0:
                fee_ratio = total_fees / abs(total_pnl) * 100 if abs(total_pnl) > 0 else 0
                if fee_ratio > 50:
                    insights.append(
                        f"수수료 {total_fees:,.0f} KRW (수익의 {fee_ratio:.0f}%) — 수수료 비중 과다"
                    )
                else:
                    insights.append(f"수수료 총 {total_fees:,.0f} KRW")
            else:
                insights.append(f"수수료 총 {total_fees:,.0f} KRW")

        # ── 3. 서지 vs 일반 거래 분리 분석 ──
        surge_trades = [t for t in sell_pnls if t["is_surge"]]
        normal_trades = [t for t in sell_pnls if not t["is_surge"]]

        if surge_trades and normal_trades:
            surge_pnl = sum(t["pnl"] for t in surge_trades)
            surge_wins = sum(1 for t in surge_trades if t["pnl"] > 0)
            surge_wr = surge_wins / len(surge_trades)
            normal_pnl = sum(t["pnl"] for t in normal_trades)
            normal_wins = sum(1 for t in normal_trades if t["pnl"] > 0)
            normal_wr = normal_wins / len(normal_trades)
            insights.append(
                f"서지 매매 {len(surge_trades)}건: 승률 {surge_wr:.0%}, {surge_pnl:+,.0f} KRW | "
                f"일반 매매 {len(normal_trades)}건: 승률 {normal_wr:.0%}, {normal_pnl:+,.0f} KRW"
            )
        elif surge_trades:
            surge_pnl = sum(t["pnl"] for t in surge_trades)
            surge_wins = sum(1 for t in surge_trades if t["pnl"] > 0)
            insights.append(
                f"서지 매매만 {len(surge_trades)}건: 승률 {surge_wins}/{len(surge_trades)}, "
                f"{surge_pnl:+,.0f} KRW"
            )

        # ── 4. 매도 유형별 분석 (SL/TP/trailing/전략/서지) ──
        sell_types: dict[str, list] = defaultdict(list)
        for t in sell_pnls:
            reason = t["reason"]
            if "SL" in reason or "손절" in reason:
                sell_types["SL"].append(t)
            elif "TP" in reason or "익절" in reason:
                sell_types["TP"].append(t)
            elif "Trailing" in reason or "trailing" in reason:
                sell_types["trailing"].append(t)
            elif "시간 초과" in reason or "보유 시간" in reason:
                sell_types["시간초과"].append(t)
            else:
                sell_types["전략"].append(t)

        if len(sell_types) > 1:
            type_parts = []
            for stype, trades in sorted(sell_types.items(), key=lambda x: len(x[1]), reverse=True):
                avg = sum(t["pnl"] for t in trades) / len(trades) if trades else 0
                type_parts.append(f"{stype} {len(trades)}건({avg:+,.0f}원)")
            insights.append(f"매도 유형: {', '.join(type_parts)}")

        # ── 5. 보유 시간 분석 ──
        hold_times = [t["hold_minutes"] for t in sell_pnls if t["hold_minutes"] is not None]
        if hold_times:
            avg_hold = sum(hold_times) / len(hold_times)
            min_hold = min(hold_times)
            max_hold = max(hold_times)
            # 초단기 매도 경고 (30분 미만)
            quick_sells = [t for t in sell_pnls
                           if t["hold_minutes"] is not None and t["hold_minutes"] < 30]
            if quick_sells:
                quick_pnl = sum(t["pnl"] for t in quick_sells)
                insights.append(
                    f"초단기 매도(<30분) {len(quick_sells)}건, "
                    f"합계 {quick_pnl:+,.0f} KRW — 진입 타이밍 점검 필요"
                )
            if avg_hold < 60:
                insights.append(f"평균 보유 {avg_hold:.0f}분 (최단 {min_hold:.0f}분, 최장 {max_hold:.0f}분)")
            else:
                insights.append(
                    f"평균 보유 {avg_hold/60:.1f}시간 "
                    f"(최단 {min_hold:.0f}분, 최장 {max_hold/60:.1f}시간)"
                )

        # ── 6. 전략별 구체적 성과 ──
        if by_strategy:
            best = max(by_strategy.items(), key=lambda x: x[1].get("total_pnl", 0))
            if best[1]["total_pnl"] > 0:
                insights.append(
                    f"최고 전략: {best[0]} (+{best[1]['total_pnl']:,.0f} KRW, "
                    f"승률 {best[1].get('win_rate', 0):.0%}, {best[1]['trades']}건)"
                )
            worst = min(by_strategy.items(), key=lambda x: x[1].get("total_pnl", 0))
            if worst[1]["total_pnl"] < 0 and worst[0] != best[0]:
                insights.append(
                    f"부진 전략: {worst[0]} ({worst[1]['total_pnl']:,.0f} KRW, "
                    f"승률 {worst[1].get('win_rate', 0):.0%}, {worst[1]['trades']}건)"
                )

        # ── 7. 코인별 구체적 성과 ──
        if by_symbol:
            for sym, stats in sorted(by_symbol.items(), key=lambda x: x[1]["total_pnl"]):
                if stats["trades"] >= 2 and stats["total_pnl"] < 0:
                    insights.append(
                        f"{sym}: {stats['trades']}건 매도 중 승률 {stats.get('win_rate',0):.0%}, "
                        f"합계 {stats['total_pnl']:+,.0f} KRW"
                    )

        # ── 8. 보유 포지션 개별 상태 ──
        if positions:
            total_unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)
            insights.append(
                f"보유 {len(positions)}개: 미실현 {total_unrealized:+,.0f} KRW"
            )
            # 큰 손실 포지션 경고
            for p in positions:
                pct = p.get("unrealized_pnl_pct", 0)
                if pct is not None and pct < -3:
                    tag = " [서지]" if p.get("is_surge") else ""
                    insights.append(
                        f"  {p['symbol']}{tag}: {pct:+.1f}% "
                        f"(매수가 {p['avg_price']:,.0f}원)"
                    )

        if not insights:
            insights.append("분석 가능한 데이터 부족")

        return insights

    def _generate_recommendations(
        self, win_rate, pf, sell_pnls, by_strategy, by_symbol, total_sells, total_fees,
    ) -> list[str]:
        recs = []

        if total_sells < 3:
            recs.append("거래 데이터 부족 — 충분한 샘플 축적 후 재평가 권장")
            return recs

        # ── 1. 코인별 연속 손실 체크 (구체적 코인 지목) ──
        coin_streaks: dict[str, int] = defaultdict(int)
        coin_current: dict[str, int] = defaultdict(int)
        for t in sell_pnls:
            sym = t["symbol"]
            if t["pnl"] <= 0:
                coin_current[sym] += 1
                coin_streaks[sym] = max(coin_streaks[sym], coin_current[sym])
            else:
                coin_current[sym] = 0

        for sym, streak in sorted(coin_streaks.items(), key=lambda x: x[1], reverse=True):
            if streak >= 2:
                coin_pnl = by_symbol.get(sym, {}).get("total_pnl", 0)
                recs.append(
                    f"{sym} {streak}연패 (합계 {coin_pnl:+,.0f} KRW) — 해당 코인 진입 재검토"
                )

        # ── 2. 전략-코인 교차 분석 ──
        strategy_coin: dict[str, dict[str, dict]] = defaultdict(
            lambda: defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
        )
        for t in sell_pnls:
            sc = strategy_coin[t["strategy"]][t["symbol"]]
            if t["pnl"] > 0:
                sc["wins"] += 1
            else:
                sc["losses"] += 1
            sc["pnl"] += t["pnl"]

        for strat, coins in strategy_coin.items():
            for coin, stats in coins.items():
                total = stats["wins"] + stats["losses"]
                if total >= 2 and stats["wins"] == 0:
                    recs.append(
                        f"'{strat}' → {coin}: {total}연패 ({stats['pnl']:+,.0f} KRW) — "
                        f"이 조합 비활성화 검토"
                    )

        # ── 3. SL 발동 빈도 분석 ──
        sl_trades = [t for t in sell_pnls if "SL" in t.get("reason", "") or "손절" in t.get("reason", "")]
        if sl_trades and total_sells > 0:
            sl_ratio = len(sl_trades) / total_sells
            if sl_ratio > 0.5:
                avg_sl_loss = sum(t["pnl_pct"] for t in sl_trades) / len(sl_trades)
                recs.append(
                    f"SL 발동 {len(sl_trades)}/{total_sells}건 ({sl_ratio:.0%}) — "
                    f"평균 {avg_sl_loss:+.1f}% 손실. 진입 타이밍 또는 SL 폭 조정 검토"
                )

        # ── 4. 서지 매매 별도 평가 ──
        surge_trades = [t for t in sell_pnls if t.get("is_surge")]
        if surge_trades:
            surge_wins = sum(1 for t in surge_trades if t["pnl"] > 0)
            surge_pnl = sum(t["pnl"] for t in surge_trades)
            if len(surge_trades) >= 2 and surge_wins == 0:
                recs.append(
                    f"서지 매매 {len(surge_trades)}건 전패 ({surge_pnl:+,.0f} KRW) — "
                    f"서지 진입 조건 강화 또는 surge_threshold 상향 검토"
                )
            # 초단기 서지 매도
            quick_surge = [t for t in surge_trades
                           if t.get("hold_minutes") is not None and t["hold_minutes"] < 60]
            if quick_surge:
                quick_pnl = sum(t["pnl"] for t in quick_surge)
                if quick_pnl < 0:
                    recs.append(
                        f"서지 매수 후 1시간 내 매도 {len(quick_surge)}건 ({quick_pnl:+,.0f} KRW) — "
                        f"서지 포지션 보호 시간 확인"
                    )

        # ── 5. 전체 성과 기반 일반 권고 ──
        if win_rate < 0.4 and not any("연패" in r for r in recs):
            recs.append(f"승률 {win_rate:.0%} — min_confidence 상향 검토")

        if pf < 1.0 and pf > 0 and not any("SL" in r for r in recs):
            recs.append(f"PF {pf:.2f}x — 손절폭 축소 또는 익절폭 확대 검토")

        # ── 6. 수수료 비율 경고 ──
        if total_fees > 0 and total_sells > 0:
            avg_fee = total_fees / total_sells
            gross = abs(sum(t["pnl"] for t in sell_pnls)) if sell_pnls else 0
            if gross > 0 and total_fees / gross > 0.5:
                recs.append(
                    f"수수료 {total_fees:,.0f} KRW가 거래 P&L의 {total_fees/gross*100:.0f}% — "
                    f"거래 빈도 축소 또는 포지션 크기 확대 검토"
                )

        # ── 7. 전체 연속 손실 체크 ──
        consecutive_losses = 0
        max_consecutive = 0
        max_streak_coins = []
        current_streak_coins = []
        for t in sell_pnls:
            if t["pnl"] <= 0:
                consecutive_losses += 1
                current_streak_coins.append(t["symbol"])
                if consecutive_losses > max_consecutive:
                    max_consecutive = consecutive_losses
                    max_streak_coins = list(current_streak_coins)
            else:
                consecutive_losses = 0
                current_streak_coins = []

        if max_consecutive >= 3:
            coins_str = ", ".join(dict.fromkeys(max_streak_coins))
            recs.append(
                f"최대 {max_consecutive}연패 ({coins_str}) — 시장 상태 재점검"
            )

        if not recs:
            recs.append("현재 성과 양호 — 기존 전략 유지 권장")

        return recs

    def _init_llm(self) -> None:
        """LLM 클라이언트 초기화. API 키 없으면 비활성."""
        try:
            config = get_config()
            self._llm_config = config.llm
            if self._llm_config.enabled and self._llm_config.api_key:
                import anthropic
                self._llm_client = anthropic.AsyncAnthropic(api_key=self._llm_config.api_key)
                logger.info("llm_trade_review_enabled", model=self._llm_config.model)
            else:
                logger.info("llm_trade_review_disabled")
        except Exception as e:
            logger.warning("llm_init_failed", error=str(e))
            self._llm_client = None

    async def _generate_llm_insights(
        self,
        sell_pnls: list[dict],
        by_strategy: dict,
        by_symbol: dict,
        open_positions: list[dict],
        total_pnl: float,
        win_rate: float,
        profit_factor: float,
        total_fees: float,
        buy_count: int,
        sell_count: int,
    ) -> tuple[list[str], list[str]]:
        """Claude API로 매매 회고 인사이트 생성."""
        if not self._llm_client or not self._llm_config:
            return [], []

        # 프롬프트 구성
        trades_summary = []
        for t in sell_pnls[-20:]:  # 최근 20건만
            trades_summary.append(
                f"  - {t['symbol']}: {t['pnl_pct']:+.1f}% ({t['pnl']:+,.0f}), "
                f"매도전략={t['strategy']}, 사유={t['reason'][:50]}"
            )
        trades_text = "\n".join(trades_summary) if trades_summary else "  매도 없음"

        strategy_text = []
        for name, stats in by_strategy.items():
            wr = stats.get("win_rate", 0)
            strategy_text.append(
                f"  - {name}: {stats['trades']}건, 승률 {wr:.0%}, PnL {stats['total_pnl']:+,.0f}"
            )
        strat_text = "\n".join(strategy_text) if strategy_text else "  전략별 데이터 없음"

        position_text = []
        for p in open_positions:
            pnl_pct = p.get("unrealized_pnl_pct", 0) or 0
            position_text.append(
                f"  - {p['symbol']}: 평단가 {p['avg_price']:,.0f}, 미실현 {pnl_pct:+.1f}%"
            )
        pos_text = "\n".join(position_text) if position_text else "  보유 포지션 없음"

        prompt = f"""당신은 암호화폐 자동매매 시스템의 트레이딩 분석가입니다.
아래 최근 24시간 매매 데이터를 분석하고, 한국어로 인사이트와 추천을 생성해주세요.

## 거래소: {self._exchange_name}

## 요약
- 매수: {buy_count}건, 매도: {sell_count}건
- 승률: {win_rate:.0%}, PF: {profit_factor:.2f}
- 실현 PnL: {total_pnl:+,.0f}
- 수수료: {total_fees:,.0f}

## 최근 매도 거래
{trades_text}

## 전략별 성과
{strat_text}

## 현재 보유 포지션
{pos_text}

---
다음 형식으로 응답하세요:

INSIGHTS:
- (3~5개 구체적 인사이트, 각각 한 줄)

RECOMMENDATIONS:
- (3개 실행 가능한 추천, 각각 한 줄)"""

        try:
            response = await self._llm_client.messages.create(
                model=self._llm_config.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )

            text = response.content[0].text
            insights = []
            recommendations = []
            section = None

            for line in text.split("\n"):
                line = line.strip()
                if "INSIGHTS:" in line.upper():
                    section = "insights"
                    continue
                elif "RECOMMENDATIONS:" in line.upper():
                    section = "recommendations"
                    continue

                if line.startswith("- ") or line.startswith("* "):
                    item = line[2:].strip()
                    if item:
                        if section == "insights":
                            insights.append(item)
                        elif section == "recommendations":
                            recommendations.append(item)

            if insights or recommendations:
                logger.info("llm_insights_generated",
                            insights=len(insights), recommendations=len(recommendations))
                return insights, recommendations

        except Exception as e:
            logger.warning("llm_insights_failed", error=str(e))

        return [], []

    @property
    def last_review(self) -> TradeReview | None:
        return self._last_review
