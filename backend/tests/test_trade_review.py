"""
Tests for TradeReviewAgent (agents/trade_review.py).

- _get_analysis_instructions: 선물/현물 분석 지시사항
- _get_capital_summary: 입출금 요약
- 숏 PnL 계산
"""
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch
from collections import defaultdict

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Order, Position, CapitalTransaction
from agents.trade_review import TradeReviewAgent
from tests.conftest import make_order


# ── _get_analysis_instructions Tests ──


def test_futures_instructions_mention_short_as_intended():
    """선물 분석 지시사항에 숏이 의도된 전략임을 명시."""
    agent = TradeReviewAgent(exchange_name="binance_futures")
    instructions = agent._get_analysis_instructions()
    assert "의도된 전략" in instructions
    assert "숏" in instructions
    # LLM에게 "숏 비활성화" 추천하지 말라고 지시
    assert "타이밍" in instructions or "조건" in instructions


def test_futures_instructions_mention_dual_timeframe():
    """선물 분석 지시사항에 듀얼 타임프레임 언급."""
    agent = TradeReviewAgent(exchange_name="binance_futures")
    instructions = agent._get_analysis_instructions()
    assert "4h" in instructions or "타임프레임" in instructions


def test_spot_instructions_mention_asymmetric():
    """현물 분석 지시사항에 비대칭 전략 언급."""
    agent = TradeReviewAgent(exchange_name="bithumb")
    instructions = agent._get_analysis_instructions()
    assert "비대칭" in instructions or "현물" in instructions


def test_spot_instructions_no_short():
    """현물은 숏 관련 내용 없음."""
    agent = TradeReviewAgent(exchange_name="bithumb")
    instructions = agent._get_analysis_instructions()
    assert "숏" not in instructions


# ── _get_capital_summary Tests ──


@pytest.mark.asyncio
async def test_capital_summary_with_transactions(session):
    """입출금 내역이 있으면 요약에 포함."""
    agent = TradeReviewAgent(exchange_name="bithumb")

    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=500_000,
        currency="KRW", source="seed", confirmed=True,
    ))
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="withdrawal", amount=200_000,
        currency="KRW", source="manual", confirmed=True,
    ))
    await session.flush()

    summary = await agent._get_capital_summary(session)
    assert summary["total_deposits"] == 500_000
    assert summary["total_withdrawals"] == 200_000
    assert summary["net_capital"] == 300_000


@pytest.mark.asyncio
async def test_capital_summary_no_transactions(session):
    """입출금 없으면 0."""
    agent = TradeReviewAgent(exchange_name="bithumb")
    summary = await agent._get_capital_summary(session)
    assert summary["total_deposits"] == 0
    assert summary["total_withdrawals"] == 0
    assert summary["net_capital"] == 0
    assert summary["recent_transactions"] == []


@pytest.mark.asyncio
async def test_capital_summary_recent_only(session):
    """최근 24시간 입출금만 recent에 포함."""
    agent = TradeReviewAgent(exchange_name="bithumb", review_window_hours=24)

    # 48시간 전 입금 (recent에 안 들어감)
    old_tx = CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=500_000,
        currency="KRW", source="seed", confirmed=True,
    )
    session.add(old_tx)
    await session.flush()
    # 수동으로 created_at을 과거로 변경
    from sqlalchemy import update
    from core.models import CapitalTransaction as CT
    await session.execute(
        update(CT)
        .where(CT.id == old_tx.id)
        .values(created_at=datetime.now(timezone.utc) - timedelta(hours=48))
    )

    # 1시간 전 출금 (recent에 들어감)
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="withdrawal", amount=200_000,
        currency="KRW", source="manual", confirmed=True,
    ))
    await session.flush()

    summary = await agent._get_capital_summary(session)
    assert summary["total_deposits"] == 500_000
    assert summary["total_withdrawals"] == 200_000
    # recent에는 출금만
    assert len(summary["recent_transactions"]) == 1
    assert summary["recent_transactions"][0]["type"] == "withdrawal"


@pytest.mark.asyncio
async def test_capital_summary_exchange_isolation(session):
    """다른 거래소 입출금은 무시."""
    agent = TradeReviewAgent(exchange_name="binance_futures")

    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=500_000,
        currency="KRW", source="seed", confirmed=True,
    ))
    session.add(CapitalTransaction(
        exchange="binance_futures", tx_type="deposit", amount=300,
        currency="USDT", source="seed", confirmed=True,
    ))
    await session.flush()

    summary = await agent._get_capital_summary(session)
    assert summary["total_deposits"] == 300
    assert summary["total_withdrawals"] == 0


# ── Short PnL in Trade Review ──


@pytest.mark.asyncio
async def test_review_short_pnl_calculated(session):
    """선물 숏 거래의 PnL이 올바르게 계산."""
    agent = TradeReviewAgent(exchange_name="binance_futures")

    now = datetime.now(timezone.utc)
    # 숏 진입 (sell) + 청산 (buy)
    orders = [
        Order(
            exchange="binance_futures", symbol="BTC/USDT", side="sell",
            order_type="market", status="filled",
            requested_price=100_000, executed_price=100_000,
            requested_quantity=0.01, executed_quantity=0.01,
            fee=0.04, fee_currency="USDT", is_paper=False,
            strategy_name="rsi", signal_confidence=0.7,
            signal_reason="RSI overbought",
            direction="short",
            created_at=now - timedelta(hours=2),
            filled_at=now - timedelta(hours=2),
        ),
        Order(
            exchange="binance_futures", symbol="BTC/USDT", side="buy",
            order_type="market", status="filled",
            requested_price=95_000, executed_price=95_000,
            requested_quantity=0.01, executed_quantity=0.01,
            fee=0.038, fee_currency="USDT", is_paper=False,
            strategy_name="rsi", signal_confidence=0.7,
            signal_reason="TP reached",
            direction="short",
            created_at=now - timedelta(hours=1),
            filled_at=now - timedelta(hours=1),
        ),
    ]
    for o in orders:
        session.add(o)
    await session.flush()

    review = await agent.review(session)
    # 숏 PnL = (100000 - 95000) * 0.01 - 0.038 = ~49.96
    assert review.total_realized_pnl > 0
    assert review.win_count == 1
    assert review.loss_count == 0


# ── PnL Matching: buy before review window ──


@pytest.mark.asyncio
async def test_review_pnl_matches_buy_before_window(session):
    """매수가 리뷰 윈도우 이전이고 매도만 윈도우 내일 때 DB에서 매수 조회."""
    agent = TradeReviewAgent(exchange_name="bithumb", review_window_hours=24)

    now = datetime.now(timezone.utc)
    # 48시간 전 매수 (리뷰 윈도우 밖)
    buy_order = Order(
        exchange="bithumb", symbol="BTC/KRW", side="buy",
        order_type="market", status="filled",
        requested_price=50_000_000, executed_price=50_000_000,
        requested_quantity=0.001, executed_quantity=0.001,
        fee=125, fee_currency="KRW", is_paper=False,
        strategy_name="rsi", signal_confidence=0.7,
        signal_reason="RSI oversold",
        created_at=now - timedelta(hours=48),
        filled_at=now - timedelta(hours=48),
    )
    session.add(buy_order)

    # 2시간 전 매도 (리뷰 윈도우 안)
    sell_order = Order(
        exchange="bithumb", symbol="BTC/KRW", side="sell",
        order_type="market", status="filled",
        requested_price=55_000_000, executed_price=55_000_000,
        requested_quantity=0.001, executed_quantity=0.001,
        fee=137, fee_currency="KRW", is_paper=False,
        strategy_name="rsi", signal_confidence=0.7,
        signal_reason="TP reached",
        created_at=now - timedelta(hours=2),
        filled_at=now - timedelta(hours=2),
    )
    session.add(sell_order)
    await session.flush()

    review = await agent.review(session)
    # PnL = (55M - 50M) * 0.001 - 137 = 5000 - 137 = 4863
    assert review.total_realized_pnl > 4000  # 이전 버그: pnl=0
    assert review.win_count == 1


@pytest.mark.asyncio
async def test_review_profit_factor_capped(session):
    """손실 없는 경우 PF가 99.0으로 캡핑."""
    agent = TradeReviewAgent(exchange_name="bithumb", review_window_hours=24)

    now = datetime.now(timezone.utc)
    # 매수 + 매도 (이익만)
    session.add(Order(
        exchange="bithumb", symbol="BTC/KRW", side="buy",
        order_type="market", status="filled",
        requested_price=50_000_000, executed_price=50_000_000,
        requested_quantity=0.001, executed_quantity=0.001,
        fee=125, fee_currency="KRW", is_paper=False,
        strategy_name="rsi", signal_confidence=0.7, signal_reason="test",
        created_at=now - timedelta(hours=3),
        filled_at=now - timedelta(hours=3),
    ))
    session.add(Order(
        exchange="bithumb", symbol="BTC/KRW", side="sell",
        order_type="market", status="filled",
        requested_price=55_000_000, executed_price=55_000_000,
        requested_quantity=0.001, executed_quantity=0.001,
        fee=137, fee_currency="KRW", is_paper=False,
        strategy_name="rsi", signal_confidence=0.7, signal_reason="test",
        created_at=now - timedelta(hours=2),
        filled_at=now - timedelta(hours=2),
    ))
    await session.flush()

    review = await agent.review(session)
    assert review.profit_factor == 99.0  # 캡핑됨 (이전: 999.0)
