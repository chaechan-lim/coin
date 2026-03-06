"""Order PnL 계산 테스트 — 매도/청산 시 realized_pnl, realized_pnl_pct 정확성 검증."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from engine.order_manager import OrderManager
from exchange.data_models import OrderResult
from strategies.base import Signal
from core.enums import SignalType


def _make_order_manager(exchange_name="bithumb", is_paper=True):
    exchange = MagicMock()
    return OrderManager(exchange, exchange_name=exchange_name, is_paper=is_paper)


def _make_signal(name="risk_management"):
    return Signal(
        strategy_name=name,
        signal_type=SignalType.SELL,
        confidence=1.0,
        reason="test",
    )


def _make_order_result(price=100.0, filled=1.0, fee=0.1, status="closed"):
    from datetime import datetime, timezone
    return OrderResult(
        order_id="test-1",
        symbol="BTC/USDT",
        side="sell",
        order_type="market",
        status=status,
        price=price,
        amount=filled,
        filled=filled,
        remaining=0,
        cost=price * filled,
        fee=fee,
        fee_currency="USDT",
        timestamp=datetime.now(timezone.utc),
    )


class TestOrderPnlCalculation:
    """Order PnL이 매도 주문에 정확히 기록되는지 테스트."""

    @pytest.mark.asyncio
    async def test_spot_sell_profit(self):
        """현물 매도 수익 — (청산가-진입가)/진입가 * 100."""
        om = _make_order_manager()
        om._exchange.create_market_sell = AsyncMock(
            return_value=_make_order_result(price=110.0, filled=1.0)
        )
        session = AsyncMock()

        order = await om.create_order(
            session, "BTC/KRW", "sell", 1.0, 110.0, _make_signal(),
            order_type="market", entry_price=100.0,
        )

        assert order.entry_price == 100.0
        assert order.realized_pnl_pct == pytest.approx(10.0)
        assert order.realized_pnl == pytest.approx(10.0)  # 1 * |110-100|

    @pytest.mark.asyncio
    async def test_spot_sell_loss(self):
        """현물 매도 손실."""
        om = _make_order_manager()
        om._exchange.create_market_sell = AsyncMock(
            return_value=_make_order_result(price=90.0, filled=1.0)
        )
        session = AsyncMock()

        order = await om.create_order(
            session, "BTC/KRW", "sell", 1.0, 90.0, _make_signal(),
            order_type="market", entry_price=100.0,
        )

        assert order.realized_pnl_pct == pytest.approx(-10.0)
        assert order.realized_pnl == pytest.approx(-10.0)

    @pytest.mark.asyncio
    async def test_futures_long_close_profit(self):
        """선물 롱 청산 수익 — 레버리지 적용."""
        om = _make_order_manager(exchange_name="binance_futures")
        om._exchange.create_market_sell = AsyncMock(
            return_value=_make_order_result(price=105.0, filled=1.0)
        )
        session = AsyncMock()

        order = await om.create_order(
            session, "BTC/USDT", "sell", 1.0, 105.0, _make_signal(),
            order_type="market", direction="long", leverage=3, entry_price=100.0,
        )

        # 기본 PnL: 5%, 레버리지 3x → 15%
        assert order.realized_pnl_pct == pytest.approx(15.0)
        assert order.realized_pnl == pytest.approx(5.0)  # 금액은 레버리지 무관

    @pytest.mark.asyncio
    async def test_futures_short_close_profit(self):
        """선물 숏 청산 수익 — 가격 하락이 수익."""
        om = _make_order_manager(exchange_name="binance_futures")
        # 숏 청산은 buy로 실행
        om._exchange.create_market_sell = AsyncMock(
            return_value=_make_order_result(price=95.0, filled=1.0)
        )
        session = AsyncMock()

        order = await om.create_order(
            session, "BTC/USDT", "sell", 1.0, 95.0, _make_signal(),
            order_type="market", direction="short", leverage=3, entry_price=100.0,
        )

        # 숏: (100 - 95) / 100 * 100 = 5%, 레버리지 3x → 15%
        assert order.realized_pnl_pct == pytest.approx(15.0)
        assert order.realized_pnl == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_futures_short_close_loss(self):
        """선물 숏 청산 손실 — 가격 상승이 손실."""
        om = _make_order_manager(exchange_name="binance_futures")
        om._exchange.create_market_sell = AsyncMock(
            return_value=_make_order_result(price=110.0, filled=1.0)
        )
        session = AsyncMock()

        order = await om.create_order(
            session, "BTC/USDT", "sell", 1.0, 110.0, _make_signal(),
            order_type="market", direction="short", leverage=3, entry_price=100.0,
        )

        # 숏: (100 - 110) / 100 * 100 = -10%, 레버리지 3x → -30%
        assert order.realized_pnl_pct == pytest.approx(-30.0)
        assert order.realized_pnl == pytest.approx(-10.0)

    @pytest.mark.asyncio
    async def test_buy_order_no_pnl(self):
        """매수 주문에는 PnL이 없어야 함."""
        om = _make_order_manager()
        om._exchange.create_market_buy = AsyncMock(
            return_value=_make_order_result(price=100.0, filled=1.0)
        )
        session = AsyncMock()

        order = await om.create_order(
            session, "BTC/KRW", "buy", 1.0, 100.0,
            Signal(strategy_name="test", signal_type=SignalType.BUY, confidence=0.8, reason="test"),
            order_type="market",
        )

        assert order.entry_price is None
        assert order.realized_pnl is None
        assert order.realized_pnl_pct is None

    @pytest.mark.asyncio
    async def test_sell_no_entry_price_no_pnl(self):
        """entry_price 없는 매도 → PnL 미계산."""
        om = _make_order_manager()
        om._exchange.create_market_sell = AsyncMock(
            return_value=_make_order_result(price=100.0, filled=1.0)
        )
        session = AsyncMock()

        order = await om.create_order(
            session, "BTC/KRW", "sell", 1.0, 100.0, _make_signal(),
            order_type="market",
            # entry_price not provided
        )

        assert order.entry_price is None
        assert order.realized_pnl is None
        assert order.realized_pnl_pct is None

    @pytest.mark.asyncio
    async def test_pnl_with_partial_fill(self):
        """부분 체결 시 PnL 금액은 체결 수량 기준."""
        om = _make_order_manager()
        om._exchange.create_market_sell = AsyncMock(
            return_value=_make_order_result(price=110.0, filled=0.5)
        )
        session = AsyncMock()

        order = await om.create_order(
            session, "BTC/KRW", "sell", 1.0, 110.0, _make_signal(),
            order_type="market", entry_price=100.0,
        )

        assert order.realized_pnl_pct == pytest.approx(10.0)
        assert order.realized_pnl == pytest.approx(5.0)  # 0.5 * |110-100|

    @pytest.mark.asyncio
    async def test_futures_no_leverage_multiplier_when_1x(self):
        """레버리지 1x → PnL% 증폭 없음."""
        om = _make_order_manager(exchange_name="binance_futures")
        om._exchange.create_market_sell = AsyncMock(
            return_value=_make_order_result(price=105.0, filled=1.0)
        )
        session = AsyncMock()

        order = await om.create_order(
            session, "BTC/USDT", "sell", 1.0, 105.0, _make_signal(),
            order_type="market", direction="long", leverage=1, entry_price=100.0,
        )

        assert order.realized_pnl_pct == pytest.approx(5.0)  # No multiplier for 1x

    @pytest.mark.asyncio
    async def test_breakeven_zero_pnl(self):
        """손익분기점 — PnL 0."""
        om = _make_order_manager()
        om._exchange.create_market_sell = AsyncMock(
            return_value=_make_order_result(price=100.0, filled=1.0)
        )
        session = AsyncMock()

        order = await om.create_order(
            session, "BTC/KRW", "sell", 1.0, 100.0, _make_signal(),
            order_type="market", entry_price=100.0,
        )

        assert order.realized_pnl_pct == pytest.approx(0.0)
        assert order.realized_pnl == pytest.approx(0.0)
