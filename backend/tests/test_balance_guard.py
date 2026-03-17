"""BalanceGuard 테스트."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from engine.balance_guard import BalanceGuard
from exchange.data_models import Balance


@pytest.fixture
def mock_exchange():
    exchange = AsyncMock()
    exchange.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=100.0, used=50.0, total=150.0),
    })
    # 선물 포지션 조회용 내부 exchange mock
    exchange._exchange = AsyncMock()
    exchange._exchange.fetch_positions = AsyncMock(return_value=[])
    return exchange


@pytest.fixture
def guard(mock_exchange):
    return BalanceGuard(
        exchange=mock_exchange,
        exchange_name="binance_futures",
        warn_pct=3.0,
        pause_pct=5.0,
        snapshot_spike_pct=10.0,
    )


@pytest.fixture
def spot_guard(mock_exchange):
    """현물 BalanceGuard."""
    return BalanceGuard(
        exchange=mock_exchange,
        exchange_name="binance_spot",
        warn_pct=3.0,
        pause_pct=5.0,
        snapshot_spike_pct=10.0,
    )


class TestBalanceGuardInit:
    def test_initial_state(self, guard):
        assert guard.is_paused is False
        assert guard.last_check is None

    def test_custom_thresholds(self, mock_exchange):
        g = BalanceGuard(mock_exchange, warn_pct=1.0, pause_pct=2.0)
        assert g._warn_pct == 1.0
        assert g._pause_pct == 2.0

    def test_is_futures(self, guard, spot_guard):
        assert guard.is_futures is True
        assert spot_guard.is_futures is False

    def test_auto_resync_count_default(self, mock_exchange):
        g = BalanceGuard(mock_exchange)
        assert g._auto_resync_count == 5

    def test_auto_resync_count_custom(self, mock_exchange):
        g = BalanceGuard(mock_exchange, auto_resync_count=10)
        assert g._auto_resync_count == 10

    def test_set_portfolio_manager(self, guard):
        pm = MagicMock()
        guard.set_portfolio_manager(pm)
        assert guard._portfolio_manager is pm


class TestFuturesBalanceCalc:
    """선물 잔고 계산 방식 검증 — walletBalance 기반."""

    @pytest.mark.asyncio
    async def test_futures_balance_uses_wallet_minus_margin(self, guard, mock_exchange):
        """선물: total(walletBalance) - unrealizedPnL - totalMargin."""
        # total=300 (walletBalance=280, unrealizedPnL=20)
        # positions: margin=40, unrealizedPnL=20
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=240.0, used=60.0, total=300.0),
        }
        mock_exchange._exchange.fetch_positions.return_value = [
            {"contracts": 1.0, "initialMargin": 40.0, "unrealizedPnl": 20.0},
        ]

        balance = await guard._fetch_exchange_balance()
        # wallet = 300 - 20 = 280
        # cash = 280 - 40 = 240
        assert balance == 240.0

    @pytest.mark.asyncio
    async def test_futures_balance_multiple_positions(self, guard, mock_exchange):
        """선물: 여러 포지션의 마진과 unrealizedPnL 합산."""
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=200.0, used=100.0, total=350.0),
        }
        mock_exchange._exchange.fetch_positions.return_value = [
            {"contracts": 1.0, "initialMargin": 30.0, "unrealizedPnl": 10.0},
            {"contracts": 2.0, "initialMargin": 50.0, "unrealizedPnl": -5.0},
            {"contracts": 0, "initialMargin": 20.0, "unrealizedPnl": 0.0},  # 빈 포지션
        ]

        balance = await guard._fetch_exchange_balance()
        # wallet = 350 - (10 + (-5)) = 350 - 5 = 345
        # cash = 345 - (30 + 50) = 345 - 80 = 265
        assert balance == 265.0

    @pytest.mark.asyncio
    async def test_futures_balance_no_positions(self, guard, mock_exchange):
        """선물: 포지션 없으면 total = cash."""
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=200.0, used=0.0, total=200.0),
        }
        mock_exchange._exchange.fetch_positions.return_value = []

        balance = await guard._fetch_exchange_balance()
        # wallet = 200 - 0 = 200, cash = 200 - 0 = 200
        assert balance == 200.0

    @pytest.mark.asyncio
    async def test_futures_balance_negative_unrealized(self, guard, mock_exchange):
        """선물: 미실현 손실 시 wallet이 total보다 큼."""
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=180.0, used=50.0, total=220.0),
        }
        mock_exchange._exchange.fetch_positions.return_value = [
            {"contracts": 1.0, "initialMargin": 50.0, "unrealizedPnl": -30.0},
        ]

        balance = await guard._fetch_exchange_balance()
        # wallet = 220 - (-30) = 250
        # cash = 250 - 50 = 200
        assert balance == 200.0

    @pytest.mark.asyncio
    async def test_futures_balance_position_fetch_fallback(self, guard, mock_exchange):
        """선물: 포지션 조회 실패 시 free로 폴백."""
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=50.0, total=150.0),
        }
        mock_exchange._exchange.fetch_positions.side_effect = Exception("API error")

        balance = await guard._fetch_exchange_balance()
        # 폴백: free 값 사용
        assert balance == 100.0

    @pytest.mark.asyncio
    async def test_futures_matches_pm_init_logic(self, guard, mock_exchange):
        """선물: BalanceGuard와 PortfolioManager.initialize_cash_from_exchange() 동일 계산."""
        # PM의 초기화 로직과 완전히 동일한 입력/출력 검증
        # PM: wallet = cash_bal.total - total_unrealized, cash = wallet - total_margin
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=220.0, used=80.0, total=300.0),
        }
        mock_exchange._exchange.fetch_positions.return_value = [
            {"contracts": 1.0, "initialMargin": 40.0, "unrealizedPnl": 20.0},
        ]

        balance = await guard._fetch_exchange_balance()
        # PM 계산: wallet = 300 - 20 = 280, cash = 280 - 40 = 240
        assert balance == 240.0


class TestSpotBalanceCalc:
    """현물 잔고 계산 — 기존 free 방식 유지."""

    @pytest.mark.asyncio
    async def test_spot_uses_free(self, spot_guard, mock_exchange):
        """현물: USDT.free 사용."""
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=50.0, total=150.0),
        }

        balance = await spot_guard._fetch_exchange_balance()
        assert balance == 100.0

    @pytest.mark.asyncio
    async def test_spot_krw_fallback(self, spot_guard, mock_exchange):
        """현물: KRW fallback."""
        mock_exchange.fetch_balance.return_value = {
            "KRW": Balance(currency="KRW", free=500000.0, used=0.0, total=500000.0),
        }

        balance = await spot_guard._fetch_exchange_balance()
        assert balance == 500000.0


class TestCheckBalance:
    @pytest.mark.asyncio
    async def test_no_divergence(self, guard, mock_exchange):
        """괴리가 없으면 정상."""
        # 선물: 포지션 없이 total = 100
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }
        mock_exchange._exchange.fetch_positions.return_value = []

        result = await guard.check_balance(100.0)
        assert result.is_warning is False
        assert result.is_critical is False
        assert result.divergence_pct < 3.0
        assert guard.is_paused is False

    @pytest.mark.asyncio
    async def test_warning_divergence(self, guard, mock_exchange):
        """3~5% 괴리 → 경고."""
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=96.0, used=0.0, total=96.0),
        }
        mock_exchange._exchange.fetch_positions.return_value = []

        result = await guard.check_balance(100.0)
        assert result.is_warning is True
        assert result.is_critical is False
        assert guard.is_paused is False

    @pytest.mark.asyncio
    async def test_critical_divergence_pauses(self, guard, mock_exchange):
        """5%+ 괴리 → 엔진 정지."""
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=90.0, used=0.0, total=90.0),
        }
        mock_exchange._exchange.fetch_positions.return_value = []

        result = await guard.check_balance(100.0)
        assert result.is_critical is True
        assert guard.is_paused is True

    @pytest.mark.asyncio
    async def test_three_consecutive_warnings_pause(self, guard, mock_exchange):
        """3회 연속 경고 → 자동 정지."""
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=96.5, used=0.0, total=96.5),
        }
        mock_exchange._exchange.fetch_positions.return_value = []

        await guard.check_balance(100.0)
        assert guard.is_paused is False
        await guard.check_balance(100.0)
        assert guard.is_paused is False
        await guard.check_balance(100.0)
        assert guard.is_paused is True

    @pytest.mark.asyncio
    async def test_warning_reset_on_normal(self, guard, mock_exchange):
        """정상 체크 후 경고 카운터 리셋."""
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=96.5, used=0.0, total=96.5),
        }
        mock_exchange._exchange.fetch_positions.return_value = []

        await guard.check_balance(100.0)
        await guard.check_balance(100.0)
        assert guard._consecutive_warnings == 2

        # 정상으로 돌아옴
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }
        await guard.check_balance(100.0)
        assert guard._consecutive_warnings == 0

    @pytest.mark.asyncio
    async def test_resume(self, guard, mock_exchange):
        """수동 재개."""
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=80.0, used=0.0, total=80.0),
        }
        mock_exchange._exchange.fetch_positions.return_value = []

        await guard.check_balance(100.0)
        assert guard.is_paused is True

        guard.resume()
        assert guard.is_paused is False
        assert guard._consecutive_warnings == 0
        assert guard._consecutive_criticals == 0

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_zero(self, guard, mock_exchange):
        """거래소 연결 실패 시 잔고 0으로 처리."""
        mock_exchange.fetch_balance.side_effect = Exception("connection error")
        result = await guard.check_balance(100.0)
        assert result.exchange_balance == 0.0
        assert result.is_critical is True


class TestAutoResync:
    """자동 재동기화 메커니즘 검증."""

    @pytest.mark.asyncio
    async def test_auto_resync_after_n_criticals(self, mock_exchange):
        """N회 연속 critical 후 자동 재동기화."""
        pm = MagicMock()
        pm.cash_balance = 188.0  # 내부 장부 (오래된 값)
        pm.initialize_cash_from_exchange = AsyncMock()

        guard = BalanceGuard(
            exchange=mock_exchange,
            exchange_name="binance_futures",
            pause_pct=5.0,
            auto_resync_count=3,  # 3회로 설정
            portfolio_manager=pm,
        )

        # 거래소: 236 USDT (내부와 20%+ 괴리)
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=236.0, used=0.0, total=236.0),
        }
        mock_exchange._exchange.fetch_positions.return_value = []

        # 1회 critical
        await guard.check_balance(188.0)
        assert guard.is_paused is True
        assert guard._consecutive_criticals == 1
        pm.initialize_cash_from_exchange.assert_not_called()

        guard._paused = False  # 수동 resume 시뮬레이션
        # 2회 critical
        await guard.check_balance(188.0)
        assert guard._consecutive_criticals == 2
        pm.initialize_cash_from_exchange.assert_not_called()

        guard._paused = False
        # 3회 critical → 자동 resync
        # resync 후 PM cash가 업데이트됨
        pm.cash_balance = 236.0
        result = await guard.check_balance(188.0)

        assert result.resynced is True
        assert guard.is_paused is False  # resync 후 자동 재개
        assert guard._consecutive_criticals == 0
        pm.initialize_cash_from_exchange.assert_called_once_with(mock_exchange)

    @pytest.mark.asyncio
    async def test_auto_resync_disabled(self, mock_exchange):
        """auto_resync_count=0이면 자동 재동기화 비활성."""
        pm = MagicMock()
        pm.cash_balance = 188.0
        pm.initialize_cash_from_exchange = AsyncMock()

        guard = BalanceGuard(
            exchange=mock_exchange,
            exchange_name="binance_futures",
            pause_pct=5.0,
            auto_resync_count=0,  # 비활성
            portfolio_manager=pm,
        )

        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=236.0, used=0.0, total=236.0),
        }
        mock_exchange._exchange.fetch_positions.return_value = []

        for _ in range(10):
            guard._paused = False
            await guard.check_balance(188.0)

        pm.initialize_cash_from_exchange.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_resync_no_pm(self, mock_exchange):
        """PM이 없으면 자동 재동기화 불가."""
        guard = BalanceGuard(
            exchange=mock_exchange,
            exchange_name="binance_futures",
            pause_pct=5.0,
            auto_resync_count=1,  # 즉시 resync 시도
            portfolio_manager=None,
        )

        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=236.0, used=0.0, total=236.0),
        }
        mock_exchange._exchange.fetch_positions.return_value = []

        result = await guard.check_balance(188.0)
        assert result.resynced is False
        assert guard.is_paused is True

    @pytest.mark.asyncio
    async def test_auto_resync_resets_counters(self, mock_exchange):
        """재동기화 후 연속 카운터 리셋."""
        pm = MagicMock()
        pm.cash_balance = 236.0
        pm.initialize_cash_from_exchange = AsyncMock()

        guard = BalanceGuard(
            exchange=mock_exchange,
            exchange_name="binance_futures",
            pause_pct=5.0,
            auto_resync_count=1,
            portfolio_manager=pm,
        )

        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=236.0, used=0.0, total=236.0),
        }
        mock_exchange._exchange.fetch_positions.return_value = []

        result = await guard.check_balance(188.0)
        assert result.resynced is True
        assert guard._consecutive_criticals == 0
        assert guard._consecutive_warnings == 0
        assert guard.is_paused is False

    @pytest.mark.asyncio
    async def test_auto_resync_failure(self, mock_exchange):
        """재동기화 실패 시 paused 유지."""
        pm = MagicMock()
        pm.cash_balance = 188.0
        pm.initialize_cash_from_exchange = AsyncMock(side_effect=Exception("resync failed"))

        guard = BalanceGuard(
            exchange=mock_exchange,
            exchange_name="binance_futures",
            pause_pct=5.0,
            auto_resync_count=1,
            portfolio_manager=pm,
        )

        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=236.0, used=0.0, total=236.0),
        }
        mock_exchange._exchange.fetch_positions.return_value = []

        result = await guard.check_balance(188.0)
        assert result.resynced is False
        assert guard.is_paused is True

    @pytest.mark.asyncio
    async def test_auto_resync_spot(self, mock_exchange):
        """현물: 자동 재동기화 시 free 값으로 직접 설정."""
        pm = MagicMock()
        pm.cash_balance = 900.0  # 오래된 값

        guard = BalanceGuard(
            exchange=mock_exchange,
            exchange_name="binance_spot",
            pause_pct=5.0,
            auto_resync_count=1,
            portfolio_manager=pm,
        )

        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=1000.0, used=0.0, total=1000.0),
        }

        result = await guard.check_balance(900.0)
        assert result.resynced is True
        assert pm._cash_balance == 1000.0
        assert guard.is_paused is False

    @pytest.mark.asyncio
    async def test_consecutive_criticals_reset_on_normal(self, guard, mock_exchange):
        """정상 체크 시 연속 critical 카운터도 리셋."""
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=80.0, used=0.0, total=80.0),
        }
        mock_exchange._exchange.fetch_positions.return_value = []

        await guard.check_balance(100.0)
        assert guard._consecutive_criticals == 1
        guard._paused = False

        # 정상으로 돌아옴
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }
        await guard.check_balance(100.0)
        assert guard._consecutive_criticals == 0

    @pytest.mark.asyncio
    async def test_consecutive_criticals_reset_on_warning(self, guard, mock_exchange):
        """경고 수준으로 내려가면 연속 critical 카운터 리셋."""
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=80.0, used=0.0, total=80.0),
        }
        mock_exchange._exchange.fetch_positions.return_value = []

        await guard.check_balance(100.0)
        assert guard._consecutive_criticals == 1
        guard._paused = False

        # 경고 수준으로 개선
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=96.5, used=0.0, total=96.5),
        }
        await guard.check_balance(100.0)
        assert guard._consecutive_criticals == 0


class TestSnapshotValidation:
    def test_normal_change(self, guard):
        assert guard.validate_snapshot(105.0, 100.0) is True

    def test_spike_rejected(self, guard):
        assert guard.validate_snapshot(115.0, 100.0) is False

    def test_none_last_total(self, guard):
        assert guard.validate_snapshot(100.0, None) is True

    def test_zero_last_total(self, guard):
        assert guard.validate_snapshot(100.0, 0.0) is True

    def test_negative_change(self, guard):
        """큰 하락도 스파이크로 감지."""
        assert guard.validate_snapshot(85.0, 100.0) is False


class TestOrderValidation:
    def test_pre_valid(self, guard):
        ok, reason = guard.validate_order_pre(100.0, 30.0)
        assert ok is True

    def test_pre_insufficient_cash(self, guard):
        ok, reason = guard.validate_order_pre(20.0, 30.0)
        assert ok is False
        assert "insufficient_cash" in reason

    def test_pre_paused(self, guard):
        guard._paused = True
        ok, reason = guard.validate_order_pre(100.0, 30.0)
        assert ok is False
        assert reason == "balance_guard_paused"

    def test_pre_invalid_cost(self, guard):
        ok, reason = guard.validate_order_pre(100.0, 0.0)
        assert ok is False

    def test_post_normal(self, guard):
        ok, reason = guard.validate_order_post(100.0, 70.0, 30.0)
        assert ok is True

    def test_post_slippage(self, guard):
        ok, reason = guard.validate_order_post(100.0, 60.0, 30.0)
        assert ok is False
        assert "slippage" in reason
