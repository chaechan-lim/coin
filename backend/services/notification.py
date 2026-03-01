import structlog
import httpx
from config import NotificationConfig

logger = structlog.get_logger(__name__)


class NotificationService:
    """Multi-provider notification service (Telegram, Discord, Slack)."""

    def __init__(self, config: NotificationConfig):
        self._config = config
        self._providers: list[str] = [
            p.strip() for p in config.provider.split(",") if p.strip()
        ]

    async def send(self, message: str) -> bool:
        """Send a notification to all configured providers."""
        if not self._config.enabled:
            logger.debug("notification_skipped", message=message[:50])
            return False

        results = []
        for provider in self._providers:
            if provider == "telegram":
                results.append(await self._send_telegram(message))
            elif provider == "discord":
                results.append(await self._send_discord(message))
            elif provider == "slack":
                results.append(await self._send_slack(message))
            else:
                logger.warning("unknown_notification_provider", provider=provider)

        return any(results)

    # ── Telegram ────────────────────────────────────────────────

    async def _send_telegram(self, message: str) -> bool:
        token = self._config.telegram_bot_token
        chat_id = self._config.telegram_chat_id
        if not token or not chat_id:
            logger.debug("telegram_not_configured")
            return False

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": message,
                        "parse_mode": "HTML",
                    },
                )
                if resp.status_code == 200:
                    logger.info("notification_sent", provider="telegram")
                    return True
                logger.warning("notification_failed", provider="telegram", status=resp.status_code)
                return False
        except Exception as e:
            logger.error("notification_error", provider="telegram", error=str(e))
            return False

    # ── Discord ─────────────────────────────────────────────────

    async def _send_discord(self, message: str) -> bool:
        url = self._config.discord_webhook_url
        if not url:
            logger.debug("discord_not_configured")
            return False

        # HTML 태그를 Discord 마크다운으로 변환
        text = _html_to_markdown(message)

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    url,
                    json={"content": text},
                )
                if resp.status_code in (200, 204):
                    logger.info("notification_sent", provider="discord")
                    return True
                logger.warning("notification_failed", provider="discord", status=resp.status_code)
                return False
        except Exception as e:
            logger.error("notification_error", provider="discord", error=str(e))
            return False

    # ── Slack ───────────────────────────────────────────────────

    async def _send_slack(self, message: str) -> bool:
        url = self._config.slack_webhook_url
        if not url:
            logger.debug("slack_not_configured")
            return False

        # HTML 태그를 Slack mrkdwn으로 변환
        text = _html_to_slack(message)

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    url,
                    json={"text": text},
                )
                if resp.status_code == 200:
                    logger.info("notification_sent", provider="slack")
                    return True
                logger.warning("notification_failed", provider="slack", status=resp.status_code)
                return False
        except Exception as e:
            logger.error("notification_error", provider="slack", error=str(e))
            return False

    # ── High-level alert methods ────────────────────────────────

    async def send_trade_alert(
        self,
        symbol: str,
        side: str,
        price: float,
        amount: float,
        strategy: str,
        reason: str,
    ) -> None:
        side_emoji = "🟢 매수" if side == "buy" else "🔴 매도"
        message = (
            f"{side_emoji} <b>{symbol}</b>\n"
            f"💰 가격: {price:,.0f} KRW\n"
            f"📦 수량: {amount:.6f}\n"
            f"📊 전략: {strategy}\n"
            f"💬 사유: {reason[:100]}"
        )
        await self.send(message)

    async def send_risk_alert(self, level: str, message: str) -> None:
        emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(level, "📢")
        await self.send(f"{emoji} <b>리스크 경고 [{level.upper()}]</b>\n{message}")

    async def send_engine_alert(self, message: str) -> None:
        await self.send(f"⚙️ <b>엔진 알림</b>\n{message}")


# ── HTML → Markdown converters ──────────────────────────────────

def _html_to_markdown(html: str) -> str:
    """Convert simple HTML tags to Discord markdown."""
    return html.replace("<b>", "**").replace("</b>", "**")


def _html_to_slack(html: str) -> str:
    """Convert simple HTML tags to Slack mrkdwn."""
    return html.replace("<b>", "*").replace("</b>", "*")
