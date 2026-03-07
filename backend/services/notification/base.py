"""알림 채널 어댑터 기본 클래스."""
from abc import ABC, abstractmethod
from typing import Any


class NotificationAdapter(ABC):
    """알림 채널 어댑터 인터페이스.

    각 채널(Discord, Telegram 등)은 이 ABC를 구현하여
    동일한 이벤트를 채널별 포맷으로 변환 후 전송.
    """

    @abstractmethod
    async def send(
        self,
        level: str,
        category: str,
        title: str,
        detail: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """이벤트를 해당 채널 포맷으로 변환 후 전송."""

    async def close(self) -> None:
        """리소스 정리. 기본 구현은 no-op."""
