"""전략 어드바이저 에이전트 — 전략 상관관계 분석 + 파라미터 민감도 + LLM 제안."""
import structlog
from dataclasses import dataclass, field
from datetime import timedelta
from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.models import Order, AgentAnalysisLog
from core.utils import utcnow
from config import get_config
from agents.performance_analytics import PerformanceReport

logger = structlog.get_logger(__name__)


@dataclass
class ParamSensitivity:
    """파라미터 민감도 분석 결과."""
    param_name: str
    current_value: float
    variants: dict[float, dict]    # value -> {win_rate, pf, pnl, trades}
    best_value: float = 0.0
    best_pf: float = 0.0
    improvement: str = ""


@dataclass
class StrategyAdvice:
    """전략 어드바이저 결과."""
    exchange: str
    generated_at: str
    # SL/TP/트레일링 효과 분석
    exit_analysis: dict = field(default_factory=dict)
    # 파라미터 민감도
    param_sensitivities: list[ParamSensitivity] = field(default_factory=list)
    # 방향별 분석 (선물)
    direction_analysis: dict = field(default_factory=dict)
    # LLM 종합 분석
    analysis_summary: str = ""
    suggestions: list[str] = field(default_factory=list)


class StrategyAdvisorAgent:
    """전략 상관 분석 + 파라미터 민감도 + LLM 종합 제안."""

    def __init__(self, exchange_name: str = "bithumb", extra_exchanges: list[str] | None = None):
        self._exchange_name = exchange_name
        self._exchange_list = [exchange_name] + (extra_exchanges or [])
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
                logger.info("strategy_advisor_llm_enabled", model=self._llm_config.model)
            else:
                logger.info("strategy_advisor_llm_disabled")
        except Exception as e:
            logger.warning("strategy_advisor_llm_init_failed", error=str(e))
            self._llm_client = None

    def _fmt(self, amount: float) -> str:
        if self._is_futures:
            return f"{amount:+,.2f} {self._currency}"
        return f"{amount:+,.0f} {self._currency}"

    async def advise(
        self,
        session: AsyncSession,
        performance_report: PerformanceReport | None = None,
    ) -> StrategyAdvice:
        """전략 분석 + 제안 생성."""
        now = utcnow()
        advice = StrategyAdvice(
            exchange=self._exchange_name,
            generated_at=now.isoformat(),
        )

        # 90일치 매도 주문
        cutoff = now - timedelta(days=90)
        result = await session.execute(
            select(Order).where(
                Order.exchange.in_(self._exchange_list),
                Order.side == "sell",
                Order.status == "filled",
                Order.filled_at >= cutoff,
            ).order_by(Order.filled_at)
        )
        sells = [o for o in result.scalars().all() if o.realized_pnl is not None]

        if not sells:
            advice.analysis_summary = "분석할 거래 데이터가 없습니다."
            return advice

        # 1. 청산 사유 분석
        advice.exit_analysis = self._analyze_exits(sells)

        # 2. 파라미터 민감도 (SL/TP)
        advice.param_sensitivities = self._analyze_param_sensitivity(sells)

        # 3. 방향별 분석 (선물)
        if self._is_futures:
            advice.direction_analysis = self._analyze_directions(sells)

        # 4. 최근 시장 분석 결과 조회
        market_context = await self._get_market_context(session)

        # 5. LLM 종합 분석
        if self._llm_client:
            summary, suggestions = await self._generate_llm_advice(
                advice, performance_report, market_context
            )
            advice.analysis_summary = summary
            advice.suggestions = suggestions
        else:
            advice.analysis_summary, advice.suggestions = self._generate_rule_based(
                advice, performance_report
            )

        return advice

    def _analyze_exits(self, sells: list) -> dict:
        """청산 사유별 분석."""
        exit_types = defaultdict(lambda: {"count": 0, "wins": 0, "total_pnl": 0.0})

        for o in sells:
            reason = o.signal_reason or ""
            if "손절" in reason or "stop" in reason.lower():
                exit_type = "stop_loss"
            elif "익절" in reason or "take_profit" in reason.lower():
                exit_type = "take_profit"
            elif "트레일" in reason or "trail" in reason.lower():
                exit_type = "trailing"
            elif "전략" in reason or "시그널" in reason:
                exit_type = "signal"
            else:
                exit_type = "other"

            exit_types[exit_type]["count"] += 1
            if o.realized_pnl >= 0:
                exit_types[exit_type]["wins"] += 1
            exit_types[exit_type]["total_pnl"] += o.realized_pnl

        result = {}
        for etype, stats in exit_types.items():
            stats["win_rate"] = stats["wins"] / stats["count"] if stats["count"] > 0 else 0
            result[etype] = stats

        return result

    def _analyze_param_sensitivity(self, sells: list) -> list[ParamSensitivity]:
        """SL/TP 파라미터 민감도 분석 (실제 거래 결과 기반)."""
        sensitivities = []

        # SL 분석: 손절로 끝난 거래의 손실률 분포
        sl_trades = [o for o in sells if o.realized_pnl_pct is not None
                     and o.signal_reason and ("손절" in o.signal_reason or "stop" in (o.signal_reason or "").lower())]
        if sl_trades:
            sl_losses = [abs(o.realized_pnl_pct) for o in sl_trades]
            avg_sl = sum(sl_losses) / len(sl_losses)

            # 현재 SL에서 걸린 횟수 vs 더 넓은 SL이었으면 구제됐을 횟수 추정
            # 손절 후 추가 하락 vs 반등 데이터는 없으므로, 통계적 분석만
            s = ParamSensitivity(
                param_name="stop_loss_pct",
                current_value=avg_sl,
                variants={},
            )
            s.improvement = (
                f"손절 {len(sl_trades)}회 발생, 평균 손실 {avg_sl:.1f}%. "
                f"전체 매도의 {len(sl_trades)/len(sells)*100:.0f}%가 손절"
            )
            sensitivities.append(s)

        # TP 분석
        tp_trades = [o for o in sells if o.realized_pnl_pct is not None
                     and o.signal_reason and ("익절" in o.signal_reason or "take_profit" in (o.signal_reason or "").lower())]
        if tp_trades:
            tp_profits = [o.realized_pnl_pct for o in tp_trades]
            avg_tp = sum(tp_profits) / len(tp_profits)

            s = ParamSensitivity(
                param_name="take_profit_pct",
                current_value=avg_tp,
                variants={},
            )
            s.improvement = (
                f"익절 {len(tp_trades)}회 발생, 평균 수익 {avg_tp:.1f}%. "
                f"전체 매도의 {len(tp_trades)/len(sells)*100:.0f}%가 익절"
            )
            sensitivities.append(s)

        # 트레일링 분석
        trail_trades = [o for o in sells if o.realized_pnl_pct is not None
                        and o.signal_reason and ("트레일" in o.signal_reason or "trail" in (o.signal_reason or "").lower())]
        if trail_trades:
            trail_profits = [o.realized_pnl_pct for o in trail_trades]
            avg_trail = sum(trail_profits) / len(trail_profits)
            trail_losses = sum(1 for p in trail_profits if p < 0)

            s = ParamSensitivity(
                param_name="trailing_stop",
                current_value=avg_trail,
                variants={},
            )
            s.improvement = (
                f"트레일링 {len(trail_trades)}회, 평균 수익 {avg_trail:.1f}%. "
                f"손실 종료 {trail_losses}회 ({trail_losses/len(trail_trades)*100:.0f}%)"
            )
            sensitivities.append(s)

        return sensitivities

    def _analyze_directions(self, sells: list) -> dict:
        """롱/숏 방향별 분석 (선물 전용)."""
        dirs = defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0, "strategies": defaultdict(int)})

        for o in sells:
            direction = o.direction or "long"
            dirs[direction]["count"] += 1
            if o.realized_pnl >= 0:
                dirs[direction]["wins"] += 1
            dirs[direction]["pnl"] += o.realized_pnl
            dirs[direction]["strategies"][o.strategy_name or "unknown"] += 1

        result = {}
        for d, stats in dirs.items():
            stats["win_rate"] = stats["wins"] / stats["count"] if stats["count"] > 0 else 0
            stats["strategies"] = dict(stats["strategies"])
            result[d] = stats

        return result

    async def _get_market_context(self, session: AsyncSession) -> str:
        """최근 시장 분석 결과 조회."""
        result = await session.execute(
            select(AgentAnalysisLog).where(
                AgentAnalysisLog.exchange == self._exchange_name,
                AgentAnalysisLog.agent_name == "market_analysis",
            ).order_by(AgentAnalysisLog.analyzed_at.desc()).limit(5)
        )
        logs = list(result.scalars().all())

        if not logs:
            return "시장 분석 데이터 없음"

        lines = []
        for log in logs:
            r = log.result or {}
            lines.append(
                f"  {log.analyzed_at.strftime('%m/%d %H:%M')}: "
                f"{r.get('state', '?')} (신뢰도 {r.get('confidence', 0):.0%})"
            )
        return "\n".join(lines)

    def _generate_rule_based(
        self, advice: StrategyAdvice, perf: PerformanceReport | None
    ) -> tuple[str, list[str]]:
        """LLM 없을 때 룰 기반 제안."""
        suggestions = []
        summary_parts = []

        # 청산 사유 분석
        exits = advice.exit_analysis
        if exits.get("stop_loss", {}).get("count", 0) > 0:
            sl = exits["stop_loss"]
            total = sum(e["count"] for e in exits.values())
            sl_pct = sl["count"] / total * 100
            if sl_pct > 50:
                suggestions.append(
                    f"손절 비율 {sl_pct:.0f}%로 높음 — 진입 조건 강화 또는 SL 확대 검토"
                )
            summary_parts.append(f"손절 {sl['count']}회({sl_pct:.0f}%)")

        if exits.get("trailing", {}).get("count", 0) > 0:
            trail = exits["trailing"]
            summary_parts.append(f"트레일링 {trail['count']}회(PnL {self._fmt(trail['total_pnl'])})")

        # 방향별 분석
        if advice.direction_analysis:
            for d, stats in advice.direction_analysis.items():
                if stats["count"] >= 5 and stats["win_rate"] < 0.25:
                    suggestions.append(
                        f"{d} 방향 승률 {stats['win_rate']:.0%} — 진입 신뢰도 상향 검토"
                    )

        # 성과 보고서 연동
        if perf:
            for name, sm in perf.by_strategy.items():
                if sm.trend == "degrading" and sm.trades_7d >= 3:
                    suggestions.append(f"{name} 성과 하락 중 — 가중치 하향 또는 일시 비활성 검토")

        if not suggestions:
            suggestions.append("현재 파라미터로 안정적 운영 중 — 급격한 변경 불필요")

        summary = f"90일 청산 분석: {', '.join(summary_parts)}" if summary_parts else "분석 완료"
        return summary, suggestions

    async def _generate_llm_advice(
        self,
        advice: StrategyAdvice,
        perf: PerformanceReport | None,
        market_context: str,
    ) -> tuple[str, list[str]]:
        """LLM 종합 분석."""
        prompt = self._build_llm_prompt(advice, perf, market_context)

        try:
            response = await self._llm_client.generate(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=self._llm_config.max_tokens,
            )
            summary, suggestions = self._parse_llm_response(response.text or "")
            if summary or suggestions:
                return summary, suggestions
        except Exception as e:
            logger.warning("advisor_llm_failed", error=str(e))

        return self._generate_rule_based(advice, perf)

    def _build_llm_prompt(
        self, advice: StrategyAdvice, perf: PerformanceReport | None, market_context: str
    ) -> str:
        exchange_type = "USDM 선물 (3x, 롱/숏)" if self._is_futures else "현물"
        lines = [
            f"당신은 암호화폐 자동매매 전략 어드바이저입니다.",
            f"거래소: {self._exchange_name} ({exchange_type}), 통화: {self._currency}",
            "",
            "## 최근 시장 상태 (최근 5회 분석)",
            market_context,
        ]

        # 성과 요약
        if perf:
            lines.append("\n## 성과 요약")
            for key, w in perf.windows.items():
                if w.total_trades > 0:
                    lines.append(
                        f"- {key}: {w.total_trades}건, 승률 {w.win_rate:.0%}, "
                        f"PF {w.profit_factor:.2f}, PnL {self._fmt(w.total_pnl)}"
                    )

            lines.append("\n## 전략별 성과")
            for name, s in sorted(perf.by_strategy.items(), key=lambda x: -x[1].pnl_30d):
                lines.append(
                    f"- {name}: 30d PnL {self._fmt(s.pnl_30d)} "
                    f"(승률 {s.win_rate_30d:.0%}→{s.win_rate_7d:.0%}, 추세: {s.trend})"
                )

            if perf.degradation_alerts:
                lines.append("\n## 성과 저하 경고")
                for alert in perf.degradation_alerts:
                    lines.append(f"- ⚠️ {alert}")

        # 청산 분석
        lines.append("\n## 청산 사유 분석 (90일)")
        for etype, stats in advice.exit_analysis.items():
            etype_kr = {"stop_loss": "손절", "take_profit": "익절", "trailing": "트레일링",
                        "signal": "시그널", "other": "기타"}.get(etype, etype)
            lines.append(
                f"- {etype_kr}: {stats['count']}회, 승률 {stats['win_rate']:.0%}, "
                f"PnL {self._fmt(stats['total_pnl'])}"
            )

        # 파라미터 민감도
        if advice.param_sensitivities:
            lines.append("\n## 파라미터 분석")
            for ps in advice.param_sensitivities:
                lines.append(f"- {ps.param_name}: {ps.improvement}")

        # 방향별 분석
        if advice.direction_analysis:
            lines.append("\n## 방향별 분석")
            for d, stats in advice.direction_analysis.items():
                lines.append(
                    f"- {d}: {stats['count']}건, 승률 {stats['win_rate']:.0%}, "
                    f"PnL {self._fmt(stats['pnl'])}"
                )

        lines.extend([
            "",
            "## 분석 지침",
            "- 데이터에 기반한 구체적 제안을 해주세요. 추상적 조언 금지.",
            "- 잘 되고 있는 것은 명시적으로 유지를 권고하세요.",
            "- 파라미터 변경 시 구체적 수치와 근거를 제시하세요.",
            "- 현재 설정: SL 8%, TP 16%, trailing 5%/3.5%, position 35%, conf 0.55 (선물)",
            "- 급격한 변경보다 점진적 조정을 선호하세요.",
            "",
            "아래 형식으로 답변해주세요:",
            "SUMMARY:",
            "(2~3문장 종합 분석)",
            "",
            "SUGGESTIONS:",
            "- (3~5개 구체적 제안, 각각 수치 포함)",
        ])

        return "\n".join(lines)

    def _parse_llm_response(self, text: str) -> tuple[str, list[str]]:
        """LLM 응답 파싱."""
        summary = ""
        suggestions = []
        section = None

        for line in text.strip().split("\n"):
            line = line.strip()
            if line.upper().startswith("SUMMARY"):
                section = "summary"
                continue
            elif line.upper().startswith("SUGGESTIONS") or line.upper().startswith("RECOMMENDATION"):
                section = "suggestions"
                continue

            if section == "summary" and line:
                summary += (" " if summary else "") + line
            elif section == "suggestions" and (line.startswith("- ") or line.startswith("* ")):
                content = line[2:].strip()
                if content:
                    suggestions.append(content)

        return summary, suggestions
