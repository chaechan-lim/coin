"""BalanceGuard 테스트."""
import pytest
from unittest.mock import AsyncMock

from engine.balance_guard import BalanceGuard
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
        assert guard.consecutive_stable == 0

    def test_custom_thresholds(self, mock_exchange):
        g = BalanceGuard(mock_exchange, warn_pct=1.0, pause_pct=2.0)
        assert g._warn_pct == 1.0
        assert g._pause_pct == 2.0

    def test_auto_resume_count_default(self, guard):
        assert guard._auto_resume_count == 3

    def test_auto_resume_count_custom(self, mock_exchange):
        g = BalanceGuard(mock_exchange, auto_resume_count=5)
        assert g._auto_resume_count == 5


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
        assert guard._consecutive_stable == 0

    @pytest.mark.asyncio
    async def test_resume_with_reason(self, guard, mock_exchange):
        """사유 포함 재개."""
        guard._paused = True
        guard.resume(reason="admin_api")
        assert guard.is_paused is False

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


class TestAutoRecovery:
    """자동 복구 메커니즘 테스트."""

    @pytest.mark.asyncio
    async def test_auto_resume_after_3_stable(self, mock_exchange):
        """일시 정지 후 3회 연속 안정 → 자동 복구."""
        guard = BalanceGuard(
            mock_exchange, warn_pct=3.0, pause_pct=5.0, auto_resume_count=3,
        )
        # 위험 수준으로 일시 정지
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=90.0, used=0.0, total=90.0),
        }
        await guard.check_balance(100.0)
        assert guard.is_paused is True

        # 거래소 잔고 정상 복구
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }

        # 1회차 안정 — 아직 일시 정지
        await guard.check_balance(100.0)
        assert guard.is_paused is True
        assert guard.consecutive_stable == 1

        # 2회차 안정 — 아직 일시 정지
        await guard.check_balance(100.0)
        assert guard.is_paused is True
        assert guard.consecutive_stable == 2

        # 3회차 안정 → 자동 복구
        await guard.check_balance(100.0)
        assert guard.is_paused is False
        assert guard.consecutive_stable == 0  # resume()이 리셋

    @pytest.mark.asyncio
    async def test_auto_resume_reset_on_warning(self, mock_exchange):
        """안정 카운터가 경고 시 리셋."""
        guard = BalanceGuard(
            mock_exchange, warn_pct=3.0, pause_pct=5.0, auto_resume_count=3,
        )
        # 위험 수준으로 일시 정지
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=90.0, used=0.0, total=90.0),
        }
        await guard.check_balance(100.0)
        assert guard.is_paused is True

        # 1회 안정
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }
        await guard.check_balance(100.0)
        assert guard.consecutive_stable == 1

        # 다시 경고 → 안정 카운터 리셋
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=96.0, used=0.0, total=96.0),
        }
        await guard.check_balance(100.0)
        assert guard._consecutive_stable == 0
        assert guard.is_paused is True

    @pytest.mark.asyncio
    async def test_auto_resume_reset_on_critical(self, mock_exchange):
        """안정 카운터가 위험 수준 시 리셋."""
        guard = BalanceGuard(
            mock_exchange, warn_pct=3.0, pause_pct=5.0, auto_resume_count=3,
        )
        guard._paused = True

        # 1회 안정
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }
        await guard.check_balance(100.0)
        assert guard.consecutive_stable == 1

        # 다시 위험 → 안정 카운터 리셋
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=80.0, used=0.0, total=80.0),
        }
        await guard.check_balance(100.0)
        assert guard._consecutive_stable == 0
        assert guard.is_paused is True

    @pytest.mark.asyncio
    async def test_auto_resume_disabled(self, mock_exchange):
        """auto_resume_count=0 → 자동 복구 비활성."""
        guard = BalanceGuard(
            mock_exchange, warn_pct=3.0, pause_pct=5.0, auto_resume_count=0,
        )
        # 위험 수준으로 일시 정지
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=90.0, used=0.0, total=90.0),
        }
        await guard.check_balance(100.0)
        assert guard.is_paused is True

        # 정상 복구 후 10회 안정 → 여전히 일시 정지
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }
        for _ in range(10):
            await guard.check_balance(100.0)
        assert guard.is_paused is True
        assert guard.consecutive_stable == 10

    @pytest.mark.asyncio
    async def test_stable_counter_not_increment_when_not_paused(self, guard, mock_exchange):
        """일시 정지가 아닐 때는 안정 카운터 증가 안 함."""
        assert guard.is_paused is False
        result = await guard.check_balance(100.0)
        assert result.is_warning is False
        assert guard.consecutive_stable == 0

    @pytest.mark.asyncio
    async def test_auto_resume_custom_count(self, mock_exchange):
        """auto_resume_count=5 → 5회 연속 안정 후 복구."""
        guard = BalanceGuard(
            mock_exchange, warn_pct=3.0, pause_pct=5.0, auto_resume_count=5,
        )
        guard._paused = True

        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }

        # 4회 안정 — 아직 정지
        for _ in range(4):
            await guard.check_balance(100.0)
        assert guard.is_paused is True
        assert guard.consecutive_stable == 4

        # 5회차 → 자동 복구
        await guard.check_balance(100.0)
        assert guard.is_paused is False

    @pytest.mark.asyncio
    async def test_auto_resume_from_3_warning_pause(self, mock_exchange):
        """3회 연속 경고로 정지 → 자동 복구."""
        guard = BalanceGuard(
            mock_exchange, warn_pct=3.0, pause_pct=5.0, auto_resume_count=3,
        )
        # 3회 연속 경고 → 자동 정지
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=96.5, used=0.0, total=96.5),
        }
        for _ in range(3):
            await guard.check_balance(100.0)
        assert guard.is_paused is True

        # 정상 복구
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }
        for _ in range(3):
            await guard.check_balance(100.0)
        assert guard.is_paused is False


class TestGetStatus:
    """get_status() 테스트."""

    def test_status_no_check(self, guard):
        """체크 전 상태."""
        status = guard.get_status()
        assert status["paused"] is False
        assert status["consecutive_warnings"] == 0
        assert status["consecutive_stable"] == 0
        assert status["auto_resume_count"] == 3
        assert status["warn_pct"] == 3.0
        assert status["pause_pct"] == 5.0
        assert status["last_check"] is None

    @pytest.mark.asyncio
    async def test_status_after_check(self, guard):
        """체크 후 상태."""
        await guard.check_balance(100.0)
        status = guard.get_status()
        assert status["last_check"] is not None
        assert "exchange_balance" in status["last_check"]
        assert "internal_balance" in status["last_check"]
        assert "divergence_pct" in status["last_check"]
        assert "checked_at" in status["last_check"]

    @pytest.mark.asyncio
    async def test_status_paused(self, guard, mock_exchange):
        """일시 정지 상태."""
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=80.0, used=0.0, total=80.0),
        }
        await guard.check_balance(100.0)
        status = guard.get_status()
        assert status["paused"] is True
        assert status["consecutive_warnings"] == 1
