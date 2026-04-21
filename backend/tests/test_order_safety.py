"""
주문 실행 안전장치 테스트.

2026-04-20 사고 기반:
- parse_order filled=0 폴백 (info.executedQty)
- reduceOnly 파라미터 전달
- 연속 청산 실패 시 자동 중지
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from exchange.base import OrderResult


# ── parse_order filled/avgPrice 폴백 ──


def test_parse_order_uses_info_executedQty_when_filled_is_zero():
    """CCXT filled=0이지만 info.executedQty가 있으면 그 값을 사용."""
    from exchange.binance_usdm_adapter import BinanceUSDMAdapter

    adapter = BinanceUSDMAdapter.__new__(BinanceUSDMAdapter)
    adapter._DEFAULT_FEE_RATE = 0.0004

    data = {
        "id": "123",
        "symbol": "BTC/USDT",
        "side": "buy",
        "type": "market",
        "status": "closed",
        "price": 0,
        "amount": 0.01,
        "filled": 0,  # CCXT가 0 반환
        "remaining": 0,
        "cost": 0,
        "fee": None,
        "timestamp": 1713600000000,
        "average": None,
        "info": {
            "executedQty": "0.01",
            "avgPrice": "75000.0",
        },
    }

    result = adapter._parse_order(data)
    assert result.filled == 0.01
    assert result.price == 75000.0
    assert result.cost == 0.01 * 75000.0


def test_parse_order_uses_filled_when_available():
    """CCXT filled가 정상이면 info 폴백 안 씀."""
    from exchange.binance_usdm_adapter import BinanceUSDMAdapter

    adapter = BinanceUSDMAdapter.__new__(BinanceUSDMAdapter)
    adapter._DEFAULT_FEE_RATE = 0.0004

    data = {
        "id": "456",
        "symbol": "BTC/USDT",
        "side": "sell",
        "type": "market",
        "status": "closed",
        "price": 76000.0,
        "amount": 0.02,
        "filled": 0.02,
        "remaining": 0,
        "cost": 1520.0,
        "fee": {"cost": 0.608, "currency": "USDT"},
        "timestamp": 1713600000000,
        "average": 76000.0,
        "info": {},
    }

    result = adapter._parse_order(data)
    assert result.filled == 0.02
    assert result.price == 76000.0


def test_parse_order_handles_all_none():
    """filled, average, info 전부 없는 극단 케이스."""
    from exchange.binance_usdm_adapter import BinanceUSDMAdapter

    adapter = BinanceUSDMAdapter.__new__(BinanceUSDMAdapter)
    adapter._DEFAULT_FEE_RATE = 0.0004

    data = {
        "id": "789",
        "symbol": "BTC/USDT",
        "side": "buy",
        "type": "market",
        "status": "closed",
        "price": 0,
        "amount": 0.01,
        "filled": 0,
        "remaining": 0,
        "cost": 0,
        "fee": None,
        "timestamp": 1713600000000,
        "average": None,
        "info": {},
    }

    result = adapter._parse_order(data)
    assert result.filled == 0
    assert result.price == 0


# ── reduceOnly 파라미터 전달 ──


def _make_adapter_with_mock():
    """초기화 없이 어댑터 생성 + 필수 속성 설정."""
    from exchange.binance_usdm_adapter import BinanceUSDMAdapter
    adapter = BinanceUSDMAdapter.__new__(BinanceUSDMAdapter)
    adapter._DEFAULT_FEE_RATE = 0.0004
    adapter._semaphore = asyncio.Semaphore(5)
    adapter._cb_failures = 0
    adapter._cb_open_until = 0
    adapter._CB_THRESHOLD = 5
    return adapter


@pytest.mark.asyncio
async def test_create_market_buy_passes_reduce_only():
    """create_market_buy(reduce_only=True)가 params에 reduceOnly를 전달."""
    adapter = _make_adapter_with_mock()
    adapter._exchange = MagicMock()
    adapter._exchange.create_market_buy_order = AsyncMock(return_value={
        "id": "1", "symbol": "BTC/USDT", "side": "buy", "type": "market",
        "status": "closed", "price": 75000, "amount": 0.01, "filled": 0.01,
        "remaining": 0, "cost": 750, "fee": None, "timestamp": 1713600000000,
        "average": 75000, "info": {},
    })

    await adapter.create_market_buy("BTC/USDT", 0.01, reduce_only=True)

    call_kwargs = adapter._exchange.create_market_buy_order.call_args
    assert call_kwargs.kwargs.get("params") == {"reduceOnly": True}


@pytest.mark.asyncio
async def test_create_market_sell_no_reduce_only_by_default():
    """reduce_only 미지정 시 params 비어있음."""
    adapter = _make_adapter_with_mock()
    adapter._exchange = MagicMock()
    adapter._exchange.create_market_sell_order = AsyncMock(return_value={
        "id": "2", "symbol": "BTC/USDT", "side": "sell", "type": "market",
        "status": "closed", "price": 75000, "amount": 0.01, "filled": 0.01,
        "remaining": 0, "cost": 750, "fee": None, "timestamp": 1713600000000,
        "average": 75000, "info": {},
    })

    await adapter.create_market_sell("BTC/USDT", 0.01)

    call_kwargs = adapter._exchange.create_market_sell_order.call_args
    assert call_kwargs.kwargs.get("params") == {}


# ── 연속 청산 실패 시 자동 중지 ──


def _make_mock_exchange():
    """체결 실패 응답을 반환하는 mock exchange."""
    ex = MagicMock()
    failed_order = SimpleNamespace(
        status="closed", filled=0, price=0,
    )
    ex.create_market_buy = AsyncMock(return_value=failed_order)
    ex.create_market_sell = AsyncMock(return_value=failed_order)
    return ex


def _make_mock_market_data():
    md = MagicMock()
    md.get_ohlcv_df = AsyncMock(return_value=None)
    return md


@pytest.mark.asyncio
async def test_hmm_auto_pause_on_consecutive_close_failures():
    """HMM 엔진: 3회 연속 청산 실패 시 _paused=True."""
    from engine.hmm_regime_live_engine import HMMRegimeLiveEngine, HMMPosition

    config = MagicMock()
    config.binance = MagicMock()
    exchange = _make_mock_exchange()
    market_data = _make_mock_market_data()

    engine = HMMRegimeLiveEngine(config, exchange, market_data, initial_capital_usdt=300)
    engine._positions["BTC/USDT"] = HMMPosition(symbol="BTC/USDT", side="short", quantity=0.01, entry_price=77000)

    assert not engine._paused
    assert engine._consecutive_close_failures == 0

    # 3번 연속 실패
    with patch("engine.hmm_regime_live_engine.emit_event", new_callable=AsyncMock):
        for i in range(3):
            await engine._close_position("BTC/USDT", 75000)

    assert engine._consecutive_close_failures == 3
    assert engine._paused is True
    # 포지션은 여전히 남아있어야 함 (청산 실패했으니)
    assert engine._position is not None


@pytest.mark.asyncio
async def test_hmm_close_failure_counter_resets_on_success():
    """HMM 엔진: 청산 성공 시 실패 카운터 리셋."""
    from engine.hmm_regime_live_engine import HMMRegimeLiveEngine, HMMPosition

    config = MagicMock()
    config.binance = MagicMock()
    exchange = MagicMock()
    market_data = _make_mock_market_data()

    engine = HMMRegimeLiveEngine(config, exchange, market_data, initial_capital_usdt=300)
    engine._positions["BTC/USDT"] = HMMPosition(symbol="BTC/USDT", side="short", quantity=0.01, entry_price=77000)
    engine._consecutive_close_failures = 2

    # 성공 응답
    success_order = SimpleNamespace(
        status="filled", filled=0.01, price=75000,
    )
    exchange.create_market_buy = AsyncMock(return_value=success_order)

    with patch("engine.hmm_regime_live_engine.emit_event", new_callable=AsyncMock):
        with patch("engine.hmm_regime_live_engine.get_session_factory"):
            await engine._close_position("BTC/USDT", 75000)

    assert engine._consecutive_close_failures == 0
    assert engine._position is None


@pytest.mark.asyncio
async def test_momentum_auto_pause_on_consecutive_close_failures():
    """Momentum 엔진: 3회 연속 청산 실패 시 _paused=True."""
    from engine.momentum_rotation_live_engine import MomentumRotationLiveEngine, MomentumPosition

    config = MagicMock()
    config.binance = MagicMock()
    exchange = _make_mock_exchange()
    market_data = _make_mock_market_data()

    engine = MomentumRotationLiveEngine(config, exchange, market_data, initial_capital_usdt=400)
    engine._positions["ETH/USDT"] = MomentumPosition(
        symbol="ETH/USDT", side="long", quantity=0.1,
        entry_price=3000, peak=3100,
    )

    with patch("engine.momentum_rotation_live_engine.emit_event", new_callable=AsyncMock):
        for _ in range(3):
            await engine._close_position("ETH/USDT")

    assert engine._consecutive_close_failures == 3
    assert engine._paused is True
    assert "ETH/USDT" in engine._positions  # 포지션 유지


@pytest.mark.asyncio
async def test_breakout_pb_auto_pause_on_consecutive_close_failures():
    """BreakoutPB 엔진: 3회 연속 청산 실패 시 _paused=True."""
    from engine.breakout_pullback_engine import BreakoutPullbackEngine, BPPosition

    config = MagicMock()
    config.binance = MagicMock()
    exchange = _make_mock_exchange()
    market_data = _make_mock_market_data()

    engine = BreakoutPullbackEngine(config, exchange, market_data, initial_capital_usdt=400)
    engine._positions["SOL/USDT"] = BPPosition(
        symbol="SOL/USDT", side="long", quantity=1.0,
        entry_price=150, sl_price=138, tp_price=162,
    )

    with patch("engine.breakout_pullback_engine.emit_event", new_callable=AsyncMock):
        for _ in range(3):
            await engine._close_position("SOL/USDT", 145)

    assert engine._consecutive_close_failures == 3
    assert engine._paused is True


@pytest.mark.asyncio
async def test_volume_momentum_auto_pause_on_consecutive_close_failures():
    """VolMom 엔진: 3회 연속 청산 실패 시 _paused=True."""
    from engine.volume_momentum_engine import VolumeMomentumEngine, VMPosition

    config = MagicMock()
    config.binance = MagicMock()
    exchange = _make_mock_exchange()
    market_data = _make_mock_market_data()

    engine = VolumeMomentumEngine(config, exchange, market_data, initial_capital_usdt=200)
    engine._positions["XRP/USDT"] = VMPosition(
        symbol="XRP/USDT", side="short", quantity=100,
        entry_price=0.5, sl_price=0.55, tp_price=0.45,
    )

    with patch("engine.volume_momentum_engine.emit_event", new_callable=AsyncMock):
        for _ in range(3):
            await engine._close_position("XRP/USDT", 0.48)

    assert engine._consecutive_close_failures == 3
    assert engine._paused is True


# ── Discord rnd_trade 카테고리 ──


def test_discord_adapter_handles_rnd_trade():
    """Discord 어댑터가 rnd_trade 카테고리를 처리."""
    from services.notification.discord import DiscordAdapter

    adapter = DiscordAdapter.__new__(DiscordAdapter)
    embed = adapter._format_event("info", "rnd_trade", "test", None, {"engine": "HMM"})
    assert embed is not None
    assert "🔬" in embed["title"]


def test_discord_adapter_handles_donchian_futures_trade():
    """Discord 어댑터가 donchian_futures_trade 카테고리를 처리."""
    from services.notification.discord import DiscordAdapter

    adapter = DiscordAdapter.__new__(DiscordAdapter)
    embed = adapter._format_event("info", "donchian_futures_trade", "test", None, {})
    assert embed is not None


def test_discord_adapter_handles_pairs_trade():
    """Discord 어댑터가 pairs_trade 카테고리를 처리."""
    from services.notification.discord import DiscordAdapter

    adapter = DiscordAdapter.__new__(DiscordAdapter)
    embed = adapter._format_event("info", "pairs_trade", "test", None, {})
    assert embed is not None


def test_discord_adapter_rnd_trade_warning():
    """Discord 어댑터가 rnd_trade warning을 처리."""
    from services.notification.discord import DiscordAdapter

    adapter = DiscordAdapter.__new__(DiscordAdapter)
    embed = adapter._format_event("warning", "rnd_trade", "warn", "detail", {"engine": "Momentum"})
    assert embed is not None
    assert "⚠️" in embed["title"]
