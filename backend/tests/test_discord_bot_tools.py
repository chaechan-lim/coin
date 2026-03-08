"""Discord 봇 도구 핸들러 테스트."""
import pytest
from datetime import timedelta
from unittest.mock import MagicMock, AsyncMock, patch

from services.discord_bot.tools import (
    ToolContext,
    execute_tool,
    TOOL_DEFINITIONS,
    WRITE_TOOLS,
    _get_exchanges,
)
from core.models import Order, Position, DailyPnL
from core.utils import utcnow


@pytest.fixture
def mock_registry():
    """EngineRegistry mock."""
    registry = MagicMock()
    registry.available_exchanges = ["bithumb", "binance_futures"]

    # bithumb engine
    bithumb_engine = MagicMock()
    bithumb_engine.is_running = True
    bithumb_engine._ec.mode = "paper"
    bithumb_engine.tracked_coins = ["BTC/KRW", "ETH/KRW"]

    # binance engine
    binance_engine = MagicMock()
    binance_engine.is_running = True
    binance_engine._ec.mode = "live"
    binance_engine.tracked_coins = ["BTC/USDT", "ETH/USDT"]

    def get_engine(name):
        return {"bithumb": bithumb_engine, "binance_futures": binance_engine}.get(name)
    registry.get_engine = get_engine

    # portfolio managers
    bithumb_pm = MagicMock()
    bithumb_pm.cash_balance = 500_000
    bithumb_pm.initial_balance = 1_000_000

    binance_pm = MagicMock()
    binance_pm.cash_balance = 800.0
    binance_pm.initial_balance = 1000.0

    def get_pm(name):
        return {"bithumb": bithumb_pm, "binance_futures": binance_pm}.get(name)
    registry.get_portfolio_manager = get_pm

    # coordinators
    coord = MagicMock()
    coord.last_market_analysis = None
    coord.run_performance_analysis = AsyncMock()
    coord.run_strategy_advice = AsyncMock()
    coord.run_trade_review = AsyncMock()
    registry.get_coordinator = MagicMock(return_value=coord)

    return registry


@pytest.fixture
def ctx(mock_registry, session_factory):
    return ToolContext(engine_registry=mock_registry, session_factory=session_factory)


# ── get_exchanges 헬퍼 ──────────────────────────────────────

def test_get_exchanges_all(ctx):
    assert _get_exchanges(ctx, None) == ["bithumb", "binance_futures"]


def test_get_exchanges_specific(ctx):
    assert _get_exchanges(ctx, "bithumb") == ["bithumb"]


def test_get_exchanges_unknown(ctx):
    assert _get_exchanges(ctx, "unknown") == []


# ── engine_status ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_engine_status_all(ctx):
    result = await execute_tool(ctx, "get_engine_status", {})
    assert "bithumb" in result
    assert result["bithumb"]["running"] is True
    assert result["bithumb"]["mode"] == "paper"
    assert "binance_futures" in result
    assert result["binance_futures"]["mode"] == "live"


@pytest.mark.asyncio
async def test_engine_status_single(ctx):
    result = await execute_tool(ctx, "get_engine_status", {"exchange": "binance_futures"})
    assert "binance_futures" in result
    assert "bithumb" not in result


# ── portfolio_summary ──────────────────────────────────────

@pytest.mark.asyncio
async def test_portfolio_summary(ctx, session):
    pos = Position(
        exchange="bithumb", symbol="BTC/KRW", quantity=0.1,
        average_buy_price=50_000_000, total_invested=5_000_000,
        current_value=5_500_000, unrealized_pnl=500_000,
        unrealized_pnl_pct=10.0,
    )
    session.add(pos)
    await session.commit()

    result = await execute_tool(ctx, "get_portfolio_summary", {"exchange": "bithumb"})
    assert "bithumb" in result
    assert result["bithumb"]["position_count"] == 1
    assert result["bithumb"]["currency"] == "KRW"


# ── positions ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_positions_empty(ctx):
    result = await execute_tool(ctx, "get_positions", {"exchange": "bithumb"})
    assert result["bithumb"] == []


@pytest.mark.asyncio
async def test_positions_with_data(ctx, session):
    pos = Position(
        exchange="binance_futures", symbol="BTC/USDT", quantity=0.05,
        average_buy_price=60000, total_invested=3000,
        current_value=3200, unrealized_pnl=200,
        unrealized_pnl_pct=6.67, direction="long", leverage=3,
        stop_loss_pct=8.0, take_profit_pct=16.0,
    )
    session.add(pos)
    await session.commit()

    result = await execute_tool(ctx, "get_positions", {"exchange": "binance_futures"})
    assert len(result["binance_futures"]) == 1
    p = result["binance_futures"][0]
    assert p["symbol"] == "BTC/USDT"
    assert p["direction"] == "long"
    assert p["leverage"] == 3


# ── recent_trades ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_recent_trades(ctx, session):
    now = utcnow()
    for i in range(3):
        o = Order(
            exchange="bithumb", symbol="BTC/KRW", side="buy",
            status="filled", strategy_name="rsi",
            requested_price=50_000_000, executed_price=50_000_000,
            requested_quantity=0.01, executed_quantity=0.01,
            filled_at=now - timedelta(hours=i),
        )
        session.add(o)
    await session.commit()

    result = await execute_tool(ctx, "get_recent_trades", {"exchange": "bithumb", "limit": 2})
    assert len(result["bithumb"]) == 2


@pytest.mark.asyncio
async def test_recent_trades_side_filter(ctx, session):
    now = utcnow()
    for side in ["buy", "sell", "buy"]:
        o = Order(
            exchange="bithumb", symbol="BTC/KRW", side=side,
            status="filled", strategy_name="rsi",
            requested_price=50_000_000, executed_price=50_000_000,
            requested_quantity=0.01, executed_quantity=0.01,
            filled_at=now,
        )
        session.add(o)
    await session.commit()

    result = await execute_tool(ctx, "get_recent_trades", {"exchange": "bithumb", "side": "sell"})
    assert len(result["bithumb"]) == 1
    assert result["bithumb"][0]["side"] == "sell"


# ── daily_pnl ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_daily_pnl(ctx, session):
    from datetime import date
    today = date.today()
    for i in range(3):
        d = DailyPnL(
            exchange="binance_futures", date=today - timedelta(days=i),
            daily_pnl=10.0 * (i + 1), daily_pnl_pct=1.0,
            realized_pnl=5.0, win_count=2, loss_count=1, trade_count=3,
        )
        session.add(d)
    await session.commit()

    result = await execute_tool(ctx, "get_daily_pnl", {"exchange": "binance_futures", "days": 7})
    assert len(result["binance_futures"]) == 3


# ── start_engine ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_engine_already_running(ctx):
    result = await execute_tool(ctx, "start_engine", {"exchange": "bithumb"})
    assert result["status"] == "already_running"


@pytest.mark.asyncio
async def test_start_engine(ctx, mock_registry):
    eng = mock_registry.get_engine("bithumb")
    eng.is_running = False
    eng.start = AsyncMock()

    result = await execute_tool(ctx, "start_engine", {"exchange": "bithumb"})
    assert result["status"] == "started"


# ── stop_engine ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_engine(ctx, mock_registry):
    eng = mock_registry.get_engine("bithumb")
    eng.stop = AsyncMock()

    result = await execute_tool(ctx, "stop_engine", {"exchange": "bithumb"})
    assert result["status"] == "stopped"


@pytest.mark.asyncio
async def test_stop_engine_not_running(ctx, mock_registry):
    eng = mock_registry.get_engine("bithumb")
    eng.is_running = False

    result = await execute_tool(ctx, "stop_engine", {"exchange": "bithumb"})
    assert result["status"] == "already_stopped"


# ── trigger_analysis ───────────────────────────────────────

@pytest.mark.asyncio
async def test_trigger_performance(ctx):
    result = await execute_tool(ctx, "trigger_analysis", {
        "exchange": "bithumb",
        "analysis_type": "performance",
    })
    assert "bithumb" in result
    assert "완료" in result["bithumb"]


@pytest.mark.asyncio
async def test_trigger_trade_review(ctx):
    result = await execute_tool(ctx, "trigger_analysis", {
        "exchange": "binance_futures",
        "analysis_type": "trade_review",
    })
    assert "binance_futures" in result


# ── unknown tool ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_tool(ctx):
    result = await execute_tool(ctx, "nonexistent", {})
    assert "error" in result


# ── tool definitions ───────────────────────────────────────

def test_tool_definitions_valid():
    """모든 도구 정의에 name, description, input_schema가 있는지 확인."""
    for tool in TOOL_DEFINITIONS:
        assert "name" in tool
        assert "description" in tool
        assert "input_schema" in tool
        assert tool["input_schema"]["type"] == "object"


def test_write_tools_subset():
    """write 도구가 정의에 포함되어 있는지 확인."""
    defined = {t["name"] for t in TOOL_DEFINITIONS}
    for wt in WRITE_TOOLS:
        assert wt in defined
