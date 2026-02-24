import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from core.utils import utcnow

from core.models import Position, PortfolioSnapshot, Trade
from services.market_data import MarketDataService

logger = structlog.get_logger(__name__)


class PortfolioManager:
    """Manages portfolio state, positions, and P&L calculation."""

    def __init__(
        self,
        market_data: MarketDataService,
        initial_balance_krw: float = 500_000,
        is_paper: bool = True,
    ):
        self._market_data = market_data
        self._cash_balance = initial_balance_krw
        self._is_paper = is_paper
        self._peak_value = initial_balance_krw
        self._realized_pnl = 0.0

    async def update_position_on_buy(
        self, session: AsyncSession, symbol: str, quantity: float, price: float, cost: float, fee: float
    ) -> None:
        """Update position after a buy trade."""
        result = await session.execute(
            select(Position).where(Position.symbol == symbol)
        )
        position = result.scalar_one_or_none()

        if position:
            # Update average buy price
            total_cost = position.average_buy_price * position.quantity + price * quantity
            position.quantity += quantity
            position.average_buy_price = total_cost / position.quantity if position.quantity > 0 else 0
            position.total_invested += cost + fee
        else:
            position = Position(
                symbol=symbol,
                quantity=quantity,
                average_buy_price=price,
                total_invested=cost + fee,
                is_paper=self._is_paper,
            )
            session.add(position)

        self._cash_balance -= (cost + fee)
        await session.flush()

        logger.info(
            "position_updated_buy",
            symbol=symbol,
            quantity=position.quantity,
            avg_price=position.average_buy_price,
            cash_remaining=self._cash_balance,
        )

    async def update_position_on_sell(
        self, session: AsyncSession, symbol: str, quantity: float, price: float, cost: float, fee: float
    ) -> None:
        """Update position after a sell trade."""
        result = await session.execute(
            select(Position).where(Position.symbol == symbol)
        )
        position = result.scalar_one_or_none()

        if not position or position.quantity < quantity:
            logger.warning("sell_exceeds_position", symbol=symbol, quantity=quantity)
            return

        # Calculate realized P&L
        sell_proceeds = cost - fee
        buy_cost = position.average_buy_price * quantity
        realized = sell_proceeds - buy_cost
        self._realized_pnl += realized

        position.quantity -= quantity
        if position.quantity <= 0.0001:  # Effectively zero
            position.quantity = 0
            position.average_buy_price = 0
            position.total_invested = 0

        self._cash_balance += sell_proceeds
        await session.flush()

        logger.info(
            "position_updated_sell",
            symbol=symbol,
            quantity_sold=quantity,
            realized_pnl=realized,
            remaining_quantity=position.quantity,
        )

    async def get_portfolio_summary(self, session: AsyncSession) -> dict:
        """Get current portfolio summary."""
        result = await session.execute(
            select(Position).where(Position.quantity > 0)
        )
        positions = list(result.scalars().all())

        total_invested = 0.0
        total_current_value = 0.0
        position_details = []

        for pos in positions:
            try:
                current_price = await self._market_data.get_current_price(pos.symbol)
                current_value = pos.quantity * current_price
                unrealized_pnl = current_value - (pos.average_buy_price * pos.quantity)
                unrealized_pnl_pct = (
                    (unrealized_pnl / (pos.average_buy_price * pos.quantity) * 100)
                    if pos.average_buy_price > 0 else 0
                )

                # Update position with current values
                pos.current_value = current_value
                pos.unrealized_pnl = unrealized_pnl
                pos.unrealized_pnl_pct = unrealized_pnl_pct
                pos.updated_at = utcnow()

                total_invested += pos.total_invested
                total_current_value += current_value

                position_details.append({
                    "symbol": pos.symbol,
                    "quantity": pos.quantity,
                    "average_buy_price": pos.average_buy_price,
                    "current_price": current_price,
                    "current_value": current_value,
                    "unrealized_pnl": unrealized_pnl,
                    "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
                })
            except Exception as e:
                logger.warning("price_fetch_failed", symbol=pos.symbol, error=str(e))

        total_value = self._cash_balance + total_current_value
        total_unrealized_pnl = total_current_value - total_invested

        # Track peak for drawdown
        if total_value > self._peak_value:
            self._peak_value = total_value
        drawdown_pct = (
            (self._peak_value - total_value) / self._peak_value * 100
            if self._peak_value > 0 else 0
        )

        return {
            "total_value_krw": round(total_value, 0),
            "cash_balance_krw": round(self._cash_balance, 0),
            "invested_value_krw": round(total_current_value, 0),
            "realized_pnl": round(self._realized_pnl, 0),
            "unrealized_pnl": round(total_unrealized_pnl, 0),
            "total_pnl": round(self._realized_pnl + total_unrealized_pnl, 0),
            "total_pnl_pct": round(
                (self._realized_pnl + total_unrealized_pnl) / self._peak_value * 100, 2
            ) if self._peak_value > 0 else 0,
            "peak_value": round(self._peak_value, 0),
            "drawdown_pct": round(drawdown_pct, 2),
            "positions": position_details,
        }

    async def take_snapshot(self, session: AsyncSession) -> PortfolioSnapshot:
        """Take a portfolio snapshot for historical tracking."""
        summary = await self.get_portfolio_summary(session)

        snapshot = PortfolioSnapshot(
            total_value_krw=summary["total_value_krw"],
            cash_balance_krw=summary["cash_balance_krw"],
            invested_value_krw=summary["invested_value_krw"],
            realized_pnl=summary["realized_pnl"],
            unrealized_pnl=summary["unrealized_pnl"],
            peak_value=summary["peak_value"],
            drawdown_pct=summary["drawdown_pct"],
        )
        session.add(snapshot)
        await session.flush()
        return snapshot

    @property
    def cash_balance(self) -> float:
        return self._cash_balance

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl
