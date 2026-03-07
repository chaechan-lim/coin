"""알림 시스템 테스트 — NotificationDispatcher + 어댑터."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from services.notification import NotificationDispatcher, NotificationAdapter
from services.notification.discord import DiscordAdapter
from services.notification.telegram import TelegramAdapter


# ── NotificationDispatcher ──────────────────────────────────────

class FakeAdapter(NotificationAdapter):
    """테스트용 어댑터."""
    def __init__(self):
        self.events: list[tuple] = []
        self.closed = False

    async def send(self, level, category, title, detail=None, metadata=None):
        self.events.append((level, category, title, detail, metadata))

    async def close(self):
        self.closed = True


class FailAdapter(NotificationAdapter):
    """항상 실패하는 어댑터 (에러 격리 테스트)."""
    async def send(self, level, category, title, detail=None, metadata=None):
        raise RuntimeError("adapter failure")

    async def close(self):
        raise RuntimeError("close failure")


@pytest.mark.asyncio
async def test_dispatcher_sends_to_all_adapters():
    d = NotificationDispatcher()
    a1, a2 = FakeAdapter(), FakeAdapter()
    d.add_adapter(a1)
    d.add_adapter(a2)
    await d.handle_event("info", "trade", "매수: BTC/KRW")
    assert len(a1.events) == 1
    assert len(a2.events) == 1
    assert a1.events[0][2] == "매수: BTC/KRW"


@pytest.mark.asyncio
async def test_dispatcher_isolates_adapter_failures():
    """한 어댑터 실패가 다른 어댑터에 영향 안 줌."""
    d = NotificationDispatcher()
    fail = FailAdapter()
    ok = FakeAdapter()
    d.add_adapter(fail)
    d.add_adapter(ok)
    await d.handle_event("info", "trade", "매수: BTC/KRW")
    assert len(ok.events) == 1  # 실패한 어댑터 이후에도 정상 전송


@pytest.mark.asyncio
async def test_dispatcher_close_all():
    d = NotificationDispatcher()
    a1, a2 = FakeAdapter(), FakeAdapter()
    d.add_adapter(a1)
    d.add_adapter(a2)
    await d.close()
    assert a1.closed
    assert a2.closed


@pytest.mark.asyncio
async def test_dispatcher_close_isolates_failure():
    d = NotificationDispatcher()
    fail = FailAdapter()
    ok = FakeAdapter()
    d.add_adapter(fail)
    d.add_adapter(ok)
    await d.close()
    assert ok.closed  # fail 어댑터 에러에도 ok 어댑터 close 실행


def test_dispatcher_adapters_property():
    d = NotificationDispatcher()
    a = FakeAdapter()
    d.add_adapter(a)
    assert len(d.adapters) == 1
    assert d.adapters[0] is a


# ── TelegramAdapter 포맷 ────────────────────────────────────────

def _make_telegram():
    return TelegramAdapter("test_token", "test_chat_id")


def test_telegram_trade_buy_format():
    t = _make_telegram()
    text = t._format_event(
        "info", "trade", "매수: BTC/KRW", None,
        {"price": 100_000_000, "strategy": "rsi", "confidence": 0.72},
    )
    assert text is not None
    assert "🟢" in text
    assert "BTC/KRW" in text
    assert "rsi" in text


def test_telegram_trade_sell_format():
    t = _make_telegram()
    text = t._format_event(
        "info", "trade", "매도: ETH/KRW", None,
        {"price": 5_000_000, "strategy": "macd", "confidence": 0.65, "pnl_pct": 3.5},
    )
    assert "🔴" in text
    assert "+3.50%" in text


def test_telegram_stop_format():
    t = _make_telegram()
    text = t._format_event(
        "warning", "trade", "손절: BTC/KRW", None,
        {"price": 95_000_000, "pnl_pct": -4.0, "reason": "stop_loss"},
    )
    assert "⚠️" in text
    assert "-4.00%" in text
    assert "stop_loss" in text


def test_telegram_futures_trade_format():
    t = _make_telegram()
    text = t._format_event(
        "info", "futures_trade", "선물 롱: BTC/USDT", None,
        {"price": 65000.0, "strategy": "rsi", "confidence": 0.60, "leverage": 3},
    )
    assert "🟢" in text
    assert "65,000.00 USDT" in text
    assert "3x" in text


def test_telegram_futures_stop_format():
    t = _make_telegram()
    text = t._format_event(
        "warning", "futures_trade", "선물 SL: BTC/USDT", None,
        {"price": 63000.0, "direction": "long", "pnl_pct": -3.5,
         "loss_amount": -10.5, "reason": "stop_loss"},
    )
    assert "🔴" in text
    assert "LONG" in text
    assert "-3.50%" in text
    assert "-10.50 USDT" in text


def test_telegram_system_start_format():
    t = _make_telegram()
    text = t._format_event(
        "info", "system", "서버 시작", "live 모드",
        {"positions_summary": "[선물] BTC↑"},
    )
    assert "🚀" in text
    assert "live 모드" in text
    assert "BTC↑" in text


def test_telegram_system_shutdown_format():
    t = _make_telegram()
    text = t._format_event("info", "system", "서버 종료", "모든 엔진 중지 완료", None)
    assert "🛑" in text


def test_telegram_daily_summary_format():
    t = _make_telegram()
    text = t._format_event(
        "info", "daily_summary", "일일 요약 [binance_futures]", None,
        {
            "exchange": "binance_futures",
            "total_value": 350.0,
            "return_pct": -2.5,
            "review": {
                "total_trades": 8,
                "buy_count": 4, "sell_count": 4,
                "win_count": 3, "loss_count": 1,
                "win_rate": 0.75, "profit_factor": 2.1,
            },
        },
    )
    assert "📋" in text
    assert "350.00 USDT" in text
    assert "-2.50%" in text
    assert "8건" in text


def test_telegram_ignores_health():
    t = _make_telegram()
    assert t._format_event("warning", "health", "이상 감지", None, {}) is None


def test_telegram_ignores_recovery():
    t = _make_telegram()
    assert t._format_event("info", "recovery", "복구 완료", None, {}) is None


def test_telegram_ignores_signal():
    t = _make_telegram()
    assert t._format_event("info", "signal", "시그널", None, {}) is None


def test_telegram_engine_critical():
    t = _make_telegram()
    text = t._format_event(
        "critical", "engine", "강제 청산", "API 404",
        {"symbol": "POWER/USDT"},
    )
    assert "🚨" in text
    assert "POWER/USDT" in text


# ── TelegramAdapter 전송 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_telegram_send_success():
    t = _make_telegram()
    t._client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    t._client.post = AsyncMock(return_value=mock_resp)
    await t._send_message("test message")
    t._client.post.assert_called_once()
    call_url = t._client.post.call_args[0][0]
    assert "api.telegram.org" in call_url
    payload = t._client.post.call_args[1]["json"]
    assert payload["text"] == "test message"
    assert payload["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_telegram_send_network_error_no_crash():
    t = _make_telegram()
    t._client = AsyncMock()
    t._client.post = AsyncMock(side_effect=Exception("network error"))
    await t._send_message("test")  # 예외 없이 완료


@pytest.mark.asyncio
async def test_telegram_rate_limit():
    t = _make_telegram()
    for _ in range(20):
        assert t._check_rate_limit() is True
    assert t._check_rate_limit() is False
