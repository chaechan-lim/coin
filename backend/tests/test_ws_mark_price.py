"""WS markPrice stream + engine integration + RegimeDetector injection 테스트."""

import asyncio
import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from config import AppConfig
from engine.futures_engine_v2 import FuturesEngineV2
from engine.regime_detector import RegimeDetector, RegimeState
from services.derivatives_data import DerivativesDataService
from exchange.data_models import (
    Balance,
    MarkPriceInfo,
    OpenInterest,
    LongShortRatio,
)
from core.enums import Regime


# ── Helpers ──────────────────────────────────────────


def _make_mark_price(
    symbol: str = "BTC/USDT",
    mark: float = 65100.0,
    index: float = 65000.0,
    funding: float = 0.0005,
) -> MarkPriceInfo:
    return MarkPriceInfo(
        symbol=symbol,
        mark_price=mark,
        index_price=index,
        last_funding_rate=funding,
        next_funding_time=datetime(2024, 1, 1, 8, 0, tzinfo=timezone.utc),
        premium_pct=((mark - index) / index * 100) if index else 0.0,
        timestamp=datetime.now(timezone.utc),
    )


def _make_regime_df(
    n=100,
    close=80000.0,
    adx=30.0,
    atr=1000.0,
    ema_20=80000.0,
    ema_50=79000.0,
    bb_upper=82000.0,
    bb_lower=78000.0,
    bb_mid=80000.0,
    volume=1000.0,
    ema_slope_dir=1,
) -> pd.DataFrame:
    ema_values = []
    for i in range(n):
        pct = ema_slope_dir * 0.002 * (i - (n - 1))
        ema_values.append(ema_20 * (1 + pct))
    data = {
        "close": [close] * n,
        "adx_14": [adx] * n,
        "atr_14": [atr] * n,
        "ema_20": ema_values,
        "ema_50": [ema_50] * n,
        "bb_upper_20": [bb_upper] * n,
        "bb_lower_20": [bb_lower] * n,
        "bb_mid_20": [bb_mid] * n,
        "volume": [volume] * n,
    }
    return pd.DataFrame(data)


# ── Engine Fixtures ──────────────────────────────────


@pytest.fixture
def mock_exchange():
    exchange = AsyncMock()
    exchange.set_leverage = AsyncMock()
    exchange.fetch_balance = AsyncMock(
        return_value={
            "USDT": Balance(currency="USDT", free=500.0, used=0.0, total=500.0),
        }
    )
    exchange.close_ws = AsyncMock()
    exchange.create_ws_exchange = AsyncMock()
    exchange.watch_mark_prices = AsyncMock(
        return_value={"BTC/USDT": _make_mark_price()}
    )
    exchange.fetch_open_interest = AsyncMock(
        return_value=OpenInterest(
            symbol="BTC/USDT",
            open_interest_value=100_000_000.0,
            timestamp=datetime.now(timezone.utc),
        )
    )
    exchange.fetch_long_short_ratio = AsyncMock(
        return_value=LongShortRatio(
            symbol="BTC/USDT",
            long_account_ratio=0.65,
            short_account_ratio=0.35,
            long_position_ratio=0.60,
            short_position_ratio=0.40,
            timestamp=datetime.now(timezone.utc),
        )
    )
    return exchange


@pytest.fixture
def mock_market_data():
    md = AsyncMock()
    md.get_current_price = AsyncMock(return_value=80000.0)
    md.get_ohlcv_df = AsyncMock(return_value=None)
    return md


@pytest.fixture
def mock_pm():
    pm = MagicMock()
    pm.cash_balance = 500.0
    pm._is_paper = False
    pm._exchange_name = "binance_futures"
    pm.apply_income = AsyncMock()
    return pm


@pytest.fixture
def mock_om():
    return MagicMock()


@pytest.fixture
def app_config():
    return AppConfig()


@pytest.fixture
def derivatives_data():
    return DerivativesDataService()


# ── Subtask 1: watch_mark_prices adapter ─────────────


class TestWatchMarkPricesAdapter:
    """BinanceUSDMAdapter.watch_mark_prices 테스트."""

    @pytest.mark.asyncio
    async def test_watch_mark_prices_returns_dict(self, mock_exchange):
        result = await mock_exchange.watch_mark_prices(["BTC/USDT"])
        assert "BTC/USDT" in result
        assert result["BTC/USDT"].mark_price == 65100.0

    @pytest.mark.asyncio
    async def test_watch_mark_prices_empty_symbols(self, mock_exchange):
        mock_exchange.watch_mark_prices = AsyncMock(return_value={})
        result = await mock_exchange.watch_mark_prices([])
        assert result == {}


# ── Subtask 2: Engine Integration ────────────────────


class TestEngineWithDerivatives:
    """FuturesEngineV2 derivatives_data 통합 테스트."""

    def test_init_without_derivatives_data(
        self, app_config, mock_exchange, mock_market_data, mock_om, mock_pm
    ):
        """derivatives_data=None (기본값) — 하위 호환."""
        engine = FuturesEngineV2(
            config=app_config,
            exchange=mock_exchange,
            market_data=mock_market_data,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
        )
        assert engine._derivatives_data is None
        assert engine._ws_mark_price_task is None

    def test_init_with_derivatives_data(
        self, app_config, mock_exchange, mock_market_data, mock_om, mock_pm, derivatives_data
    ):
        """derivatives_data 주입 시 저장."""
        engine = FuturesEngineV2(
            config=app_config,
            exchange=mock_exchange,
            market_data=mock_market_data,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
            derivatives_data=derivatives_data,
        )
        assert engine._derivatives_data is derivatives_data

    def test_regime_detector_receives_derivatives_data(
        self, app_config, mock_exchange, mock_market_data, mock_om, mock_pm, derivatives_data
    ):
        """RegimeDetector에 derivatives_data가 전달되는지 확인."""
        engine = FuturesEngineV2(
            config=app_config,
            exchange=mock_exchange,
            market_data=mock_market_data,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
            derivatives_data=derivatives_data,
        )
        assert engine._regime._derivatives_data is derivatives_data


class TestMarkPriceLoop:
    """_ws_mark_price_loop 테스트."""

    @pytest.mark.asyncio
    async def test_mark_price_loop_updates_cache(
        self, app_config, mock_exchange, mock_market_data, mock_om, mock_pm, derivatives_data
    ):
        """마크프라이스 루프가 캐시를 업데이트하는지 확인."""
        engine = FuturesEngineV2(
            config=app_config,
            exchange=mock_exchange,
            market_data=mock_market_data,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
            derivatives_data=derivatives_data,
        )
        engine._is_running = True

        # 1회 실행 후 중지
        call_count = 0

        async def _watch_once(symbols):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                engine._is_running = False
            return {"BTC/USDT": _make_mark_price()}

        mock_exchange.watch_mark_prices = AsyncMock(side_effect=_watch_once)
        # REST 수집 무시 (OI/LS)
        mock_exchange.fetch_open_interest = AsyncMock(side_effect=Exception("skip"))
        mock_exchange.fetch_long_short_ratio = AsyncMock(side_effect=Exception("skip"))

        with patch("engine.futures_engine_v2.asyncio.sleep", new_callable=AsyncMock):
            await engine._ws_mark_price_loop()

        assert derivatives_data.get_mark_price("BTC/USDT") is not None
        assert derivatives_data.get_mark_price("BTC/USDT").mark_price == 65100.0

    @pytest.mark.asyncio
    async def test_mark_price_loop_handles_errors(
        self, app_config, mock_exchange, mock_market_data, mock_om, mock_pm, derivatives_data
    ):
        """에러 발생 시 로깅하고 계속 실행."""
        engine = FuturesEngineV2(
            config=app_config,
            exchange=mock_exchange,
            market_data=mock_market_data,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
            derivatives_data=derivatives_data,
        )
        engine._is_running = True

        call_count = 0

        async def _watch_with_error(symbols):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ConnectionError("ws error")
            engine._is_running = False
            return {"BTC/USDT": _make_mark_price()}

        mock_exchange.watch_mark_prices = AsyncMock(side_effect=_watch_with_error)

        with patch("engine.futures_engine_v2.asyncio.sleep", new_callable=AsyncMock):
            await engine._ws_mark_price_loop()

        # 에러 후 복구하여 캐시 업데이트 성공
        assert derivatives_data.get_mark_price("BTC/USDT") is not None

    @pytest.mark.asyncio
    async def test_mark_price_loop_cancelled(
        self, app_config, mock_exchange, mock_market_data, mock_om, mock_pm, derivatives_data
    ):
        """CancelledError 시 루프 종료."""
        engine = FuturesEngineV2(
            config=app_config,
            exchange=mock_exchange,
            market_data=mock_market_data,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
            derivatives_data=derivatives_data,
        )
        engine._is_running = True
        mock_exchange.watch_mark_prices = AsyncMock(
            side_effect=asyncio.CancelledError
        )

        # CancelledError로 루프 종료 — 예외 없이 반환
        await engine._ws_mark_price_loop()

    @pytest.mark.asyncio
    async def test_empty_result_triggers_error(
        self, app_config, mock_exchange, mock_market_data, mock_om, mock_pm, derivatives_data
    ):
        """모든 심볼 수집 실패(빈 결과) 시 consecutive_errors 증가."""
        engine = FuturesEngineV2(
            config=app_config,
            exchange=mock_exchange,
            market_data=mock_market_data,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
            derivatives_data=derivatives_data,
        )
        engine._is_running = True

        call_count = 0

        async def _watch_empty_then_stop(symbols):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return {}  # 전체 실패 → RuntimeError 발생
            engine._is_running = False
            return {"BTC/USDT": _make_mark_price()}

        mock_exchange.watch_mark_prices = AsyncMock(side_effect=_watch_empty_then_stop)

        with patch("engine.futures_engine_v2.asyncio.sleep", new_callable=AsyncMock):
            await engine._ws_mark_price_loop()

        # 빈 결과 에러 후 복구
        assert derivatives_data.get_mark_price("BTC/USDT") is not None

    @pytest.mark.asyncio
    async def test_no_ws_reconnect_on_rest_failure(
        self, app_config, mock_exchange, mock_market_data, mock_om, mock_pm, derivatives_data
    ):
        """REST 실패 시 WS 재연결(_ws_reconnect) 호출하지 않음."""
        engine = FuturesEngineV2(
            config=app_config,
            exchange=mock_exchange,
            market_data=mock_market_data,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
            derivatives_data=derivatives_data,
        )
        engine._is_running = True

        call_count = 0

        async def _watch_errors(symbols):
            nonlocal call_count
            call_count += 1
            if call_count <= 4:  # 4회 에러 (>= _WS_MAX_ERRORS=3)
                raise ConnectionError("rest failure")
            engine._is_running = False
            return {"BTC/USDT": _make_mark_price()}

        mock_exchange.watch_mark_prices = AsyncMock(side_effect=_watch_errors)

        with patch("engine.futures_engine_v2.asyncio.sleep", new_callable=AsyncMock):
            with patch.object(engine, "_ws_reconnect", new_callable=AsyncMock) as mock_reconnect:
                await engine._ws_mark_price_loop()

        # WS 재연결은 호출되지 않아야 함
        mock_reconnect.assert_not_called()
        # close_ws도 호출되지 않아야 함
        mock_exchange.close_ws.assert_not_called()


class TestEngineLifecycle:
    """start/stop에서 mark_price 태스크 관리."""

    @pytest.mark.asyncio
    async def test_start_creates_mark_price_task(
        self, app_config, mock_exchange, mock_market_data, mock_om, mock_pm, derivatives_data
    ):
        """start() 시 derivatives_data가 있으면 mark price 태스크 생성."""
        engine = FuturesEngineV2(
            config=app_config,
            exchange=mock_exchange,
            market_data=mock_market_data,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
            derivatives_data=derivatives_data,
        )

        with patch.object(engine, "_check_downtime_stops", new_callable=AsyncMock):
            with patch.object(engine, "_regime_loop", new_callable=AsyncMock):
                with patch.object(engine, "_tier1_loop", new_callable=AsyncMock):
                    with patch.object(engine, "_tier2_loop", new_callable=AsyncMock):
                        with patch.object(engine, "_balance_guard_loop", new_callable=AsyncMock):
                            with patch.object(engine, "_income_loop", new_callable=AsyncMock):
                                with patch.object(engine, "_persist_loop", new_callable=AsyncMock):
                                    with patch.object(engine, "_ws_price_monitor_loop", new_callable=AsyncMock):
                                        with patch.object(engine, "_ws_balance_loop", new_callable=AsyncMock):
                                            with patch.object(engine, "_ws_position_loop", new_callable=AsyncMock):
                                                with patch.object(engine, "_ws_mark_price_loop", new_callable=AsyncMock):
                                                    await engine.start()

        assert engine._ws_mark_price_task is not None
        assert engine._ws_mark_price_task in engine._tasks

        # 정리
        await engine.stop()

    @pytest.mark.asyncio
    async def test_start_no_mark_price_task_without_derivatives(
        self, app_config, mock_exchange, mock_market_data, mock_om, mock_pm
    ):
        """start() 시 derivatives_data=None이면 mark price 태스크 없음."""
        engine = FuturesEngineV2(
            config=app_config,
            exchange=mock_exchange,
            market_data=mock_market_data,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
        )

        with patch.object(engine, "_check_downtime_stops", new_callable=AsyncMock):
            with patch.object(engine, "_regime_loop", new_callable=AsyncMock):
                with patch.object(engine, "_tier1_loop", new_callable=AsyncMock):
                    with patch.object(engine, "_tier2_loop", new_callable=AsyncMock):
                        with patch.object(engine, "_balance_guard_loop", new_callable=AsyncMock):
                            with patch.object(engine, "_income_loop", new_callable=AsyncMock):
                                with patch.object(engine, "_persist_loop", new_callable=AsyncMock):
                                    with patch.object(engine, "_ws_price_monitor_loop", new_callable=AsyncMock):
                                        with patch.object(engine, "_ws_balance_loop", new_callable=AsyncMock):
                                            with patch.object(engine, "_ws_position_loop", new_callable=AsyncMock):
                                                await engine.start()

        assert engine._ws_mark_price_task is None

        await engine.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_mark_price_task(
        self, app_config, mock_exchange, mock_market_data, mock_om, mock_pm, derivatives_data
    ):
        """stop() 시 mark price 태스크가 None으로 리셋."""
        engine = FuturesEngineV2(
            config=app_config,
            exchange=mock_exchange,
            market_data=mock_market_data,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
            derivatives_data=derivatives_data,
        )

        with patch.object(engine, "_check_downtime_stops", new_callable=AsyncMock):
            with patch.object(engine, "_regime_loop", new_callable=AsyncMock):
                with patch.object(engine, "_tier1_loop", new_callable=AsyncMock):
                    with patch.object(engine, "_tier2_loop", new_callable=AsyncMock):
                        with patch.object(engine, "_balance_guard_loop", new_callable=AsyncMock):
                            with patch.object(engine, "_income_loop", new_callable=AsyncMock):
                                with patch.object(engine, "_persist_loop", new_callable=AsyncMock):
                                    with patch.object(engine, "_ws_price_monitor_loop", new_callable=AsyncMock):
                                        with patch.object(engine, "_ws_balance_loop", new_callable=AsyncMock):
                                            with patch.object(engine, "_ws_position_loop", new_callable=AsyncMock):
                                                with patch.object(engine, "_ws_mark_price_loop", new_callable=AsyncMock):
                                                    await engine.start()

        await engine.stop()
        assert engine._ws_mark_price_task is None


# ── Subtask 3: RegimeDetector Injection ──────────────


class TestRegimeDetectorDerivatives:
    """RegimeDetector 파생상품 데이터 주입 테스트."""

    def test_init_without_derivatives(self):
        """derivatives_data=None 기본값 — 기존 동작 유지."""
        detector = RegimeDetector()
        assert detector._derivatives_data is None

    def test_init_with_derivatives(self, derivatives_data):
        """derivatives_data 주입."""
        detector = RegimeDetector(derivatives_data=derivatives_data)
        assert detector._derivatives_data is derivatives_data

    @pytest.mark.asyncio
    async def test_update_without_derivatives_unchanged(self):
        """derivatives_data=None → 기존 동작과 완전 동일."""
        detector = RegimeDetector()
        df = _make_regime_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        with patch("engine.regime_detector.emit_event", new_callable=AsyncMock):
            state = await detector.update(df, "BTC/USDT")
        assert state.regime == Regime.TRENDING_UP
        assert state.derivatives_snapshot is None

    @pytest.mark.asyncio
    async def test_update_with_empty_snapshot(self, derivatives_data):
        """데이터가 없는 DerivativesDataService → snapshot=None."""
        detector = RegimeDetector(derivatives_data=derivatives_data)
        df = _make_regime_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        with patch("engine.regime_detector.emit_event", new_callable=AsyncMock):
            state = await detector.update(df, "BTC/USDT")
        assert state.regime == Regime.TRENDING_UP
        assert state.derivatives_snapshot is None

    @pytest.mark.asyncio
    async def test_update_with_normal_derivatives(self, derivatives_data):
        """정상 파생상품 데이터 → snapshot 포함, 시그널 없음."""
        derivatives_data.update_mark_price(
            "BTC/USDT",
            _make_mark_price(mark=65050, index=65000, funding=0.0001),
        )
        detector = RegimeDetector(derivatives_data=derivatives_data)
        df = _make_regime_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        with patch("engine.regime_detector.emit_event", new_callable=AsyncMock):
            state = await detector.update(df, "BTC/USDT")
        assert state.derivatives_snapshot is not None
        assert state.derivatives_snapshot["signals"] == []

    @pytest.mark.asyncio
    async def test_premium_extreme_high(self, derivatives_data):
        """프리미엄 극단 (높음) → premium_extreme 시그널."""
        derivatives_data.update_mark_price(
            "BTC/USDT",
            _make_mark_price(mark=65500, index=65000, funding=0.0001),
        )
        detector = RegimeDetector(derivatives_data=derivatives_data)
        df = _make_regime_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        with patch("engine.regime_detector.emit_event", new_callable=AsyncMock):
            state = await detector.update(df, "BTC/USDT")
        assert "premium_extreme" in state.derivatives_snapshot["signals"]

    @pytest.mark.asyncio
    async def test_premium_extreme_low(self, derivatives_data):
        """프리미엄 극단 (낮음) → premium_extreme 시그널."""
        derivatives_data.update_mark_price(
            "BTC/USDT",
            _make_mark_price(mark=64600, index=65000, funding=0.0001),
        )
        detector = RegimeDetector(derivatives_data=derivatives_data)
        df = _make_regime_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        with patch("engine.regime_detector.emit_event", new_callable=AsyncMock):
            state = await detector.update(df, "BTC/USDT")
        assert "premium_extreme" in state.derivatives_snapshot["signals"]

    @pytest.mark.asyncio
    async def test_funding_extreme(self, derivatives_data):
        """펀딩비율 극단 → funding_extreme 시그널."""
        derivatives_data.update_mark_price(
            "BTC/USDT",
            _make_mark_price(mark=65050, index=65000, funding=0.005),
        )
        detector = RegimeDetector(derivatives_data=derivatives_data)
        df = _make_regime_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        with patch("engine.regime_detector.emit_event", new_callable=AsyncMock):
            state = await detector.update(df, "BTC/USDT")
        assert "funding_extreme" in state.derivatives_snapshot["signals"]

    @pytest.mark.asyncio
    async def test_ls_ratio_extreme_long(self, derivatives_data):
        """롱숏비율 극단 (롱 과열) → ls_ratio_extreme 시그널."""
        derivatives_data.update_mark_price(
            "BTC/USDT",
            _make_mark_price(mark=65050, index=65000, funding=0.0001),
        )
        derivatives_data.update_long_short_ratio(
            "BTC/USDT",
            LongShortRatio(
                symbol="BTC/USDT",
                long_account_ratio=0.80,
                short_account_ratio=0.20,  # ratio = 4.0 > 3.0
                long_position_ratio=0.70,
                short_position_ratio=0.30,
                timestamp=datetime.now(timezone.utc),
            ),
        )
        detector = RegimeDetector(derivatives_data=derivatives_data)
        df = _make_regime_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        with patch("engine.regime_detector.emit_event", new_callable=AsyncMock):
            state = await detector.update(df, "BTC/USDT")
        assert "ls_ratio_extreme" in state.derivatives_snapshot["signals"]

    @pytest.mark.asyncio
    async def test_ls_ratio_extreme_short(self, derivatives_data):
        """롱숏비율 극단 (숏 과열) → ls_ratio_extreme 시그널."""
        derivatives_data.update_mark_price(
            "BTC/USDT",
            _make_mark_price(mark=65050, index=65000, funding=0.0001),
        )
        derivatives_data.update_long_short_ratio(
            "BTC/USDT",
            LongShortRatio(
                symbol="BTC/USDT",
                long_account_ratio=0.20,
                short_account_ratio=0.80,  # ratio = 0.25 < 0.33
                long_position_ratio=0.30,
                short_position_ratio=0.70,
                timestamp=datetime.now(timezone.utc),
            ),
        )
        detector = RegimeDetector(derivatives_data=derivatives_data)
        df = _make_regime_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        with patch("engine.regime_detector.emit_event", new_callable=AsyncMock):
            state = await detector.update(df, "BTC/USDT")
        assert "ls_ratio_extreme" in state.derivatives_snapshot["signals"]

    @pytest.mark.asyncio
    async def test_confidence_never_exceeds_bounds(self, derivatives_data):
        """신뢰도 조정 후에도 0.0-1.0 범위 유지."""
        # 높은 기본 신뢰도 + 모든 시그널 활성화 (극단값)
        derivatives_data.update_mark_price(
            "BTC/USDT",
            _make_mark_price(mark=66000, index=65000, funding=0.01),
        )
        derivatives_data.update_long_short_ratio(
            "BTC/USDT",
            LongShortRatio(
                symbol="BTC/USDT",
                long_account_ratio=0.90,
                short_account_ratio=0.10,
                long_position_ratio=0.85,
                short_position_ratio=0.15,
                timestamp=datetime.now(timezone.utc),
            ),
        )
        detector = RegimeDetector(derivatives_data=derivatives_data)
        # VOLATILE 감지 → 모든 보조 시그널이 confidence 증가
        df = _make_regime_df(
            adx=15, bb_upper=90000, bb_lower=70000, bb_mid=80000, ema_slope_dir=0
        )
        with patch("engine.regime_detector.emit_event", new_callable=AsyncMock):
            state = await detector.update(df, "BTC/USDT")
        assert 0.0 <= state.confidence <= 1.0

    @pytest.mark.asyncio
    async def test_regime_not_overridden_by_derivatives(self, derivatives_data):
        """파생상품 시그널이 기존 레짐 분류를 변경하지 않음."""
        # 극단적 파생상품 데이터
        derivatives_data.update_mark_price(
            "BTC/USDT",
            _make_mark_price(mark=67000, index=65000, funding=0.01),
        )
        detector = RegimeDetector(derivatives_data=derivatives_data)
        # 강한 TRENDING_UP 조건
        df = _make_regime_df(adx=35, ema_20=82000, ema_50=79000, ema_slope_dir=1)
        with patch("engine.regime_detector.emit_event", new_callable=AsyncMock):
            state = await detector.update(df, "BTC/USDT")
        # 레짐은 여전히 TRENDING_UP (파생상품 데이터로 VOLATILE로 전환되지 않음)
        assert state.regime == Regime.TRENDING_UP

    @pytest.mark.asyncio
    async def test_derivatives_snapshot_in_regime_state(self, derivatives_data):
        """RegimeState에 derivatives_snapshot 필드 포함."""
        state = RegimeState(
            regime=Regime.RANGING,
            confidence=0.7,
            adx=20.0,
            bb_width=3.0,
            atr_pct=2.0,
            volume_ratio=1.0,
            trend_direction=0,
            timestamp=datetime.now(timezone.utc),
            derivatives_snapshot={"premium_pct": 0.15, "signals": []},
        )
        assert state.derivatives_snapshot is not None
        assert state.derivatives_snapshot["premium_pct"] == 0.15

    def test_regime_state_default_snapshot_none(self):
        """RegimeState의 derivatives_snapshot 기본값은 None."""
        state = RegimeState(
            regime=Regime.RANGING,
            confidence=0.7,
            adx=20.0,
            bb_width=3.0,
            atr_pct=2.0,
            volume_ratio=1.0,
            trend_direction=0,
            timestamp=datetime.now(timezone.utc),
        )
        assert state.derivatives_snapshot is None


class TestFetchDerivativesRest:
    """_fetch_derivatives_rest REST 수집 테스트."""

    @pytest.mark.asyncio
    async def test_rest_updates_cache(
        self, app_config, mock_exchange, mock_market_data, mock_om, mock_pm, derivatives_data
    ):
        """REST 수집이 OI와 LS ratio를 캐시에 업데이트."""
        engine = FuturesEngineV2(
            config=app_config,
            exchange=mock_exchange,
            market_data=mock_market_data,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
            derivatives_data=derivatives_data,
        )

        await engine._fetch_derivatives_rest(["BTC/USDT"])

        assert derivatives_data.get_open_interest("BTC/USDT") is not None
        assert derivatives_data.get_long_short_ratio("BTC/USDT") is not None

    @pytest.mark.asyncio
    async def test_rest_graceful_on_error(
        self, app_config, mock_exchange, mock_market_data, mock_om, mock_pm, derivatives_data
    ):
        """REST 에러 시 예외 없이 계속."""
        mock_exchange.fetch_open_interest = AsyncMock(side_effect=Exception("API error"))
        mock_exchange.fetch_long_short_ratio = AsyncMock(side_effect=Exception("API error"))

        engine = FuturesEngineV2(
            config=app_config,
            exchange=mock_exchange,
            market_data=mock_market_data,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
            derivatives_data=derivatives_data,
        )

        # 예외 없이 완료
        await engine._fetch_derivatives_rest(["BTC/USDT"])

        assert derivatives_data.get_open_interest("BTC/USDT") is None
        assert derivatives_data.get_long_short_ratio("BTC/USDT") is None
