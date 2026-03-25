"""COIN-49: V2 레짐 상태와 에이전트 시장 상태 불일치 표시 테스트.

V2 엔진이 활성화된 경우 market-analysis/latest API가 v2_regime 필드를 포함하는지,
V2 엔진이 없는 경우 v2_regime 필드가 없는지 검증.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from api.dashboard import router as dashboard_router
from api.dependencies import engine_registry
from core.enums import MarketState, Regime
from core.models import Base
from engine.regime_detector import RegimeState


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard_router)
    return app


def _save_registry_state(exchange: str):
    saved = {
        "engine": engine_registry._engines.get(exchange),
        "pm": engine_registry._portfolio_managers.get(exchange),
        "comb": engine_registry._combiners.get(exchange),
        "coord": engine_registry._coordinators.get(exchange),
    }
    return saved


def _register(name: str, engine, combiner=None, coordinator=None) -> None:
    engine_registry._engines[name] = engine
    engine_registry._portfolio_managers[name] = None
    engine_registry._combiners[name] = combiner
    engine_registry._coordinators[name] = coordinator


def _restore(name: str, saved: dict) -> None:
    for store, key in [
        (engine_registry._engines, "engine"),
        (engine_registry._portfolio_managers, "pm"),
        (engine_registry._combiners, "comb"),
        (engine_registry._coordinators, "coord"),
    ]:
        if saved[key] is None:
            store.pop(name, None)
        else:
            store[name] = saved[key]


async def _db_override():
    """Provide an in-memory SQLite session for the endpoint."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as sess:
        yield sess
    await engine.dispose()


def _make_regime_state(
    regime: Regime = Regime.RANGING,
    confidence: float = 0.75,
    adx: float = 18.5,
    atr_pct: float = 2.1,
    trend_direction: int = 0,
) -> RegimeState:
    """Create a RegimeState for testing."""
    return RegimeState(
        regime=regime,
        confidence=confidence,
        adx=adx,
        bb_width=3.5,
        atr_pct=atr_pct,
        volume_ratio=1.2,
        trend_direction=trend_direction,
        timestamp=datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc),
    )


def _mock_v2_engine_with_regime(regime_state: RegimeState | None = None):
    """V2 engine mock with regime_detector property (FuturesEngineV2 public API)."""
    eng = MagicMock()
    eng.is_running = True
    detector = MagicMock()
    detector.current = regime_state
    eng.regime_detector = detector
    return eng


def _mock_coordinator_with_analysis(state: MarketState = MarketState.UPTREND):
    """Coordinator mock with last_market_analysis."""
    from agents.market_analysis import MarketAnalysis

    coord = MagicMock()
    coord.last_market_analysis = MarketAnalysis(
        state=state,
        confidence=0.8,
        volatility_level="medium",
        reasoning="Test reasoning",
        indicators={"rsi": 55.0},
        recommended_weights={"rsi": 0.21, "bollinger_rsi": 0.26},
    )
    return coord


def _mock_engine_no_regime():
    """V1 engine without regime_detector property."""
    eng = MagicMock(spec=[])
    eng.is_running = True
    eng.strategies = {}
    eng.tracked_coins = []
    eng._ec = MagicMock()
    eng._ec.mode = "paper"
    eng._ec.evaluation_interval_sec = 300
    return eng


# ── Tests: V2 regime in market-analysis/latest ───────────────────────────────


class TestV2RegimeInMarketAnalysis:
    """COIN-49: market-analysis/latest includes v2_regime when V2 engine is active."""

    @pytest.mark.asyncio
    async def test_includes_v2_regime_when_v2_engine_active(self):
        """V2 엔진 활성화 시 v2_regime 필드가 응답에 포함된다."""
        exchange = "binance_futures"
        saved = _save_registry_state(exchange)
        regime = _make_regime_state(
            Regime.TRENDING_UP,
            confidence=0.85,
            adx=32.1,
            atr_pct=1.8,
            trend_direction=1,
        )
        eng = _mock_v2_engine_with_regime(regime)
        coord = _mock_coordinator_with_analysis(MarketState.UPTREND)
        _register(exchange, eng, coordinator=coord)
        try:
            from db.session import get_db

            app = _make_test_app()
            app.dependency_overrides[get_db] = _db_override
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    "/agents/market-analysis/latest", params={"exchange": exchange}
                )
            assert resp.status_code == 200
            data = resp.json()
            # Agent state present
            assert data["state"] == "uptrend"
            assert data["confidence"] == 0.8
            # V2 regime present
            assert "v2_regime" in data
            v2 = data["v2_regime"]
            assert v2["regime"] == "trending_up"
            assert v2["confidence"] == 0.85
            assert v2["adx"] == 32.1
            assert v2["atr_pct"] == 1.8
            assert v2["trend_direction"] == 1
            assert "timestamp" in v2
        finally:
            _restore(exchange, saved)

    @pytest.mark.asyncio
    async def test_no_v2_regime_when_v1_engine(self):
        """V1 엔진(RegimeDetector 없음) 시 v2_regime 필드가 없다."""
        exchange = "bithumb"
        saved = _save_registry_state(exchange)
        eng = _mock_engine_no_regime()
        coord = _mock_coordinator_with_analysis(MarketState.SIDEWAYS)
        _register(exchange, eng, coordinator=coord)
        try:
            from db.session import get_db

            app = _make_test_app()
            app.dependency_overrides[get_db] = _db_override
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    "/agents/market-analysis/latest", params={"exchange": exchange}
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["state"] == "sideways"
            assert "v2_regime" not in data
        finally:
            _restore(exchange, saved)

    @pytest.mark.asyncio
    async def test_v2_regime_none_when_no_regime_detected_yet(self):
        """V2 엔진이 있지만 아직 레짐 감지 전(current=None)이면 v2_regime 없음."""
        exchange = "binance_futures"
        saved = _save_registry_state(exchange)
        eng = _mock_v2_engine_with_regime(None)  # No regime yet
        coord = _mock_coordinator_with_analysis(MarketState.SIDEWAYS)
        _register(exchange, eng, coordinator=coord)
        try:
            from db.session import get_db

            app = _make_test_app()
            app.dependency_overrides[get_db] = _db_override
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    "/agents/market-analysis/latest", params={"exchange": exchange}
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["state"] == "sideways"
            assert "v2_regime" not in data
        finally:
            _restore(exchange, saved)

    @pytest.mark.asyncio
    async def test_v2_regime_all_states(self):
        """모든 V2 레짐 상태가 올바르게 직렬화된다."""
        exchange = "binance_futures"
        saved = _save_registry_state(exchange)
        from db.session import get_db

        for regime_enum, expected_str in [
            (Regime.TRENDING_UP, "trending_up"),
            (Regime.TRENDING_DOWN, "trending_down"),
            (Regime.RANGING, "ranging"),
            (Regime.VOLATILE, "volatile"),
        ]:
            regime = _make_regime_state(regime_enum)
            eng = _mock_v2_engine_with_regime(regime)
            coord = _mock_coordinator_with_analysis()
            _register(exchange, eng, coordinator=coord)
            try:
                app = _make_test_app()
                app.dependency_overrides[get_db] = _db_override
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    resp = await client.get(
                        "/agents/market-analysis/latest", params={"exchange": exchange}
                    )
                assert resp.status_code == 200
                data = resp.json()
                assert data["v2_regime"]["regime"] == expected_str
            finally:
                _restore(exchange, saved)

    @pytest.mark.asyncio
    async def test_v2_regime_in_unknown_fallback(self):
        """코디네이터 분석 없고 DB도 비었을 때 "unknown" 응답에도 v2_regime 포함."""
        exchange = "binance_futures"
        saved = _save_registry_state(exchange)
        regime = _make_regime_state(Regime.VOLATILE, confidence=0.65)
        eng = _mock_v2_engine_with_regime(regime)
        # Coordinator with no analysis and empty DB → hits final "unknown" fallback
        coord = MagicMock()
        coord.last_market_analysis = None
        _register(exchange, eng, coordinator=coord)
        try:
            from db.session import get_db

            app = _make_test_app()
            app.dependency_overrides[get_db] = _db_override
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    "/agents/market-analysis/latest", params={"exchange": exchange}
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["state"] == "unknown"
            # V2 regime still attached even in the "unknown" fallback path
            assert "v2_regime" in data
            assert data["v2_regime"]["regime"] == "volatile"
        finally:
            _restore(exchange, saved)


class TestGetV2RegimeHelper:
    """_get_v2_regime helper function 단위 테스트."""

    def test_returns_none_when_no_engine(self):
        """엔진 없으면 None 반환."""
        from api.dashboard import _get_v2_regime

        exchange = "nonexistent"
        saved = _save_registry_state(exchange)
        engine_registry._engines.pop(exchange, None)
        try:
            result = _get_v2_regime(exchange)
            assert result is None
        finally:
            _restore(exchange, saved)

    def test_returns_none_when_no_regime_attr(self):
        """V1 엔진(_regime 없음)이면 None 반환."""
        from api.dashboard import _get_v2_regime

        exchange = "bithumb"
        saved = _save_registry_state(exchange)
        eng = _mock_engine_no_regime()
        _register(exchange, eng)
        try:
            result = _get_v2_regime(exchange)
            assert result is None
        finally:
            _restore(exchange, saved)

    def test_returns_none_when_regime_current_is_none(self):
        """V2 엔진이지만 current가 None이면 None 반환."""
        from api.dashboard import _get_v2_regime

        exchange = "binance_futures"
        saved = _save_registry_state(exchange)
        eng = _mock_v2_engine_with_regime(None)
        _register(exchange, eng)
        try:
            result = _get_v2_regime(exchange)
            assert result is None
        finally:
            _restore(exchange, saved)

    def test_returns_regime_dict_when_available(self):
        """V2 레짐 상태가 있으면 올바른 dict 반환."""
        from api.dashboard import _get_v2_regime

        exchange = "binance_futures"
        saved = _save_registry_state(exchange)
        regime = _make_regime_state(
            Regime.TRENDING_DOWN,
            confidence=0.9,
            adx=35.0,
            atr_pct=3.5,
            trend_direction=-1,
        )
        eng = _mock_v2_engine_with_regime(regime)
        _register(exchange, eng)
        try:
            result = _get_v2_regime(exchange)
            assert result is not None
            assert result["regime"] == "trending_down"
            assert result["confidence"] == 0.9
            assert result["adx"] == 35.0
            assert result["atr_pct"] == 3.5
            assert result["trend_direction"] == -1
            assert "timestamp" in result
        finally:
            _restore(exchange, saved)

    def test_confidence_rounded_to_3_decimals(self):
        """confidence 값이 소수점 3자리로 반올림."""
        from api.dashboard import _get_v2_regime

        exchange = "binance_futures"
        saved = _save_registry_state(exchange)
        regime = _make_regime_state(Regime.RANGING, confidence=0.666666)
        eng = _mock_v2_engine_with_regime(regime)
        _register(exchange, eng)
        try:
            result = _get_v2_regime(exchange)
            assert result["confidence"] == 0.667
        finally:
            _restore(exchange, saved)

    def test_returns_none_on_serialization_error(self):
        """RegimeState 직렬화 중 예외 발생 시 None 반환 — API 500 방지."""
        from api.dashboard import _get_v2_regime

        exchange = "binance_futures"
        saved = _save_registry_state(exchange)
        # timestamp.isoformat() 에서 AttributeError 발생하도록 설정
        broken_regime_state = MagicMock()
        broken_regime_state.regime.value = "trending_up"
        broken_regime_state.confidence = 0.8
        broken_regime_state.adx = 25.0
        broken_regime_state.atr_pct = 2.0
        broken_regime_state.trend_direction = 1
        broken_regime_state.timestamp.isoformat.side_effect = AttributeError("no timestamp")
        eng = _mock_v2_engine_with_regime(broken_regime_state)
        _register(exchange, eng)
        try:
            result = _get_v2_regime(exchange)
            assert result is None
        finally:
            _restore(exchange, saved)
