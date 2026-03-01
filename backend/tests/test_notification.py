"""NotificationService multi-provider tests."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from config import NotificationConfig
from services.notification import NotificationService, _html_to_markdown, _html_to_slack


# ── helpers ─────────────────────────────────────────────────────

def _make_service(
    provider="telegram",
    enabled=True,
    telegram_token="tok",
    telegram_chat="123",
    discord_url="https://discord.com/api/webhooks/test",
    slack_url="https://hooks.slack.com/services/test",
) -> NotificationService:
    cfg = NotificationConfig(
        enabled=enabled,
        provider=provider,
        telegram_bot_token=telegram_token,
        telegram_chat_id=telegram_chat,
        discord_webhook_url=discord_url,
        slack_webhook_url=slack_url,
    )
    return NotificationService(cfg)


def _mock_response(status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    return resp


# ── disabled / skip ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_disabled():
    svc = _make_service(enabled=False)
    assert await svc.send("hello") is False


@pytest.mark.asyncio
async def test_send_telegram_not_configured():
    svc = _make_service(provider="telegram", telegram_token="", telegram_chat="")
    assert await svc.send("hello") is False


@pytest.mark.asyncio
async def test_send_discord_not_configured():
    svc = _make_service(provider="discord", discord_url="")
    assert await svc.send("hello") is False


@pytest.mark.asyncio
async def test_send_slack_not_configured():
    svc = _make_service(provider="slack", slack_url="")
    assert await svc.send("hello") is False


# ── telegram ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_telegram_success():
    svc = _make_service(provider="telegram")
    with patch("services.notification.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response(200)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        assert await svc.send("test") is True
        mock_client.post.assert_called_once()
        call_url = mock_client.post.call_args[0][0]
        assert "api.telegram.org" in call_url


@pytest.mark.asyncio
async def test_telegram_failure():
    svc = _make_service(provider="telegram")
    with patch("services.notification.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response(400)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        assert await svc.send("test") is False


# ── discord ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discord_success():
    svc = _make_service(provider="discord")
    with patch("services.notification.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response(204)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        assert await svc.send("<b>bold</b> test") is True
        call_json = mock_client.post.call_args[1]["json"]
        assert "**bold**" in call_json["content"]


# ── slack ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_slack_success():
    svc = _make_service(provider="slack")
    with patch("services.notification.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response(200)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        assert await svc.send("<b>bold</b> test") is True
        call_json = mock_client.post.call_args[1]["json"]
        assert "*bold*" in call_json["text"]


# ── multi-provider ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multi_provider():
    svc = _make_service(provider="discord,slack")
    with patch("services.notification.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response(200)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        assert await svc.send("hello") is True
        assert mock_client.post.call_count == 2


# ── high-level alerts ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_trade_alert():
    svc = _make_service(provider="discord")
    with patch.object(svc, "send", new_callable=AsyncMock) as mock_send:
        await svc.send_trade_alert("BTC/KRW", "buy", 100_000_000, 0.001, "rsi", "oversold")
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "BTC/KRW" in msg
        assert "매수" in msg


@pytest.mark.asyncio
async def test_risk_alert():
    svc = _make_service(provider="discord")
    with patch.object(svc, "send", new_callable=AsyncMock) as mock_send:
        await svc.send_risk_alert("warning", "drawdown 15%")
        mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_engine_alert():
    svc = _make_service(provider="discord")
    with patch.object(svc, "send", new_callable=AsyncMock) as mock_send:
        await svc.send_engine_alert("엔진 시작")
        mock_send.assert_called_once()


# ── converters ─────────────────────────────────────────────────

def test_html_to_markdown():
    assert _html_to_markdown("<b>hello</b>") == "**hello**"


def test_html_to_slack():
    assert _html_to_slack("<b>hello</b>") == "*hello*"


# ── error handling ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_telegram_network_error():
    svc = _make_service(provider="telegram")
    with patch("services.notification.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("connection refused")
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        assert await svc.send("test") is False
