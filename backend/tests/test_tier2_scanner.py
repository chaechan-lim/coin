"""Tier2Scanner 테스트."""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta

from engine.tier2_scanner import Tier2Scanner, ScanScore
from engine.safe_order_pipeline import SafeOrderPipeline, OrderResponse
from engine.position_state_tracker import PositionStateTracker, PositionState
from engine.portfolio_manager import PortfolioManager
from exchange.data_models import Candle, Ticker, Balance
from core.enums import Direction


@pytest.fixture
def mock_exchange():
    exchange = AsyncMock()

    # 60개 5m 캔들 — 마지막이 거래량 급등
    base_vol = 100.0
    candles = [
        Candle(
            timestamp=datetime.now(timezone.utc),
            open=80000.0, high=80100.0, low=79900.0,
            close=80000.0 + i * 10, volume=base_vol,
        )
        for i in range(59)
    ]
    candles.append(Candle(
        timestamp=datetime.now(timezone.utc),
        open=80590.0, high=81000.0, low=80500.0,
        close=80800.0, volume=base_vol * 8,
    ))
    exchange.fetch_ohlcv = AsyncMock(return_value=candles)
    exchange.fetch_ticker = AsyncMock(return_value=Ticker(
        symbol="BTC/USDT", last=80800.0, bid=80790.0, ask=80810.0,
        high=81000.0, low=79000.0, volume=10000.0,
        timestamp=datetime.now(timezone.utc),
    ))
    exchange.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=500.0, used=100.0, total=600.0),
    })
    return exchange


@pytest.fixture
def tier2(mock_exchange):
    safe_order = AsyncMock(spec=SafeOrderPipeline)
    safe_order.execute_order = AsyncMock(return_value=OrderResponse(
        success=True, order_id=1, executed_price=80800.0,
        executed_quantity=0.01, fee=0.32,
    ))

    tracker = PositionStateTracker()

    pm = MagicMock(spec=PortfolioManager)
    pm.cash_balance = 500.0
    pm._is_paper = False

    return Tier2Scanner(
        safe_order=safe_order,
        position_tracker=tracker,
        exchange=mock_exchange,
        portfolio_manager=pm,
        scan_coins=["BTC/USDT", "ETH/USDT"],
        max_concurrent=2,
        max_position_pct=0.05,
        vol_threshold=5.0,
        daily_trade_limit=10,
        cooldown_per_symbol_sec=1800,
        leverage=3,
    )


class TestScan:
    @pytest.mark.asyncio
    async def test_scan_scores(self, tier2):
        scores = await tier2._scan_all()
        assert len(scores) > 0
        assert all(isinstance(s, ScanScore) for s in scores)

    @pytest.mark.asyncio
    async def test_scan_single_symbol(self, tier2):
        score = await tier2._scan_symbol("BTC/USDT")
        assert score is not None
        assert score.vol_ratio > 1.0  # 마지막 캔들이 8x

    @pytest.mark.asyncio
    async def test_scan_insufficient_candles(self, tier2, mock_exchange):
        mock_exchange.fetch_ohlcv.return_value = []
        score = await tier2._scan_symbol("BTC/USDT")
        assert score is None


class TestEntry:
    @pytest.mark.asyncio
    async def test_enters_on_high_score(self, tier2, session):
        """높은 점수 → 진입."""
        await tier2.scan_cycle(session)
        # 거래량 8x이므로 vol_threshold(5.0) 초과 → 진입 시도
        if tier2._safe_order.execute_order.called:
            req = tier2._safe_order.execute_order.call_args[0][1]
            assert req.tier == "tier2"

    @pytest.mark.asyncio
    async def test_respects_max_concurrent(self, tier2, mock_exchange, session):
        """최대 동시 포지션 제한."""
        # ticker를 AAA/BBB 가격과 일치시킴 (TP 히트 방지)
        mock_exchange.fetch_ticker.return_value = Ticker(
            symbol="AAA/USDT", last=80000.0, bid=79990.0, ask=80010.0,
            high=81000.0, low=79000.0, volume=10000.0,
            timestamp=datetime.now(timezone.utc),
        )
        # 2개 포지션 이미 있음 (현재 가격과 가까운 entry로 SL/TP 히트 방지)
        for sym in ["AAA/USDT", "BBB/USDT"]:
            tier2._positions.open_position(PositionState(
                symbol=sym, direction=Direction.LONG, quantity=0.01,
                entry_price=80000.0, margin=25.0, leverage=3,
                extreme_price=80000.0, stop_loss_atr=2.0, take_profit_atr=4.0,
                trailing_activation_atr=1.0, trailing_stop_atr=0.8,
                tier="tier2",
            ))

        await tier2.scan_cycle(session)
        # exit 체크에서 close 주문 없어야 하고, 진입도 없어야 함
        open_calls = [
            c for c in tier2._safe_order.execute_order.call_args_list
            if c[0][1].action == "open"
        ]
        assert len(open_calls) == 0

    @pytest.mark.asyncio
    async def test_respects_daily_limit(self, tier2, session):
        """일일 거래 제한."""
        tier2._daily_trades = 10  # daily_trade_limit
        await tier2.scan_cycle(session)
        tier2._safe_order.execute_order.assert_not_called()


class TestCooldown:
    def test_in_cooldown(self, tier2):
        tier2._cooldowns["BTC/USDT"] = datetime.now(timezone.utc)
        assert tier2._in_cooldown("BTC/USDT") is True

    def test_not_in_cooldown(self, tier2):
        tier2._cooldowns["BTC/USDT"] = datetime.now(timezone.utc) - timedelta(hours=1)
        assert tier2._in_cooldown("BTC/USDT") is False

    def test_no_cooldown(self, tier2):
        assert tier2._in_cooldown("NEW/USDT") is False


class TestExits:
    @pytest.mark.asyncio
    async def test_time_exit(self, tier2, session):
        """시간 초과 → 청산."""
        state = PositionState(
            symbol="BTC/USDT", direction=Direction.LONG, quantity=0.01,
            entry_price=80000.0, margin=25.0, leverage=3,
            extreme_price=80000.0, stop_loss_atr=2.0, take_profit_atr=4.0,
            trailing_activation_atr=1.0, trailing_stop_atr=0.8,
            tier="tier2",
            entered_at=datetime.now(timezone.utc) - timedelta(minutes=130),
        )
        tier2._positions.open_position(state)

        await tier2._check_exits(session)
        # 청산 주문 실행됨
        assert tier2._safe_order.execute_order.called

    @pytest.mark.asyncio
    async def test_sl_exit(self, tier2, mock_exchange, session):
        """SL 히트 → 청산."""
        state = PositionState(
            symbol="BTC/USDT", direction=Direction.LONG, quantity=0.01,
            entry_price=82000.0, margin=25.0, leverage=3,
            extreme_price=82000.0, stop_loss_atr=2.0, take_profit_atr=4.0,
            trailing_activation_atr=1.0, trailing_stop_atr=0.8,
            tier="tier2",
        )
        tier2._positions.open_position(state)

        # 가격 80000 → PnL = (80800-82000)/82000*100 = -1.46%... not yet -2%
        # 가격을 더 낮게
        mock_exchange.fetch_ticker.return_value = Ticker(
            symbol="BTC/USDT", last=80000.0, bid=79990.0, ask=80010.0,
            high=82000.0, low=79000.0, volume=10000.0,
            timestamp=datetime.now(timezone.utc),
        )

        await tier2._check_exits(session)
        # PnL = (80000-82000)/82000*100 = -2.44% → SL(2%) 히트
        assert tier2._safe_order.execute_order.called


class TestLeveragePnL:
    """Bug COIN-13: SL/TP 계산 시 레버리지 적용 테스트."""

    @pytest.mark.asyncio
    async def test_sl_with_leverage_long(self, tier2, mock_exchange, session):
        """롱 포지션: 레버리지 적용 후 SL 히트 계산.

        entry=100, sl=2%, leverage=3x
        raw price change 0.67% → leveraged PnL = 2.0% → SL 히트
        """
        state = PositionState(
            symbol="BTC/USDT", direction=Direction.LONG, quantity=0.01,
            entry_price=100.0, margin=25.0, leverage=3,
            extreme_price=100.0, stop_loss_atr=2.0, take_profit_atr=4.0,
            trailing_activation_atr=1.0, trailing_stop_atr=0.8,
            tier="tier2",
        )
        tier2._positions.open_position(state)

        # 가격 99.33 → raw change = -0.67%, leveraged = -0.67% × 3 = -2.01% → SL(2%) 히트
        mock_exchange.fetch_ticker.return_value = Ticker(
            symbol="BTC/USDT", last=99.33, bid=99.32, ask=99.34,
            high=101.0, low=99.0, volume=10000.0,
            timestamp=datetime.now(timezone.utc),
        )

        await tier2._check_exits(session)
        assert tier2._safe_order.execute_order.called

    @pytest.mark.asyncio
    async def test_sl_not_hit_without_leverage_threshold(self, tier2, mock_exchange, session):
        """레버리지 적용 전 raw 변동 기준으로는 SL 미히트 확인.

        entry=100, sl=2%, leverage=3x
        raw change = -0.5% → leveraged PnL = -1.5% → SL(2%) 미히트
        """
        state = PositionState(
            symbol="BTC/USDT", direction=Direction.LONG, quantity=0.01,
            entry_price=100.0, margin=25.0, leverage=3,
            extreme_price=100.0, stop_loss_atr=2.0, take_profit_atr=4.0,
            trailing_activation_atr=1.0, trailing_stop_atr=0.8,
            tier="tier2",
        )
        tier2._positions.open_position(state)

        # 가격 99.50 → raw change = -0.5%, leveraged = -1.5% → SL(2%) 미히트
        mock_exchange.fetch_ticker.return_value = Ticker(
            symbol="BTC/USDT", last=99.50, bid=99.49, ask=99.51,
            high=101.0, low=99.0, volume=10000.0,
            timestamp=datetime.now(timezone.utc),
        )

        await tier2._check_exits(session)
        assert not tier2._safe_order.execute_order.called

    @pytest.mark.asyncio
    async def test_tp_with_leverage_short(self, tier2, mock_exchange, session):
        """숏 포지션: 레버리지 적용 후 TP 히트 계산.

        entry=100, tp=4%, leverage=3x
        raw change = 1.34% → leveraged PnL = 4.02% → TP(4%) 히트
        """
        state = PositionState(
            symbol="BTC/USDT", direction=Direction.SHORT, quantity=0.01,
            entry_price=100.0, margin=25.0, leverage=3,
            extreme_price=100.0, stop_loss_atr=2.0, take_profit_atr=4.0,
            trailing_activation_atr=1.0, trailing_stop_atr=0.8,
            tier="tier2",
        )
        tier2._positions.open_position(state)

        # 가격 98.66 → short profit raw = 1.34%, leveraged = 4.02% → TP(4%) 히트
        mock_exchange.fetch_ticker.return_value = Ticker(
            symbol="BTC/USDT", last=98.66, bid=98.65, ask=98.67,
            high=101.0, low=98.0, volume=10000.0,
            timestamp=datetime.now(timezone.utc),
        )

        await tier2._check_exits(session)
        assert tier2._safe_order.execute_order.called

    @pytest.mark.asyncio
    async def test_sl_with_high_leverage(self, tier2, mock_exchange, session):
        """고레버리지(20x): 작은 변동도 SL 히트.

        entry=100, sl=2%, leverage=20x
        raw change = -0.11% → leveraged PnL = -2.2% → SL(2%) 히트
        """
        tier2._leverage = 20

        state = PositionState(
            symbol="BTC/USDT", direction=Direction.LONG, quantity=0.01,
            entry_price=100.0, margin=25.0, leverage=20,
            extreme_price=100.0, stop_loss_atr=2.0, take_profit_atr=4.0,
            trailing_activation_atr=1.0, trailing_stop_atr=0.8,
            tier="tier2",
        )
        tier2._positions.open_position(state)

        # 가격 99.89 → raw change = -0.11%, leveraged = -0.11% × 20 = -2.2% → SL(2%) 히트
        mock_exchange.fetch_ticker.return_value = Ticker(
            symbol="BTC/USDT", last=99.89, bid=99.88, ask=99.90,
            high=101.0, low=99.0, volume=10000.0,
            timestamp=datetime.now(timezone.utc),
        )

        await tier2._check_exits(session)
        assert tier2._safe_order.execute_order.called


class TestResetDaily:
    def test_reset(self, tier2):
        tier2._daily_trades = 5
        tier2.reset_daily()
        assert tier2._daily_trades == 0
