import structlog
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession

from core.enums import MarketState, RiskLevel
from core.models import AgentAnalysisLog
from core.event_bus import emit_event
from agents.market_analysis import MarketAnalysisAgent, MarketAnalysis, SPOT_WEIGHT_PROFILES, FUTURES_WEIGHT_PROFILES
from agents.risk_management import RiskManagementAgent, RiskAlert
from agents.trade_review import TradeReviewAgent, TradeReview
from agents.performance_analytics import PerformanceAnalyticsAgent, PerformanceReport
from agents.strategy_advisor import StrategyAdvisorAgent, StrategyAdvice
from strategies.combiner import SignalCombiner
from db.session import get_session_factory

logger = structlog.get_logger(__name__)

# COIN-53: MarketAnalysisAgent 비활성화.
# 에이전트 판정이 실제 매매에 미사용 (현물=엔진 자체, 선물=RegimeDetector).
# False로 설정하면 스케줄 실행 중단, 코드는 보존됨.
MARKET_ANALYSIS_ENABLED = False


class AgentCoordinator:
    """
    Coordinates AI agents and feeds their outputs into the trading engine.
    - Market Analysis → updates strategy weights in SignalCombiner
    - Risk Management → sets flags that suppress/boost signals
    """

    def __init__(
        self,
        market_agent: MarketAnalysisAgent,
        risk_agent: RiskManagementAgent,
        combiner: SignalCombiner,
        trade_review_agent: TradeReviewAgent | None = None,
        exchange_name: str = "bithumb",
        performance_agent: PerformanceAnalyticsAgent | None = None,
        strategy_advisor: StrategyAdvisorAgent | None = None,
    ):
        self._market_agent = market_agent
        self._risk_agent = risk_agent
        self._combiner = combiner
        self._trade_review_agent = trade_review_agent
        self._performance_agent = performance_agent
        self._strategy_advisor = strategy_advisor
        self._exchange_name = exchange_name
        self._engine = None  # Set after engine creation
        self._weight_profiles = FUTURES_WEIGHT_PROFILES if "futures" in exchange_name else SPOT_WEIGHT_PROFILES
        self._last_market_analysis: MarketAnalysis | None = None
        self._last_risk_alerts: list[RiskAlert] = []
        self._last_trade_review: TradeReview | None = None
        self._last_performance_report: PerformanceReport | None = None
        self._last_strategy_advice: StrategyAdvice | None = None

    def set_engine(self, engine) -> None:
        """Set reference to trading engine for control operations."""
        self._engine = engine

    async def run_market_analysis(self) -> MarketAnalysis | None:
        """Run market analysis (정보 제공용 — 가중치는 엔진 _maybe_update_market_state()가 관리).

        COIN-53: MARKET_ANALYSIS_ENABLED=False일 때 즉시 반환 (LLM/API 비용 절감).
        캐시된 _last_market_analysis는 보존 — API는 마지막 캐시 또는 DB 폴백 반환.
        """
        if not MARKET_ANALYSIS_ENABLED:
            logger.debug("market_analysis_disabled", exchange=self._exchange_name)
            return self._last_market_analysis

        analysis = await self._market_agent.analyze()

        # 엔진의 공식 시장 상태로 동기화 (엔진 = 4h 기준, 에이전트 = 1h 기준 → 불일치 방지)
        if self._engine and hasattr(self._engine, '_market_state'):
            engine_state_str = self._engine._market_state
            try:
                engine_state = MarketState(engine_state_str)
                if engine_state != analysis.state:
                    logger.info(
                        "agent_state_synced_to_engine",
                        agent_state=analysis.state.value,
                        engine_state=engine_state_str,
                    )
                    analysis.state = engine_state
                    analysis.recommended_weights = self._weight_profiles.get(
                        engine_state, analysis.recommended_weights
                    )
            except ValueError:
                pass  # 엔진 상태가 enum에 없으면 에이전트 상태 유지

        self._last_market_analysis = analysis

        # 에이전트 분석 결과는 DB 저장 + 대시보드 표시용

        # Persist analysis
        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                log = AgentAnalysisLog(
                    exchange=self._exchange_name,
                    agent_name="market_analysis",
                    analysis_type="market_state",
                    result={
                        "state": analysis.state.value,
                        "confidence": analysis.confidence,
                        "volatility": analysis.volatility_level,
                        "reasoning": analysis.reasoning,
                        "indicators": analysis.indicators,
                    },
                    recommended_weights=analysis.recommended_weights,
                )
                session.add(log)
                await session.commit()
        except Exception as e:
            logger.error("failed_to_persist_market_analysis", error=str(e))

        logger.info(
            "market_analyzed",
            state=analysis.state.value,
            confidence=analysis.confidence,
        )
        await emit_event(
            "info", "strategy",
            f"시장 분석: {analysis.state.value} (신뢰도 {analysis.confidence:.0%})",
            metadata={"state": analysis.state.value, "confidence": analysis.confidence,
                      "exchange": self._exchange_name},
        )
        return analysis

    async def run_risk_evaluation(self, cash_balance: float) -> list[RiskAlert]:
        """Run risk evaluation and apply constraints to engine."""
        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                alerts = await self._risk_agent.evaluate(session, cash_balance)
                self._last_risk_alerts = alerts

                # Apply risk actions to engine
                if self._engine:
                    coins_to_pause = []
                    coins_to_suppress = []

                    for alert in alerts:
                        # reduce_buying = 경고만 (로그/대시보드 표시), 매수 차단 안 함
                        if alert.action == "reduce_buying":
                            logger.info("risk_reduce_buying",
                                        message=alert.message,
                                        drawdown=alert.details.get("drawdown_pct"))
                            continue

                        if alert.level == RiskLevel.CRITICAL:
                            if alert.action in ("stop_buying", "emergency_sell"):
                                if alert.affected_coins:
                                    coins_to_pause.extend(alert.affected_coins)
                                else:
                                    # Critical with no specific coins = pause all
                                    coins_to_pause.extend(
                                        self._engine._config.trading.tracked_coins
                                    )
                            elif alert.affected_coins:
                                coins_to_pause.extend(alert.affected_coins)
                        elif alert.level == RiskLevel.WARNING:
                            if alert.affected_coins:
                                coins_to_suppress.extend(alert.affected_coins)

                    if coins_to_pause:
                        self._engine.pause_buying(list(set(coins_to_pause)))
                    if coins_to_suppress:
                        self._engine.suppress_buys(list(set(coins_to_suppress)))

                # 리스크 CRITICAL 경고를 시스템 로그에 발행
                for alert in alerts:
                    if alert.level == RiskLevel.CRITICAL:
                        await emit_event(
                            "warning", "risk",
                            f"리스크 경고: {alert.message}",
                            metadata={"action": alert.action, "exchange": self._exchange_name},
                        )

                    # Resume coins with no alerts
                    all_alerted = set(coins_to_pause + coins_to_suppress)
                    previously_alerted = self._engine._paused_coins | self._engine._suppressed_coins
                    to_resume = previously_alerted - all_alerted
                    if to_resume:
                        self._engine.resume_buying(list(to_resume))

                # Persist alerts
                for alert in alerts:
                    log = AgentAnalysisLog(
                        exchange=self._exchange_name,
                        agent_name="risk_management",
                        analysis_type="risk_alert",
                        result={
                            "level": alert.level.value,
                            "message": alert.message,
                            "action": alert.action,
                            "affected_coins": alert.affected_coins,
                            "details": alert.details,
                        },
                        risk_level=alert.level.value,
                    )
                    session.add(log)

                await session.commit()

        except Exception as e:
            logger.error("risk_evaluation_failed", error=str(e))

        return self._last_risk_alerts

    async def run_trade_review(self) -> TradeReview | None:
        """주기적 매매 회고 실행."""
        if not self._trade_review_agent:
            return None

        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                review = await self._trade_review_agent.review(session)
                self._last_trade_review = review

                # DB 저장
                log = AgentAnalysisLog(
                    exchange=self._exchange_name,
                    agent_name="trade_review",
                    analysis_type="trade_performance",
                    result={
                        "period_hours": review.period_hours,
                        "total_trades": review.total_trades,
                        "buy_count": review.buy_count,
                        "sell_count": review.sell_count,
                        "win_count": review.win_count,
                        "loss_count": review.loss_count,
                        "win_rate": review.win_rate,
                        "total_realized_pnl": review.total_realized_pnl,
                        "avg_pnl_per_trade": review.avg_pnl_per_trade,
                        "profit_factor": review.profit_factor,
                        "largest_win": review.largest_win,
                        "largest_loss": review.largest_loss,
                        "by_strategy": review.by_strategy,
                        "by_symbol": review.by_symbol,
                        "open_positions": review.open_positions,
                        "insights": review.insights,
                        "recommendations": review.recommendations,
                        "analyzed_at": review.analyzed_at,
                    },
                )
                session.add(log)
                await session.commit()

                await emit_event(
                    "info", "strategy",
                    f"매매 회고: {review.total_trades}건, 승률 {review.win_rate:.0%}, PnL {review.total_realized_pnl:+,.2f}",
                    metadata={
                        "review_kind": "trade_review",
                        "total_trades": review.total_trades,
                        "buy_count": review.buy_count,
                        "sell_count": review.sell_count,
                        "win_count": review.win_count,
                        "loss_count": review.loss_count,
                        "win_rate": review.win_rate,
                        "pnl": review.total_realized_pnl,
                        "profit_factor": review.profit_factor,
                        "largest_win": review.largest_win,
                        "largest_loss": review.largest_loss,
                        "by_strategy": review.by_strategy,
                        "by_symbol": review.by_symbol,
                        "open_positions": review.open_positions,
                        "insights": review.insights,
                        "recommendations": review.recommendations,
                        "exchange": self._exchange_name,
                    },
                )

                return review
        except Exception as e:
            logger.error("trade_review_failed", error=str(e))
            return self._last_trade_review

    async def run_performance_analysis(self) -> PerformanceReport | None:
        """일일 성과 분석 실행."""
        if not self._performance_agent:
            return None
        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                report = await self._performance_agent.analyze(session)
                self._last_performance_report = report

                log = AgentAnalysisLog(
                    exchange=self._exchange_name,
                    agent_name="performance_analytics",
                    analysis_type="performance_report",
                    result={
                        "windows": {k: vars(v) for k, v in report.windows.items()},
                        "by_strategy": {k: vars(v) for k, v in report.by_strategy.items()},
                        "by_symbol": {k: vars(v) for k, v in report.by_symbol.items()},
                        "degradation_alerts": report.degradation_alerts,
                        "insights": report.insights,
                        "recommendations": report.recommendations,
                    },
                )
                session.add(log)
                await session.commit()

                alert_str = f", 경고 {len(report.degradation_alerts)}건" if report.degradation_alerts else ""
                w30 = report.windows.get("30d")
                pnl_str = ""
                if w30 and w30.total_trades > 0:
                    pnl_str = f", 30일 PF {w30.profit_factor:.2f}"
                await emit_event(
                    "info", "strategy",
                    f"성과 분석 완료{pnl_str}{alert_str}",
                    metadata={"exchange": self._exchange_name},
                )
                return report
        except Exception as e:
            logger.error("performance_analysis_failed", error=str(e))
            return self._last_performance_report

    async def run_strategy_advice(self) -> StrategyAdvice | None:
        """전략 어드바이저 실행."""
        if not self._strategy_advisor:
            return None
        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                advice = await self._strategy_advisor.advise(
                    session, self._last_performance_report
                )
                self._last_strategy_advice = advice

                log = AgentAnalysisLog(
                    exchange=self._exchange_name,
                    agent_name="strategy_advisor",
                    analysis_type="strategy_advice",
                    result={
                        "exit_analysis": advice.exit_analysis,
                        "param_sensitivities": [vars(p) for p in advice.param_sensitivities],
                        "direction_analysis": advice.direction_analysis,
                        "analysis_summary": advice.analysis_summary,
                        "suggestions": advice.suggestions,
                    },
                )
                session.add(log)
                await session.commit()

                n_suggestions = len(advice.suggestions)
                await emit_event(
                    "info", "strategy",
                    f"전략 어드바이저: {n_suggestions}개 제안",
                    metadata={"exchange": self._exchange_name},
                )
                return advice
        except Exception as e:
            logger.error("strategy_advice_failed", error=str(e))
            return self._last_strategy_advice

    @property
    def last_market_analysis(self) -> MarketAnalysis | None:
        return self._last_market_analysis

    @property
    def last_risk_alerts(self) -> list[RiskAlert]:
        return self._last_risk_alerts

    @property
    def last_trade_review(self) -> TradeReview | None:
        return self._last_trade_review

    @property
    def last_performance_report(self) -> PerformanceReport | None:
        return self._last_performance_report

    @property
    def last_strategy_advice(self) -> StrategyAdvice | None:
        return self._last_strategy_advice
