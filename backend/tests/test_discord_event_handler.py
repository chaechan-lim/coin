"""DiscordEventHandler unit tests."""
import time
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from services.discord_event_handler import DiscordEventHandler


# ── helpers ─────────────────────────────────────────────────────

def _make_handler(url="https://discord.com/api/webhooks/test/token"):
    return DiscordEventHandler(url)


def _mock_response(status_code=204):
    resp = MagicMock()
    resp.status_code = status_code
    return resp


# ── 필터: 매수 이벤트 → embed 생성 ─────────────────────────────

def test_trade_buy_embed():
    h = _make_handler()
    embed = h._format_event(
        "info", "trade", "매수: BTC/KRW", None,
        {"price": 100_000_000, "amount_krw": 500_000, "strategy": "rsi", "confidence": 0.72, "market_state": "sideways"},
    )
    assert embed is not None
    assert "매수" in embed["title"]
    assert embed["color"] == 0x2ECC71  # green
    assert any(f["name"] == "전략" for f in embed["fields"])


def test_trade_buy_embed_with_sl_tp():
    """현물 매수 시 손절가/익절가 필드 표시."""
    h = _make_handler()
    embed = h._format_event(
        "info", "trade", "매수: BTC/KRW", None,
        {
            "price": 100_000_000, "amount_krw": 500_000,
            "strategy": "rsi", "confidence": 0.72,
            "sl_price": 95_000_000, "tp_price": 110_000_000,
            "market_state": "sideways",
        },
    )
    assert embed is not None
    sl = [f for f in embed["fields"] if f["name"] == "손절가"]
    tp = [f for f in embed["fields"] if f["name"] == "익절가"]
    assert len(sl) == 1
    assert "95,000,000" in sl[0]["value"]
    assert len(tp) == 1
    assert "110,000,000" in tp[0]["value"]


def test_trade_sell_embed_no_sl_tp():
    """매도 시에는 sl_price/tp_price 없으므로 손절가/익절가 필드 없음."""
    h = _make_handler()
    embed = h._format_event(
        "info", "trade", "매도: ETH/KRW", None,
        {"price": 5_000_000, "strategy": "macd_crossover", "confidence": 0.65, "pnl_pct": 3.5},
    )
    assert embed is not None
    assert not any(f["name"] in ("손절가", "익절가") for f in embed["fields"])


def test_trade_sell_embed():
    h = _make_handler()
    embed = h._format_event(
        "info", "trade", "매도: ETH/KRW", None,
        {"price": 5_000_000, "strategy": "macd_crossover", "confidence": 0.65, "pnl_pct": 3.5},
    )
    assert embed is not None
    assert "매도" in embed["title"]
    assert embed["color"] == 0xE74C3C  # red
    pnl_field = [f for f in embed["fields"] if f["name"] == "PnL"]
    assert len(pnl_field) == 1
    assert "+3.50%" in pnl_field[0]["value"]


# ── 필터: SL/TP 경고 ───────────────────────────────────────────

def test_stop_loss_embed():
    h = _make_handler()
    embed = h._format_event(
        "warning", "trade", "손절: BTC/KRW", "SL 4% 도달",
        {"price": 95_000_000, "pnl_pct": -4.0, "reason": "stop_loss"},
    )
    assert embed is not None
    assert embed["color"] == 0xF39C12  # orange


# ── 필터: 선물 매매 ────────────────────────────────────────────

def test_futures_long_embed():
    h = _make_handler()
    embed = h._format_event(
        "info", "futures_trade", "선물 롱: BTC/USDT", None,
        {"price": 65000.0, "strategy": "rsi", "confidence": 0.60, "leverage": 3},
    )
    assert embed is not None
    assert embed["color"] == 0x2ECC71  # green (long)
    lev = [f for f in embed["fields"] if f["name"] == "레버리지"]
    assert lev[0]["value"] == "3x"


def test_futures_long_embed_with_sl_tp():
    """선물 롱 진입 시 손절가/익절가 표시 (가격 >= 10 → 소수점 2자리)."""
    h = _make_handler()
    embed = h._format_event(
        "info", "futures_trade", "선물 롱: SOL/USDT", None,
        {
            "price": 86.48, "strategy": "rsi", "confidence": 0.72,
            "leverage": 3, "sl_price": 82.42, "tp_price": 94.42,
        },
    )
    assert embed is not None
    sl = [f for f in embed["fields"] if f["name"] == "손절가"]
    tp = [f for f in embed["fields"] if f["name"] == "익절가"]
    assert len(sl) == 1
    assert "82.42" in sl[0]["value"]
    assert "USDT" in sl[0]["value"]
    assert len(tp) == 1
    assert "94.42" in tp[0]["value"]


def test_futures_short_embed_with_sl_tp_small_price():
    """선물 숏 진입 시 손절가/익절가 표시 (가격 < 10 → 소수점 4자리)."""
    h = _make_handler()
    embed = h._format_event(
        "info", "futures_trade", "선물 숏: DOGE/USDT", None,
        {
            "price": 0.0954, "strategy": "bollinger_rsi", "confidence": 0.68,
            "leverage": 3, "sl_price": 0.0998, "tp_price": 0.0877,
        },
    )
    assert embed is not None
    sl = [f for f in embed["fields"] if f["name"] == "손절가"]
    tp = [f for f in embed["fields"] if f["name"] == "익절가"]
    assert len(sl) == 1
    assert "0.0998" in sl[0]["value"]
    assert len(tp) == 1
    assert "0.0877" in tp[0]["value"]


def test_futures_close_embed_no_sl_tp():
    """선물 청산 시에는 sl_price/tp_price 없으므로 손절가/익절가 필드 없음."""
    h = _make_handler()
    embed = h._format_event(
        "info", "futures_trade", "선물 청산: BTC/USDT", None,
        {"price": 65000.0, "pnl_pct": 5.2, "leverage": 3},
    )
    assert embed is not None
    assert not any(f["name"] in ("손절가", "익절가") for f in embed["fields"])


def test_futures_short_embed():
    h = _make_handler()
    embed = h._format_event(
        "info", "futures_trade", "선물 숏: ETH/USDT", None,
        {"price": 3200.0, "strategy": "bollinger_rsi", "confidence": 0.58},
    )
    assert embed is not None
    assert embed["color"] == 0xE74C3C  # red (short)


# ── 필터: 로테이션 ─────────────────────────────────────────────

def test_rotation_embed():
    h = _make_handler()
    embed = h._format_event(
        "info", "rotation", "서지 매수: DOGE/KRW", None,
        {"price": 500, "surge_ratio": 5.2, "amount_krw": 75_000},
    )
    assert embed is not None
    assert "🚀" in embed["title"]


# ── 필터: 리스크 경고 ──────────────────────────────────────────

def test_risk_warning_embed():
    h = _make_handler()
    embed = h._format_event(
        "warning", "risk", "드로다운 경고", "MDD 8% 도달",
        {"drawdown_pct": 8.0},
    )
    assert embed is not None
    assert "🚨" in embed["title"]
    assert embed["color"] == 0xF39C12


# ── 필터: 시스템 ───────────────────────────────────────────────

def test_system_embed():
    h = _make_handler()
    embed = h._format_event("info", "system", "서버 시작", "paper 모드", None)
    assert embed is not None
    assert "🚀" in embed["title"]
    assert embed["color"] == 0x3498DB


def test_system_shutdown_embed():
    h = _make_handler()
    embed = h._format_event("info", "system", "서버 종료", "모든 엔진 중지 완료", None)
    assert embed is not None
    assert "🛑" in embed["title"]


def test_system_embed_with_metadata():
    h = _make_handler()
    meta = {
        "spot_coins": ["BTC/KRW"],
        "futures_coins": ["BTC/USDT", "ETH/USDT"],
        "positions_summary": "[선물] BTC↑ | 현금 500 USDT",
    }
    embed = h._format_event("info", "system", "서버 시작", "live 모드", meta)
    assert embed is not None
    field_names = [f["name"] for f in embed.get("fields", [])]
    assert "현물 추적" in field_names
    assert "선물 추적" in field_names
    assert "포지션" in field_names


# ── 필터: 엔진 라이프사이클 ──────────────────────────────────────

def test_engine_start_embed():
    h = _make_handler()
    embed = h._format_event("info", "engine", "binance_futures 엔진 시작", None,
                            {"mode": "live", "exchange": "binance_futures"})
    assert embed is not None
    assert "▶️" in embed["title"]
    assert embed["color"] == 0x3498DB


def test_engine_stop_embed():
    h = _make_handler()
    embed = h._format_event("info", "engine", "binance_futures 엔진 중지", None,
                            {"exchange": "binance_futures"})
    assert embed is not None
    assert "⏹️" in embed["title"]


def test_engine_error_still_handled():
    """engine warning/error는 기존대로 처리."""
    h = _make_handler()
    embed = h._format_event("warning", "engine", "평가 실패", "timeout",
                            {"symbol": "BTC/USDT"})
    assert embed is not None
    assert "⚠️" in embed["title"]


# ── 필터: 시그널 ───────────────────────────────────────────────

def test_signal_embed():
    h = _make_handler()
    embed = h._format_event(
        "info", "signal", "시그널: BTC/KRW BUY", "RSI oversold",
        {"action": "BUY", "confidence": 0.72, "strategies": ["rsi(72%)", "bollinger_rsi(68%)"]},
    )
    assert embed is not None
    assert "📊" in embed["title"]
    assert embed["color"] == 0x9B59B6
    strats = [f for f in embed["fields"] if f["name"] == "참여 전략"]
    assert "rsi(72%)" in strats[0]["value"]


# ── 필터: 일일 요약 ────────────────────────────────────────────

def test_daily_summary_embed_basic():
    """review 없이 기본 포트폴리오 지표만 있는 일일 요약."""
    h = _make_handler()
    embed = h._format_event(
        "info", "daily_summary", "일일 요약 [bithumb]", "총 자산: 520,000 ₩",
        {
            "exchange": "bithumb",
            "total_value": 520000,
            "return_pct": 4.0,
            "realized_pnl": 15000,
            "unrealized_pnl": 5000,
            "total_fees": 1200,
            "drawdown_pct": 1.5,
            "positions": 3,
            "trades_today": 5,
        },
    )
    assert embed is not None
    assert "📋" in embed["title"]
    ret = [f for f in embed["fields"] if f["name"] == "원금 대비"]
    assert "+4.00%" in ret[0]["value"]
    realized = [f for f in embed["fields"] if f["name"] == "실현 손익"]
    assert "15,000" in realized[0]["value"]
    dd = [f for f in embed["fields"] if f["name"] == "고점 대비"]
    assert "-1.50%" in dd[0]["value"]


def test_daily_summary_embed_with_review():
    """review 포함 시 전략별 성과, 인사이트, 추천 표시."""
    h = _make_handler()
    embed = h._format_event(
        "info", "daily_summary", "일일 요약 [binance_futures]", "총 자산: 350.00 USDT",
        {
            "exchange": "binance_futures",
            "total_value": 350.0,
            "return_pct": -2.5,
            "realized_pnl": -5.0,
            "unrealized_pnl": 2.0,
            "total_fees": 0.8,
            "drawdown_pct": 3.0,
            "positions": 1,
            "review": {
                "total_trades": 8,
                "buy_count": 4,
                "sell_count": 4,
                "win_count": 3,
                "loss_count": 1,
                "win_rate": 0.75,
                "profit_factor": 2.1,
                "by_strategy": {
                    "rsi": {"trades": 3, "wins": 2, "total_pnl": 5.0, "win_rate": 0.67},
                    "macd_crossover": {"trades": 2, "wins": 1, "total_pnl": -2.0, "win_rate": 0.5},
                },
                "insights": ["승률 75%, 양호한 수준", "RSI 전략이 최고 성과"],
                "recommendations": ["MACD 전략 파라미터 재검토 필요"],
            },
        },
    )
    assert embed is not None
    fields_by_name = {f["name"]: f["value"] for f in embed["fields"]}
    assert "75%" in fields_by_name["승률"]
    assert "2.10x" in fields_by_name["Profit Factor"]
    assert "rsi" in fields_by_name["전략별 성과"]
    assert "인사이트" in fields_by_name
    assert "추천" in fields_by_name


# ── 필터: 무시되는 이벤트 (HOLD 등) ────────────────────────────

def test_ignored_event_returns_none():
    h = _make_handler()
    assert h._format_event("debug", "engine", "heartbeat", None, None) is None
    assert h._format_event("info", "portfolio", "스냅샷 저장", None, None) is None
    assert h._format_event("info", "market", "시장 분석 완료", None, None) is None


# ── 레이트 리밋 ────────────────────────────────────────────────

def test_rate_limit_blocks_after_5():
    h = _make_handler()
    for i in range(5):
        assert h._check_rate_limit() is True, f"call {i} should pass"
    assert h._check_rate_limit() is False, "6th call should be blocked"


def test_rate_limit_recovers_after_window():
    h = _make_handler()
    # 5건 소진
    for _ in range(5):
        h._check_rate_limit()
    # 타임스탬프를 6초 전으로 조작
    now = time.monotonic()
    h._timestamps.clear()
    for _ in range(5):
        h._timestamps.append(now - 6.0)
    assert h._check_rate_limit() is True


# ── 에러 복원력: handle_event 절대 예외 미전파 ───────────────────

@pytest.mark.asyncio
async def test_handle_event_no_exception_on_send_error():
    h = _make_handler()
    h._client = AsyncMock()
    h._client.post = AsyncMock(side_effect=Exception("network error"))
    # 매수 이벤트 → 포맷 성공 → 전송 실패 → 예외 없이 종료
    await h.handle_event(
        "info", "trade", "매수: BTC/KRW", None,
        {"price": 100_000_000, "strategy": "rsi", "confidence": 0.7},
    )
    # 예외 없이 도달하면 성공


@pytest.mark.asyncio
async def test_handle_event_ignored_silently():
    h = _make_handler()
    h._client = AsyncMock()
    # 무시되는 이벤트 → _send_embed 호출 안 함
    await h.handle_event("debug", "engine", "heartbeat", None, None)
    h._client.post.assert_not_called()


# ── 전송 성공 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_embed_success():
    h = _make_handler()
    h._client = AsyncMock()
    h._client.post = AsyncMock(return_value=_mock_response(204))
    await h._send_embed({"title": "test", "color": 0x000000})
    h._client.post.assert_called_once()
    payload = h._client.post.call_args[1]["json"]
    assert "embeds" in payload
    assert payload["embeds"][0]["title"] == "test"


# ── close ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_close():
    h = _make_handler()
    h._client = AsyncMock()
    h._client.aclose = AsyncMock()
    await h.close()
    h._client.aclose.assert_called_once()
