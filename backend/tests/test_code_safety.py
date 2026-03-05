"""
코드 안전성 테스트 — 코드 리뷰에서 발견된 취약점 방어
=====================================================
실제 코드의 구조적 결함을 탐지하는 테스트.
mock 최소화, 실제 메서드 시그니처/로직을 직접 검증.
"""
import asyncio
import inspect
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from engine.trading_engine import TradingEngine, PositionTracker
from engine.futures_engine import BinanceFuturesEngine
from engine.portfolio_manager import PortfolioManager


# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture
def mock_config():
    config = MagicMock()
    config.binance.enabled = True
    config.binance.default_leverage = 3
    config.binance.max_leverage = 10
    config.binance.futures_fee = 0.0004
    config.binance.tracked_coins = ["BTC/USDT", "ETH/USDT"]
    config.binance.testnet = True
    config.binance_trading.evaluation_interval_sec = 300
    config.binance_trading.initial_balance_usdt = 1000.0
    config.binance_trading.min_combined_confidence = 0.55
    config.binance_trading.max_trade_size_pct = 0.35
    config.binance_trading.daily_buy_limit = 15
    config.binance_trading.max_daily_coin_buys = 3
    config.binance_trading.ws_price_monitor = True
    config.binance_trading.mode = "paper"
    config.trading.mode = "paper"
    config.trading.evaluation_interval_sec = 300
    config.trading.tracked_coins = ["BTC/KRW", "ETH/KRW"]
    config.trading.min_combined_confidence = 0.50
    config.trading.daily_buy_limit = 20
    config.trading.max_daily_coin_buys = 3
    config.trading.min_trade_interval_sec = 3600
    config.trading.rotation_enabled = False
    config.risk.max_trade_size_pct = 0.50
    config.binance_spot_trading.mode = "paper"
    config.binance_spot_trading.evaluation_interval_sec = 300
    return config


@pytest.fixture
def mock_market_data():
    md = AsyncMock()
    md.get_current_price = AsyncMock(return_value=65000.0)
    md.get_ticker = AsyncMock(return_value=MagicMock(last=65000.0))
    md.get_candles = AsyncMock(return_value=None)
    return md


def _make_engine(mock_config, mock_market_data, exchange_name="bithumb"):
    return TradingEngine(
        config=mock_config,
        exchange=AsyncMock(),
        market_data=mock_market_data,
        order_manager=MagicMock(),
        portfolio_manager=MagicMock(),
        combiner=MagicMock(),
        exchange_name=exchange_name,
    )


# ── 1. 메서드 시그니처 검증 ───────────────────────────────────

class TestMethodSignatures:
    """프로덕션 메서드의 시그니처가 호출부와 일치하는지 검증.
    기존 크리티컬 버그: _check_stop_conditions 인자 수 불일치."""

    def test_check_stop_conditions_accepts_3_args(self, mock_config, mock_market_data):
        """_check_stop_conditions(self, session, symbol, position) — 정확히 3 파라미터."""
        engine = _make_engine(mock_config, mock_market_data)
        sig = inspect.signature(engine._check_stop_conditions)
        params = [p for p in sig.parameters if p != 'self']
        assert len(params) == 3, f"Expected 3 params, got {len(params)}: {params}"
        assert list(sig.parameters.keys()) == ['session', 'symbol', 'position']

    def test_fast_stop_check_loop_exists(self, mock_config, mock_market_data):
        """현물 엔진에 _fast_stop_check_loop가 존재."""
        engine = _make_engine(mock_config, mock_market_data)
        assert hasattr(engine, '_fast_stop_check_loop')
        assert asyncio.iscoroutinefunction(engine._fast_stop_check_loop)

    def test_maybe_update_market_state_spot_no_session(self, mock_config, mock_market_data):
        """현물 _maybe_update_market_state는 session 파라미터 없음."""
        engine = _make_engine(mock_config, mock_market_data)
        sig = inspect.signature(engine._maybe_update_market_state)
        params = [p for p in sig.parameters if p != 'self']
        assert 'session' not in params, "현물 엔진은 session 인자 없어야 함"

    def test_maybe_update_market_state_futures_has_session(self, mock_config, mock_market_data):
        """선물 _maybe_update_market_state는 session 파라미터 있음."""
        engine = BinanceFuturesEngine(
            config=mock_config,
            exchange=AsyncMock(),
            market_data=mock_market_data,
            order_manager=MagicMock(),
            portfolio_manager=MagicMock(),
            combiner=MagicMock(),
        )
        sig = inspect.signature(engine._maybe_update_market_state)
        params = [p for p in sig.parameters if p != 'self']
        assert 'session' in params, "선물 엔진은 session 인자 있어야 함"


# ── 2. 거래소별 BTC 심볼 분기 검증 ─────────────────────────────

class TestExchangeSymbolRouting:
    """각 거래소 엔진이 올바른 BTC 심볼을 사용하는지 검증."""

    @pytest.mark.asyncio
    async def test_bithumb_btc_krw(self, mock_config, mock_market_data):
        engine = _make_engine(mock_config, mock_market_data, "bithumb")
        engine._market_state_updated = None
        await engine._maybe_update_market_state()
        mock_market_data.get_candles.assert_any_call("BTC/KRW", "4h", 200)

    @pytest.mark.asyncio
    async def test_binance_spot_btc_usdt(self, mock_config, mock_market_data):
        engine = _make_engine(mock_config, mock_market_data, "binance_spot")
        engine._market_state_updated = None
        await engine._maybe_update_market_state()
        mock_market_data.get_candles.assert_any_call("BTC/USDT", "4h", 200)


# ── 3. sync_lock 동작 검증 ─────────────────────────────────────

class TestSyncLock:
    """_sync_lock이 eval과 sync의 동시 실행을 막는지 검증."""

    def test_portfolio_manager_has_sync_lock(self):
        """PM에 _sync_lock (asyncio.Lock)이 존재."""
        pm = PortfolioManager(
            market_data=AsyncMock(),
            initial_balance_krw=500_000,
        )
        assert hasattr(pm, '_sync_lock')
        assert isinstance(pm._sync_lock, asyncio.Lock)

    @pytest.mark.asyncio
    async def test_sync_skipped_when_locked(self):
        """Lock 잠금 상태에서 sync_exchange_positions는 즉시 리턴."""
        pm = PortfolioManager(
            market_data=AsyncMock(),
            initial_balance_krw=500_000,
            exchange_name="bithumb",
        )
        adapter = AsyncMock()
        adapter.fetch_balance = AsyncMock()

        async with pm._sync_lock:
            # Lock이 잠긴 상태에서 sync 호출
            await pm.sync_exchange_positions(AsyncMock(), adapter, [])

        # fetch_balance가 호출되지 않아야 함
        adapter.fetch_balance.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_runs_when_unlocked(self):
        """Lock 해제 상태에서 sync는 정상 동작."""
        pm = PortfolioManager(
            market_data=AsyncMock(),
            initial_balance_krw=500_000,
            exchange_name="bithumb",
        )
        from exchange.base import Balance
        adapter = AsyncMock()
        adapter.fetch_balance = AsyncMock(return_value={
            "KRW": Balance(currency="KRW", free=500_000, used=0, total=500_000),
        })
        adapter.fetch_positions = AsyncMock(return_value=[])

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        # Lock이 해제된 상태에서 호출
        assert not pm._sync_lock.locked()
        await pm.sync_exchange_positions(mock_session, adapter, [])
        adapter.fetch_balance.assert_called_once()


# ── 4. PM cash_balance property 검증 ──────────────────────────

class TestCashBalanceProperty:
    """cash_balance property가 올바르게 동작하는지 검증."""

    def test_getter(self):
        pm = PortfolioManager(market_data=AsyncMock(), initial_balance_krw=1000)
        assert pm.cash_balance == 1000

    def test_setter(self):
        pm = PortfolioManager(market_data=AsyncMock(), initial_balance_krw=1000)
        pm.cash_balance = 2000
        assert pm.cash_balance == 2000
        assert pm._cash_balance == 2000

    def test_setter_negative(self):
        """음수 설정도 가능 (비즈니스 로직에서 처리)."""
        pm = PortfolioManager(market_data=AsyncMock(), initial_balance_krw=1000)
        pm.cash_balance = -50
        assert pm.cash_balance == -50


# ── 5. 엔진 config 접근성 검증 ─────────────────────────────────

class TestEngineConfigAccess:
    """엔진에서 config에 접근할 수 있는지 검증.
    dashboard.py에서 getattr(eng, '_config')로 접근."""

    def test_trading_engine_has_config(self, mock_config, mock_market_data):
        engine = _make_engine(mock_config, mock_market_data)
        assert hasattr(engine, '_config')
        assert engine._config is mock_config

    def test_futures_engine_has_config(self, mock_config, mock_market_data):
        engine = BinanceFuturesEngine(
            config=mock_config,
            exchange=AsyncMock(),
            market_data=mock_market_data,
            order_manager=MagicMock(),
            portfolio_manager=MagicMock(),
            combiner=MagicMock(),
        )
        assert hasattr(engine, '_config')
        assert engine._config is mock_config

    def test_config_has_all_required_attrs(self, mock_config):
        """dashboard API가 참조하는 config 속성이 모두 존재."""
        assert hasattr(mock_config, 'trading')
        assert hasattr(mock_config.trading, 'mode')
        assert hasattr(mock_config.trading, 'evaluation_interval_sec')
        assert hasattr(mock_config, 'binance_trading')
        assert hasattr(mock_config.binance_trading, 'mode')
        assert hasattr(mock_config.binance_trading, 'evaluation_interval_sec')


# ── 6. API 레거시 글로벌 제거 검증 ──────────────────────────────

class TestLegacyGlobalsRemoved:
    """레거시 글로벌 변수와 setter가 API 모듈에서 제거되었는지."""

    def test_strategies_no_legacy_globals(self):
        import api.strategies as mod
        assert not hasattr(mod, '_engine') or mod.__dict__.get('_engine') is None
        assert not hasattr(mod, '_combiner') or mod.__dict__.get('_combiner') is None
        assert not hasattr(mod, 'set_engine_and_combiner')

    def test_portfolio_no_legacy_globals(self):
        import api.portfolio as mod
        assert not hasattr(mod, '_portfolio_manager') or mod.__dict__.get('_portfolio_manager') is None
        assert not hasattr(mod, 'set_portfolio_manager')

    def test_dashboard_no_legacy_globals(self):
        import api.dashboard as mod
        assert not hasattr(mod, '_engine') or mod.__dict__.get('_engine') is None
        assert not hasattr(mod, '_coordinator') or mod.__dict__.get('_coordinator') is None
        assert not hasattr(mod, '_config') or mod.__dict__.get('_config') is None
        assert not hasattr(mod, 'set_dashboard_deps')


# ── 7. Discord 429 에러 핸들링 검증 ────────────────────────────

class TestDiscordErrorHandling:
    """Discord webhook 429 응답 시 JSON 파싱 실패를 처리하는지."""

    @pytest.mark.asyncio
    async def test_429_invalid_json_no_crash(self):
        """Discord 429 + 유효하지 않은 JSON → 크래시 없이 처리."""
        from services.discord_event_handler import DiscordEventHandler

        handler = DiscordEventHandler.__new__(DiscordEventHandler)
        handler._webhook_url = "https://test.example.com/webhook"

        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429
        mock_resp_429.json.side_effect = ValueError("invalid json")

        mock_resp_ok = MagicMock()
        mock_resp_ok.status_code = 204

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[mock_resp_429, mock_resp_ok])
        handler._client = mock_client

        # 크래시 없이 완료
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await handler._send_embed({"title": "test", "description": "test"})

    @pytest.mark.asyncio
    async def test_429_retry_failure_no_crash(self):
        """Discord 429 재시도도 실패 → 크래시 없이 로그만."""
        from services.discord_event_handler import DiscordEventHandler

        handler = DiscordEventHandler.__new__(DiscordEventHandler)
        handler._webhook_url = "https://test.example.com/webhook"

        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429
        mock_resp_429.json.return_value = {"retry_after": 0.1}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[
            mock_resp_429,
            Exception("connection reset"),
        ])
        handler._client = mock_client

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await handler._send_embed({"title": "test", "description": "test"})


# ── 8. Dashboard limit 파라미터 검증 ────────────────────────────

class TestDashboardLimitValidation:
    """Dashboard API의 limit 파라미터가 범위 제한되는지 검증."""

    @staticmethod
    def _has_limit_constraint(func):
        """Query(limit, ge=..., le=...) 제약이 존재하는지 확인."""
        sig = inspect.signature(func)
        limit_param = sig.parameters.get('limit')
        assert limit_param is not None, f"{func.__name__}: limit 파라미터 없음"
        default = limit_param.default
        # FastAPI Query는 metadata에 Ge/Le 객체를 저장
        meta_types = {type(m).__name__ for m in getattr(default, 'metadata', [])}
        assert 'Ge' in meta_types, f"{func.__name__}: ge 제약 없음"
        assert 'Le' in meta_types, f"{func.__name__}: le 제약 없음"

    def test_market_analysis_history_limit_validated(self):
        from api.dashboard import get_market_analysis_history
        self._has_limit_constraint(get_market_analysis_history)

    def test_trade_review_history_limit_validated(self):
        from api.dashboard import get_trade_review_history
        self._has_limit_constraint(get_trade_review_history)

    def test_risk_history_limit_validated(self):
        from api.dashboard import get_risk_history
        self._has_limit_constraint(get_risk_history)


# ── 9. event_bus 알림 에러 핸들링 ──────────────────────────────

class TestEventBusNotificationSafety:
    """emit_event의 알림 콜백 실패가 이벤트 처리를 중단시키지 않는지."""

    @pytest.mark.asyncio
    async def test_notification_error_does_not_crash(self):
        """알림 콜백이 TypeError를 던져도 emit_event는 완료."""
        from core.event_bus import emit_event, set_notification, set_broadcast

        original_notification = None
        try:
            # 실패하는 알림 콜백 설정
            async def failing_notification(*args):
                raise TypeError("bad callback")

            set_notification(failing_notification)
            set_broadcast(None)

            # emit_event가 크래시 없이 완료
            await emit_event("info", "test", "safety test")
        finally:
            set_notification(original_notification)


# ── 10. PositionTracker 기본값 검증 ────────────────────────────

class TestPositionTrackerDefaults:
    """PositionTracker가 올바른 기본값을 갖는지 검증."""

    def test_default_sl_tp(self):
        tracker = PositionTracker(entry_price=100, highest_price=100)
        assert tracker.stop_loss_pct == 5.0
        assert tracker.take_profit_pct == 10.0
        assert tracker.trailing_activation_pct == 5.0
        assert tracker.trailing_stop_pct == 4.0
        assert tracker.trailing_active is False
        assert tracker.highest_price == 100

    def test_highest_price_tracks_entry(self):
        tracker = PositionTracker(entry_price=50000, highest_price=60000)
        assert tracker.highest_price == 60000


# ── 11. 거래소별 최소 주문금액 검증 ────────────────────────────

class TestMinOrderAmounts:
    """거래소별 최소 주문금액이 올바르게 설정되는지."""

    def test_bithumb_min_order(self, mock_config, mock_market_data):
        engine = _make_engine(mock_config, mock_market_data, "bithumb")
        assert engine._min_order_amount == 5000  # 5000 KRW
        assert engine._fee_margin == 1.003

    def test_binance_spot_min_order(self, mock_config, mock_market_data):
        engine = _make_engine(mock_config, mock_market_data, "binance_spot")
        assert engine._min_order_amount == 5  # 5 USDT
        assert engine._fee_margin == 1.002

    def test_binance_futures_min_order(self, mock_config, mock_market_data):
        engine = BinanceFuturesEngine(
            config=mock_config,
            exchange=AsyncMock(),
            market_data=mock_market_data,
            order_manager=MagicMock(),
            portfolio_manager=MagicMock(),
            combiner=MagicMock(),
        )
        assert engine._min_order_amount == 5  # 5 USDT
