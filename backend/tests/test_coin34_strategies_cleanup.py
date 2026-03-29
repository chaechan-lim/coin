"""
COIN-34: Tests for inactive strategy removal and grouped signal logs.

1. FuturesEngineV2.strategies property returns only 4 active strategies
2. GET /api/v1/strategies/logs/grouped returns cycle-grouped signals
3. _compute_combined_signal mirrors SignalCombiner logic
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from api.strategies import router as strategies_router, _compute_combined_signal
from api.dependencies import engine_registry
from core.models import Base, StrategyLog
from core.schemas import SignalCycleGroupResponse, StrategySignalItem


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_test_app() -> FastAPI:
    app = FastAPI()
    # strategies_router already has prefix="/strategies"
    app.include_router(strategies_router)
    return app


def _mock_engine(strategies: dict | None = None) -> MagicMock:
    eng = MagicMock()
    eng.is_running = True
    eng.strategies = strategies or {}
    eng.tracked_coins = []
    eng._ec = MagicMock()
    eng._ec.mode = "live"
    eng._ec.evaluation_interval_sec = 300
    return eng


def _mock_combiner(
    weights: dict | None = None, min_confidence: float = 0.55
) -> MagicMock:
    comb = MagicMock()
    comb.weights = weights or {
        "cis_momentum": 0.42,
        "bnf_deviation": 0.25,
        "donchian_channel": 0.23,
        "larry_williams": 0.10,
    }
    comb.min_confidence = min_confidence
    return comb


def _save_and_clear(exchange: str):
    return {
        "engine": engine_registry._engines.get(exchange),
        "pm": engine_registry._portfolio_managers.get(exchange),
        "comb": engine_registry._combiners.get(exchange),
        "coord": engine_registry._coordinators.get(exchange),
    }


def _register(name: str, engine, combiner=None) -> None:
    engine_registry._engines[name] = engine
    engine_registry._portfolio_managers[name] = None
    engine_registry._combiners[name] = combiner
    engine_registry._coordinators[name] = None


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
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as sess:
        yield sess
    await engine.dispose()


async def _db_with_logs():
    """DB session pre-populated with strategy logs for two cycles."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as sess:
        now = datetime(2026, 3, 19, 12, 0, 0, tzinfo=timezone.utc)
        # Cycle 1: BTC/USDT at 12:00
        for strat, sig, conf in [
            ("cis_momentum", "BUY", 0.72),
            ("bnf_deviation", "BUY", 0.68),
            ("donchian_channel", "HOLD", 0.45),
            ("larry_williams", "BUY", 0.65),
        ]:
            sess.add(
                StrategyLog(
                    exchange="binance_futures",
                    strategy_name=strat,
                    symbol="BTC/USDT",
                    signal_type=sig,
                    confidence=conf,
                    reason=f"{strat} signal",
                    was_executed=(strat == "cis_momentum"),
                    logged_at=now + timedelta(seconds=1),
                )
            )

        # Cycle 2: ETH/USDT at 12:05
        cycle2_time = now + timedelta(minutes=5)
        for strat, sig, conf in [
            ("cis_momentum", "SELL", 0.62),
            ("bnf_deviation", "SELL", 0.58),
            ("donchian_channel", "SELL", 0.70),
            ("larry_williams", "HOLD", 0.40),
        ]:
            sess.add(
                StrategyLog(
                    exchange="binance_futures",
                    strategy_name=strat,
                    symbol="ETH/USDT",
                    signal_type=sig,
                    confidence=conf,
                    reason=f"{strat} signal",
                    was_executed=False,
                    logged_at=cycle2_time + timedelta(seconds=2),
                )
            )

        await sess.commit()
        yield sess
    await engine.dispose()


async def _db_with_many_cycles():
    """DB session pre-populated with 5 distinct evaluation cycles for pagination tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as sess:
        base = datetime(2026, 3, 19, 12, 0, 0, tzinfo=timezone.utc)
        symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT"]
        for idx, sym in enumerate(symbols):
            cycle_time = base + timedelta(minutes=idx * 5)
            for strat, sig, conf in [
                ("cis_momentum", "BUY", 0.70 + idx * 0.01),
                ("bnf_deviation", "BUY", 0.65 + idx * 0.01),
                ("donchian_channel", "HOLD", 0.45),
                ("larry_williams", "SELL", 0.60),
            ]:
                sess.add(
                    StrategyLog(
                        exchange="binance_futures",
                        strategy_name=strat,
                        symbol=sym,
                        signal_type=sig,
                        confidence=conf,
                        reason=f"{strat} signal",
                        was_executed=False,
                        logged_at=cycle_time + timedelta(seconds=1),
                    )
                )
        await sess.commit()
        yield sess
    await engine.dispose()


# ── 1. FuturesEngineV2.strategies — only active 4 strategies ────────────────


class TestFuturesEngineV2StrategiesProperty:
    """Verify that FuturesEngineV2.strategies excludes regime strategies."""

    def test_strategies_spot_mode_returns_only_spot_strategies(self):
        """COIN-46: strategy_mode=spot → SpotEvaluator의 현물 4전략만 반환."""
        from engine.futures_engine_v2 import FuturesEngineV2

        mock_strategy_1 = MagicMock()
        mock_strategy_1.name = "cis_momentum"
        mock_strategy_2 = MagicMock()
        mock_strategy_2.name = "bnf_deviation"
        mock_strategy_3 = MagicMock()
        mock_strategy_3.name = "donchian_channel"
        mock_strategy_4 = MagicMock()
        mock_strategy_4.name = "larry_williams"

        engine = MagicMock(spec=FuturesEngineV2)
        engine._strategy_mode = "spot"
        engine._long_evaluator = MagicMock()
        engine._long_evaluator._strategies = [
            mock_strategy_1,
            mock_strategy_2,
            mock_strategy_3,
            mock_strategy_4,
        ]

        result = FuturesEngineV2.strategies.fget(engine)

        assert set(result.keys()) == {
            "cis_momentum",
            "bnf_deviation",
            "donchian_channel",
            "larry_williams",
        }

    def test_strategies_regime_mode_returns_regime_strategies(self):
        """COIN-46: strategy_mode=regime → 레짐 3전략 반환."""
        from engine.futures_engine_v2 import FuturesEngineV2

        regime_strat_1 = MagicMock()
        regime_strat_1.name = "trend_follower"
        regime_strat_2 = MagicMock()
        regime_strat_2.name = "mean_reversion"
        regime_strat_3 = MagicMock()
        regime_strat_3.name = "vol_breakout"

        engine = MagicMock(spec=FuturesEngineV2)
        engine._strategy_mode = "regime"
        engine._strategies = MagicMock()
        engine._strategies.all_strategies = {
            "TRENDING_UP": regime_strat_1,
            "TRENDING_DOWN": regime_strat_1,  # same instance
            "RANGING": regime_strat_2,
            "VOLATILE": regime_strat_3,
        }

        result = FuturesEngineV2.strategies.fget(engine)

        assert set(result.keys()) == {
            "trend_follower",
            "mean_reversion",
            "vol_breakout",
        }

    def test_strategies_no_evaluator_returns_empty(self):
        """If evaluator has no _strategies, return empty dict (spot mode)."""
        from engine.futures_engine_v2 import FuturesEngineV2

        engine = MagicMock(spec=FuturesEngineV2)
        engine._strategy_mode = "spot"
        engine._long_evaluator = MagicMock(spec=[])  # no _strategies attr

        result = FuturesEngineV2.strategies.fget(engine)
        assert result == {}

    def test_strategies_count_is_four_spot_mode(self):
        """COIN-46: strategy_mode=spot → 4 active strategies."""
        from engine.futures_engine_v2 import FuturesEngineV2

        strats = []
        for name in [
            "cis_momentum",
            "bnf_deviation",
            "donchian_channel",
            "larry_williams",
        ]:
            s = MagicMock()
            s.name = name
            strats.append(s)

        engine = MagicMock(spec=FuturesEngineV2)
        engine._strategy_mode = "spot"
        engine._long_evaluator = MagicMock()
        engine._long_evaluator._strategies = strats

        result = FuturesEngineV2.strategies.fget(engine)
        assert len(result) == 4


# ── 2. _compute_combined_signal tests ───────────────────────────────────────


class TestComputeCombinedSignal:
    """Test the backend combined signal computation."""

    def _make_log(self, strategy_name: str, signal_type: str, confidence: float):
        log = MagicMock()
        log.strategy_name = strategy_name
        log.signal_type = signal_type
        log.confidence = confidence
        return log

    def test_all_buy_above_threshold(self):
        weights = {"a": 0.5, "b": 0.5}
        logs = [
            self._make_log("a", "BUY", 0.8),
            self._make_log("b", "BUY", 0.7),
        ]
        action, conf = _compute_combined_signal(logs, weights, 0.55)
        assert action == "BUY"
        assert conf > 0.55

    def test_all_sell_above_threshold(self):
        weights = {"a": 0.5, "b": 0.5}
        logs = [
            self._make_log("a", "SELL", 0.8),
            self._make_log("b", "SELL", 0.7),
        ]
        action, conf = _compute_combined_signal(logs, weights, 0.55)
        assert action == "SELL"
        assert conf > 0.55

    def test_hold_is_abstain(self):
        """HOLD strategies should not contribute to active weight."""
        weights = {"a": 0.5, "b": 0.5}
        logs = [
            self._make_log("a", "HOLD", 0.5),
            self._make_log("b", "HOLD", 0.5),
        ]
        action, conf = _compute_combined_signal(logs, weights, 0.55)
        assert action == "HOLD"
        assert conf == 0.0

    def test_below_min_active_weight_returns_hold(self):
        """When active weight < 0.12, return HOLD."""
        weights = {"a": 0.05, "b": 0.05}
        logs = [
            self._make_log("a", "BUY", 0.9),
            self._make_log("b", "HOLD", 0.5),
        ]
        action, conf = _compute_combined_signal(logs, weights, 0.55)
        assert action == "HOLD"

    def test_below_min_confidence_returns_hold(self):
        """When winning score < min_confidence, return HOLD with the score."""
        weights = {"a": 0.5, "b": 0.5}
        logs = [
            self._make_log("a", "BUY", 0.40),
            self._make_log("b", "BUY", 0.30),
        ]
        action, conf = _compute_combined_signal(logs, weights, 0.55)
        assert action == "HOLD"
        assert conf > 0  # score is recorded even though below threshold

    def test_mixed_signals_buy_wins(self):
        weights = {"a": 0.6, "b": 0.4}
        logs = [
            self._make_log("a", "BUY", 0.95),
            self._make_log("b", "SELL", 0.6),
        ]
        action, conf = _compute_combined_signal(logs, weights, 0.55)
        # buy_norm = (0.6*0.95)/(0.6+0.4) = 0.57 > 0.55
        assert action == "BUY"

    def test_empty_logs_returns_hold(self):
        action, conf = _compute_combined_signal([], {}, 0.55)
        assert action == "HOLD"
        assert conf == 0.0

    def test_unknown_strategy_uses_default_weight(self):
        """Strategies not in weights dict get default weight of 0.1."""
        logs = [
            self._make_log("unknown_strat", "BUY", 0.9),
        ]
        action, conf = _compute_combined_signal(logs, {}, 0.55)
        # active_weight = 0.1 which is < 0.12 MIN_ACTIVE_WEIGHT
        assert action == "HOLD"


# ── 3. Grouped signal logs endpoint tests ───────────────────────────────────


@pytest.mark.asyncio
async def test_grouped_logs_returns_cycle_groups():
    """GET /strategies/logs/grouped returns cycle-grouped signals."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = _mock_engine(
        strategies={
            "cis_momentum": MagicMock(),
            "bnf_deviation": MagicMock(),
        }
    )
    comb = _mock_combiner()
    _register(exchange, eng, combiner=comb)
    try:
        from db.session import get_db

        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_with_logs
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/strategies/logs/grouped", params={"exchange": exchange}
            )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2  # Two cycles: BTC + ETH

        # Each group should have expected fields
        for group in data:
            assert "symbol" in group
            assert "cycle_time" in group
            assert "combined_signal" in group
            assert "combined_confidence" in group
            assert "strategy_count" in group
            assert "executed" in group
            assert "signals" in group
            assert isinstance(group["signals"], list)
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_grouped_logs_symbol_filter():
    """GET /strategies/logs/grouped?symbol=BTC/USDT filters by symbol."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = _mock_engine()
    comb = _mock_combiner()
    _register(exchange, eng, combiner=comb)
    try:
        from db.session import get_db

        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_with_logs
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/strategies/logs/grouped",
                params={
                    "exchange": exchange,
                    "symbol": "BTC/USDT",
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["symbol"] == "BTC/USDT"
        assert data[0]["strategy_count"] == 4
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_grouped_logs_combined_signal_buy():
    """BTC cycle has 3 BUY + 1 HOLD → combined should be BUY."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = _mock_engine()
    comb = _mock_combiner()
    _register(exchange, eng, combiner=comb)
    try:
        from db.session import get_db

        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_with_logs
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/strategies/logs/grouped",
                params={
                    "exchange": exchange,
                    "symbol": "BTC/USDT",
                },
            )
        data = resp.json()
        assert data[0]["combined_signal"] == "BUY"
        assert data[0]["combined_confidence"] > 0.55
        assert data[0]["executed"] is True  # cis_momentum was executed
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_grouped_logs_combined_signal_sell():
    """ETH cycle has 3 SELL + 1 HOLD → combined should be SELL."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = _mock_engine()
    comb = _mock_combiner()
    _register(exchange, eng, combiner=comb)
    try:
        from db.session import get_db

        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_with_logs
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/strategies/logs/grouped",
                params={
                    "exchange": exchange,
                    "symbol": "ETH/USDT",
                },
            )
        data = resp.json()
        assert data[0]["combined_signal"] == "SELL"
        assert data[0]["combined_confidence"] > 0.55
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_grouped_logs_signals_contain_strategy_details():
    """Each group's signals list contains individual strategy details."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = _mock_engine()
    comb = _mock_combiner()
    _register(exchange, eng, combiner=comb)
    try:
        from db.session import get_db

        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_with_logs
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/strategies/logs/grouped",
                params={
                    "exchange": exchange,
                    "symbol": "BTC/USDT",
                },
            )
        data = resp.json()
        signals = data[0]["signals"]
        assert len(signals) == 4

        strategy_names = {s["strategy_name"] for s in signals}
        assert strategy_names == {
            "cis_momentum",
            "bnf_deviation",
            "donchian_channel",
            "larry_williams",
        }

        for sig in signals:
            assert "signal_type" in sig
            assert "confidence" in sig
            assert "reason" in sig
            assert "was_executed" in sig
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_grouped_logs_empty_exchange():
    """Empty exchange returns empty list."""
    exchange = "binance_spot"
    saved = _save_and_clear(exchange)
    eng = _mock_engine()
    _register(exchange, eng)
    try:
        from db.session import get_db

        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_with_logs
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/strategies/logs/grouped", params={"exchange": exchange}
            )
        assert resp.status_code == 200
        assert resp.json() == []
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_grouped_logs_pagination():
    """Pagination limits number of groups returned."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = _mock_engine()
    comb = _mock_combiner()
    _register(exchange, eng, combiner=comb)
    try:
        from db.session import get_db

        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_with_logs
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/strategies/logs/grouped",
                params={
                    "exchange": exchange,
                    "size": 1,
                },
            )
        data = resp.json()
        assert len(data) == 1  # Only first group
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_grouped_logs_multipage_pagination_no_duplicates():
    """Pages return disjoint, complete groups — no split cycles or duplicates.

    Inserts 5 evaluation cycles. page=1,size=2 and page=2,size=2 must
    return 4 distinct, complete groups with no overlap.  page=3,size=2
    returns the remaining 1 group.
    """
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = _mock_engine()
    comb = _mock_combiner()
    _register(exchange, eng, combiner=comb)
    try:
        from db.session import get_db

        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_with_many_cycles
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Page 1: first 2 groups
            resp1 = await client.get(
                "/strategies/logs/grouped",
                params={"exchange": exchange, "page": 1, "size": 2},
            )
            assert resp1.status_code == 200
            page1 = resp1.json()
            assert len(page1) == 2

            # Page 2: next 2 groups
            resp2 = await client.get(
                "/strategies/logs/grouped",
                params={"exchange": exchange, "page": 2, "size": 2},
            )
            assert resp2.status_code == 200
            page2 = resp2.json()
            assert len(page2) == 2

            # Page 3: remaining 1 group
            resp3 = await client.get(
                "/strategies/logs/grouped",
                params={"exchange": exchange, "page": 3, "size": 2},
            )
            assert resp3.status_code == 200
            page3 = resp3.json()
            assert len(page3) == 1

        # All groups should have 4 strategies each (no split cycles)
        all_groups = page1 + page2 + page3
        for group in all_groups:
            assert group["strategy_count"] == 4, (
                f"Cycle for {group['symbol']} has {group['strategy_count']} "
                f"strategies, expected 4 (split cycle bug?)"
            )

        # Symbols across pages should be disjoint (no duplicates)
        page1_symbols = {g["symbol"] for g in page1}
        page2_symbols = {g["symbol"] for g in page2}
        page3_symbols = {g["symbol"] for g in page3}
        assert page1_symbols.isdisjoint(page2_symbols), (
            f"Page 1 and 2 overlap: {page1_symbols & page2_symbols}"
        )
        assert page2_symbols.isdisjoint(page3_symbols), (
            f"Page 2 and 3 overlap: {page2_symbols & page3_symbols}"
        )

        # All 5 symbols should be accounted for
        all_symbols = page1_symbols | page2_symbols | page3_symbols
        assert len(all_symbols) == 5
    finally:
        _restore(exchange, saved)


@pytest.mark.asyncio
async def test_grouped_logs_no_combiner_uses_defaults():
    """Without a combiner, should still compute signal with defaults."""
    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = _mock_engine()
    _register(exchange, eng, combiner=None)
    try:
        from db.session import get_db

        app = _make_test_app()
        app.dependency_overrides[get_db] = _db_with_logs
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/strategies/logs/grouped", params={"exchange": exchange}
            )
        assert resp.status_code == 200
        data = resp.json()
        # Should still return groups, using default weight 0.1
        assert len(data) >= 1
    finally:
        _restore(exchange, saved)


# ── 4. Schema validation tests ──────────────────────────────────────────────


class TestSchemas:
    def test_signal_cycle_group_response_validates(self):
        """SignalCycleGroupResponse schema validates correctly."""
        group = SignalCycleGroupResponse(
            symbol="BTC/USDT",
            cycle_time=datetime(2026, 3, 19, 12, 0, 0, tzinfo=timezone.utc),
            combined_signal="BUY",
            combined_confidence=0.72,
            strategy_count=4,
            executed=True,
            signals=[
                StrategySignalItem(
                    strategy_name="cis_momentum",
                    signal_type="BUY",
                    confidence=0.72,
                    reason="Strong momentum",
                    was_executed=True,
                ),
            ],
        )
        assert group.symbol == "BTC/USDT"
        assert group.combined_signal == "BUY"
        assert len(group.signals) == 1

    def test_strategy_signal_item_validates(self):
        """StrategySignalItem schema validates correctly."""
        item = StrategySignalItem(
            strategy_name="bnf_deviation",
            signal_type="SELL",
            confidence=0.65,
            reason="High deviation",
            was_executed=False,
        )
        assert item.strategy_name == "bnf_deviation"
        assert item.signal_type == "SELL"

    def test_signal_cycle_group_hold_zero_confidence(self):
        """HOLD groups can have zero confidence."""
        group = SignalCycleGroupResponse(
            symbol="ETH/USDT",
            cycle_time=datetime.now(timezone.utc),
            combined_signal="HOLD",
            combined_confidence=0.0,
            strategy_count=4,
            executed=False,
            signals=[],
        )
        assert group.combined_signal == "HOLD"
        assert group.combined_confidence == 0.0


# ── 5. Engine status reflects active strategies only ────────────────────────


@pytest.mark.asyncio
async def test_engine_status_shows_only_active_strategies():
    """Engine status should only show 4 active strategies, not regime strategies."""
    from api.dashboard import router as dashboard_router

    exchange = "binance_futures"
    saved = _save_and_clear(exchange)
    eng = _mock_engine(
        strategies={
            "cis_momentum": MagicMock(),
            "bnf_deviation": MagicMock(),
            "donchian_channel": MagicMock(),
            "larry_williams": MagicMock(),
        }
    )
    _register(exchange, eng)
    try:
        from db.session import get_db

        app = FastAPI()
        app.include_router(dashboard_router)
        app.dependency_overrides[get_db] = _db_override
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/engine/status", params={"exchange": exchange})
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["strategies_active"]) == {
            "cis_momentum",
            "bnf_deviation",
            "donchian_channel",
            "larry_williams",
        }
        # Regime strategies should NOT appear
        assert "trend_follower" not in data["strategies_active"]
        assert "mean_reversion" not in data["strategies_active"]
        assert "vol_breakout" not in data["strategies_active"]
    finally:
        _restore(exchange, saved)
