import structlog
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession

from core.enums import RiskLevel
from core.models import AgentAnalysisLog
from agents.market_analysis import MarketAnalysisAgent, MarketAnalysis
from agents.risk_management import RiskManagementAgent, RiskAlert
from agents.trade_review import TradeReviewAgent, TradeReview
from strategies.combiner import SignalCombiner
from db.session import get_session_factory

logger = structlog.get_logger(__name__)


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
    ):
        self._market_agent = market_agent
        self._risk_agent = risk_agent
        self._combiner = combiner
        self._trade_review_agent = trade_review_agent
        self._engine = None  # Set after engine creation
        self._last_market_analysis: MarketAnalysis | None = None
        self._last_risk_alerts: list[RiskAlert] = []
        self._last_trade_review: TradeReview | None = None

    def set_engine(self, engine) -> None:
        """Set reference to trading engine for control operations."""
        self._engine = engine

    async def run_market_analysis(self) -> MarketAnalysis:
        """Run market analysis and update strategy weights."""
        analysis = await self._market_agent.analyze()
        self._last_market_analysis = analysis

        # Update combiner weights
        self._combiner.update_weights(analysis.recommended_weights)

        # Persist analysis
        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                log = AgentAnalysisLog(
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
            "market_analysis_applied",
            state=analysis.state.value,
            weights=analysis.recommended_weights,
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
                        if alert.level == RiskLevel.CRITICAL:
                            if alert.affected_coins:
                                coins_to_pause.extend(alert.affected_coins)
                            else:
                                # Critical with no specific coins = pause all
                                self._engine.pause_buying(
                                    list(self._engine._config.trading.tracked_coins)
                                )
                        elif alert.level == RiskLevel.WARNING:
                            if alert.affected_coins:
                                coins_to_suppress.extend(alert.affected_coins)

                    if coins_to_pause:
                        self._engine.pause_buying(coins_to_pause)
                    if coins_to_suppress:
                        self._engine.suppress_buys(coins_to_suppress)

                    # Resume coins with no alerts
                    all_alerted = set(coins_to_pause + coins_to_suppress)
                    previously_alerted = self._engine._paused_coins | self._engine._suppressed_coins
                    to_resume = previously_alerted - all_alerted
                    if to_resume:
                        self._engine.resume_buying(list(to_resume))

                # Persist alerts
                for alert in alerts:
                    log = AgentAnalysisLog(
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
                    },
                )
                session.add(log)
                await session.commit()

                return review
        except Exception as e:
            logger.error("trade_review_failed", error=str(e))
            return self._last_trade_review

    @property
    def last_market_analysis(self) -> MarketAnalysis | None:
        return self._last_market_analysis

    @property
    def last_risk_alerts(self) -> list[RiskAlert]:
        return self._last_risk_alerts

    @property
    def last_trade_review(self) -> TradeReview | None:
        return self._last_trade_review
