import structlog
from dataclasses import dataclass
from datetime import timedelta
from core.utils import utcnow
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from config import RiskConfig
from core.enums import RiskLevel
from core.models import Position, Trade, PortfolioSnapshot
from services.market_data import MarketDataService

logger = structlog.get_logger(__name__)


@dataclass
class RiskAlert:
    level: RiskLevel
    message: str
    action: str  # "reduce_position", "stop_buying", "emergency_sell", "log_only"
    affected_coins: list[str]
    details: dict


class RiskManagementAgent:
    """
    Monitors portfolio risk and generates alerts.
    Runs every 5 minutes.
    """

    def __init__(
        self,
        config: RiskConfig,
        market_data: MarketDataService,
    ):
        self._config = config
        self._market_data = market_data
        self._alerts: list[RiskAlert] = []

    async def evaluate(
        self,
        session: AsyncSession,
        cash_balance: float,
    ) -> list[RiskAlert]:
        """Evaluate all risk metrics and return alerts."""
        self._alerts = []

        # Get current positions
        result = await session.execute(
            select(Position).where(Position.quantity > 0)
        )
        positions = list(result.scalars().all())

        if not positions:
            return self._alerts

        # Calculate total portfolio value
        total_value = cash_balance
        position_values: dict[str, float] = {}

        for pos in positions:
            try:
                price = await self._market_data.get_current_price(pos.symbol)
                value = pos.quantity * price
                position_values[pos.symbol] = value
                total_value += value
            except Exception:
                position_values[pos.symbol] = pos.current_value or 0
                total_value += pos.current_value or 0

        # Run all risk checks
        await self._check_concentration(position_values, total_value)
        await self._check_drawdown(session, total_value)
        await self._check_daily_loss(session, total_value)
        await self._check_position_sizes(position_values, total_value)

        if self._alerts:
            logger.warning(
                "risk_alerts_generated",
                count=len(self._alerts),
                levels=[a.level.value for a in self._alerts],
            )

        return self._alerts

    async def _check_concentration(
        self, position_values: dict[str, float], total_value: float
    ) -> None:
        """Check if any single coin exceeds max concentration."""
        if total_value <= 0:
            return

        for symbol, value in position_values.items():
            pct = value / total_value
            if pct > self._config.max_single_coin_pct:
                level = RiskLevel.CRITICAL if pct > self._config.max_single_coin_pct + 0.1 else RiskLevel.WARNING
                self._alerts.append(RiskAlert(
                    level=level,
                    message=f"{symbol} 비중 {pct*100:.1f}%가 한도 {self._config.max_single_coin_pct*100:.0f}% 초과",
                    action="reduce_position" if level == RiskLevel.CRITICAL else "stop_buying",
                    affected_coins=[symbol],
                    details={"current_pct": round(pct * 100, 1), "limit_pct": self._config.max_single_coin_pct * 100},
                ))

    async def _check_drawdown(
        self, session: AsyncSession, current_value: float
    ) -> None:
        """Check maximum drawdown from portfolio peak."""
        result = await session.execute(
            select(func.max(PortfolioSnapshot.peak_value))
        )
        peak = result.scalar()
        if not peak or peak <= 0:
            return

        drawdown = (peak - current_value) / peak
        if drawdown > self._config.max_drawdown_pct:
            level = RiskLevel.CRITICAL if drawdown > self._config.max_drawdown_pct * 1.5 else RiskLevel.WARNING
            self._alerts.append(RiskAlert(
                level=level,
                message=f"포트폴리오 낙폭 {drawdown*100:.1f}%가 한도 {self._config.max_drawdown_pct*100:.0f}% 초과. "
                f"고점: {peak:,.0f}원, 현재: {current_value:,.0f}원",
                action="stop_buying" if level == RiskLevel.WARNING else "emergency_sell",
                affected_coins=[],  # All coins affected
                details={"drawdown_pct": round(drawdown * 100, 1), "peak": peak, "current": current_value},
            ))

    async def _check_daily_loss(
        self, session: AsyncSession, current_value: float
    ) -> None:
        """Check daily P&L loss limit."""
        today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        # Get today's starting portfolio value
        result = await session.execute(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.snapshot_at >= today_start)
            .order_by(PortfolioSnapshot.snapshot_at.asc())
            .limit(1)
        )
        first_snapshot = result.scalar_one_or_none()
        if not first_snapshot:
            return

        daily_start_value = first_snapshot.total_value_krw
        if daily_start_value <= 0:
            return

        daily_loss = (daily_start_value - current_value) / daily_start_value
        if daily_loss > self._config.daily_loss_limit_pct:
            self._alerts.append(RiskAlert(
                level=RiskLevel.CRITICAL,
                message=f"일일 손실 {daily_loss*100:.1f}%가 한도 {self._config.daily_loss_limit_pct*100:.0f}% 초과. "
                f"오늘 시작: {daily_start_value:,.0f}원, 현재: {current_value:,.0f}원",
                action="stop_buying",
                affected_coins=[],
                details={"daily_loss_pct": round(daily_loss * 100, 1), "start_value": daily_start_value},
            ))

    async def _check_position_sizes(
        self, position_values: dict[str, float], total_value: float
    ) -> None:
        """Warn about individual position sizes."""
        if total_value <= 0:
            return

        for symbol, value in position_values.items():
            pct = value / total_value
            if pct > self._config.max_trade_size_pct * 2:  # Warn when double the trade size
                self._alerts.append(RiskAlert(
                    level=RiskLevel.INFO,
                    message=f"{symbol} 포지션 크기 {value:,.0f}원 ({pct*100:.1f}%) - 대형 포지션 주의",
                    action="log_only",
                    affected_coins=[symbol],
                    details={"position_value": value, "pct": round(pct * 100, 1)},
                ))

    @property
    def active_alerts(self) -> list[RiskAlert]:
        return self._alerts
