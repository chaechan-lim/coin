"""SafeOrderPipeline 테스트."""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from engine.safe_order_pipeline import SafeOrderPipeline, OrderRequest, OrderResponse
from engine.balance_guard import BalanceGuard
from engine.portfolio_manager import PortfolioManager
from engine.order_manager import OrderManager
from exchange.data_models import OrderResult, Balance
from core.constants import MIN_NOTIONAL
from core.enums import Direction
from core.models import Position, Order, Trade


@pytest.fixture
def mock_exchange():
    exchange = AsyncMock()
    exchange.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=500.0, used=100.0, total=600.0),
    })
    return exchange


@pytest.fixture
def mock_market_data():
    md = AsyncMock()
    md.get_current_price = AsyncMock(return_value=80000.0)
    return md


@pytest.fixture
def portfolio_manager(mock_market_data):
    pm = PortfolioManager(
        market_data=mock_market_data,
        initial_balance_krw=500.0,
        is_paper=False,
        exchange_name="binance_futures",
    )
    return pm


@pytest.fixture
def balance_guard(mock_exchange):
    return BalanceGuard(
        exchange=mock_exchange,
        exchange_name="binance_futures",
    )


@pytest.fixture
def order_manager(mock_exchange):
    return OrderManager(
        exchange=mock_exchange,
        is_paper=False,
        exchange_name="binance_futures",
        fee_currency="USDT",
    )


@pytest.fixture
def pipeline(order_manager, portfolio_manager, balance_guard, mock_exchange):
    return SafeOrderPipeline(
        order_manager=order_manager,
        portfolio_manager=portfolio_manager,
        balance_guard=balance_guard,
        exchange=mock_exchange,
        leverage=3,
    )


def make_order_result(
    price=80000.0, filled=0.01, cost=800.0, fee=0.32,
) -> OrderResult:
    return OrderResult(
        order_id="test-123",
        symbol="BTC/USDT",
        side="buy",
        order_type="market",
        status="closed",
        price=price,
        amount=filled,
        filled=filled,
        remaining=0.0,
        cost=cost,
        fee=fee,
        fee_currency="USDT",
        timestamp=datetime.now(timezone.utc),
    )


def make_request(
    action="open",
    direction=Direction.LONG,
    margin=100.0,
    quantity=0.01,
    price=80000.0,
    **kwargs,
) -> OrderRequest:
    return OrderRequest(
        symbol=kwargs.get("symbol", "BTC/USDT"),
        direction=direction,
        action=action,
        quantity=quantity,
        price=price,
        margin=margin,
        leverage=kwargs.get("leverage", 3),
        strategy_name=kwargs.get("strategy_name", "trend_follower"),
        confidence=kwargs.get("confidence", 0.7),
        tier=kwargs.get("tier", "tier1"),
        entry_price=kwargs.get("entry_price", 0.0),
    )


class TestPreValidation:
    def test_valid_open(self, pipeline):
        req = make_request(margin=100.0)
        ok, reason = pipeline._validate_open(req, 500.0)
        assert ok is True

    def test_insufficient_cash(self, pipeline):
        req = make_request(margin=600.0)
        ok, reason = pipeline._validate_open(req, 500.0)
        assert ok is False
        assert "insufficient_cash" in reason

    def test_zero_quantity(self, pipeline):
        req = make_request(quantity=0.0)
        ok, reason = pipeline._validate_open(req, 500.0)
        assert ok is False
        assert "invalid_quantity" in reason

    def test_zero_margin(self, pipeline):
        req = make_request(margin=0.0)
        ok, reason = pipeline._validate_open(req, 500.0)
        assert ok is False

    def test_zero_price(self, pipeline):
        req = make_request(price=0.0)
        ok, reason = pipeline._validate_open(req, 500.0)
        assert ok is False

    def test_valid_close(self, pipeline):
        req = make_request(action="close", entry_price=80000.0)
        ok, reason = pipeline._validate_close(req)
        assert ok is True

    def test_close_zero_quantity(self, pipeline):
        req = make_request(action="close", quantity=0.0)
        ok, reason = pipeline._validate_close(req)
        assert ok is False


class TestDetermineSide:
    def test_open_long(self, pipeline):
        req = make_request(action="open", direction=Direction.LONG)
        assert pipeline._determine_side(req) == "buy"

    def test_open_short(self, pipeline):
        req = make_request(action="open", direction=Direction.SHORT)
        assert pipeline._determine_side(req) == "sell"

    def test_close_long(self, pipeline):
        req = make_request(action="close", direction=Direction.LONG)
        assert pipeline._determine_side(req) == "sell"

    def test_close_short(self, pipeline):
        req = make_request(action="close", direction=Direction.SHORT)
        assert pipeline._determine_side(req) == "buy"


class TestExecuteOrder:
    @pytest.mark.asyncio
    async def test_successful_open(self, pipeline, mock_exchange, session):
        """정상 주문 실행."""
        mock_exchange.create_market_buy.return_value = make_order_result()
        req = make_request(margin=100.0)

        resp = await pipeline.execute_order(session, req)
        assert resp.success is True
        assert resp.order_id is not None
        assert resp.executed_price == 80000.0
        assert resp.executed_quantity == 0.01

    @pytest.mark.asyncio
    async def test_successful_close(self, pipeline, mock_exchange, session, portfolio_manager):
        """청산 주문 실행."""
        # 먼저 포지션 생성
        pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.01,
            average_buy_price=80000.0,
            total_invested=100.0,
            direction="long",
            leverage=3,
            is_paper=False,
        )
        session.add(pos)
        await session.flush()

        mock_exchange.create_market_sell.return_value = make_order_result(
            price=82000.0, filled=0.01, cost=820.0, fee=0.33,
        )

        req = make_request(
            action="close",
            direction=Direction.LONG,
            quantity=0.01,
            price=82000.0,
            entry_price=80000.0,
            margin=100.0,
        )

        resp = await pipeline.execute_order(session, req)
        assert resp.success is True

    @pytest.mark.asyncio
    async def test_guard_paused_rejects(self, pipeline, session):
        """BalanceGuard 정지 상태면 거부."""
        pipeline._guard._paused = True
        req = make_request()

        resp = await pipeline.execute_order(session, req)
        assert resp.success is False
        assert "paused" in resp.error

    @pytest.mark.asyncio
    async def test_exchange_error(self, pipeline, mock_exchange, session):
        """거래소 에러 시 실패 반환."""
        mock_exchange.create_market_buy.side_effect = Exception("timeout")
        req = make_request()

        resp = await pipeline.execute_order(session, req)
        assert resp.success is False
        assert "exchange_error" in resp.error

    @pytest.mark.asyncio
    async def test_not_filled(self, pipeline, mock_exchange, session):
        """미체결 시 실패."""
        mock_exchange.create_market_buy.return_value = OrderResult(
            order_id="test-456",
            symbol="BTC/USDT",
            side="buy",
            order_type="market",
            status="open",
            price=0.0,
            amount=0.01,
            filled=0.0,
            remaining=0.01,
            cost=0.0,
            fee=0.0,
            fee_currency="USDT",
            timestamp=datetime.now(timezone.utc),
        )
        req = make_request()

        resp = await pipeline.execute_order(session, req)
        assert resp.success is False
        assert "not_filled" in resp.error

    @pytest.mark.asyncio
    async def test_insufficient_cash_rejected(self, pipeline, session):
        """잔고 부족 시 거부."""
        req = make_request(margin=600.0)  # cash is 500

        resp = await pipeline.execute_order(session, req)
        assert resp.success is False
        assert "insufficient_cash" in resp.error

    @pytest.mark.asyncio
    async def test_open_short(self, pipeline, mock_exchange, session):
        """숏 오픈."""
        mock_exchange.create_market_sell.return_value = make_order_result(
            price=80000.0, filled=0.01, cost=800.0, fee=0.32,
        )
        req = make_request(direction=Direction.SHORT)

        resp = await pipeline.execute_order(session, req)
        assert resp.success is True
        mock_exchange.create_market_sell.assert_called_once()

    @pytest.mark.asyncio
    async def test_db_records_created(self, pipeline, mock_exchange, session):
        """주문 후 Order + Trade DB 레코드 생성 확인."""
        mock_exchange.create_market_buy.return_value = make_order_result()
        req = make_request()

        resp = await pipeline.execute_order(session, req)
        assert resp.success is True

        from sqlalchemy import select
        orders = (await session.execute(select(Order))).scalars().all()
        assert len(orders) == 1
        assert orders[0].strategy_name == "trend_follower"
        assert orders[0].direction == "long"

        trades = (await session.execute(select(Trade))).scalars().all()
        assert len(trades) == 1

    @pytest.mark.asyncio
    async def test_cash_balance_updated(self, pipeline, mock_exchange, session, portfolio_manager):
        """주문 후 잔고가 정확히 반영."""
        initial_cash = portfolio_manager.cash_balance
        mock_exchange.create_market_buy.return_value = make_order_result(
            cost=800.0, fee=0.32,
        )
        req = make_request(margin=100.0)

        resp = await pipeline.execute_order(session, req)
        assert resp.success is True
        # 잔고가 줄어야 함
        assert portfolio_manager.cash_balance < initial_cash


class TestSetLeverage:
    """Bug COIN-13: 주문 전 set_leverage 호출 테스트."""

    @pytest.mark.asyncio
    async def test_set_leverage_called_before_order(self, pipeline, mock_exchange, session):
        """주문 실행 전 set_leverage가 호출되어야 함."""
        mock_exchange.create_market_buy.return_value = make_order_result()
        mock_exchange.set_leverage = AsyncMock()
        req = make_request(leverage=3)

        resp = await pipeline.execute_order(session, req)
        assert resp.success is True
        mock_exchange.set_leverage.assert_called_once_with("BTC/USDT", 3)

    @pytest.mark.asyncio
    async def test_set_leverage_uses_request_leverage(self, pipeline, mock_exchange, session):
        """set_leverage는 request의 leverage 값을 사용해야 함."""
        mock_exchange.create_market_buy.return_value = make_order_result()
        mock_exchange.set_leverage = AsyncMock()
        req = make_request(leverage=5)

        resp = await pipeline.execute_order(session, req)
        assert resp.success is True
        mock_exchange.set_leverage.assert_called_once_with("BTC/USDT", 5)

    @pytest.mark.asyncio
    async def test_set_leverage_failure_does_not_block_order(self, pipeline, mock_exchange, session):
        """set_leverage 실패해도 주문은 정상 진행되어야 함."""
        mock_exchange.create_market_buy.return_value = make_order_result()
        mock_exchange.set_leverage = AsyncMock(side_effect=Exception("leverage error"))
        req = make_request()

        resp = await pipeline.execute_order(session, req)
        assert resp.success is True
        assert resp.executed_price == 80000.0

    @pytest.mark.asyncio
    async def test_set_leverage_called_for_close_order(self, pipeline, mock_exchange, session):
        """청산 주문에서도 set_leverage가 호출되어야 함."""
        pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.01,
            average_buy_price=80000.0,
            total_invested=100.0,
            direction="long",
            leverage=3,
            is_paper=False,
        )
        session.add(pos)
        await session.flush()

        mock_exchange.create_market_sell.return_value = make_order_result(
            price=82000.0, filled=0.01, cost=820.0, fee=0.33,
        )
        mock_exchange.set_leverage = AsyncMock()
        req = make_request(
            action="close", direction=Direction.LONG,
            entry_price=80000.0, margin=100.0, price=82000.0,
        )

        resp = await pipeline.execute_order(session, req)
        assert resp.success is True
        mock_exchange.set_leverage.assert_called_once()


class TestFuturesMarginDeduction:
    """선물 주문 시 notional이 아닌 margin만 차감되는지 확인."""

    @pytest.mark.asyncio
    async def test_futures_open_deducts_margin_not_cost(
        self, pipeline, mock_exchange, session, portfolio_manager,
    ):
        """선물 open: cash에서 margin(100)만 차감, notional(800)이 아님."""
        initial_cash = portfolio_manager.cash_balance  # 500
        mock_exchange.create_market_buy.return_value = make_order_result(
            price=80000.0, filled=0.01, cost=800.0, fee=0.32,
        )
        req = make_request(margin=100.0, quantity=0.01, price=80000.0)

        resp = await pipeline.execute_order(session, req)
        assert resp.success is True
        # margin(100) + fee(0.32) 만 차감되어야 함, cost(800)가 아님
        expected_cash = initial_cash - 100.0 - 0.32
        assert abs(portfolio_manager.cash_balance - expected_cash) < 0.01

    @pytest.mark.asyncio
    async def test_futures_open_total_invested_is_margin(
        self, pipeline, mock_exchange, session, portfolio_manager,
    ):
        """선물 포지션의 total_invested가 margin 기반이어야 함."""
        mock_exchange.create_market_buy.return_value = make_order_result(
            price=80000.0, filled=0.01, cost=800.0, fee=0.32,
        )
        req = make_request(margin=100.0, quantity=0.01, price=80000.0)

        await pipeline.execute_order(session, req)

        from sqlalchemy import select
        pos = (await session.execute(
            select(Position).where(Position.symbol == "BTC/USDT")
        )).scalar_one()
        # total_invested = margin + fee = 100.32, NOT cost + fee = 800.32
        assert pos.total_invested < 200.0  # 확실히 margin 기반
        assert abs(pos.total_invested - (100.0 + 0.32)) < 0.01

    @pytest.mark.asyncio
    async def test_spot_open_deducts_full_cost(self, mock_exchange, session, mock_market_data):
        """현물은 기존대로 full cost 차감."""
        spot_pm = PortfolioManager(
            market_data=mock_market_data,
            initial_balance_krw=1000.0,
            is_paper=False,
            exchange_name="binance_spot",
        )
        guard = BalanceGuard(
            exchange=mock_exchange,
            exchange_name="binance_spot",
        )
        om = OrderManager(
            exchange=mock_exchange,
            is_paper=False,
            exchange_name="binance_spot",
            fee_currency="USDT",
        )
        spot_pipeline = SafeOrderPipeline(
            order_manager=om,
            portfolio_manager=spot_pm,
            balance_guard=guard,
            exchange=mock_exchange,
            leverage=1,
        )

        mock_exchange.create_market_buy.return_value = make_order_result(
            price=100.0, filled=5.0, cost=500.0, fee=0.50,
        )
        req = make_request(margin=500.0, quantity=5.0, price=100.0, leverage=1)

        resp = await spot_pipeline.execute_order(session, req)
        assert resp.success is True
        # 현물: cost(500) + fee(0.50) 차감
        expected_cash = 1000.0 - 500.0 - 0.50
        assert abs(spot_pm.cash_balance - expected_cash) < 0.01


class TestPnLCalculation:
    @pytest.mark.asyncio
    async def test_long_profit_pnl(self, pipeline, mock_exchange, session):
        """롱 수익 PnL 계산."""
        # 포지션 생성
        pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.01,
            average_buy_price=80000.0,
            total_invested=100.0,
            direction="long",
            leverage=3,
            is_paper=False,
        )
        session.add(pos)
        await session.flush()

        mock_exchange.create_market_sell.return_value = make_order_result(
            price=82000.0, filled=0.01, cost=820.0, fee=0.33,
        )

        req = make_request(
            action="close",
            direction=Direction.LONG,
            entry_price=80000.0,
            margin=100.0,
            price=82000.0,
        )

        resp = await pipeline.execute_order(session, req)
        assert resp.success is True

        from sqlalchemy import select
        order = (await session.execute(select(Order))).scalars().first()
        assert order.realized_pnl_pct is not None
        # COIN-65: realized_pnl_pct는 레버리지 미적용 raw 가격변동%.
        # 80000→82000 = 2.5% (3× 레버리지여도 15%가 아님)
        assert order.realized_pnl_pct == pytest.approx(2.5)

    @pytest.mark.asyncio
    async def test_short_profit_pnl(self, pipeline, mock_exchange, session):
        """숏 수익 PnL 계산 — raw 가격변동% (레버리지 미적용)."""
        pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.01,
            average_buy_price=80000.0,
            total_invested=100.0,
            direction="short",
            leverage=3,
            is_paper=False,
        )
        session.add(pos)
        await session.flush()

        mock_exchange.create_market_buy.return_value = make_order_result(
            price=78000.0, filled=0.01, cost=780.0, fee=0.31,
        )

        req = make_request(
            action="close",
            direction=Direction.SHORT,
            entry_price=80000.0,
            margin=100.0,
            price=78000.0,
        )

        resp = await pipeline.execute_order(session, req)
        assert resp.success is True

        from sqlalchemy import select
        order = (await session.execute(select(Order))).scalars().first()
        # COIN-65: 80000→78000 숏 수익 = 2.5% raw (3× 레버리지여도 7.5%가 아님)
        assert order.realized_pnl_pct == pytest.approx(2.5)


class TestQuantityPrecisionAdjustment:
    """COIN-31: BTC 최소 notional $100 보장 테스트."""

    def test_adjust_quantity_no_exchange_precision(self, pipeline):
        """거래소 precision 정보 없으면 원본 반환."""
        result = pipeline._adjust_quantity_for_exchange(
            "BTC/USDT", 0.00125, 84000.0,
        )
        assert result == 0.00125

    def test_adjust_quantity_btc_truncation_below_min(self, pipeline, mock_exchange):
        """BTC qty 0.00125 → 0.001 절삭 시 notional $84 < $100 → 올림."""
        mock_exchange.amount_to_precision = MagicMock(return_value="0.001")
        mock_exchange.market = MagicMock(return_value={
            "precision": {"amount": 3},
        })

        result = pipeline._adjust_quantity_for_exchange(
            "BTC/USDT", 0.00125, 84000.0,
        )
        assert result == 0.002
        assert result * 84000.0 >= MIN_NOTIONAL

    def test_adjust_quantity_btc_sufficient_notional(self, pipeline, mock_exchange):
        """절삭 후에도 notional >= 105 이면 그대로 반환."""
        mock_exchange.amount_to_precision = MagicMock(return_value="0.002")
        mock_exchange.market = MagicMock(return_value={"precision": {"amount": 3}})

        result = pipeline._adjust_quantity_for_exchange(
            "BTC/USDT", 0.0025, 84000.0,
        )
        assert result == 0.002

    def test_adjust_quantity_eth_no_issue(self, pipeline, mock_exchange):
        """ETH는 가격 낮아서 precision 절삭해도 notional 충분."""
        mock_exchange.amount_to_precision = MagicMock(return_value="0.05")
        mock_exchange.market = MagicMock(return_value={"precision": {"amount": 2}})

        result = pipeline._adjust_quantity_for_exchange(
            "ETH/USDT", 0.055, 2200.0,
        )
        assert result == 0.05

    def test_adjust_quantity_precision_as_float_step(self, pipeline, mock_exchange):
        """ccxt precision이 float step_size로 제공되는 경우."""
        mock_exchange.amount_to_precision = MagicMock(return_value="0.001")
        mock_exchange.market = MagicMock(return_value={"precision": {"amount": 0.001}})

        result = pipeline._adjust_quantity_for_exchange(
            "BTC/USDT", 0.00125, 84000.0,
        )
        assert result == 0.002

    def test_adjust_quantity_ceil_float_precision_guard(self, pipeline, mock_exchange):
        """부동소수점 오차로 정확한 배수가 잘못 올림되지 않는지 검증.

        min_qty가 step_size의 정확한 배수일 때
        0.002/0.001 → 2.0000000000000004 같은 오차가 ceil을 3으로 만들면 안 됨.
        """
        # MIN_NOTIONAL=105, price=52500 → min_qty=0.002 (정확히 step_size*2)
        mock_exchange.amount_to_precision = MagicMock(return_value="0.001")
        mock_exchange.market = MagicMock(return_value={"precision": {"amount": 0.001}})

        result = pipeline._adjust_quantity_for_exchange(
            "BTC/USDT", 0.0015, 52500.0,
        )
        # 0.002가 되어야 함 (0.003이 아님)
        assert result == 0.002

    @pytest.mark.asyncio
    async def test_execute_order_adjusts_quantity_and_margin(
        self, pipeline, mock_exchange, session,
    ):
        """주문 실행 시 precision 보정 + margin 갱신 확인."""
        mock_exchange.amount_to_precision = MagicMock(return_value="0.001")
        mock_exchange.market = MagicMock(return_value={"precision": {"amount": 3}})

        mock_exchange.create_market_sell.return_value = make_order_result(
            price=84000.0, filled=0.002, cost=168.0, fee=0.067,
        )

        req = make_request(
            direction=Direction.SHORT,
            margin=35.0,
            quantity=0.00125,
            price=84000.0,
        )

        resp = await pipeline.execute_order(session, req)
        assert resp.success is True
        mock_exchange.create_market_sell.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_order_rejects_when_cash_insufficient_after_adjust(
        self, pipeline, mock_exchange, session, portfolio_manager,
    ):
        """올림 후 margin이 cash 초과 시 거부."""
        # adjusted qty 0.002 * 84000 / leverage 3 = 56.0 > cash 50.0
        portfolio_manager.cash_balance = 50.0

        mock_exchange.amount_to_precision = MagicMock(return_value="0.001")
        mock_exchange.market = MagicMock(return_value={"precision": {"amount": 3}})

        req = make_request(margin=35.0, quantity=0.00125, price=84000.0)

        resp = await pipeline.execute_order(session, req)
        assert resp.success is False
        assert "insufficient_cash_for_min_notional" in resp.error

    @pytest.mark.asyncio
    async def test_close_order_not_adjusted(self, pipeline, mock_exchange, session):
        """청산 주문은 precision 보정 안 함."""
        pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.001,
            average_buy_price=84000.0,
            total_invested=28.0,
            direction="short",
            leverage=3,
            is_paper=False,
        )
        session.add(pos)
        await session.flush()

        mock_exchange.create_market_buy.return_value = make_order_result(
            price=82000.0, filled=0.001, cost=82.0, fee=0.033,
        )

        req = make_request(
            action="close",
            direction=Direction.SHORT,
            quantity=0.001,
            price=82000.0,
            entry_price=84000.0,
            margin=28.0,
        )

        resp = await pipeline.execute_order(session, req)
        assert resp.success is True
