"""성과 분석 에이전트 — 롤링 윈도우 성과 추적 + 전략/코인별 기여도 분석."""
import structlog
from dataclasses import dataclass, field
from datetime import timedelta
from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, and_

from core.models import Order, DailyPnL, Position
from core.utils import utcnow
from config import get_config

def _ensure_aware(dt):
    """timezone-naive datetime을 UTC aware로 변환."""
    if dt and dt.tzinfo is None:
        from datetime import timezone
        return dt.replace(tzinfo=timezone.utc)
    return dt

logger = structlog.get_logger(__name__)


@dataclass
class WindowMetrics:
    """롤링 윈도우 성과 지표."""
    period_days: int
    total_trades: int = 0       # 청산(매도) 건수
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    avg_hold_hours: float = 0.0


@dataclass
class StrategyMetrics:
    """전략별 성과."""
    name: str
    trades_7d: int = 0
    trades_30d: int = 0
    win_rate_7d: float = 0.0
    win_rate_30d: float = 0.0
    pnl_7d: float = 0.0
    pnl_30d: float = 0.0
    pnl_contribution_pct: float = 0.0   # 전체 PnL 대비 기여율
    trend: str = "stable"                # improving / stable / degrading


@dataclass
class CoinMetrics:
    """코인별 성과."""
    symbol: str
    trades_30d: int = 0
    win_rate_30d: float = 0.0
    pnl_30d: float = 0.0
    consecutive_losses: int = 0
    last_trade_pnl: float = 0.0


@dataclass
class PerformanceReport:
    """성과 분석 결과."""
    exchange: str
    generated_at: str
    windows: dict[str, WindowMetrics] = field(default_factory=dict)
    by_strategy: dict[str, StrategyMetrics] = field(default_factory=dict)
    by_symbol: dict[str, CoinMetrics] = field(default_factory=dict)
    degradation_alerts: list[str] = field(default_factory=list)
    insights: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


class PerformanceAnalyticsAgent:
    """롤링 윈도우 성과 추적 + 성과 저하 감지 + LLM 인사이트."""

    WINDOWS = [7, 14, 30]

    def __init__(self, exchange_name: str = "bithumb", extra_exchanges: list[str] | None = None):
        self._exchange_name = exchange_name
        self._exchange_list = extra_exchanges if extra_exchanges else [exchange_name]
        self._is_futures = "futures" in exchange_name
        self._currency = "USDT" if "binance" in exchange_name else "KRW"
        self._llm_client = None
        self._llm_config = None
        self._init_llm()

    def _init_llm(self) -> None:
        try:
            config = get_config()
            self._llm_config = config.llm
            if self._llm_config.enabled and self._llm_config.api_key:
                from services.llm import LLMClient
                self._llm_client = LLMClient(self._llm_config)
                logger.info("performance_analytics_llm_enabled", model=self._llm_config.model)
            else:
                logger.info("performance_analytics_llm_disabled")
        except Exception as e:
            logger.warning("performance_analytics_llm_init_failed", error=str(e))
            self._llm_client = None

    def _fmt(self, amount: float) -> str:
        if self._is_futures:
            return f"{amount:+,.2f} {self._currency}"
        return f"{amount:+,.0f} {self._currency}"

    async def analyze(self, session: AsyncSession) -> PerformanceReport:
        """성과 분석 실행."""
        now = utcnow()
        report = PerformanceReport(
            exchange=self._exchange_name,
            generated_at=now.isoformat(),
        )

        # 30일치 매도 주문 조회 (모든 윈도우에서 사용)
        cutoff_30d = now - timedelta(days=30)
        result = await session.execute(
            select(Order).where(
                Order.exchange.in_(self._exchange_list),
                Order.side == "sell",
                Order.status == "filled",
                Order.filled_at >= cutoff_30d,
            ).order_by(Order.filled_at)
        )
        all_sells = list(result.scalars().all())

        # 윈도우별 지표
        for days in self.WINDOWS:
            cutoff = now - timedelta(days=days)
            window_sells = [o for o in all_sells if o.filled_at and _ensure_aware(o.filled_at) >= cutoff]
            report.windows[f"{days}d"] = self._compute_window(window_sells, days)

        # 전략별 분석
        report.by_strategy = self._compute_strategy_metrics(all_sells, now)

        # 코인별 분석
        report.by_symbol = self._compute_coin_metrics(all_sells, now)

        # 성과 저하 감지
        report.degradation_alerts = self._detect_degradation(report)

        # LLM 인사이트
        if self._llm_client and all_sells:
            insights, recommendations = await self._generate_llm_insights(report)
            report.insights = insights
            report.recommendations = recommendations
        else:
            report.insights, report.recommendations = self._generate_rule_based_insights(report)

        return report

    def _compute_window(self, sells: list, days: int) -> WindowMetrics:
        """윈도우 지표 계산."""
        m = WindowMetrics(period_days=days)
        if not sells:
            return m

        gross_profit = 0.0
        gross_loss = 0.0
        hold_hours = []

        for o in sells:
            pnl = o.realized_pnl
            if pnl is None:
                continue
            m.total_trades += 1
            m.total_pnl += pnl
            if pnl >= 0:
                m.win_count += 1
                gross_profit += pnl
                if pnl > m.largest_win:
                    m.largest_win = pnl
            else:
                m.loss_count += 1
                gross_loss += abs(pnl)
                if pnl < m.largest_loss:
                    m.largest_loss = pnl

            # 보유 시간 (entry_price가 있으면 매수 시점 추정)
            if o.filled_at and o.created_at:
                hold_hours.append((o.filled_at - o.created_at).total_seconds() / 3600)

        if m.total_trades > 0:
            m.win_rate = m.win_count / m.total_trades
            m.avg_pnl = m.total_pnl / m.total_trades
        if gross_loss > 0:
            m.profit_factor = min(gross_profit / gross_loss, 99.0)
        elif gross_profit > 0:
            m.profit_factor = 99.0
        if hold_hours:
            m.avg_hold_hours = sum(hold_hours) / len(hold_hours)

        return m

    def _compute_strategy_metrics(self, all_sells: list, now) -> dict[str, StrategyMetrics]:
        """전략별 성과 계산."""
        cutoff_7d = now - timedelta(days=7)
        by_strategy: dict[str, StrategyMetrics] = {}

        # 30일 전체
        strat_30d: dict[str, list] = defaultdict(list)
        strat_7d: dict[str, list] = defaultdict(list)

        for o in all_sells:
            if o.realized_pnl is None:
                continue
            name = o.strategy_name or "unknown"
            strat_30d[name].append(o)
            if o.filled_at and _ensure_aware(o.filled_at) >= cutoff_7d:
                strat_7d[name].append(o)

        total_pnl_30d = sum(o.realized_pnl for o in all_sells if o.realized_pnl is not None)

        for name in set(list(strat_30d.keys()) + list(strat_7d.keys())):
            s30 = strat_30d.get(name, [])
            s7 = strat_7d.get(name, [])

            m = StrategyMetrics(name=name)
            m.trades_30d = len(s30)
            m.trades_7d = len(s7)

            wins_30 = sum(1 for o in s30 if o.realized_pnl >= 0)
            wins_7 = sum(1 for o in s7 if o.realized_pnl >= 0)
            m.win_rate_30d = wins_30 / len(s30) if s30 else 0.0
            m.win_rate_7d = wins_7 / len(s7) if s7 else 0.0
            m.pnl_30d = sum(o.realized_pnl for o in s30)
            m.pnl_7d = sum(o.realized_pnl for o in s7)

            if total_pnl_30d != 0:
                m.pnl_contribution_pct = m.pnl_30d / abs(total_pnl_30d) * 100

            # 추세 판단: 7일 vs 30일 승률 비교
            if m.trades_7d >= 3 and m.trades_30d >= 5:
                diff = m.win_rate_7d - m.win_rate_30d
                if diff > 0.10:
                    m.trend = "improving"
                elif diff < -0.10:
                    m.trend = "degrading"

            by_strategy[name] = m

        return by_strategy

    def _compute_coin_metrics(self, all_sells: list, now) -> dict[str, CoinMetrics]:
        """코인별 성과 계산."""
        by_coin: dict[str, list] = defaultdict(list)
        for o in all_sells:
            if o.realized_pnl is not None:
                by_coin[o.symbol].append(o)

        result: dict[str, CoinMetrics] = {}
        for symbol, orders in by_coin.items():
            m = CoinMetrics(symbol=symbol)
            m.trades_30d = len(orders)
            wins = sum(1 for o in orders if o.realized_pnl >= 0)
            m.win_rate_30d = wins / len(orders) if orders else 0.0
            m.pnl_30d = sum(o.realized_pnl for o in orders)
            m.last_trade_pnl = orders[-1].realized_pnl if orders else 0.0

            # 연속 손실 계산 (최근부터 역순)
            streak = 0
            for o in reversed(orders):
                if o.realized_pnl < 0:
                    streak += 1
                else:
                    break
            m.consecutive_losses = streak

            result[symbol] = m

        return result

    def _detect_degradation(self, report: PerformanceReport) -> list[str]:
        """성과 저하 감지."""
        alerts = []

        w7 = report.windows.get("7d")
        w30 = report.windows.get("30d")

        if w7 and w30 and w30.total_trades >= 5 and w7.total_trades >= 3:
            # 승률 급락
            if w30.win_rate - w7.win_rate > 0.15:
                alerts.append(
                    f"승률 급락: 30일 {w30.win_rate:.0%} → 7일 {w7.win_rate:.0%} "
                    f"({(w30.win_rate - w7.win_rate):.0%}p 하락)"
                )
            # PF 1.0 미만
            if w7.profit_factor < 1.0 and w7.total_trades >= 5:
                alerts.append(
                    f"7일 PF {w7.profit_factor:.2f} (1.0 미만 — 손실 구간)"
                )

        # 코인별 연속 손실
        for symbol, cm in report.by_symbol.items():
            if cm.consecutive_losses >= 4:
                coin = symbol.split("/")[0]
                alerts.append(
                    f"{coin} {cm.consecutive_losses}연패 (30일 PnL {self._fmt(cm.pnl_30d)})"
                )

        # 전략별 0% 승률
        for name, sm in report.by_strategy.items():
            if sm.trades_7d >= 3 and sm.win_rate_7d == 0:
                alerts.append(f"전략 {name}: 7일 {sm.trades_7d}건 전패")
            if sm.trend == "degrading":
                alerts.append(
                    f"전략 {name} 성과 저하: 승률 {sm.win_rate_30d:.0%}→{sm.win_rate_7d:.0%}"
                )

        return alerts

    def _generate_rule_based_insights(self, report: PerformanceReport) -> tuple[list[str], list[str]]:
        """LLM 없을 때 룰 기반 인사이트."""
        insights = []
        recommendations = []

        w7 = report.windows.get("7d")
        w30 = report.windows.get("30d")

        if w7 and w7.total_trades > 0:
            insights.append(f"7일: {w7.total_trades}건, 승률 {w7.win_rate:.0%}, PF {w7.profit_factor:.2f}, PnL {self._fmt(w7.total_pnl)}")
        if w30 and w30.total_trades > 0:
            insights.append(f"30일: {w30.total_trades}건, 승률 {w30.win_rate:.0%}, PF {w30.profit_factor:.2f}, PnL {self._fmt(w30.total_pnl)}")

        # 최고/최저 전략
        if report.by_strategy:
            best = max(report.by_strategy.values(), key=lambda s: s.pnl_30d)
            worst = min(report.by_strategy.values(), key=lambda s: s.pnl_30d)
            if best.pnl_30d > 0:
                insights.append(f"최고 전략: {best.name} (PnL {self._fmt(best.pnl_30d)}, 승률 {best.win_rate_30d:.0%})")
            if worst.pnl_30d < 0:
                insights.append(f"최저 전략: {worst.name} (PnL {self._fmt(worst.pnl_30d)}, 승률 {worst.win_rate_30d:.0%})")

        # 권장
        for symbol, cm in report.by_symbol.items():
            if cm.consecutive_losses >= 4:
                coin = symbol.split("/")[0]
                recommendations.append(f"{coin}: {cm.consecutive_losses}연패 — 일시 제외 검토")

        for name, sm in report.by_strategy.items():
            if sm.trend == "degrading":
                recommendations.append(f"{name}: 성과 하락 추세 — 가중치 하향 검토")

        if not recommendations:
            recommendations.append("현재 특별한 조정이 필요하지 않습니다.")

        return insights, recommendations

    async def _generate_llm_insights(self, report: PerformanceReport) -> tuple[list[str], list[str]]:
        """LLM으로 심층 인사이트 생성."""
        prompt = self._build_llm_prompt(report)

        try:
            response = await self._llm_client.generate(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=self._llm_config.max_tokens,
            )
            insights, recs = self._parse_llm_response(response.text or "")
            if insights or recs:
                return insights, recs
        except Exception as e:
            logger.warning("perf_llm_failed", error=str(e))

        return self._generate_rule_based_insights(report)

    def _build_llm_prompt(self, report: PerformanceReport) -> str:
        """LLM 프롬프트 구성."""
        exchange_type = "USDM 선물 (3x 레버리지, 롱/숏 양방향)" if self._is_futures else "현물"
        lines = [
            f"당신은 암호화폐 자동매매 시스템의 성과 분석 전문가입니다.",
            f"거래소: {self._exchange_name} ({exchange_type}), 통화: {self._currency}",
            "",
            "## 롤링 윈도우 성과",
        ]

        for key, w in report.windows.items():
            if w.total_trades > 0:
                lines.append(
                    f"- {key}: {w.total_trades}건, 승률 {w.win_rate:.0%}, "
                    f"PF {w.profit_factor:.2f}, PnL {self._fmt(w.total_pnl)}, "
                    f"최대 수익 {self._fmt(w.largest_win)}, 최대 손실 {self._fmt(w.largest_loss)}"
                )

        lines.append("\n## 전략별 성과 (30일)")
        for name, s in sorted(report.by_strategy.items(), key=lambda x: -x[1].pnl_30d):
            lines.append(
                f"- {name}: {s.trades_30d}건(7d:{s.trades_7d}), "
                f"승률 {s.win_rate_30d:.0%}(7d:{s.win_rate_7d:.0%}), "
                f"PnL {self._fmt(s.pnl_30d)}, 추세: {s.trend}"
            )

        lines.append("\n## 코인별 성과 (30일)")
        for symbol, c in sorted(report.by_symbol.items(), key=lambda x: -x[1].pnl_30d):
            coin = symbol.split("/")[0]
            streak_str = f", {c.consecutive_losses}연패 중" if c.consecutive_losses >= 3 else ""
            lines.append(
                f"- {coin}: {c.trades_30d}건, 승률 {c.win_rate_30d:.0%}, "
                f"PnL {self._fmt(c.pnl_30d)}{streak_str}"
            )

        if report.degradation_alerts:
            lines.append("\n## 감지된 성과 저하")
            for alert in report.degradation_alerts:
                lines.append(f"- ⚠️ {alert}")

        lines.extend([
            "",
            "## 분석 지침",
            "- 수치에 기반한 구체적 분석을 해주세요. 일반론 금지.",
            "- 전략이 수익을 내고 있다면 긍정적으로 평가하세요.",
            "- 성과 저하 원인을 추론하고, 구체적 개선안을 제시하세요.",
            "- 코인별 연속 손실이 있으면 제외/교체를 직접 권고하세요.",
            "- 전략 간 시너지/충돌이 있으면 분석해주세요.",
            "",
            "아래 형식으로 답변해주세요:",
            "INSIGHTS:",
            "- (3~5개 구체적 분석)",
            "",
            "RECOMMENDATIONS:",
            "- (3~5개 실행 가능한 권장사항)",
        ])

        return "\n".join(lines)

    def _parse_llm_response(self, text: str) -> tuple[list[str], list[str]]:
        """LLM 응답 파싱."""
        insights = []
        recommendations = []
        section = None

        for line in text.strip().split("\n"):
            line = line.strip()
            if line.upper().startswith("INSIGHTS"):
                section = "insights"
                continue
            elif line.upper().startswith("RECOMMENDATIONS"):
                section = "recommendations"
                continue

            if line.startswith("- ") or line.startswith("* "):
                content = line[2:].strip()
                if content:
                    if section == "insights":
                        insights.append(content)
                    elif section == "recommendations":
                        recommendations.append(content)

        return insights, recommendations
