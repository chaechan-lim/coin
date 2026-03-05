"""헬스 모니터 테스트.

HealthMonitor의 5가지 건강 검진이 올바르게 동작하는지 검증.
"""
import pytest
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from engine.health_monitor import HealthMonitor, HealthCheckResult


# ── 공통 픽스처 ──────────────────────────────────────────────────

@pytest.fixture
def mock_engine():
    eng = MagicMock()
    eng._eval_error_counts = {}
    eng._position_trackers = {}
    eng.pause_buying = MagicMock()
    eng.resume_buying = MagicMock()
    eng._exchange_name = "test_exchange"
    return eng


@pytest.fixture
def mock_pm():
    pm = MagicMock()
    pm.cash_balance = 1000.0
    pm.reconcile_cash_from_db = AsyncMock()
    pm.sync_exchange_positions = AsyncMock()
    return pm


@pytest.fixture
def mock_exchange():
    return AsyncMock()


@pytest.fixture
def mock_market_data():
    md = AsyncMock()
    md.get_ticker = AsyncMock(return_value=MagicMock(last=50000))
    return md


@pytest.fixture
def health(mock_engine, mock_pm, mock_exchange, mock_market_data):
    return HealthMonitor(
        engine=mock_engine,
        portfolio_manager=mock_pm,
        exchange_adapter=mock_exchange,
        market_data=mock_market_data,
        exchange_name="test_exchange",
        tracked_coins=["BTC/USDT", "ETH/USDT"],
    )


# ── DB 세션 모킹 헬퍼 ──────────────────────────────────────────

def _mock_session_factory():
    """get_session_factory 모킹용."""
    mock_sf = MagicMock()
    mock_session = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_sf.return_value = mock_ctx
    return mock_sf, mock_session


# ── 1. Cash 정합성 ──────────────────────────────────────────────

class TestCashConsistency:
    @pytest.mark.asyncio
    async def test_healthy_cash(self, health, mock_pm):
        mock_pm.cash_balance = 1000.0
        session = AsyncMock()
        result = await health._check_cash_consistency(session)
        assert result.healthy is True
        assert result.name == "cash_consistency"

    @pytest.mark.asyncio
    async def test_negative_cash_auto_fixed(self, health, mock_pm):
        mock_pm.cash_balance = -50.0

        async def fix_cash(session):
            mock_pm.cash_balance = 100.0

        mock_pm.reconcile_cash_from_db = AsyncMock(side_effect=fix_cash)

        session = AsyncMock()
        result = await health._check_cash_consistency(session)
        assert result.name == "cash_negative"
        assert result.auto_fixed is True

    @pytest.mark.asyncio
    async def test_negative_cash_needs_sync(self, health, mock_pm):
        mock_pm.cash_balance = -50.0

        call_count = 0

        async def maybe_fix(session, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:  # sync fixes it
                mock_pm.cash_balance = 50.0

        mock_pm.reconcile_cash_from_db = AsyncMock()  # doesn't fix
        mock_pm.sync_exchange_positions = AsyncMock(side_effect=maybe_fix)

        session = AsyncMock()
        result = await health._check_cash_consistency(session)
        assert result.name == "cash_negative"
        mock_pm.sync_exchange_positions.assert_called_once()

    @pytest.mark.asyncio
    async def test_zero_cash_no_positions(self, health, mock_pm):
        mock_pm.cash_balance = 0.0

        session = AsyncMock()
        # Mock: 포지션 없음
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=mock_result)

        result = await health._check_cash_consistency(session)
        assert result.name == "cash_zero_no_positions"
        assert result.healthy is False
        mock_pm.reconcile_cash_from_db.assert_called_once()

    @pytest.mark.asyncio
    async def test_zero_cash_with_positions_is_ok(self, health, mock_pm):
        """포지션이 있으면 cash=0도 정상 (전액 투자 상태)."""
        mock_pm.cash_balance = 0.0

        session = AsyncMock()
        # Mock: 포지션 있음
        mock_pos = MagicMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_pos]
        session.execute = AsyncMock(return_value=mock_result)

        # cash=0 + 포지션 있음 → healthy check passes (doesn't enter the no-position branch)
        # But cash == 0 still triggers the "if cash == 0" branch, which checks positions
        result = await health._check_cash_consistency(session)
        # Should be healthy since there are positions
        assert result.healthy is True


# ── 2. 포지션 정합성 ────────────────────────────────────────────

class TestPositionConsistency:
    @pytest.mark.asyncio
    async def test_no_positions_healthy(self, health):
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=mock_result)

        result = await health._check_position_consistency(session)
        assert result.healthy is True

    @pytest.mark.asyncio
    async def test_entry_price_zero_auto_fix(self, health, mock_market_data):
        session = AsyncMock()
        pos = MagicMock()
        pos.symbol = "BTC/USDT"
        pos.avg_buy_price = 0
        pos.stop_loss_pct = None

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [pos]
        session.execute = AsyncMock(return_value=mock_result)
        session.commit = AsyncMock()

        result = await health._check_position_consistency(session)
        assert result.healthy is False
        assert result.auto_fixed is True
        assert pos.avg_buy_price == 50000  # 현재가 대입

    @pytest.mark.asyncio
    async def test_missing_tracker_detected(self, health, mock_engine):
        session = AsyncMock()
        pos = MagicMock()
        pos.symbol = "ETH/USDT"
        pos.avg_buy_price = 3000.0
        pos.stop_loss_pct = None  # DB에도 tracker 없음

        mock_engine._position_trackers = {}  # 엔진에도 없음

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [pos]
        session.execute = AsyncMock(return_value=mock_result)
        session.commit = AsyncMock()

        result = await health._check_position_consistency(session)
        assert result.healthy is False
        assert "tracker 없음" in result.detail


# ── 3. API 건강 ─────────────────────────────────────────────────

class TestAPIHealth:
    @pytest.mark.asyncio
    async def test_api_healthy(self, health, mock_market_data):
        result = await health._check_api_health()
        assert result.healthy is True
        assert health._api_fail_streak == 0

    @pytest.mark.asyncio
    async def test_api_fail_increments_streak(self, health, mock_market_data):
        mock_market_data.get_ticker = AsyncMock(side_effect=Exception("timeout"))
        result = await health._check_api_health()
        assert result.healthy is False
        assert health._api_fail_streak == 1

    @pytest.mark.asyncio
    async def test_api_3_fails_pauses_buying(self, health, mock_market_data, mock_engine):
        mock_market_data.get_ticker = AsyncMock(side_effect=Exception("timeout"))

        with patch("engine.health_monitor.emit_event", new_callable=AsyncMock):
            for _ in range(3):
                await health._check_api_health()

        assert health._api_fail_streak == 3
        assert health._api_paused is True
        mock_engine.pause_buying.assert_called_once()

    @pytest.mark.asyncio
    async def test_api_recovery_resumes_buying(self, health, mock_market_data, mock_engine):
        health._api_fail_streak = 3
        health._api_paused = True

        mock_market_data.get_ticker = AsyncMock(return_value=MagicMock(last=50000))

        with patch("engine.health_monitor.emit_event", new_callable=AsyncMock):
            result = await health._check_api_health()

        assert result.healthy is True
        assert health._api_paused is False
        mock_engine.resume_buying.assert_called_once()


# ── 4. 에러 추세 ────────────────────────────────────────────────

class TestErrorRateTrend:
    def test_no_errors_healthy(self, health, mock_engine):
        mock_engine._eval_error_counts = {}
        result = health._check_error_rate_trend()
        assert result.healthy is True

    def test_low_errors_healthy(self, health, mock_engine):
        mock_engine._eval_error_counts = {"BTC/USDT": 1}
        result = health._check_error_rate_trend()
        assert result.healthy is True

    def test_high_errors_unhealthy(self, health, mock_engine):
        mock_engine._eval_error_counts = {"BTC/USDT": 2, "ETH/USDT": 3}
        result = health._check_error_rate_trend()
        assert result.healthy is False
        assert "BTC/USDT" in result.detail
        assert "ETH/USDT" in result.detail


# ── 5. 멈춘 포지션 ─────────────────────────────────────────────

class TestStuckPositions:
    @pytest.mark.asyncio
    async def test_no_stuck_positions(self, health):
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=mock_result)

        result = await health._check_stuck_positions(session)
        assert result.healthy is True

    @pytest.mark.asyncio
    async def test_stuck_position_detected(self, health):
        session = AsyncMock()
        stuck_pos = MagicMock()
        stuck_pos.symbol = "BTC/USDT"
        stuck_pos.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [stuck_pos]
        session.execute = AsyncMock(return_value=mock_result)

        result = await health._check_stuck_positions(session)
        assert result.healthy is False
        assert "BTC/USDT" in result.detail

    @pytest.mark.asyncio
    async def test_stuck_no_updated_at(self, health):
        session = AsyncMock()
        stuck_pos = MagicMock()
        stuck_pos.symbol = "ETH/USDT"
        stuck_pos.updated_at = None

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [stuck_pos]
        session.execute = AsyncMock(return_value=mock_result)

        result = await health._check_stuck_positions(session)
        assert result.healthy is False
        assert "updated_at없음" in result.detail


# ── 통합 검진 ───────────────────────────────────────────────────

class TestRunHealthChecks:
    @pytest.mark.asyncio
    async def test_all_healthy(self, health, mock_pm, mock_market_data):
        mock_pm.cash_balance = 1000.0

        mock_sf, mock_session = _mock_session_factory()

        # position/stuck queries return empty
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch("db.session.get_session_factory", return_value=mock_sf):
            results = await health.run_health_checks()

        assert len(results) == 5
        assert all(r.healthy for r in results)

    @pytest.mark.asyncio
    async def test_unhealthy_emits_event(self, health, mock_pm, mock_market_data):
        mock_pm.cash_balance = -50.0

        mock_sf, mock_session = _mock_session_factory()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        with patch("db.session.get_session_factory", return_value=mock_sf), \
             patch("engine.health_monitor.emit_event", new_callable=AsyncMock) as mock_emit:
            results = await health.run_health_checks()

        # At least one unhealthy
        unhealthy = [r for r in results if not r.healthy]
        assert len(unhealthy) > 0

        # emit_event called with "health" category
        mock_emit.assert_called()
        call_args = mock_emit.call_args
        assert call_args[0][1] == "health"  # category
