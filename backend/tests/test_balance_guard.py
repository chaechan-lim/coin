"""BalanceGuard 테스트."""
import pytest
from unittest.mock import AsyncMock, patch

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

    def test_custom_thresholds(self, mock_exchange):
        g = BalanceGuard(mock_exchange, warn_pct=1.0, pause_pct=2.0)
        assert g._warn_pct == 1.0
        assert g._pause_pct == 2.0

    def test_auto_resume_stable_count_default(self, guard):
        """자동 복구 기본 안정 횟수는 3."""
        assert guard._auto_resume_stable_count == 3

    def test_auto_resume_stable_count_custom(self, mock_exchange):
        """자동 복구 안정 횟수를 커스텀 설정 가능."""
        g = BalanceGuard(mock_exchange, auto_resume_stable_count=5)
        assert g._auto_resume_stable_count == 5

    def test_consecutive_stable_initial(self, guard):
        """초기 stable 카운터는 0."""
        assert guard._consecutive_stable == 0


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


class TestAutoRecovery:
    """COIN-15: 자동 복구 메커니즘 테스트."""

    @pytest.mark.asyncio
    async def test_auto_resume_after_3_stable_checks(self, guard, mock_exchange):
        """일시 정지 후 3회 연속 안정 → 자동 재개."""
        # 1. 위험 괴리 → 일시 정지
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=80.0, used=0.0, total=80.0),
        }
        await guard.check_balance(100.0)
        assert guard.is_paused is True

        # 2. 안정 상태로 복귀
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }

        # 1회차 안정 — 아직 재개 안됨
        await guard.check_balance(100.0)
        assert guard.is_paused is True
        assert guard._consecutive_stable == 1

        # 2회차 안정 — 아직 재개 안됨
        await guard.check_balance(100.0)
        assert guard.is_paused is True
        assert guard._consecutive_stable == 2

        # 3회차 안정 → 자동 재개
        await guard.check_balance(100.0)
        assert guard.is_paused is False
        assert guard._consecutive_stable == 0  # resume()에서 리셋

    @pytest.mark.asyncio
    async def test_stable_count_resets_on_warning_during_recovery(self, guard, mock_exchange):
        """복구 중 다시 경고 발생 → stable 카운터 리셋."""
        # 1. 위험 괴리 → 일시 정지
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=80.0, used=0.0, total=80.0),
        }
        await guard.check_balance(100.0)
        assert guard.is_paused is True

        # 2. 안정 2회
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }
        await guard.check_balance(100.0)
        await guard.check_balance(100.0)
        assert guard._consecutive_stable == 2

        # 3. 다시 경고 → stable 카운터 리셋
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=96.0, used=0.0, total=96.0),
        }
        await guard.check_balance(100.0)
        assert guard._consecutive_stable == 0
        assert guard.is_paused is True  # 아직 일시 정지 상태

    @pytest.mark.asyncio
    async def test_stable_count_resets_on_critical_during_recovery(self, guard, mock_exchange):
        """복구 중 다시 위험 발생 → stable 카운터 리셋."""
        # 1. 위험 괴리 → 일시 정지
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=80.0, used=0.0, total=80.0),
        }
        await guard.check_balance(100.0)
        assert guard.is_paused is True

        # 2. 안정 1회
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }
        await guard.check_balance(100.0)
        assert guard._consecutive_stable == 1

        # 3. 다시 위험 → stable 카운터 리셋
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=80.0, used=0.0, total=80.0),
        }
        await guard.check_balance(100.0)
        assert guard._consecutive_stable == 0
        assert guard.is_paused is True

    @pytest.mark.asyncio
    async def test_no_auto_resume_when_not_paused(self, guard, mock_exchange):
        """일시 정지 상태가 아닐 때는 stable 카운터 증가 안함."""
        # 정상 체크 3회 — 자동 재개 로직 트리거 안됨
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }
        await guard.check_balance(100.0)
        await guard.check_balance(100.0)
        await guard.check_balance(100.0)
        assert guard._consecutive_stable == 0
        assert guard.is_paused is False

    @pytest.mark.asyncio
    async def test_custom_auto_resume_count(self, mock_exchange):
        """커스텀 auto_resume_stable_count=2로 설정 시 2회 안정 후 자동 재개."""
        guard = BalanceGuard(
            mock_exchange, auto_resume_stable_count=2,
        )

        # 일시 정지
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=80.0, used=0.0, total=80.0),
        }
        await guard.check_balance(100.0)
        assert guard.is_paused is True

        # 안정 2회 → 자동 재개
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }
        await guard.check_balance(100.0)
        assert guard.is_paused is True  # 1회차
        await guard.check_balance(100.0)
        assert guard.is_paused is False  # 2회차 → 재개

    @pytest.mark.asyncio
    async def test_auto_resume_emits_event(self, guard, mock_exchange):
        """자동 재개 시 이벤트 발생."""
        # 일시 정지
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=80.0, used=0.0, total=80.0),
        }
        await guard.check_balance(100.0)
        assert guard.is_paused is True

        # 안정 3회 → 이벤트 확인
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }
        with patch("engine.balance_guard.emit_event", new_callable=AsyncMock) as mock_emit:
            await guard.check_balance(100.0)
            await guard.check_balance(100.0)
            await guard.check_balance(100.0)

            # 자동 재개 이벤트 확인
            assert guard.is_paused is False
            calls = [c for c in mock_emit.call_args_list if c[0][0] == "info"]
            assert len(calls) >= 1
            assert "자동 재개" in calls[0][0][2]

    @pytest.mark.asyncio
    async def test_resume_with_reason(self, guard, mock_exchange):
        """resume()에 reason 파라미터 전달."""
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=80.0, used=0.0, total=80.0),
        }
        await guard.check_balance(100.0)
        assert guard.is_paused is True

        guard.resume(reason="manual_api")
        assert guard.is_paused is False
        assert guard._consecutive_stable == 0

    @pytest.mark.asyncio
    async def test_consecutive_pause_and_recovery_cycles(self, guard, mock_exchange):
        """반복적 정지/복구 사이클이 올바르게 동작."""
        # 1차 정지
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=80.0, used=0.0, total=80.0),
        }
        await guard.check_balance(100.0)
        assert guard.is_paused is True

        # 1차 복구
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }
        for _ in range(3):
            await guard.check_balance(100.0)
        assert guard.is_paused is False

        # 2차 정지
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=80.0, used=0.0, total=80.0),
        }
        await guard.check_balance(100.0)
        assert guard.is_paused is True

        # 2차 복구
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }
        for _ in range(3):
            await guard.check_balance(100.0)
        assert guard.is_paused is False


class TestGetStatus:
    """COIN-15: BalanceGuard 상태 조회 테스트."""

    def test_status_initial(self, guard):
        """초기 상태 반환."""
        status = guard.get_status()
        assert status["is_paused"] is False
        assert status["consecutive_warnings"] == 0
        assert status["consecutive_stable"] == 0
        assert status["auto_resume_stable_count"] == 3
        assert status["warn_pct"] == 3.0
        assert status["pause_pct"] == 5.0
        assert status["last_check"] is None

    @pytest.mark.asyncio
    async def test_status_after_check(self, guard):
        """체크 후 상태에 last_check 포함."""
        await guard.check_balance(100.0)
        status = guard.get_status()
        assert status["last_check"] is not None
        assert "exchange_balance" in status["last_check"]
        assert "internal_balance" in status["last_check"]
        assert "divergence_pct" in status["last_check"]
        assert "checked_at" in status["last_check"]

    @pytest.mark.asyncio
    async def test_status_while_paused(self, guard, mock_exchange):
        """일시 정지 상태 반영."""
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=80.0, used=0.0, total=80.0),
        }
        await guard.check_balance(100.0)
        status = guard.get_status()
        assert status["is_paused"] is True
        assert status["consecutive_warnings"] == 1

    @pytest.mark.asyncio
    async def test_status_during_recovery(self, guard, mock_exchange):
        """복구 중 stable 카운터 반영."""
        # 정지
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=80.0, used=0.0, total=80.0),
        }
        await guard.check_balance(100.0)

        # 안정 1회
        mock_exchange.fetch_balance.return_value = {
            "USDT": Balance(currency="USDT", free=100.0, used=0.0, total=100.0),
        }
        await guard.check_balance(100.0)

        status = guard.get_status()
        assert status["is_paused"] is True
        assert status["consecutive_stable"] == 1


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
