"""복구 매니저 테스트.

RecoveryManager가 분류된 에러에 대해 올바른 복구 액션을 수행하는지 검증.
"""
import pytest
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from core.error_classifier import ClassifiedError, ErrorCategory
from engine.recovery import RecoveryManager, RecoveryResult


# ── 공통 픽스처 ──────────────────────────────────────────────────

@pytest.fixture
def mock_engine():
    eng = MagicMock()
    eng.suppress_buys = MagicMock()
    eng.pause_buying = MagicMock()
    eng.resume_buying = MagicMock()
    return eng


@pytest.fixture
def mock_pm():
    pm = MagicMock()
    pm.cash_balance = 100.0
    pm.reconcile_cash_from_db = AsyncMock()
    pm.sync_exchange_positions = AsyncMock()
    return pm


@pytest.fixture
def mock_exchange():
    return AsyncMock()


@pytest.fixture
def recovery(mock_engine, mock_pm, mock_exchange):
    return RecoveryManager(
        engine=mock_engine,
        portfolio_manager=mock_pm,
        exchange_adapter=mock_exchange,
        exchange_name="test_exchange",
        tracked_coins=["BTC/USDT", "ETH/USDT"],
    )


def _make_classified(
    category=ErrorCategory.TRANSIENT,
    symbol="BTC/USDT",
    context="buy_order",
    retryable=True,
    max_retries=3,
    backoff_base=2.0,
    recovery_action=None,
):
    return ClassifiedError(
        category=category,
        original=Exception("test error"),
        symbol=symbol,
        context=context,
        retryable=retryable,
        max_retries=max_retries,
        backoff_base=backoff_base,
        recovery_action=recovery_action,
    )


class TestRecoveryTransient:
    """TRANSIENT 복구: 백오프 대기 허용."""

    @pytest.mark.asyncio
    async def test_transient_returns_resolved(self, recovery):
        classified = _make_classified(ErrorCategory.TRANSIENT, backoff_base=2.0)
        result = await recovery.attempt_recovery(classified)
        assert result.resolved is True
        assert result.action_taken == "backoff_wait"

    @pytest.mark.asyncio
    async def test_transient_with_zero_backoff(self, recovery):
        classified = _make_classified(ErrorCategory.TRANSIENT, backoff_base=0)
        result = await recovery.attempt_recovery(classified)
        assert result.resolved is True


class TestRecoveryResource:
    """RESOURCE 복구: reconcile_cash → sync_exchange."""

    @pytest.mark.asyncio
    async def test_resource_reconcile_success(self, recovery, mock_pm):
        """reconcile 후 잔고가 증가하면 resolved."""
        original_cash = mock_pm.cash_balance

        async def bump_cash(session):
            mock_pm.cash_balance = 200.0  # 잔고 증가

        mock_pm.reconcile_cash_from_db = AsyncMock(side_effect=bump_cash)

        classified = _make_classified(ErrorCategory.RESOURCE, recovery_action="reconcile_cash")
        with patch("db.session.get_session_factory") as mock_sf:
            mock_session = AsyncMock()
            mock_sf.return_value = MagicMock(
                __call__=MagicMock(return_value=mock_session),
            )
            # AsyncContextManager mock
            mock_sf.return_value().__aenter__ = AsyncMock(return_value=mock_session)
            mock_sf.return_value().__aexit__ = AsyncMock(return_value=False)

            result = await recovery.attempt_recovery(classified)

        assert result.resolved is True
        assert result.action_taken == "reconcile_cash"

    @pytest.mark.asyncio
    async def test_resource_sync_fallback(self, recovery, mock_pm):
        """reconcile 후 잔고 변동 없으면 sync 시도."""
        mock_pm.cash_balance = 0.0

        classified = _make_classified(ErrorCategory.RESOURCE, recovery_action="reconcile_cash")
        with patch("db.session.get_session_factory") as mock_sf:
            mock_session = AsyncMock()
            mock_sf.return_value = MagicMock(
                __call__=MagicMock(return_value=mock_session),
            )
            mock_sf.return_value().__aenter__ = AsyncMock(return_value=mock_session)
            mock_sf.return_value().__aexit__ = AsyncMock(return_value=False)

            result = await recovery.attempt_recovery(classified)

        assert result.resolved is False
        assert "sync_exchange" in result.action_taken
        mock_pm.sync_exchange_positions.assert_called_once()

    @pytest.mark.asyncio
    async def test_resource_exception_handled(self, recovery, mock_pm):
        """복구 중 예외 발생 시 graceful fail."""
        mock_pm.reconcile_cash_from_db = AsyncMock(side_effect=Exception("DB error"))

        classified = _make_classified(ErrorCategory.RESOURCE)
        with patch("db.session.get_session_factory") as mock_sf:
            mock_session = AsyncMock()
            mock_sf.return_value = MagicMock(
                __call__=MagicMock(return_value=mock_session),
            )
            mock_sf.return_value().__aenter__ = AsyncMock(return_value=mock_session)
            mock_sf.return_value().__aexit__ = AsyncMock(return_value=False)

            result = await recovery.attempt_recovery(classified)

        assert result.resolved is False
        assert "failed" in result.action_taken


class TestRecoveryState:
    """STATE 복구: sync_positions."""

    @pytest.mark.asyncio
    async def test_state_sync_success(self, recovery, mock_pm):
        classified = _make_classified(ErrorCategory.STATE, recovery_action="sync_positions")
        with patch("db.session.get_session_factory") as mock_sf:
            mock_session = AsyncMock()
            mock_sf.return_value = MagicMock(
                __call__=MagicMock(return_value=mock_session),
            )
            mock_sf.return_value().__aenter__ = AsyncMock(return_value=mock_session)
            mock_sf.return_value().__aexit__ = AsyncMock(return_value=False)

            result = await recovery.attempt_recovery(classified)

        assert result.resolved is True
        assert result.action_taken == "sync_positions"
        mock_pm.sync_exchange_positions.assert_called_once()

    @pytest.mark.asyncio
    async def test_state_sync_failure(self, recovery, mock_pm):
        mock_pm.sync_exchange_positions = AsyncMock(side_effect=Exception("sync failed"))

        classified = _make_classified(ErrorCategory.STATE)
        with patch("db.session.get_session_factory") as mock_sf:
            mock_session = AsyncMock()
            mock_sf.return_value = MagicMock(
                __call__=MagicMock(return_value=mock_session),
            )
            mock_sf.return_value().__aenter__ = AsyncMock(return_value=mock_session)
            mock_sf.return_value().__aexit__ = AsyncMock(return_value=False)

            result = await recovery.attempt_recovery(classified)

        assert result.resolved is False


class TestRecoveryPermanent:
    """PERMANENT 복구: 코인 억제 + 알림."""

    @pytest.mark.asyncio
    async def test_permanent_suppresses_coin(self, recovery, mock_engine):
        classified = _make_classified(
            ErrorCategory.PERMANENT, symbol="LUNA/USDT",
            recovery_action="suppress_coin",
        )
        with patch("engine.recovery.emit_event", new_callable=AsyncMock):
            result = await recovery.attempt_recovery(classified)

        assert result.resolved is True
        assert result.action_taken == "suppress_coin"
        mock_engine.suppress_buys.assert_called_once_with(["LUNA/USDT"])

    @pytest.mark.asyncio
    async def test_permanent_no_symbol(self, recovery):
        classified = _make_classified(
            ErrorCategory.PERMANENT, symbol=None,
            recovery_action="suppress_coin",
        )
        with patch("engine.recovery.emit_event", new_callable=AsyncMock):
            result = await recovery.attempt_recovery(classified)

        assert result.resolved is False
        assert result.action_taken == "no_symbol"


class TestRecoveryThrottle:
    """일일 10회 쓰로틀."""

    @pytest.mark.asyncio
    async def test_throttle_after_max_daily(self, recovery):
        classified = _make_classified(ErrorCategory.TRANSIENT, symbol="BTC/USDT", context="buy_order")

        # 10번 성공
        for _ in range(RecoveryManager.MAX_DAILY_RECOVERIES):
            result = await recovery.attempt_recovery(classified)
            assert result.resolved is True

        # 11번째 → throttled
        result = await recovery.attempt_recovery(classified)
        assert result.resolved is False
        assert result.action_taken == "throttled"

    @pytest.mark.asyncio
    async def test_different_keys_independent(self, recovery):
        """다른 심볼:컨텍스트 키는 독립적으로 카운팅."""
        c1 = _make_classified(ErrorCategory.TRANSIENT, symbol="BTC/USDT", context="buy_order")
        c2 = _make_classified(ErrorCategory.TRANSIENT, symbol="ETH/USDT", context="buy_order")

        for _ in range(RecoveryManager.MAX_DAILY_RECOVERIES):
            await recovery.attempt_recovery(c1)

        # BTC → throttled
        r1 = await recovery.attempt_recovery(c1)
        assert r1.action_taken == "throttled"

        # ETH → still OK
        r2 = await recovery.attempt_recovery(c2)
        assert r2.resolved is True


class TestRecoveryDailyReset:
    """일일 리셋."""

    @pytest.mark.asyncio
    async def test_reset_daily_clears_counts(self, recovery):
        classified = _make_classified(ErrorCategory.TRANSIENT)

        for _ in range(RecoveryManager.MAX_DAILY_RECOVERIES):
            await recovery.attempt_recovery(classified)

        # throttled
        r = await recovery.attempt_recovery(classified)
        assert r.action_taken == "throttled"

        # 리셋
        recovery.reset_daily()

        # 다시 가능
        r = await recovery.attempt_recovery(classified)
        assert r.resolved is True

    def test_reset_daily_method(self, recovery):
        recovery._recovery_counts = {"BTC:buy": 5, "ETH:sell": 3}
        recovery.reset_daily()
        assert recovery._recovery_counts == {}
