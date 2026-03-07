"""알림 디스패처 — 이벤트를 등록된 모든 어댑터로 전달."""
import structlog
from typing import Any

from services.notification.base import NotificationAdapter

logger = structlog.get_logger(__name__)


class NotificationDispatcher:
    """이벤트를 등록된 모든 알림 어댑터로 디스패치.

    event_bus._notification_fn 콜백으로 등록되어,
    emit_event() 호출 시 모든 어댑터에 이벤트를 전달.
    """

    def __init__(self):
        self._adapters: list[NotificationAdapter] = []

    def add_adapter(self, adapter: NotificationAdapter) -> None:
        self._adapters.append(adapter)
        logger.info("notification_adapter_registered", adapter=type(adapter).__name__)

    @property
    def adapters(self) -> list[NotificationAdapter]:
        return list(self._adapters)

    async def handle_event(
        self,
        level: str,
        category: str,
        title: str,
        detail: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """모든 어댑터에 이벤트 전송. 개별 실패는 무시."""
        for adapter in self._adapters:
            try:
                await adapter.send(level, category, title, detail, metadata)
            except Exception as e:
                logger.warning(
                    "notification_adapter_error",
                    adapter=type(adapter).__name__,
                    error=str(e),
                    title=title,
                )

    async def close(self) -> None:
        """모든 어댑터 리소스 정리."""
        for adapter in self._adapters:
            try:
                await adapter.close()
            except Exception as e:
                logger.warning(
                    "adapter_close_error",
                    adapter=type(adapter).__name__,
                    error=str(e),
                )
