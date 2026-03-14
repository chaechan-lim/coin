"""BalanceGuard 테스트."""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

from engine.balance_guard import BalanceGuard, BalanceCheckResult
from exchange.data_models import Balance


@pytest.fixture
def mock_exchange():
    exchange = AsyncMock()
    exchange.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=100.0, used=50.0, total=150.0),
    })
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


class TestBalanceGuardInit:
    def test_initial_state(self, guard):
        assert guard.is_paused is False
        assert guard.last_check is None

    def test_custom_thresholds(self, mock_exchange):
        g = BalanceGuard(mock_exchange, warn_pct=1.0, pause_pct=2.0)
        assert g._warn_pct == 1.0
        assert g._pause_pct == 2.0


class TestCheckBalance:
    @pytest.mark.asyncio
    async def test_no_divergence(self, guard):
        """괴리가 없으면 정상."""
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
        result = await guard.check_balance(100.0)
        assert result.is_critical is True
        assert guard.is_paused is True

    @pytest.mark.asyncio
    async def test_three_consecutive_warnings_pause(self, guard, mock_exchange):
        """3회 연속 경고 → 자동 정지."""
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=96.5, used=0.0, total=96.5),
        }
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
        await guard.check_balance(100.0)
        assert guard.is_paused is True

        guard.resume()
        assert guard.is_paused is False
        assert guard._consecutive_warnings == 0

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_zero(self, guard, mock_exchange):
        """거래소 연결 실패 시 잔고 0으로 처리."""
        mock_exchange.fetch_balance.side_effect = Exception("connection error")
        result = await guard.check_balance(100.0)
        assert result.exchange_balance == 0.0
        assert result.is_critical is True


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
