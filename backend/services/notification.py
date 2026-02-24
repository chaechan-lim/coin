import structlog
import httpx
from config import NotificationConfig

logger = structlog.get_logger(__name__)


class NotificationService:
    """Telegram notification service for trading alerts."""

    def __init__(self, config: NotificationConfig):
        self._config = config
        self._base_url = f"https://api.telegram.org/bot{config.telegram_bot_token}"

    async def send(self, message: str) -> bool:
        """Send a notification message."""
        if not self._config.enabled or not self._config.telegram_bot_token:
            logger.debug("notification_skipped", message=message[:50])
            return False

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self._base_url}/sendMessage",
                    json={
                        "chat_id": self._config.telegram_chat_id,
                        "text": message,
                        "parse_mode": "HTML",
                    },
                )
                if resp.status_code == 200:
                    logger.info("notification_sent")
                    return True
                else:
                    logger.warning("notification_failed", status=resp.status_code)
                    return False
        except Exception as e:
            logger.error("notification_error", error=str(e))
            return False

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
