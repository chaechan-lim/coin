"""
BTC-Neutral 자본 부족 가드 + alt 롤백 테스트.

배경: 5/2 발견 — 자본 부족으로 BTC qty < 0.001 minimum 일 때
alt만 체결되고 BTC가 거부되어 ghost 누적.
"""
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pandas as pd
import numpy as np

from engine.btc_neutral_alt_mr_engine import BTCNeutralAltMREngine


def _make_df(price: float, n: int = 5):
    idx = pd.date_range(end="2026-05-02", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "open": [price]*n, "high": [price*1.01]*n, "low": [price*0.99]*n,
        "close": [price]*n, "volume": [1000.0]*n,
    }, index=idx)


def _make_engine(capital: float = 200):
    config = MagicMock()
    exchange = MagicMock()
    exchange.amount_to_precision = MagicMock(side_effect=lambda s, a: str(a))
    market_data = MagicMock()
    eng = BTCNeutralAltMREngine(config, exchange, market_data, initial_capital_usdt=capital)
    return eng


@pytest.mark.asyncio
async def test_skip_when_btc_qty_below_min_precision():
    """BTC 수량이 0.001 미만이면 진입 자체 차단 (alt도 발주 안 함)."""
    eng = _make_engine(capital=200)  # 200 * 0.15 * 2 = 60 notional, 30 alt + 30 BTC
    # BTC 가격 80000 → 30/80000 = 0.000375 BTC < 0.001
    eng._market_data.get_ohlcv_df = AsyncMock(side_effect=lambda sym, *a, **kw: _make_df(80000.0) if "BTC" in sym else _make_df(2400.0))

    with patch("engine.btc_neutral_alt_mr_engine.emit_event", new_callable=AsyncMock) as emit:
        await eng._open_pair("ETH/USDT", "long", -2.5)

    # alt 주문조차 호출되지 않아야 함
    eng._exchange.create_market_buy.assert_not_called()
    eng._exchange.create_market_sell.assert_not_called()
    # 자본 부족 경고 emit
    emit.assert_called_once()
    assert emit.call_args.args[0] == "warning"
    assert "자본 부족" in emit.call_args.args[2]


@pytest.mark.asyncio
async def test_proceed_when_btc_qty_sufficient():
    """충분한 자본이면 정상 진입."""
    eng = _make_engine(capital=2000)  # 2000 * 0.15 * 2 = 600 notional, 300 alt + 300 BTC
    # BTC 60000 → 300/60000 = 0.005 BTC > 0.001
    eng._market_data.get_ohlcv_df = AsyncMock(side_effect=lambda sym, *a, **kw: _make_df(60000.0) if "BTC" in sym else _make_df(2400.0))

    # 가짜 OrderResult: alt + BTC 모두 체결
    from exchange.data_models import OrderResult
    from datetime import datetime, timezone
    def _ok(side, sym, qty, price):
        return OrderResult(
            order_id=f"id_{sym}_{side}", symbol=sym, side=side,
            order_type="market", status="filled",
            price=price, amount=qty, filled=qty, remaining=0,
            cost=qty*price, fee=0, fee_currency="USDT",
            timestamp=datetime.now(timezone.utc),
        )
    async def buy(sym, qty, **kw):
        return _ok("buy", sym, qty, 60000 if "BTC" in sym else 2400)
    async def sell(sym, qty, **kw):
        return _ok("sell", sym, qty, 60000 if "BTC" in sym else 2400)
    eng._exchange.create_market_buy = AsyncMock(side_effect=buy)
    eng._exchange.create_market_sell = AsyncMock(side_effect=sell)

    with patch("engine.btc_neutral_alt_mr_engine.emit_event", new_callable=AsyncMock), \
         patch("engine.btc_neutral_alt_mr_engine.get_session_factory"):
        await eng._open_pair("ETH/USDT", "long", -2.5)

    # alt 진입 + BTC 진입 둘 다 호출
    assert eng._exchange.create_market_buy.call_count == 1  # alt long
    assert eng._exchange.create_market_sell.call_count == 1  # BTC short
    assert "ETH/USDT" in eng._positions


@pytest.mark.asyncio
async def test_alt_rollback_when_btc_raises():
    """BTC 주문이 예외 발생하면 alt 자동 롤백 (outer except).

    alt_side='short' 케이스: alt sell + BTC buy. BTC buy가 raise 하면
    alt 롤백은 buy (reduce_only=True) 로 close short.
    """
    eng = _make_engine(capital=2000)
    eng._market_data.get_ohlcv_df = AsyncMock(
        side_effect=lambda sym, *a, **kw: _make_df(60000.0) if "BTC" in sym else _make_df(2400.0)
    )

    from exchange.data_models import OrderResult
    from datetime import datetime, timezone
    alt_filled_order = OrderResult(
        order_id="alt1", symbol="ETH/USDT", side="sell", order_type="market",
        status="filled", price=2400, amount=0.125, filled=0.125,
        remaining=0, cost=300, fee=0.12, fee_currency="USDT",
        timestamp=datetime.now(timezone.utc),
    )

    rollback_called = []
    async def buy(sym, qty, **kw):
        if "BTC" in sym:
            # BTC long entry 시도가 실패 (precision)
            raise RuntimeError("amount must be greater than minimum precision")
        # alt 롤백 buy (reduce_only) 호출
        rollback_called.append((sym, qty, kw.get("reduce_only")))
        return OrderResult(
            order_id="rb1", symbol=sym, side="buy", order_type="market",
            status="filled", price=2410, amount=qty, filled=qty,
            remaining=0, cost=qty*2410, fee=0, fee_currency="USDT",
            timestamp=datetime.now(timezone.utc),
        )
    async def sell(sym, qty, **kw):
        # alt short 진입
        return alt_filled_order
    eng._exchange.create_market_buy = AsyncMock(side_effect=buy)
    eng._exchange.create_market_sell = AsyncMock(side_effect=sell)

    with patch("engine.btc_neutral_alt_mr_engine.emit_event", new_callable=AsyncMock), \
         patch("engine.btc_neutral_alt_mr_engine.get_session_factory"):
        # alt_side='short' → BTC long (실패) → alt 롤백 buy
        await eng._open_pair("ETH/USDT", "short", 2.5)

    # alt 롤백 호출됐는지 확인
    assert len(rollback_called) == 1
    rb_sym, rb_qty, rb_ro = rollback_called[0]
    assert rb_sym == "ETH/USDT"
    assert rb_qty == 0.125
    assert rb_ro is True
    # 포지션 미등록
    assert "ETH/USDT" not in eng._positions


@pytest.mark.asyncio
async def test_critical_alert_when_rollback_fails():
    """롤백마저 실패하면 critical 알림 발송 (고아 발생).

    alt_side='short' 케이스: alt sell 성공 → BTC buy raise → alt 롤백 buy 도 raise
    """
    eng = _make_engine(capital=2000)
    eng._market_data.get_ohlcv_df = AsyncMock(
        side_effect=lambda sym, *a, **kw: _make_df(60000.0) if "BTC" in sym else _make_df(2400.0)
    )

    from exchange.data_models import OrderResult
    from datetime import datetime, timezone
    alt_filled = OrderResult(
        order_id="a", symbol="ETH/USDT", side="sell", order_type="market",
        status="filled", price=2400, amount=0.125, filled=0.125,
        remaining=0, cost=300, fee=0, fee_currency="USDT",
        timestamp=datetime.now(timezone.utc),
    )
    async def buy(sym, qty, **kw):
        # BTC entry buy 와 alt 롤백 buy 모두 실패
        raise RuntimeError("precision/network")
    async def sell(sym, qty, **kw):
        return alt_filled
    eng._exchange.create_market_buy = AsyncMock(side_effect=buy)
    eng._exchange.create_market_sell = AsyncMock(side_effect=sell)

    with patch("engine.btc_neutral_alt_mr_engine.emit_event", new_callable=AsyncMock) as emit, \
         patch("engine.btc_neutral_alt_mr_engine.get_session_factory"):
        await eng._open_pair("ETH/USDT", "short", 2.5)

    # critical 알림 발송 (orphan 발생)
    crit_calls = [c for c in emit.call_args_list if c.args[0] == "critical"]
    assert len(crit_calls) >= 1
    assert "고아" in crit_calls[0].args[2]
