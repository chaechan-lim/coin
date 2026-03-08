"""전략 어드바이저 에이전트 테스트."""
import pytest
from datetime import timedelta
from unittest.mock import patch

from agents.strategy_advisor import StrategyAdvisorAgent, StrategyAdvice
from agents.performance_analytics import PerformanceReport, StrategyMetrics, WindowMetrics, CoinMetrics
from core.models import Order
from core.utils import utcnow


@pytest.fixture
def advisor():
    with patch("agents.strategy_advisor.get_config") as mock_cfg:
        mock_cfg.return_value.llm.enabled = False
        mock_cfg.return_value.llm.api_key = ""
        return StrategyAdvisorAgent(exchange_name="binance_futures")


@pytest.fixture
def spot_advisor():
    with patch("agents.strategy_advisor.get_config") as mock_cfg:
        mock_cfg.return_value.llm.enabled = False
        mock_cfg.return_value.llm.api_key = ""
        return StrategyAdvisorAgent(exchange_name="bithumb")


def _make_sell(session, symbol="BTC/USDT", pnl=10.0, pnl_pct=5.0,
               strategy="bollinger_rsi", reason="", exchange="binance_futures",
               direction=None, days_ago=1):
    now = utcnow()
    o = Order(
        exchange=exchange,
        symbol=symbol,
        side="sell",
        status="filled",
        strategy_name=strategy,
        signal_reason=reason,
        direction=direction,
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
async def test_advise_no_orders(advisor, session):
    """거래 없을 때 기본 결과."""
    advice = await advisor.advise(session)
    assert isinstance(advice, StrategyAdvice)
    assert "데이터가 없습니다" in advice.analysis_summary


@pytest.mark.asyncio
async def test_exit_analysis(advisor, session):
    """청산 사유 분석."""
    _make_sell(session, reason="손절 (BTC/USDT) -5%", pnl=-50.0, days_ago=5)
    _make_sell(session, reason="손절 (ETH/USDT) -3%", pnl=-30.0, days_ago=10)
    _make_sell(session, reason="익절 (BTC/USDT) +10%", pnl=100.0, days_ago=15)
    _make_sell(session, reason="트레일링 (SOL/USDT) +7%", pnl=70.0, days_ago=20)
    _make_sell(session, reason="전략 SELL 시그널", pnl=20.0, days_ago=25)
    await session.commit()

    advice = await advisor.advise(session)
    assert "stop_loss" in advice.exit_analysis
    assert advice.exit_analysis["stop_loss"]["count"] == 2
    assert "take_profit" in advice.exit_analysis
    assert "trailing" in advice.exit_analysis
    assert "signal" in advice.exit_analysis


@pytest.mark.asyncio
async def test_param_sensitivity_sl(advisor, session):
    """SL 파라미터 민감도 분석."""
    for i in range(5):
        _make_sell(session, reason="손절 (BTC/USDT) -8%", pnl=-80.0, pnl_pct=-8.0, days_ago=i * 10 + 1)
    await session.commit()

    advice = await advisor.advise(session)
    sl_sensitivity = [p for p in advice.param_sensitivities if p.param_name == "stop_loss_pct"]
    assert len(sl_sensitivity) == 1
    assert "손절" in sl_sensitivity[0].improvement


@pytest.mark.asyncio
async def test_direction_analysis_futures(advisor, session):
    """선물 방향별 분석."""
    _make_sell(session, direction="long", pnl=50.0, days_ago=5)
    _make_sell(session, direction="long", pnl=-20.0, days_ago=10)
    _make_sell(session, direction="short", pnl=30.0, days_ago=15)
    _make_sell(session, direction="short", pnl=-10.0, days_ago=20)
    await session.commit()

    advice = await advisor.advise(session)
    assert "long" in advice.direction_analysis
    assert "short" in advice.direction_analysis
    assert advice.direction_analysis["long"]["count"] == 2
    assert advice.direction_analysis["short"]["count"] == 2


@pytest.mark.asyncio
async def test_direction_analysis_spot(spot_advisor, session):
    """현물은 방향별 분석 없음."""
    _make_sell(session, exchange="bithumb", pnl=10.0, days_ago=5, symbol="BTC/KRW")
    await session.commit()

    advice = await spot_advisor.advise(session)
    assert advice.direction_analysis == {}


@pytest.mark.asyncio
async def test_advise_with_performance_report(advisor, session):
    """성과 보고서와 함께 분석."""
    _make_sell(session, pnl=10.0, days_ago=5)
    await session.commit()

    perf = PerformanceReport(
        exchange="binance_futures",
        generated_at="2026-03-08T00:00:00",
        windows={"7d": WindowMetrics(period_days=7, total_trades=5, win_rate=0.4)},
        by_strategy={"rsi": StrategyMetrics(name="rsi", trend="degrading", trades_7d=3, win_rate_7d=0.2, win_rate_30d=0.5)},
        degradation_alerts=["전략 rsi 성과 저하"],
    )

    advice = await advisor.advise(session, performance_report=perf)
    # 성과 저하 전략에 대한 제안이 있어야 함
    has_rsi_suggestion = any("rsi" in s.lower() for s in advice.suggestions)
    assert has_rsi_suggestion


@pytest.mark.asyncio
async def test_exchange_isolation(advisor, session):
    """거래소별 격리."""
    _make_sell(session, exchange="binance_futures", pnl=10.0, days_ago=5)
    _make_sell(session, exchange="bithumb", pnl=100.0, days_ago=5, symbol="BTC/KRW")
    await session.commit()

    advice = await advisor.advise(session)
    # bithumb 거래는 포함되지 않아야 함
    total_exits = sum(s["count"] for s in advice.exit_analysis.values())
    assert total_exits == 1


@pytest.mark.asyncio
async def test_high_sl_frequency_suggestion(advisor, session):
    """손절 빈도 높으면 경고."""
    # 10건 중 7건이 손절
    for i in range(7):
        _make_sell(session, reason="손절 -8%", pnl=-80.0, days_ago=i * 10 + 1)
    for i in range(3):
        _make_sell(session, reason="익절 +16%", pnl=160.0, days_ago=i * 10 + 5)
    await session.commit()

    advice = await advisor.advise(session)
    has_sl_warning = any("손절" in s for s in advice.suggestions)
    assert has_sl_warning


@pytest.mark.asyncio
async def test_rule_based_summary(advisor, session):
    """룰 기반 요약 생성."""
    _make_sell(session, reason="손절", pnl=-50.0, days_ago=5)
    _make_sell(session, reason="트레일링", pnl=30.0, days_ago=10)
    await session.commit()

    advice = await advisor.advise(session)
    assert advice.analysis_summary != ""
    assert len(advice.suggestions) > 0


@pytest.mark.asyncio
async def test_llm_response_parsing(advisor):
    """LLM 응답 파싱."""
    text = """
SUMMARY:
현재 시스템은 손절 비율이 높아 진입 품질 개선이 필요합니다.

SUGGESTIONS:
- SL을 8%에서 10%로 확대하여 노이즈 손절 감소
- bollinger_rsi 전략의 7일 성과가 우수하므로 가중치 0.31 유지
- SOL 코인은 4연패 중이므로 일시 제외 권장
"""
    summary, suggestions = advisor._parse_llm_response(text)
    assert "손절 비율" in summary
    assert len(suggestions) == 3
    assert "SL" in suggestions[0]
