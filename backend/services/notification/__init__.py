"""
알림 시스템 — 어댑터 패턴

emit_event() → event_bus → NotificationDispatcher
                              ├─ DiscordAdapter  (embed)
                              ├─ TelegramAdapter (HTML)
                              └─ (확장 가능)
"""
from services.notification.dispatcher import NotificationDispatcher
from services.notification.base import NotificationAdapter

__all__ = ["NotificationDispatcher", "NotificationAdapter"]
