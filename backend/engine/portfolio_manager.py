import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case
from core.utils import utcnow

from core.models import Position, PortfolioSnapshot, Trade, Order
from services.market_data import MarketDataService

logger = structlog.get_logger(__name__)


class PortfolioManager:
    """Manages portfolio state, positions, and P&L calculation."""

    def __init__(
        self,
        market_data: MarketDataService,
        initial_balance_krw: float = 500_000,
        is_paper: bool = True,
        exchange_name: str = "bithumb",
    ):
        self._market_data = market_data
        self._initial_balance = initial_balance_krw
        self._cash_balance = initial_balance_krw
        self._is_paper = is_paper
        self._exchange_name = exchange_name
        self._peak_value = initial_balance_krw
        self._realized_pnl = 0.0

    async def update_position_on_buy(
        self, session: AsyncSession, symbol: str, quantity: float, price: float, cost: float, fee: float,
        is_surge: bool = False,
    ) -> None:
        """Update position after a buy trade."""
        from datetime import datetime, timezone
        result = await session.execute(
            select(Position).where(
                Position.symbol == symbol,
                Position.exchange == self._exchange_name,
            )
        )
        position = result.scalar_one_or_none()

        if position:
            # Update average buy price
            total_cost = position.average_buy_price * position.quantity + price * quantity
            position.quantity += quantity
            position.average_buy_price = total_cost / position.quantity if position.quantity > 0 else 0
            position.total_invested += cost + fee
            if is_surge:
                position.is_surge = True
            if not position.entered_at:
                position.entered_at = datetime.now(timezone.utc)
        else:
            position = Position(
                exchange=self._exchange_name,
                symbol=symbol,
                quantity=quantity,
                average_buy_price=price,
                total_invested=cost + fee,
                is_paper=self._is_paper,
                is_surge=is_surge,
                entered_at=datetime.now(timezone.utc),
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
            select(Position).where(
                Position.symbol == symbol,
                Position.exchange == self._exchange_name,
            )
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

        old_quantity = position.quantity
        position.quantity -= quantity
        if position.quantity <= 0.0001:  # Effectively zero
            position.quantity = 0
            position.average_buy_price = 0
            position.total_invested = 0
            position.is_surge = False
            position.entered_at = None
        else:
            # 부분 매도: total_invested를 남은 비율만큼 축소
            position.total_invested *= (position.quantity / old_quantity)

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
            select(Position).where(
                Position.quantity > 0,
                Position.exchange == self._exchange_name,
            )
        )
        positions = list(result.scalars().all())

        total_invested = 0.0
        total_current_value = 0.0
        position_details = []

        is_futures = "futures" in self._exchange_name

        for pos in positions:
            try:
                current_price = await self._market_data.get_current_price(pos.symbol)
                notional = pos.quantity * current_price
                entry_notional = pos.average_buy_price * pos.quantity

                # 선물: 숏은 PnL 방향 반전, 현물: 항상 롱
                is_short = is_futures and getattr(pos, "direction", "long") == "short"
                if is_short:
                    unrealized_pnl = entry_notional - notional
                else:
                    unrealized_pnl = notional - entry_notional

                # 선물: 에쿼티 = 마진(total_invested) + 미실현PnL
                # 현물: 에쿼티 = qty × current_price
                if is_futures:
                    current_value = pos.total_invested + unrealized_pnl
                    pnl_base = pos.total_invested  # 마진 대비 수익률
                else:
                    current_value = notional
                    pnl_base = entry_notional

                unrealized_pnl_pct = (
                    (unrealized_pnl / pnl_base * 100) if pnl_base > 0 else 0
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
                    "direction": getattr(pos, "direction", None),
                    "leverage": getattr(pos, "leverage", None),
                    "liquidation_price": getattr(pos, "liquidation_price", None),
                })
            except Exception as e:
                logger.warning("price_fetch_failed", symbol=pos.symbol, error=str(e))

        # 총 수수료 / 거래 횟수 집계 (orders + trades 양쪽에서)
        fee_result = await session.execute(
            select(
                func.coalesce(func.sum(Order.fee), 0),
                func.count(Order.id),
            ).where(Order.exchange == self._exchange_name)
        )
        fee_row = fee_result.one()
        total_fees = float(fee_row[0])
        trade_count = int(fee_row[1])
        # orders.fee=0인 경우 trades 테이블에서 보충
        if total_fees == 0 and trade_count > 0:
            trade_fee_result = await session.execute(
                select(func.coalesce(func.sum(Trade.fee), 0))
                .where(Trade.exchange == self._exchange_name)
            )
            total_fees = float(trade_fee_result.scalar())

        total_value = self._cash_balance + total_current_value
        total_unrealized_pnl = total_current_value - total_invested

        # Track peak for drawdown
        if total_value > self._peak_value:
            self._peak_value = total_value
        drawdown_pct = (
            (self._peak_value - total_value) / self._peak_value * 100
            if self._peak_value > 0 else 0
        )

        # USDT(선물)는 소수점 2자리, KRW(현물)는 정수
        dp = 2 if "futures" in self._exchange_name else 0

        return {
            "exchange": self._exchange_name,
            "total_value_krw": round(total_value, dp),
            "cash_balance_krw": round(self._cash_balance, dp),
            "invested_value_krw": round(total_current_value, dp),
            "initial_balance_krw": round(self._initial_balance, dp),
            "realized_pnl": round(self._realized_pnl, dp),
            "unrealized_pnl": round(total_unrealized_pnl, dp),
            "total_pnl": round(self._realized_pnl + total_unrealized_pnl, dp),
            "total_pnl_pct": round(
                (self._realized_pnl + total_unrealized_pnl) / self._initial_balance * 100, 2
            ) if self._initial_balance > 0 else 0,
            "total_fees": round(total_fees, dp),
            "trade_count": trade_count,
            "peak_value": round(self._peak_value, dp),
            "drawdown_pct": round(drawdown_pct, 2),
            "positions": position_details,
        }

    async def take_snapshot(self, session: AsyncSession) -> PortfolioSnapshot:
        """Take a portfolio snapshot for historical tracking."""
        summary = await self.get_portfolio_summary(session)

        snapshot = PortfolioSnapshot(
            exchange=self._exchange_name,
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

    async def reconcile_cash_from_db(self, session: AsyncSession) -> None:
        """DB 포지션 기준으로 현금 잔고를 재계산 (인메모리 누수 방지).

        공식: cash = initial_balance - total_invested + realized_pnl - total_fees
        """
        result = await session.execute(
            select(Position).where(
                Position.quantity > 0,
                Position.exchange == self._exchange_name,
            )
        )
        positions = list(result.scalars().all())
        total_invested = sum(p.total_invested for p in positions)

        # 수수료 합산
        fee_result = await session.execute(
            select(func.coalesce(func.sum(Order.fee), 0))
            .where(Order.exchange == self._exchange_name)
        )
        total_fees = float(fee_result.scalar())
        if total_fees == 0:
            trade_fee_result = await session.execute(
                select(func.coalesce(func.sum(Trade.fee), 0))
                .where(Trade.exchange == self._exchange_name)
            )
            total_fees = float(trade_fee_result.scalar())

        old_cash = self._cash_balance
        self._cash_balance = (
            self._initial_balance - total_invested + self._realized_pnl - total_fees
        )
        if abs(old_cash - self._cash_balance) > 1.0:
            logger.warning(
                "cash_balance_reconciled",
                old=round(old_cash, 2),
                new=round(self._cash_balance, 2),
                diff=round(old_cash - self._cash_balance, 2),
            )

    async def sync_exchange_positions(
        self, session: AsyncSession, exchange_adapter, tracked_coins: list[str]
    ) -> None:
        """거래소 실제 잔고를 DB 포지션과 동기화.

        - 거래소에 보유 중이지만 DB에 없는 코인 → 포지션 생성
        - DB 포지션 수량 vs 거래소 실제 수량 불일치 → 거래소 기준으로 보정
        - 실제 현금 잔고로 cash_balance 갱신
        """
        try:
            balances = await exchange_adapter.fetch_balance()
        except Exception as e:
            logger.warning("sync_exchange_balances_failed", error=str(e))
            return

        # 현금 통화 결정
        if "futures" in self._exchange_name:
            cash_symbol = "USDT"
        else:
            cash_symbol = "KRW"

        # 실제 현금 잔고
        cash_bal = balances.get(cash_symbol)
        actual_cash = cash_bal.free if cash_bal else 0

        # 기존 DB 포지션 조회
        result = await session.execute(
            select(Position).where(Position.exchange == self._exchange_name)
        )
        db_positions = {p.symbol: p for p in result.scalars().all()}

        # tracked_coins에 포함된 심볼 + 실제 잔고가 있는 코인 처리
        synced_count = 0
        total_invested = 0.0

        for symbol, bal in balances.items():
            if symbol == cash_symbol or bal.total <= 0:
                continue

            # 심볼 형식 변환: "ADA" → "ADA/KRW" 또는 "ADA/USDT"
            pair = f"{symbol}/{cash_symbol}"

            # 너무 작은 잔고 무시 (dust)
            try:
                current_price = await self._market_data.get_current_price(pair)
                coin_value = bal.total * current_price
                # 현물: 1000원 미만, 선물: 1 USDT 미만 무시
                min_value = 1.0 if "futures" in self._exchange_name else 1000
                if coin_value < min_value:
                    continue
            except Exception:
                continue

            db_pos = db_positions.get(pair)

            if db_pos is None:
                # DB에 없는 포지션 → 신규 생성 (기존 보유 코인)
                from datetime import datetime, timezone
                new_pos = Position(
                    exchange=self._exchange_name,
                    symbol=pair,
                    quantity=bal.total,
                    average_buy_price=current_price,  # 현재가를 진입가로 설정
                    total_invested=bal.total * current_price,
                    is_paper=self._is_paper,
                    entered_at=datetime.now(timezone.utc),
                )
                session.add(new_pos)
                total_invested += new_pos.total_invested
                synced_count += 1
                logger.info(
                    "position_synced_from_exchange",
                    symbol=pair, quantity=bal.total,
                    price=current_price, value=round(coin_value, 2),
                )
            elif abs(db_pos.quantity - bal.total) / max(db_pos.quantity, 0.0001) > 0.01:
                # DB 수량과 거래소 수량이 1% 이상 차이 → 거래소 기준으로 보정
                old_qty = db_pos.quantity
                db_pos.quantity = bal.total
                # total_invested도 비례 조정
                if old_qty > 0:
                    ratio = bal.total / old_qty
                    db_pos.total_invested *= ratio
                total_invested += db_pos.total_invested
                logger.info(
                    "position_quantity_adjusted",
                    symbol=pair, old=old_qty, new=bal.total,
                )
            else:
                total_invested += db_pos.total_invested

        await session.flush()

        # 실제 현금 기준으로 cash_balance 재설정 (initial_balance는 고정 원금 유지)
        old_cash = self._cash_balance
        self._cash_balance = actual_cash

        if synced_count > 0 or abs(old_cash - actual_cash) > 1.0:
            logger.info(
                "exchange_positions_synced",
                exchange=self._exchange_name,
                synced=synced_count,
                actual_cash=round(actual_cash, 2),
                initial_balance=round(self._initial_balance, 2),
            )

    async def restore_state_from_db(self, session: AsyncSession) -> None:
        """서버 재시작 시 최신 스냅샷에서 peak_value, realized_pnl 복원."""
        result = await session.execute(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.exchange == self._exchange_name)
            .order_by(PortfolioSnapshot.snapshot_at.desc())
            .limit(1)
        )
        snapshot = result.scalar_one_or_none()
        if snapshot:
            self._peak_value = snapshot.peak_value or self._peak_value
            self._realized_pnl = snapshot.realized_pnl or 0.0
            logger.info(
                "portfolio_state_restored",
                exchange=self._exchange_name,
                peak_value=round(self._peak_value, 2),
                realized_pnl=round(self._realized_pnl, 2),
            )
        else:
            # 첫 실행: peak를 현재 실제 자산으로 설정 (config값보다 낮을 수 있음)
            actual_total = self._cash_balance
            if actual_total > 0:
                self._peak_value = actual_total
            logger.info(
                "no_snapshot_peak_from_actual",
                exchange=self._exchange_name,
                peak_value=round(self._peak_value, 2),
            )

    async def load_initial_balance_from_db(self, session: AsyncSession) -> None:
        """DB CapitalTransaction에서 확정된 입출금 합계로 initial_balance 재계산."""
        from core.models import CapitalTransaction
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
        if deposits > 0 or withdrawals > 0:
            self._initial_balance = deposits - withdrawals
            logger.info(
                "initial_balance_from_capital",
                exchange=self._exchange_name,
                deposits=round(deposits, 2),
                withdrawals=round(withdrawals, 2),
                initial_balance=round(self._initial_balance, 2),
            )

    @property
    def cash_balance(self) -> float:
        return self._cash_balance

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl
