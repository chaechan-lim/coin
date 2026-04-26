"""
Coordinator: 거래 0건 윈도우에선 노티/DB 저장 스킵 검증.

전수조사 후속: 매매 회고/성과 분석/전략 어드바이저가 거래 없을 때 잡음 방지.
"""
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from agents.coordinator import AgentCoordinator
from agents.trade_review import TradeReview
from agents.performance_analytics import PerformanceReport, WindowMetrics
from agents.strategy_advisor import StrategyAdvice


def _make_coordinator(trade_review=None, performance=None, advisor=None):
    market_agent = MagicMock()
    risk_agent = MagicMock()
    combiner = MagicMock()
    coord = AgentCoordinator(
        market_agent, risk_agent, combiner,
        trade_review_agent=trade_review,
        performance_agent=performance,
        strategy_advisor=advisor,
        exchange_name="binance_futures",
    )
    return coord


# ── trade_review ──


@pytest.mark.asyncio
async def test_trade_review_skipped_when_zero_trades():
    """거래 0건이면 emit_event/AgentAnalysisLog.add 호출 안 됨."""
    empty_review = TradeReview(
        period_hours=24,
        total_trades=0, buy_count=0, sell_count=0,
        win_count=0, loss_count=0, win_rate=0.0,
        total_realized_pnl=0.0, avg_pnl_per_trade=0.0,
        profit_factor=0.0, largest_win=0.0, largest_loss=0.0,
        by_strategy={}, by_symbol={}, open_positions=[],
        insights=["거래 없음"], recommendations=["대기"],
        analyzed_at="2026-04-27T00:00:00",
    )
    agent = MagicMock()
    agent.review = AsyncMock(return_value=empty_review)
    coord = _make_coordinator(trade_review=agent)

    with patch("agents.coordinator.emit_event", new_callable=AsyncMock) as emit, \
         patch("agents.coordinator.get_session_factory") as gsf:
        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        gsf.return_value.return_value.__aenter__.return_value = session
        gsf.return_value.return_value.__aexit__.return_value = None

        result = await coord.run_trade_review()

    assert result is empty_review
    emit.assert_not_called()
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_trade_review_emits_when_has_trades():
    """거래 1건 이상이면 정상 emit."""
    review = TradeReview(
        period_hours=24,
        total_trades=2, buy_count=1, sell_count=1,
        win_count=1, loss_count=0, win_rate=1.0,
        total_realized_pnl=5.0, avg_pnl_per_trade=2.5,
        profit_factor=99.0, largest_win=5.0, largest_loss=0.0,
        by_strategy={}, by_symbol={}, open_positions=[],
        insights=["good"], recommendations=["maintain"],
        analyzed_at="2026-04-27T00:00:00",
    )
    agent = MagicMock()
    agent.review = AsyncMock(return_value=review)
    coord = _make_coordinator(trade_review=agent)

    with patch("agents.coordinator.emit_event", new_callable=AsyncMock) as emit, \
         patch("agents.coordinator.get_session_factory") as gsf:
        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        gsf.return_value.return_value.__aenter__.return_value = session
        gsf.return_value.return_value.__aexit__.return_value = None

        await coord.run_trade_review()

    emit.assert_called_once()
    session.add.assert_called_once()


# ── performance_analytics ──


@pytest.mark.asyncio
async def test_performance_skipped_when_30d_zero():
    """30일 거래 0건이면 노티/DB 저장 스킵."""
    report = PerformanceReport(exchange="binance_futures", generated_at="2026-04-27")
    report.windows = {
        "7d": WindowMetrics(period_days=7),
        "14d": WindowMetrics(period_days=14),
        "30d": WindowMetrics(period_days=30, total_trades=0),
    }
    agent = MagicMock()
    agent.analyze = AsyncMock(return_value=report)
    coord = _make_coordinator(performance=agent)

    with patch("agents.coordinator.emit_event", new_callable=AsyncMock) as emit, \
         patch("agents.coordinator.get_session_factory") as gsf:
        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        gsf.return_value.return_value.__aenter__.return_value = session
        gsf.return_value.return_value.__aexit__.return_value = None

        await coord.run_performance_analysis()

    emit.assert_not_called()
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_performance_emits_when_has_trades():
    report = PerformanceReport(exchange="binance_futures", generated_at="2026-04-27")
    w30 = WindowMetrics(period_days=30, total_trades=10, win_count=7, win_rate=0.7,
                       profit_factor=2.0, total_pnl=15.0, largest_win=5.0, largest_loss=-1.0)
    report.windows = {"30d": w30}
    report.insights = ["좋음"]
    report.recommendations = ["유지"]
    agent = MagicMock()
    agent.analyze = AsyncMock(return_value=report)
    coord = _make_coordinator(performance=agent)

    with patch("agents.coordinator.emit_event", new_callable=AsyncMock) as emit, \
         patch("agents.coordinator.get_session_factory") as gsf:
        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        gsf.return_value.return_value.__aenter__.return_value = session
        gsf.return_value.return_value.__aexit__.return_value = None

        await coord.run_performance_analysis()

    emit.assert_called_once()
    session.add.assert_called_once()


# ── strategy_advisor ──


@pytest.mark.asyncio
async def test_strategy_advisor_skipped_when_no_data():
    """거래 데이터 없을 때 노티/DB 스킵."""
    advice = StrategyAdvice(exchange="binance_futures", generated_at="2026-04-27")
    # suggestions 비어있고 exit_analysis 비어있음
    advice.suggestions = []
    advice.exit_analysis = {}
    agent = MagicMock()
    agent.advise = AsyncMock(return_value=advice)
    coord = _make_coordinator(advisor=agent)

    with patch("agents.coordinator.emit_event", new_callable=AsyncMock) as emit, \
         patch("agents.coordinator.get_session_factory") as gsf:
        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        gsf.return_value.return_value.__aenter__.return_value = session
        gsf.return_value.return_value.__aexit__.return_value = None

        await coord.run_strategy_advice()

    emit.assert_not_called()
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_strategy_advisor_emits_when_has_suggestions():
    advice = StrategyAdvice(exchange="binance_futures", generated_at="2026-04-27")
    advice.suggestions = ["tp 12로 조정"]
    advice.exit_analysis = {"avg_hold_hours": 12.5}
    agent = MagicMock()
    agent.advise = AsyncMock(return_value=advice)
    coord = _make_coordinator(advisor=agent)

    with patch("agents.coordinator.emit_event", new_callable=AsyncMock) as emit, \
         patch("agents.coordinator.get_session_factory") as gsf:
        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        gsf.return_value.return_value.__aenter__.return_value = session
        gsf.return_value.return_value.__aexit__.return_value = None

        await coord.run_strategy_advice()

    emit.assert_called_once()
    session.add.assert_called_once()
