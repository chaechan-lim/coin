import asyncio
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from core.utils import utcnow

from core.models import Order, Trade, StrategyLog
from core.enums import OrderStatus
from exchange.base import ExchangeAdapter
from exchange.data_models import OrderResult
from strategies.base import Signal
from strategies.combiner import CombinedDecision

logger = structlog.get_logger(__name__)


def _f(v) -> float | None:
    """numpy.float64 등을 Python float으로 변환 (asyncpg 호환)."""
    return float(v) if v is not None else None


def _clean_indicators(d: dict | None) -> dict | None:
    """지표 딕셔너리의 numpy 타입을 Python 기본 타입으로 변환."""
    if not d:
        return d
    result = {}
    for k, v in d.items():
        try:
            result[k] = float(v) if hasattr(v, "__float__") else v
        except (TypeError, ValueError):
            result[k] = str(v)
    return result


class OrderManager:
    """Manages order lifecycle: create, track, fill, cancel."""

    def __init__(self, exchange: ExchangeAdapter, is_paper: bool = True,
                 exchange_name: str = "bithumb", fee_currency: str = "KRW"):
        self._exchange = exchange
        self._is_paper = is_paper
        self._exchange_name = exchange_name
        self._fee_currency = fee_currency

    async def _poll_fill(self, order_id: str, symbol: str, timeout: float = 8) -> OrderResult:
        """Poll exchange until order is filled or timeout."""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            try:
                r = await self._exchange.fetch_order(order_id, symbol)
                if r.status in ("closed", "canceled"):
                    return r
            except Exception as e:
                logger.warning("poll_fill_error", oid=order_id, error=str(e))
                break
        # 타임아웃: 마지막 상태 반환
        try:
            return await self._exchange.fetch_order(order_id, symbol)
        except Exception:
            # 폴링 실패 시 원본 결과 유지 — 호출자가 처리
            from exchange.data_models import OrderResult
            return OrderResult(
                order_id=order_id, symbol=symbol, side="", order_type="limit",
                status="open", price=0, amount=0, filled=0, remaining=0,
                cost=0, fee=0, fee_currency=self._fee_currency, timestamp=None, info={},
            )

    async def create_order(
        self,
        session: AsyncSession,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        signal: Signal,
        decision: CombinedDecision | None = None,
        order_type: str = "limit",
        direction: str | None = None,
        leverage: int | None = None,
        margin_used: float | None = None,
        entry_price: float | None = None,
    ) -> Order:
        """Create and execute an order with full strategy attribution."""

        # Execute on exchange
        if order_type == "market":
            if side == "buy":
                result = await self._exchange.create_market_buy(symbol, amount)
            else:
                result = await self._exchange.create_market_sell(symbol, amount)
        else:
            if side == "buy":
                result = await self._exchange.create_limit_buy(symbol, amount, price)
            else:
                result = await self._exchange.create_limit_sell(symbol, amount, price)

        # 지정가 미체결이면 추가 폴링 (시장가는 즉시 체결)
        if order_type == "limit" and result.status != "closed" and result.order_id:
            try:
                final = await self._exchange.fetch_order(result.order_id, symbol)
                if final.status in ("closed", "canceled"):
                    result = final
            except Exception:
                pass  # fetch 실패 시 원본 결과 유지

        # Determine status
        status = OrderStatus.FILLED.value if result.status == "closed" else OrderStatus.OPEN.value

        # Build contributing strategies info
        contributing = None
        combined_score = None
        if decision:
            contributing = [
                {
                    "name": s.strategy_name,
                    "signal": s.signal_type.value,
                    "confidence": _f(s.confidence),
                    "reason": s.reason,
                }
                for s in decision.contributing_signals
            ]
            combined_score = _f(decision.combined_confidence)

        # PnL 계산 (매도/청산 시)
        calc_pnl = None
        calc_pnl_pct = None
        if side == "sell" and entry_price and entry_price > 0:
            exec_price = result.price if result.filled > 0 else price
            if direction == "short":
                calc_pnl_pct = (entry_price - exec_price) / entry_price * 100
            else:
                calc_pnl_pct = (exec_price - entry_price) / entry_price * 100
            if leverage and leverage > 1:
                calc_pnl_pct *= leverage
            calc_pnl = (result.filled or amount) * abs(exec_price - entry_price)
            if direction == "short" and exec_price > entry_price:
                calc_pnl = -calc_pnl
            elif direction != "short" and exec_price < entry_price:
                calc_pnl = -calc_pnl

        # Create DB record
        order = Order(
            exchange=self._exchange_name,
            exchange_order_id=result.order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            status=status,
            requested_price=_f(price),
            executed_price=_f(result.price) if result.filled > 0 else None,
            requested_quantity=_f(amount),
            executed_quantity=_f(result.filled),
            fee=_f(result.fee),
            fee_currency=result.fee_currency,
            is_paper=self._is_paper,
            direction=direction,
            leverage=leverage,
            margin_used=_f(margin_used),
            entry_price=_f(entry_price),
            realized_pnl=_f(calc_pnl),
            realized_pnl_pct=_f(calc_pnl_pct),
            strategy_name=signal.strategy_name,
            signal_confidence=_f(signal.confidence),
            signal_reason=signal.reason,
            combined_score=combined_score,
            contributing_strategies=contributing,
            filled_at=utcnow() if status == OrderStatus.FILLED.value else None,
        )
        session.add(order)
        await session.flush()

        # Create trade record if filled
        if result.filled > 0:
            trade = Trade(
                exchange=self._exchange_name,
                order_id=order.id,
                symbol=symbol,
                side=side,
                price=_f(result.price),
                quantity=_f(result.filled),
                cost=_f(result.cost),
                fee=_f(result.fee),
                is_paper=self._is_paper,
                executed_at=utcnow(),
            )
            session.add(trade)

        # Log the strategy signal
        strategy_log = StrategyLog(
            exchange=self._exchange_name,
            strategy_name=signal.strategy_name,
            symbol=symbol,
            signal_type=signal.signal_type.value,
            confidence=_f(signal.confidence),
            reason=signal.reason,
            indicators=_clean_indicators(signal.indicators),
            was_executed=(status == OrderStatus.FILLED.value),
            order_id=order.id,
        )
        session.add(strategy_log)

        await session.flush()

        logger.info(
            "order_created",
            order_id=order.id,
            symbol=symbol,
            side=side,
            amount=amount,
            price=price,
            strategy=signal.strategy_name,
            confidence=signal.confidence,
            status=status,
        )

        return order

    async def log_signal_only(
        self, session: AsyncSession, signal: Signal, symbol: str
    ) -> None:
        """Log a strategy signal that didn't result in a trade."""
        strategy_log = StrategyLog(
            exchange=self._exchange_name,
            strategy_name=signal.strategy_name,
            symbol=symbol,
            signal_type=signal.signal_type.value,
            confidence=_f(signal.confidence),
            reason=signal.reason,
            indicators=_clean_indicators(signal.indicators),
            was_executed=False,
        )
        session.add(strategy_log)

    async def get_open_orders(self, session: AsyncSession) -> list[Order]:
        """Get all open/pending orders."""
        result = await session.execute(
            select(Order).where(
                Order.status.in_([OrderStatus.OPEN.value, OrderStatus.PENDING.value])
            )
        )
        return list(result.scalars().all())

    async def cancel_order_by_id(
        self, session: AsyncSession, order_id: int
    ) -> bool:
        """Cancel an order."""
        result = await session.execute(
            select(Order).where(Order.id == order_id)
        )
        order = result.scalar_one_or_none()
        if not order:
            return False

        if order.exchange_order_id:
            try:
                await self._exchange.cancel_order(order.exchange_order_id, order.symbol)
            except Exception as e:
                logger.warning("cancel_exchange_error", oid=order.exchange_order_id, error=str(e))

        order.status = OrderStatus.CANCELLED.value
        order.updated_at = utcnow()
        return True
