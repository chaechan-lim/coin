"""성과 분석 에이전트 테스트."""
import pytest
from datetime import timedelta
from unittest.mock import patch, AsyncMock

from agents.performance_analytics import (
    PerformanceAnalyticsAgent,
    PerformanceReport,
    WindowMetrics,
)
from core.models import Order
from core.utils import utcnow


@pytest.fixture
def agent():
    with patch("agents.performance_analytics.get_config") as mock_cfg:
        mock_cfg.return_value.llm.enabled = False
        mock_cfg.return_value.llm.api_key = ""
        return PerformanceAnalyticsAgent(exchange_name="binance_futures")


@pytest.fixture
def spot_agent():
    with patch("agents.performance_analytics.get_config") as mock_cfg:
        mock_cfg.return_value.llm.enabled = False
        mock_cfg.return_value.llm.api_key = ""
        return PerformanceAnalyticsAgent(exchange_name="bithumb")


def _make_sell(session, symbol="BTC/USDT", pnl=10.0, pnl_pct=5.0,
               strategy="bollinger_rsi", reason="", exchange="binance_futures",
               days_ago=1):
    now = utcnow()
    o = Order(
        exchange=exchange,
        symbol=symbol,
        side="sell",
        status="filled",
        strategy_name=strategy,
        signal_reason=reason,
        requested_price=100.0,
        executed_price=100.0,
        requested_quantity=1.0,
        executed_quantity=1.0,
        fee=0.04,
        realized_pnl=pnl,
        realized_pnl_pct=pnl_pct,
        filled_at=now - timedelta(days=days_ago),
        created_at=now - timedelta(days=days_ago, hours=2),
    )
    session.add(o)
    return o


@pytest.mark.asyncio
async def test_analyze_no_orders(agent, session):
    """거래 없을 때 빈 보고서."""
    report = await agent.analyze(session)
    assert isinstance(report, PerformanceReport)
    assert report.windows["7d"].total_trades == 0
    assert report.windows["30d"].total_trades == 0
    assert report.degradation_alerts == []


@pytest.mark.asyncio
async def test_analyze_with_wins_and_losses(agent, session):
    """승패 혼합 분석."""
    _make_sell(session, pnl=50.0, pnl_pct=10.0, days_ago=2)
    _make_sell(session, pnl=30.0, pnl_pct=6.0, days_ago=3)
    _make_sell(session, pnl=-20.0, pnl_pct=-4.0, days_ago=4)
    _make_sell(session, pnl=-10.0, pnl_pct=-2.0, days_ago=5)
    await session.commit()

    report = await agent.analyze(session)
    w7 = report.windows["7d"]
    assert w7.total_trades == 4
    assert w7.win_count == 2
    assert w7.loss_count == 2
    assert w7.win_rate == 0.5
    assert w7.total_pnl == 50.0
    assert w7.largest_win == 50.0
    assert w7.largest_loss == -20.0
    assert w7.profit_factor > 0


@pytest.mark.asyncio
async def test_window_isolation(agent, session):
    """7일/14일/30일 윈도우가 올바르게 분리."""
    _make_sell(session, pnl=10.0, days_ago=3)   # 7d ✓, 14d ✓, 30d ✓
    _make_sell(session, pnl=20.0, days_ago=10)  # 7d ✗, 14d ✓, 30d ✓
    _make_sell(session, pnl=30.0, days_ago=20)  # 7d ✗, 14d ✗, 30d ✓
    await session.commit()

    report = await agent.analyze(session)
    assert report.windows["7d"].total_trades == 1
    assert report.windows["14d"].total_trades == 2
    assert report.windows["30d"].total_trades == 3


@pytest.mark.asyncio
async def test_strategy_metrics(agent, session):
    """전략별 성과 계산."""
    _make_sell(session, strategy="rsi", pnl=10.0, days_ago=2)
    _make_sell(session, strategy="rsi", pnl=-5.0, days_ago=3)
    _make_sell(session, strategy="bollinger_rsi", pnl=20.0, days_ago=4)
    await session.commit()

    report = await agent.analyze(session)
    assert "rsi" in report.by_strategy
    assert report.by_strategy["rsi"].trades_30d == 2
    assert report.by_strategy["rsi"].pnl_30d == 5.0
    assert report.by_strategy["bollinger_rsi"].trades_30d == 1


@pytest.mark.asyncio
async def test_strategy_trend_detection(agent, session):
    """전략 성과 추세 감지 (improving/degrading)."""
    # 30일 전체: 5건 중 2승 (40%)
    for i in range(3):
        _make_sell(session, strategy="rsi", pnl=-5.0, days_ago=20 + i)
    _make_sell(session, strategy="rsi", pnl=10.0, days_ago=22)
    _make_sell(session, strategy="rsi", pnl=10.0, days_ago=25)
    # 최근 7일: 3건 중 3승 (100%) → improving
    for i in range(3):
        _make_sell(session, strategy="rsi", pnl=10.0, days_ago=i + 1)
    await session.commit()

    report = await agent.analyze(session)
    assert report.by_strategy["rsi"].trend == "improving"


@pytest.mark.asyncio
async def test_coin_consecutive_losses(agent, session):
    """코인별 연속 손실 감지."""
    _make_sell(session, symbol="SOL/USDT", pnl=10.0, days_ago=20)
    _make_sell(session, symbol="SOL/USDT", pnl=-5.0, days_ago=10)
    _make_sell(session, symbol="SOL/USDT", pnl=-3.0, days_ago=5)
    _make_sell(session, symbol="SOL/USDT", pnl=-8.0, days_ago=2)
    _make_sell(session, symbol="SOL/USDT", pnl=-2.0, days_ago=1)
    await session.commit()

    report = await agent.analyze(session)
    assert report.by_symbol["SOL/USDT"].consecutive_losses == 4


@pytest.mark.asyncio
async def test_degradation_alert_winrate_drop(agent, session):
    """승률 급락 경고."""
    # 30일 전체: 높은 승률
    for i in range(7):
        _make_sell(session, pnl=10.0, days_ago=20 + i)
    for i in range(3):
        _make_sell(session, pnl=-5.0, days_ago=25 + i)
    # 최근 7일: 낮은 승률
    _make_sell(session, pnl=5.0, days_ago=1)
    for i in range(4):
        _make_sell(session, pnl=-10.0, days_ago=i + 2)
    await session.commit()

    report = await agent.analyze(session)
    # 30d: 8/15 = 53%, 7d: 1/5 = 20% → 33%p 하락
    has_winrate_alert = any("승률 급락" in a for a in report.degradation_alerts)
    assert has_winrate_alert


@pytest.mark.asyncio
async def test_degradation_alert_consecutive_losses(agent, session):
    """코인 연속 손실 경고."""
    for i in range(5):
        _make_sell(session, symbol="ADA/USDT", pnl=-3.0, days_ago=i + 1)
    await session.commit()

    report = await agent.analyze(session)
    has_coin_alert = any("ADA" in a and "연패" in a for a in report.degradation_alerts)
    assert has_coin_alert


@pytest.mark.asyncio
async def test_exchange_isolation(agent, session):
    """거래소별 격리."""
    _make_sell(session, exchange="binance_futures", pnl=10.0, days_ago=1)
    _make_sell(session, exchange="bithumb", pnl=100.0, days_ago=1)
    await session.commit()

    report = await agent.analyze(session)
    assert report.windows["7d"].total_trades == 1
    assert report.windows["7d"].total_pnl == 10.0


@pytest.mark.asyncio
async def test_currency_format(agent, spot_agent):
    """통화 포맷."""
    assert "USDT" in agent._fmt(10.0)
    assert "KRW" in spot_agent._fmt(10000)


@pytest.mark.asyncio
async def test_rule_based_insights(agent, session):
    """룰 기반 인사이트 생성."""
    _make_sell(session, pnl=10.0, strategy="rsi", days_ago=2)
    _make_sell(session, pnl=-5.0, strategy="rsi", days_ago=3)
    await session.commit()

    report = await agent.analyze(session)
    assert len(report.insights) > 0


@pytest.mark.asyncio
async def test_profit_factor_calculation(agent, session):
    """PF 계산 검증."""
    _make_sell(session, pnl=100.0, days_ago=1)
    _make_sell(session, pnl=-50.0, days_ago=2)
    await session.commit()

    report = await agent.analyze(session)
    assert report.windows["7d"].profit_factor == pytest.approx(2.0, abs=0.01)


@pytest.mark.asyncio
async def test_all_wins_profit_factor(agent, session):
    """전승 시 PF 상한."""
    _make_sell(session, pnl=10.0, days_ago=1)
    _make_sell(session, pnl=20.0, days_ago=2)
    await session.commit()

    report = await agent.analyze(session)
    assert report.windows["7d"].profit_factor == 99.0
