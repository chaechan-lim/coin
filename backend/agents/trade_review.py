"""주기적 매매 회고 에이전트 — 거래 히스토리 분석 + 구체적 인사이트 생성."""
import structlog
from dataclasses import dataclass, field
from datetime import timedelta
from collections import defaultdict
from core.utils import utcnow
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.models import Trade, Order, Position, CapitalTransaction
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
        self._is_futures = "futures" in exchange_name
        self._currency = "USDT" if self._is_futures else "KRW"
        self._last_review: TradeReview | None = None
        self._llm_client = None
        self._llm_config = None
        self._init_llm()

    def _fmt(self, amount: float) -> str:
        """통화에 맞는 금액 포맷. USDT: 소수점 2자리, KRW: 정수."""
        if self._is_futures:
            return f"{amount:+,.2f} {self._currency}"
        return f"{amount:+,.0f} {self._currency}"

    def _get_analysis_instructions(self) -> str:
        """거래소별 LLM 분석 지시사항."""
        if self._is_futures:
            return (
                "- 이 시스템은 USDM 선물 자동매매입니다. 롱/숏 양방향 거래를 합니다.\n"
                "- 롱은 상승장, 숏은 하락장에서 수익을 내기 위한 **의도된 전략**입니다.\n"
                "- 숏이 손실을 봤다면 '숏을 비활성화하라'가 아니라, 진입 타이밍/조건을 분석해주세요.\n"
                "- 방향별 성과를 분리 분석하되, 각 방향의 시장 상황 적합성을 평가해주세요.\n"
                "- 레버리지 3x, 동적 SL(ATR 기반), 듀얼 타임프레임(4h+1h) 기반입니다.\n"
                "- 전략이 수익이 나고 있다면 긍정적으로 평가하고, 불필요한 변경을 권하지 마세요."
            )
        return (
            "- 이 시스템은 현물 거래소입니다. 매수/매도 기반 분석을 해주세요.\n"
            "- 비대칭 전략: 하락장에서 매수 차단, 상승장에서 적극 매수합니다.\n"
            "- 서지(surge) 매매: 급등 코인 자동 감지 후 매수합니다."
        )

    def _fmt_price(self, price: float) -> str:
        """가격 포맷."""
        if self._is_futures:
            return f"{price:,.2f}"
        return f"{price:,.0f}"

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
                "direction": getattr(p, "direction", "long"),
                "leverage": getattr(p, "leverage", 1),
                "liquidation_price": getattr(p, "liquidation_price", None),
                "margin_used": getattr(p, "margin_used", 0),
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

        # 선물 숏 여부 판별: direction=="short"인 주문이 있는 심볼
        short_symbols = {
            o.symbol for o in orders
            if getattr(o, "direction", None) == "short"
        }

        # 매수/매도 매칭: 코인별 기록 추적
        sell_pnls = []
        buy_records: dict[str, list[dict]] = defaultdict(list)
        sell_records: dict[str, list[dict]] = defaultdict(list)  # 숏 entry 추적
        total_fees = 0.0

        # 리뷰 윈도우 이전 진입 주문 조회용 헬퍼
        async def _find_entry_before_cutoff(symbol: str, side: str, before_time):
            """리뷰 기간 전에 체결된 가장 최근 진입 주문 조회."""
            result = await session.execute(
                select(Order)
                .where(
                    Order.symbol == symbol,
                    Order.side == side,
                    Order.status == "filled",
                    Order.exchange == self._exchange_name,
                    Order.filled_at < cutoff,
                )
                .order_by(Order.filled_at.desc())
                .limit(3)
            )
            rows = list(result.scalars().all())
            if not rows:
                return None, None, None
            avg_price = sum(
                (r.executed_price or r.requested_price) for r in rows
            ) / len(rows)
            last = rows[0]
            hold_min = None
            if last.filled_at and before_time:
                hold_min = (before_time - last.filled_at).total_seconds() / 60
            return avg_price, last.strategy_name, hold_min

        for o in orders:
            fee = o.fee or 0
            total_fees += fee
            price = o.executed_price or o.requested_price
            qty = o.executed_quantity or o.requested_quantity

            if o.symbol in short_symbols:
                # 숏 거래: sell = entry(진입), buy = exit(청산)
                if o.side == "sell":
                    sell_records[o.symbol].append({
                        "price": price,
                        "qty": qty,
                        "strategy": o.strategy_name,
                        "time": o.filled_at,
                        "fee": fee,
                    })
                elif o.side == "buy":
                    # 숏 청산 (buy = exit)
                    coin_sells = sell_records.get(o.symbol, [])
                    avg_entry = None
                    hold_minutes = None
                    entry_strategy = None
                    if coin_sells:
                        avg_entry = sum(s["price"] for s in coin_sells) / len(coin_sells)
                        last_entry = coin_sells[-1]
                        entry_strategy = last_entry["strategy"]
                        if last_entry["time"] and o.filled_at:
                            hold_minutes = (o.filled_at - last_entry["time"]).total_seconds() / 60
                    else:
                        # 리뷰 윈도우 이전 숏 진입 → DB에서 조회
                        avg_entry, entry_strategy, hold_minutes = await _find_entry_before_cutoff(
                            o.symbol, "sell", o.filled_at
                        )
                    if avg_entry is None:
                        avg_entry = price  # 최후 폴백

                    # 숏 P&L = (진입매도가 - 청산매수가) * qty - fees
                    pnl = (avg_entry - price) * qty - fee
                    pnl_pct = (avg_entry - price) / avg_entry * 100 if avg_entry > 0 else 0
                    sell_pnls.append({
                        "symbol": o.symbol,
                        "strategy": o.strategy_name,
                        "buy_strategy": entry_strategy,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "sell_price": avg_entry,   # entry price
                        "avg_buy": price,          # exit price
                        "sell_time": o.filled_at,
                        "hold_minutes": hold_minutes,
                        "fee": fee,
                        "reason": o.signal_reason or "",
                        "is_surge": entry_strategy == "rotation_surge",
                        "direction": "short",
                    })
            else:
                # 현물/롱: 기존 로직 (buy = entry, sell = exit)
                if o.side == "buy":
                    buy_records[o.symbol].append({
                        "price": price,
                        "qty": qty,
                        "strategy": o.strategy_name,
                        "time": o.filled_at,
                        "fee": fee,
                    })
                elif o.side == "sell":
                    coin_buys = buy_records.get(o.symbol, [])
                    avg_buy = None
                    hold_minutes = None
                    buy_strategy = None
                    if coin_buys:
                        avg_buy = sum(b["price"] for b in coin_buys) / len(coin_buys)
                        last_buy = coin_buys[-1]
                        buy_strategy = last_buy["strategy"]
                        if last_buy["time"] and o.filled_at:
                            hold_minutes = (o.filled_at - last_buy["time"]).total_seconds() / 60
                    else:
                        # 리뷰 윈도우 이전 진입 → DB에서 조회
                        avg_buy, buy_strategy, hold_minutes = await _find_entry_before_cutoff(
                            o.symbol, "buy", o.filled_at
                        )
                    if avg_buy is None:
                        avg_buy = price  # 최후 폴백

                    pnl = (price - avg_buy) * qty - fee
                    pnl_pct = (price - avg_buy) / avg_buy * 100 if avg_buy > 0 else 0
                    sell_pnls.append({
                        "symbol": o.symbol,
                        "strategy": o.strategy_name,
                        "buy_strategy": buy_strategy,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "sell_price": price,
                        "avg_buy": avg_buy,
                        "sell_time": o.filled_at,
                        "hold_minutes": hold_minutes,
                        "fee": fee,
                        "reason": o.signal_reason or "",
                        "is_surge": buy_strategy == "rotation_surge",
                        "direction": "long",
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
            99.0 if gross_profit > 0 else 0
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
            # 입출금 정보 조회
            capital_summary = await self._get_capital_summary(session)
            llm_insights, llm_recs = await self._generate_llm_insights(
                sell_pnls, dict(by_strategy), dict(by_symbol), open_positions,
                total_pnl, win_rate, profit_factor, total_fees,
                len(buys), len(sells), capital_summary,
            )
            if llm_insights:
                insights = llm_insights
            if llm_recs:
                recommendations = llm_recs

        dp = 2 if self._is_futures else 0  # USDT: 2dp, KRW: 0dp
        review = TradeReview(
            period_hours=self._review_window_hours,
            total_trades=len(orders),
            buy_count=len(buys),
            sell_count=len(sells),
            win_count=win_count,
            loss_count=loss_count,
            win_rate=round(win_rate, 4),
            total_realized_pnl=round(total_pnl, dp),
            avg_pnl_per_trade=round(total_pnl / total_sell, dp) if total_sell > 0 else 0,
            profit_factor=round(min(profit_factor, 99.0), 2),
            largest_win=round(largest_win, dp),
            largest_loss=round(largest_loss, dp),
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
        cur = self._currency

        # ── 0. 거래소 유형 표시 ──
        if self._is_futures:
            insights.append("📊 바이낸스 USDM 선물 (레버리지 거래)")

        # ── 1. 전체 성과 요약 ──
        if win_rate >= 0.6:
            insights.append(f"승률 {win_rate:.0%} — 양호")
        elif win_rate > 0 and win_rate < 0.4:
            insights.append(f"승률 {win_rate:.0%} — 저조")

        if total_pnl > 0:
            insights.append(f"실현 수익 {self._fmt(total_pnl)}")
        elif total_pnl < 0:
            insights.append(f"실현 손실 {self._fmt(total_pnl)}")

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
                        f"수수료 {self._fmt(total_fees)} (수익의 {fee_ratio:.0f}%) — 수수료 비중 과다"
                    )
                else:
                    insights.append(f"수수료 총 {self._fmt(total_fees)}")
            else:
                insights.append(f"수수료 총 {self._fmt(total_fees)}")

        # ── 3. 롱/숏 분리 분석 (선물) ──
        if self._is_futures:
            long_trades = [t for t in sell_pnls if t.get("direction") == "long"]
            short_trades = [t for t in sell_pnls if t.get("direction") == "short"]
            if long_trades and short_trades:
                long_pnl = sum(t["pnl"] for t in long_trades)
                long_wins = sum(1 for t in long_trades if t["pnl"] > 0)
                long_wr = long_wins / len(long_trades)
                short_pnl = sum(t["pnl"] for t in short_trades)
                short_wins = sum(1 for t in short_trades if t["pnl"] > 0)
                short_wr = short_wins / len(short_trades)
                insights.append(
                    f"롱 {len(long_trades)}건: 승률 {long_wr:.0%}, {self._fmt(long_pnl)} | "
                    f"숏 {len(short_trades)}건: 승률 {short_wr:.0%}, {self._fmt(short_pnl)}"
                )
            elif short_trades:
                short_pnl = sum(t["pnl"] for t in short_trades)
                insights.append(f"숏 전용 {len(short_trades)}건: {self._fmt(short_pnl)}")

        # ── 3b. 서지 vs 일반 거래 분리 분석 (현물) ──
        if not self._is_futures:
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
                    f"서지 매매 {len(surge_trades)}건: 승률 {surge_wr:.0%}, {self._fmt(surge_pnl)} | "
                    f"일반 매매 {len(normal_trades)}건: 승률 {normal_wr:.0%}, {self._fmt(normal_pnl)}"
                )
            elif surge_trades:
                surge_pnl = sum(t["pnl"] for t in surge_trades)
                surge_wins = sum(1 for t in surge_trades if t["pnl"] > 0)
                insights.append(
                    f"서지 매매만 {len(surge_trades)}건: 승률 {surge_wins}/{len(surge_trades)}, "
                    f"{self._fmt(surge_pnl)}"
                )

        # ── 4. 청산 유형별 분석 ──
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
            elif "청산가" in reason or "liquidation" in reason.lower():
                sell_types["긴급청산"].append(t)
            else:
                sell_types["전략"].append(t)

        if len(sell_types) > 1:
            type_parts = []
            for stype, trades in sorted(sell_types.items(), key=lambda x: len(x[1]), reverse=True):
                avg = sum(t["pnl"] for t in trades) / len(trades) if trades else 0
                type_parts.append(f"{stype} {len(trades)}건({self._fmt(avg)})")
            insights.append(f"청산 유형: {', '.join(type_parts)}")

        # ── 5. 보유 시간 분석 ──
        hold_times = [t["hold_minutes"] for t in sell_pnls if t["hold_minutes"] is not None]
        if hold_times:
            avg_hold = sum(hold_times) / len(hold_times)
            min_hold = min(hold_times)
            max_hold = max(hold_times)
            quick_sells = [t for t in sell_pnls
                           if t["hold_minutes"] is not None and t["hold_minutes"] < 30]
            if quick_sells:
                quick_pnl = sum(t["pnl"] for t in quick_sells)
                insights.append(
                    f"초단기 청산(<30분) {len(quick_sells)}건, "
                    f"합계 {self._fmt(quick_pnl)} — 진입 타이밍 점검 필요"
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
                    f"최고 전략: {best[0]} ({self._fmt(best[1]['total_pnl'])}, "
                    f"승률 {best[1].get('win_rate', 0):.0%}, {best[1]['trades']}건)"
                )
            worst = min(by_strategy.items(), key=lambda x: x[1].get("total_pnl", 0))
            if worst[1]["total_pnl"] < 0 and worst[0] != best[0]:
                insights.append(
                    f"부진 전략: {worst[0]} ({self._fmt(worst[1]['total_pnl'])}, "
                    f"승률 {worst[1].get('win_rate', 0):.0%}, {worst[1]['trades']}건)"
                )

        # ── 7. 코인별 구체적 성과 ──
        if by_symbol:
            for sym, stats in sorted(by_symbol.items(), key=lambda x: x[1]["total_pnl"]):
                if stats["trades"] >= 2 and stats["total_pnl"] < 0:
                    insights.append(
                        f"{sym}: {stats['trades']}건 중 승률 {stats.get('win_rate',0):.0%}, "
                        f"합계 {self._fmt(stats['total_pnl'])}"
                    )

        # ── 8. 보유 포지션 개별 상태 ──
        if positions:
            total_unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)
            insights.append(
                f"보유 {len(positions)}개: 미실현 {self._fmt(total_unrealized)}"
            )
            for p in positions:
                pct = p.get("unrealized_pnl_pct", 0)
                direction = p.get("direction", "long")
                leverage = p.get("leverage", 1)
                liq = p.get("liquidation_price")

                # 방향/레버리지 태그 (선물)
                dir_tag = ""
                if self._is_futures:
                    dir_label = "롱" if direction == "long" else "숏"
                    dir_tag = f" [{dir_label} {leverage}x]"

                if pct is not None and pct < -3:
                    surge_tag = " [서지]" if p.get("is_surge") else ""
                    liq_text = ""
                    if liq and self._is_futures:
                        price = p.get("avg_price", 0)
                        if price > 0:
                            liq_dist = abs(liq - price) / price * 100
                            liq_text = f", 청산가 거리 {liq_dist:.1f}%"
                    insights.append(
                        f"  {p['symbol']}{dir_tag}{surge_tag}: {pct:+.1f}% "
                        f"(진입가 {self._fmt_price(p['avg_price'])}{liq_text})"
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

        # ── 1. 코인별 연속 손실 체크 ──
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
                    f"{sym} {streak}연패 (합계 {self._fmt(coin_pnl)}) — 해당 코인 진입 재검토"
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
                        f"'{strat}' → {coin}: {total}연패 ({self._fmt(stats['pnl'])}) — "
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

        # ── 4. 선물 롱/숏 밸런스 분석 ──
        if self._is_futures:
            long_trades = [t for t in sell_pnls if t.get("direction") == "long"]
            short_trades = [t for t in sell_pnls if t.get("direction") == "short"]
            if long_trades and short_trades:
                long_wr = sum(1 for t in long_trades if t["pnl"] > 0) / len(long_trades)
                short_wr = sum(1 for t in short_trades if t["pnl"] > 0) / len(short_trades)
                if long_wr > 0 and short_wr == 0 and len(short_trades) >= 2:
                    recs.append(
                        f"숏 포지션 {len(short_trades)}건 전패 — 숏 진입 타이밍/조건 강화 필요"
                    )
                elif short_wr > 0 and long_wr == 0 and len(long_trades) >= 2:
                    recs.append(
                        f"롱 포지션 {len(long_trades)}건 전패 — 롱 진입 타이밍/조건 강화 필요"
                    )
        else:
            # 현물 서지 매매 평가
            surge_trades = [t for t in sell_pnls if t.get("is_surge")]
            if surge_trades:
                surge_wins = sum(1 for t in surge_trades if t["pnl"] > 0)
                surge_pnl = sum(t["pnl"] for t in surge_trades)
                if len(surge_trades) >= 2 and surge_wins == 0:
                    recs.append(
                        f"서지 매매 {len(surge_trades)}건 전패 ({self._fmt(surge_pnl)}) — "
                        f"서지 진입 조건 강화 또는 surge_threshold 상향 검토"
                    )
                quick_surge = [t for t in surge_trades
                               if t.get("hold_minutes") is not None and t["hold_minutes"] < 60]
                if quick_surge:
                    quick_pnl = sum(t["pnl"] for t in quick_surge)
                    if quick_pnl < 0:
                        recs.append(
                            f"서지 매수 후 1시간 내 매도 {len(quick_surge)}건 ({self._fmt(quick_pnl)}) — "
                            f"서지 포지션 보호 시간 확인"
                        )

        # ── 5. 전체 성과 기반 일반 권고 ──
        if win_rate < 0.4 and not any("연패" in r for r in recs):
            recs.append(f"승률 {win_rate:.0%} — min_confidence 상향 검토")

        if pf < 1.0 and pf > 0 and not any("SL" in r for r in recs):
            recs.append(f"PF {pf:.2f}x — 손절폭 축소 또는 익절폭 확대 검토")

        # ── 6. 수수료 비율 경고 ──
        if total_fees > 0 and total_sells > 0:
            gross = abs(sum(t["pnl"] for t in sell_pnls)) if sell_pnls else 0
            if gross > 0 and total_fees / gross > 0.5:
                recs.append(
                    f"수수료 {self._fmt(total_fees)}가 거래 P&L의 {total_fees/gross*100:.0f}% — "
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

    async def _get_capital_summary(self, session: AsyncSession) -> dict:
        """입출금 내역 요약 조회."""
        from sqlalchemy import func, case
        result = await session.execute(
            select(
                func.coalesce(func.sum(
                    case((CapitalTransaction.tx_type == "deposit", CapitalTransaction.amount), else_=0)
                ), 0),
                func.coalesce(func.sum(
                    case((CapitalTransaction.tx_type == "withdrawal", CapitalTransaction.amount), else_=0)
                ), 0),
            ).where(
                CapitalTransaction.exchange == self._exchange_name,
                CapitalTransaction.confirmed == True,  # noqa: E712
            )
        )
        deposits, withdrawals = result.one()

        # 최근 출금 내역 (24시간 이내)
        cutoff = utcnow() - timedelta(hours=self._review_window_hours)
        recent_result = await session.execute(
            select(CapitalTransaction)
            .where(
                CapitalTransaction.exchange == self._exchange_name,
                CapitalTransaction.confirmed == True,  # noqa: E712
                CapitalTransaction.created_at >= cutoff,
            )
            .order_by(CapitalTransaction.created_at.desc())
        )
        recent_txs = list(recent_result.scalars().all())

        return {
            "total_deposits": float(deposits),
            "total_withdrawals": float(withdrawals),
            "net_capital": float(deposits - withdrawals),
            "recent_transactions": [
                {
                    "type": tx.tx_type,
                    "amount": float(tx.amount),
                    "currency": tx.currency,
                }
                for tx in recent_txs
            ],
        }

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
        capital_summary: dict | None = None,
    ) -> tuple[list[str], list[str]]:
        """Claude API로 매매 회고 인사이트 생성."""
        if not self._llm_client or not self._llm_config:
            return [], []

        cur = self._currency

        # 거래소 유형 설명
        if self._is_futures:
            exchange_desc = (
                "바이낸스 USDM 선물 (레버리지 3x, 롱/숏 양방향)\n"
                "- 통화: USDT, 수수료: maker/taker 0.04%\n"
                "- 마진(margin): 실제 투입 금액, 노셔널: 마진×레버리지\n"
                "- 전략 체계: 4h(장기) + 1h(단기) 듀얼 타임프레임 분석\n"
                "- 방향 결정: 6개 전략(MA, RSI, MACD, Bollinger, Stochastic, OBV)의 "
                "가중 합산 시그널이 BUY → 롱, SELL → 숏 진입\n"
                "- 시장 상태별 포지션 사이징: crash 25%, downtrend 50%, 나머지 100%\n"
                "- 숏은 하락장에서 수익을 내기 위한 **의도된 전략**임\n"
                "- SL 8%, TP 16%, 트레일링 5/3.5% (레버리지 축소 적용)"
            )
        else:
            exchange_desc = (
                "빗썸 현물 (매수/매도만 가능, 롱 전용)\n"
                "- 통화: KRW\n"
                "- 수수료: 0.25%\n"
                "- 서지(surge) 매매: 급등 코인 자동 매수"
            )

        # 매도 거래 요약 (최근 20건)
        trades_summary = []
        for t in sell_pnls[-20:]:
            direction = t.get("direction", "long")
            dir_label = "숏" if direction == "short" else "롱"
            if self._is_futures:
                trades_summary.append(
                    f"  - {t['symbol']} [{dir_label}]: {t['pnl_pct']:+.1f}% "
                    f"({t['pnl']:+,.2f} {cur}), "
                    f"전략={t['strategy']}, 사유={t['reason'][:50]}"
                )
            else:
                trades_summary.append(
                    f"  - {t['symbol']}: {t['pnl_pct']:+.1f}% ({t['pnl']:+,.0f} {cur}), "
                    f"전략={t['strategy']}, 사유={t['reason'][:50]}"
                )
        trades_text = "\n".join(trades_summary) if trades_summary else "  청산 없음"

        # 전략별 성과
        strategy_text = []
        for name, stats in by_strategy.items():
            wr = stats.get("win_rate", 0)
            strategy_text.append(
                f"  - {name}: {stats['trades']}건, 승률 {wr:.0%}, PnL {self._fmt(stats['total_pnl'])}"
            )
        strat_text = "\n".join(strategy_text) if strategy_text else "  전략별 데이터 없음"

        # 포지션 요약
        position_text = []
        for p in open_positions:
            pnl_pct = p.get("unrealized_pnl_pct", 0) or 0
            if self._is_futures:
                direction = p.get("direction", "long")
                leverage = p.get("leverage", 1)
                margin = p.get("margin_used", 0)
                liq = p.get("liquidation_price")
                dir_label = "롱" if direction == "long" else "숏"
                liq_text = f", 청산가 {liq:,.2f}" if liq else ""
                position_text.append(
                    f"  - {p['symbol']} [{dir_label} {leverage}x]: "
                    f"진입가 {p['avg_price']:,.2f}, 마진 {margin:,.2f} {cur}, "
                    f"미실현 {pnl_pct:+.1f}%{liq_text}"
                )
            else:
                position_text.append(
                    f"  - {p['symbol']}: 평단가 {p['avg_price']:,.0f}, 미실현 {pnl_pct:+.1f}%"
                )
        pos_text = "\n".join(position_text) if position_text else "  보유 포지션 없음"

        # 롱/숏 요약 (선물)
        direction_summary = ""
        if self._is_futures:
            long_trades = [t for t in sell_pnls if t.get("direction") == "long"]
            short_trades = [t for t in sell_pnls if t.get("direction") == "short"]
            long_pnl = sum(t["pnl"] for t in long_trades)
            short_pnl = sum(t["pnl"] for t in short_trades)
            long_wr = (sum(1 for t in long_trades if t["pnl"] > 0) / len(long_trades) * 100) if long_trades else 0
            short_wr = (sum(1 for t in short_trades if t["pnl"] > 0) / len(short_trades) * 100) if short_trades else 0
            direction_summary = f"""
## 방향별 성과
- 롱: {len(long_trades)}건, 승률 {long_wr:.0f}%, PnL {long_pnl:+,.2f} {cur}
- 숏: {len(short_trades)}건, 승률 {short_wr:.0f}%, PnL {short_pnl:+,.2f} {cur}
"""

        pnl_fmt = f"{total_pnl:+,.2f} {cur}" if self._is_futures else f"{total_pnl:+,.0f} {cur}"
        fee_fmt = f"{total_fees:,.2f} {cur}" if self._is_futures else f"{total_fees:,.0f} {cur}"

        # 입출금 요약 (가용 시)
        capital_text = ""
        if capital_summary:
            dep = capital_summary["total_deposits"]
            wd = capital_summary["total_withdrawals"]
            net = capital_summary["net_capital"]
            recent = capital_summary["recent_transactions"]

            if dep > 0 or wd > 0:
                if self._is_futures:
                    capital_text = f"\n## 입출금 현황\n- 총 입금: {dep:,.2f} {cur}, 총 출금: {wd:,.2f} {cur}, 순 원금: {net:,.2f} {cur}"
                else:
                    capital_text = f"\n## 입출금 현황\n- 총 입금: {dep:,.0f} {cur}, 총 출금: {wd:,.0f} {cur}, 순 원금: {net:,.0f} {cur}"

                if recent:
                    recent_lines = []
                    for tx in recent:
                        tx_label = "입금" if tx["type"] == "deposit" else "출금"
                        if self._is_futures:
                            recent_lines.append(f"  - {tx_label}: {tx['amount']:,.2f} {tx['currency']}")
                        else:
                            recent_lines.append(f"  - {tx_label}: {tx['amount']:,.0f} {tx['currency']}")
                    capital_text += f"\n- 최근 {self._review_window_hours}시간 입출금:\n" + "\n".join(recent_lines)
                capital_text += "\n- **주의: 출금은 자본 회수이지 거래 손실이 아닙니다. PnL 분석에서 출금을 손실로 해석하지 마세요.**"

        prompt = f"""당신은 암호화폐 자동매매 시스템의 트레이딩 분석가입니다.
아래 최근 24시간 매매 데이터를 분석하고, 한국어로 인사이트와 추천을 생성해주세요.

## 거래소
{exchange_desc}

## 요약
- 진입: {buy_count}건, 청산: {sell_count}건
- 승률: {win_rate:.0%}, PF: {profit_factor:.2f}
- 실현 PnL: {pnl_fmt}
- 수수료: {fee_fmt}
{direction_summary}{capital_text}
## 최근 청산 거래
{trades_text}

## 전략별 성과
{strat_text}

## 현재 보유 포지션
{pos_text}

---
주의사항:
- 통화 단위는 {cur}입니다.
{self._get_analysis_instructions()}

다음 형식으로 응답하세요:

INSIGHTS:
- (3~5개 구체적 인사이트, 각각 한 줄)

RECOMMENDATIONS:
- (3개 실행 가능한 추천, 각각 한 줄)"""

        import asyncio

        models = [self._llm_config.model]
        if self._llm_config.fallback_model:
            models.append(self._llm_config.fallback_model)

        for model in models:
            for attempt in range(3):
                try:
                    response = await self._llm_client.messages.create(
                        model=model,
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
                                    model=model, insights=len(insights), recommendations=len(recommendations))
                        return insights, recommendations
                    break  # 응답은 받았지만 파싱 실패 — 다음 모델 시도

                except Exception as e:
                    wait = 2 ** attempt * 3  # 3s, 6s, 12s
                    logger.warning("llm_insights_failed", model=model, error=str(e),
                                   attempt=attempt + 1, retry_in=wait)
                    if attempt < 2:
                        await asyncio.sleep(wait)
            else:
                logger.warning("llm_model_exhausted", model=model)

        return [], []

    @property
    def last_review(self) -> TradeReview | None:
        return self._last_review
